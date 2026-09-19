#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert.py — 自动识别 wcp 包结构，产出 Winlator bionic 可直接导入 assets 的资产。

设计目标：不维护手工 profile 映射表。运行时探测每个 wcp：
  - 是 wine/proton 包（含 prefixPack）→ 拆成「双包」：
        {id}.tar.zst                     （wine 本体，原 wcp 已是 tar.zst，直接重命名）
        {id}_container_pattern.tzst      （从 prefixPack 自动转换的版本匹配 prefix 模板）
  - 是组件包（box64/fex/dxvk/vkd3d/d7vk/wowbox64）→ 原样透传为目标 .tzst

container_pattern 转换规则（与官方 9.0 模板逐字节对照得出）：
  - 去掉 tar 根 "." 条目
  - 剔除 macOS AppleDouble（._ 开头）垃圾文件
  - dosdevices 只保留 c: -> ../drive_c 与 z: -> /，删掉 d:/e:
  - 补 .wine/.update-timestamp = b'disable' (mode 0700)，阻止 wineboot 重复初始化
  - 保留该版本自己的 system.reg 等全部 prefix 文件
  - GNU tar + ZSTD(level 19)

用法：
  convert.py wine      --wcp X.wcp --id proton-11.0.2-arm64ec --out dist/
  convert.py component --wcp X.wcp --type DXVK --ver Dxvk-3.0.2-gplasync --out dist/
  convert.py catalog   --dist dist/ --base-url https://OWNER/REPO/releases/latest
"""
import argparse, io, os, sys, tarfile, time, json, hashlib
import zstandard as zstd

MAX_IN = 1500 * 1024 * 1024  # prefixPack 解压上限


def _open_tar_stream(path):
    """自动探测外层压缩格式（zstd / xz / 未压缩 tar），返回 tarfile 流。"""
    f = open(path, 'rb')
    head = f.read(6); f.seek(0)
    # zstd magic: 28 B5 2F FD
    if head[:4] == b'\x28\xb5\x2f\xfd':
        r = zstd.ZstdDecompressor().stream_reader(f)
        return tarfile.open(fileobj=r, mode='r|'), f, r
    # xz magic: FD 37 7A 58 5A 00
    if head[:6] == b'\xfd7zXZ\x00':
        import lzma
        r = lzma.open(f, 'rb')
        return tarfile.open(fileobj=r, mode='r|'), f, r
    # 未压缩 tar（ustar at offset 257）
    f.seek(257); tag = f.read(5); f.seek(0)
    if tag == b'ustar':
        return tarfile.open(fileobj=f, mode='r|'), f, None
    # 兜底：先试 zstd，失败试 xz
    try:
        r = zstd.ZstdDecompressor().stream_reader(f)
        return tarfile.open(fileobj=r, mode='r|'), f, r
    except Exception:
        f.seek(0)
        import lzma
        r = lzma.open(f, 'rb')
        return tarfile.open(fileobj=r, mode='r|'), f, r


def _read_wcp_members(path):
    """流式读 wcp（zstd/xz/未压缩），返回 [(TarInfo, bytes or None)]，用完即关。"""
    out = []
    tf, f, r = _open_tar_stream(path)
    try:
        for m in tf:
            data = tf.extractfile(m).read() if m.isfile() else None
            out.append((m, data))
    finally:
        tf.close()
        if r is not None:
            try: r.close()
            except Exception: pass
        f.close()
    return out


def _open_inner_tar(raw):
    """prefixPack 可能是 zstd 也可能是 xz，自动探测。"""
    try:
        inner = zstd.ZstdDecompressor().decompress(raw, max_output_size=MAX_IN)
        return tarfile.open(fileobj=io.BytesIO(inner))
    except Exception:
        return tarfile.open(fileobj=io.BytesIO(raw), mode='r:xz')


def is_apple(n):
    return n.split('/')[-1].startswith('._')


def detect_wcp(path):
    """自动探测 wcp 是 wine 包还是组件包。"""
    members = _read_wcp_members(path)
    names = [m.name for m, _ in members]
    topdirs = {n.split('/')[0] for n in names if n not in ('.', './', '')}
    pp = next((n for n in names if n.startswith('prefixPack')), None)
    has_bin = any(n.startswith('bin/') or n == 'bin' for n in names)
    has_profile = any(n.endswith('profile.json') for n in names)
    kind = 'wine' if (pp and (has_bin or has_profile)) else 'component'
    return kind, pp, members


def build_pattern_from_prefixpack(pp_members, out_path):
    """
    从 prefixPack 成员列表（name 已去 ./）构造干净的 container_pattern.tzst。
    pp_members: list[(norm_name, TarInfo, bytes)]
    """
    now = int(time.time())
    dirs, links, files = set(), {}, {}
    for n, m, data in pp_members:
        if n in ('', '.'):
            continue
        if is_apple(n):
            continue
        if n in ('.wine/dosdevices/d:', '.wine/dosdevices/e:'):
            continue
        if m.isdir():
            dirs.add(n)
        elif m.issym():
            links[n] = '/' if n == '.wine/dosdevices/z:' else m.linkname
        elif m.isfile():
            if n == '.wine/.update-timestamp':
                continue  # 我们统一重写
            files[n] = (m, data)
    for d in ('.wine', '.wine/dosdevices', '.wine/drive_c'):
        dirs.add(d)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.GNU_FORMAT) as tf:
        def add_dir(p):
            ti = tarfile.TarInfo(p); ti.type = tarfile.DIRTYPE; ti.mode = 0o755; ti.mtime = now
            tf.addfile(ti)
        add_dir('.wine')
        ti = tarfile.TarInfo('.wine/.update-timestamp'); ti.size = 7
        ti.type = tarfile.REGTYPE; ti.mode = 0o700; ti.mtime = now
        tf.addfile(ti, io.BytesIO(b'disable'))
        add_dir('.wine/dosdevices')
        for name, tgt in (('.wine/dosdevices/c:', '../drive_c'), ('.wine/dosdevices/z:', '/')):
            ti = tarfile.TarInfo(name); ti.type = tarfile.SYMTYPE
            ti.linkname = tgt; ti.mode = 0o777; ti.mtime = now
            tf.addfile(ti)
        add_dir('.wine/drive_c')
        for d in sorted(dirs, key=lambda x: (x.count('/'), x)):
            if d not in ('.wine', '.wine/dosdevices', '.wine/drive_c'):
                add_dir(d)
        for n in sorted(files):
            m, data = files[n]
            ti = tarfile.TarInfo(n); ti.size = len(data); ti.type = tarfile.REGTYPE
            ti.mode = m.mode or 0o644; ti.mtime = now
            tf.addfile(ti, io.BytesIO(data))
    comp = zstd.ZstdCompressor(level=19).compress(buf.getvalue())
    with open(out_path, 'wb') as f:
        f.write(comp)
    return len(comp)


def cmd_wine(args):
    kind, pp, members = detect_wcp(args.wcp)
    if kind != 'wine' or not pp:
        print(f"[!] {args.wcp} 未识别为 wine 包（kind={kind}）", file=sys.stderr)
        sys.exit(2)
    # 1) 本体包：wcp 本身即 tar.zst，直接复制为 {id}.tar.zst
    body = os.path.join(args.out, f"{args.id}.tar.zst")
    with open(args.wcp, 'rb') as fr, open(body, 'wb') as fw:
        fw.write(fr.read())
    # 2) 解 prefixPack
    pp_raw = next(data for m, data in members if m.name == pp)
    with _open_inner_tar(pp_raw) as tf:
        pp_list = []
        for m in tf.getmembers():
            n = m.name[2:] if m.name.startswith('./') else m.name
            d = tf.extractfile(m).read() if m.isfile() else None
            pp_list.append((n, m, d))
    pat = os.path.join(args.out, f"{args.id}_container_pattern.tzst")
    nbytes = build_pattern_from_prefixpack(pp_list, pat)
    print(f"[wine] {args.id}: body={os.path.getsize(body)//1024//1024}MB "
          f"pattern={nbytes//1024//1024}MB")


def cmd_component(args):
    """组件包原样透传，文件名用 {ver}.tzst"""
    out = os.path.join(args.out, f"{args.ver}.tzst")
    with open(args.wcp, 'rb') as fr, open(out, 'wb') as fw:
        fw.write(fr.read())
    print(f"[comp] {args.type}/{args.ver}: {os.path.getsize(out)//1024//1024}MB")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    w = sub.add_parser('wine')
    w.add_argument('--wcp', required=True); w.add_argument('--id', required=True)
    w.add_argument('--out', required=True); w.set_defaults(fn=cmd_wine)
    c = sub.add_parser('component')
    c.add_argument('--wcp', required=True); c.add_argument('--type', required=True)
    c.add_argument('--ver', required=True); c.add_argument('--out', required=True)
    c.set_defaults(fn=cmd_component)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    a.fn(a)


if __name__ == '__main__':
    main()

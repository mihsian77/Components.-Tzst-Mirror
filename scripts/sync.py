#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync.py — 全自动同步流水线编排。

流程：
  1. 读 config.yaml
  2. 拉上游 catalog（默认 nicholas 的 contents.json）
  3. 对每个收录条目：
       - 按其上游 release tag 判断路由：tag 含 nightly -> latest/，否则 -> stable/
       - 下载 wcp
       - 若是 wine/proton 类 -> convert.py 拆双包；组件类 -> 透传
       - 发布到 OWNER/REPO 的对应 tag（latest/<cat> 或 stable/<cat>）
       - 滚动覆盖：先删掉该 tag 下本次不在保留名单里的旧 asset
  4. 重建本仓 contents.json（type/verName/remoteUrl），提交回 main

环境变量（GitHub Actions 自动注入）：
  GITHUB_TOKEN   鉴权
  GITHUB_REPOSITORY  OWNER/REPO
"""
import os, sys, json, subprocess, tempfile, urllib.request, shutil, hashlib
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import convert  # noqa

CFG = os.path.join(ROOT, 'config.yaml')
OUT = tempfile.mkdtemp(prefix='artifacts_')


def run(cmd):
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if r.returncode != 0:
        print("CMD FAIL:", " ".join(cmd)); print(r.stderr)
    return r


def gh(*args):
    return run(['gh', *args])


def gh_json(url):
    r = gh('api', url)
    return json.loads(r.stdout) if r.returncode == 0 else []


def load_config():
    with open(CFG, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def fetch_catalog(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def route_tag(upstream_tag, cat):
    """上游 release tag -> 本仓 tag。nightly 类 -> latest/，否则 -> stable/。"""
    base = 'latest' if 'nightly' in upstream_tag.lower() else 'stable'
    return f"{base}/{cat.lower()}"


# 分类友好名
FRIENDLY = {
    'proton': 'Proton', 'wine': 'Wine',
    'box64': 'Box64 转译层', 'wowbox64': 'WOW64 Box64',
    'fexcore': 'FEXCore 转译层', 'dxvk': 'DXVK（Vulkan→DirectX）',
    'vkd3d': 'VKD3D（DirectX12→Vulkan）', 'd7vk': 'D7VK（DirectDraw→Vulkan）',
}

def _release_meta(tag):
    """tag=latest/proton 或 stable/dxvk -> (title, body)"""
    level, cat = tag.split('/', 1)
    cat_key = cat.lower()
    name = FRIENDLY.get(cat_key, cat)
    level_cn = '最新版（nightly）' if level == 'latest' else '稳定版'
    title = f'{name} · {level_cn}'
    return title, level, cat_key

def publish(tag, keep_files, keep_names, catalog=None):
    """把 keep_files 上传到 tag；删除同 tag 下不在 keep_names 的旧 asset。"""
    repo = os.environ['GITHUB_REPOSITORY']
    catalog = catalog or []
    title, level, cat_key = _release_meta(tag)
    # 文件名 -> 上游tag 映射
    src_map = {}
    for e in catalog:
        if e.get('file') and e.get('up_tag'):
            src_map[e['file']] = e['up_tag']
    lines = [f'## {title}', '', 'ZSTD 格式，可直接放入 Winlator assets。', '']
    lines.append('| 文件 | 上游来源 |')
    lines.append('|---|---|')
    for n in sorted(keep_names):
        src = src_map.get(n, '—')
        lines.append(f'| `{n}` | `{src}` |')
    body = '\n'.join(lines) + '\n'
    # 确保 release 存在
    r = gh('release', 'view', tag)
    if r.returncode != 0:
        gh('release', 'create', tag, '--title', title, '--notes', body, '--latest=false')
    else:
        # 已存在则更新标题和说明
        gh('release', 'edit', tag, '--title', title, '--notes', body)
    # 清旧 asset
    assets = gh_json(f"repos/{repo}/releases/tags/{tag}")
    for a in assets.get('assets', []):
        if a['name'] not in keep_names:
            gh('api', '--method', 'DELETE', f"repos/{repo}/releases/assets/{a['id']}")
    # 打一个整包zip（方便一键下载；不影响contents.json里的单文件URL）
    zip_path = os.path.join(OUT, f'{cat_key}.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in keep_files:
            zf.write(f, os.path.basename(f))
    upload_list = list(keep_files) + [zip_path]
    # 传新文件
    if upload_list:
        gh('release', 'upload', tag, *upload_list, '--clobber')


def main():
    cfg = load_config()
    collect = set(cfg['collect'])
    wine_types = set(cfg.get('wine_types', []))
    upstream_url = cfg['upstream_catalog']
    items = fetch_catalog(upstream_url)

    bucket = {}   # tag -> {'files':[], 'names':set(), 'catalog':[]}
    failed = []
    for idx, it in enumerate(items, 1):
        typ = it.get('type', '')
        if typ not in collect:
            continue
        remote = it.get('remoteUrl', '')
        if not remote:
            continue
        parts = remote.split('/releases/download/')
        up_tag = parts[1].split('/')[0] if len(parts) == 2 else 'stable'
        cat_tag = route_tag(up_tag, typ)
        ver = it.get('verName', os.path.basename(remote).replace('.wcp', ''))
        wcp = os.path.join(OUT, os.path.basename(remote))
        print(f"\n[{idx}/{len(items)}] {typ}/{ver}  [{up_tag} -> {cat_tag}]", flush=True)
        try:
            # 带进度的下载
            with urllib.request.urlopen(remote, timeout=120) as r, open(wcp, 'wb') as fout:
                total = int(r.headers.get('Content-Length', 0))
                got = 0
                while True:
                    chunk = r.read(1024*1024)
                    if not chunk: break
                    fout.write(chunk); got += len(chunk)
                    if total:
                        print(f"\r   下载 {got//1024//1024}/{total//1024//1024} MB", end='', flush=True)
            print(f"\n   下载完成 {os.path.getsize(wcp)//1024//1024}MB", flush=True)

            if typ in wine_types:
                bid = ver
                _wine_pair(wcp, bid, OUT)
                body = os.path.join(OUT, f"{bid}.tar.zst")
                pat = os.path.join(OUT, f"{bid}_container_pattern.tzst")
                files = [body, pat]
            else:
                dest = os.path.join(OUT, f"{ver}.tzst")
                convert.transcode_to_zst(wcp, dest)
                files = [dest]

            b = bucket.setdefault(cat_tag, {'files': [], 'names': set(), 'catalog': []})
            for f in files:
                if os.path.exists(f):
                    b['files'].append(f)
                    b['names'].add(os.path.basename(f))
            for f in files:
                name = os.path.basename(f)
                b['catalog'].append({
                    'type': typ,
                    'verName': ver if not name.endswith('_container_pattern.tzst') else None,
                    'file': name,
                    'up_tag': up_tag,
                })
            print(f"   ✅ OK -> {cat_tag}", flush=True)
        except Exception as e:
            print(f"   ❌ 跳过: {type(e).__name__}: {e}", flush=True)
            failed.append(f"{typ}/{ver}: {e}")

    if failed:
        print(f"\n=== 失败 {len(failed)} 个 ===")
        for x in failed: print("  -", x)

    # 逐 tag 发布（每个tag独立容错）
    repo = os.environ['GITHUB_REPOSITORY']
    for tag, b in bucket.items():
        try:
            publish(tag, b['files'], b['names'], b.get('catalog', []))
            print(f"published {tag}: {len(b['names'])} files")
        except Exception as e:
            print(f"!! 发布 {tag} 失败: {type(e).__name__}: {e}")

    # 生成本仓 contents.json（放在仓库根，raw 可读）
    flat = []
    for tag, b in bucket.items():
        for e in b['catalog']:
            if not e['verName']:
                continue
            flat.append({
                'type': e['type'],
                'verName': e['verName'],
                'verCode': '0',
                'remoteUrl': f"https://github.com/{repo}/releases/download/{tag}/{e['file']}",
            })
    with open(os.path.join(ROOT, 'contents.json'), 'w', encoding='utf-8') as f:
        json.dump(flat, f, ensure_ascii=False, indent=2)
    print(f"catalog: {len(flat)} entries")


def _wine_pair(wcp, bid, outdir):
    """调用 convert 的内部双包逻辑，绕过 argparse。"""
    kind, pp, members = convert.detect_wcp(wcp)
    body = os.path.join(outdir, f"{bid}.tar.zst")
    with open(wcp, 'rb') as fr, open(body, 'wb') as fw:
        fw.write(fr.read())
    pp_raw = next(data for m, data in members if m.name == pp)
    with convert._open_inner_tar(pp_raw) as tf:
        pp_list = []
        for m in tf.getmembers():
            n = m.name[2:] if m.name.startswith('./') else m.name
            d = tf.extractfile(m).read() if m.isfile() else None
            pp_list.append((n, m, d))
    pat = os.path.join(outdir, f"{bid}_container_pattern.tzst")
    convert.build_pattern_from_prefixpack(pp_list, pat)


if __name__ == '__main__':
    main()

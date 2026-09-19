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


def publish(tag, keep_files, keep_names):
    """把 keep_files 上传到 tag；删除同 tag 下不在 keep_names 的旧 asset。"""
    repo = os.environ['GITHUB_REPOSITORY']
    # 确保 release 存在
    r = gh('release', 'view', tag)
    if r.returncode != 0:
        gh('release', 'create', tag, '--title', tag, '--notes', 'auto', '--latest=false')
    # 清旧 asset
    assets = gh_json(f"repos/{repo}/releases/tags/{tag}")
    for a in assets.get('assets', []):
        if a['name'] not in keep_names:
            gh('api', '--method', 'DELETE', f"repos/{repo}/releases/assets/{a['id']}")
    # 传新文件
    if keep_files:
        gh('release', 'upload', tag, *keep_files, '--clobber')


def main():
    cfg = load_config()
    collect = set(cfg['collect'])
    wine_types = set(cfg.get('wine_types', []))
    upstream_url = cfg['upstream_catalog']
    items = fetch_catalog(upstream_url)

    bucket = {}   # tag -> {'files':[], 'names':set(), 'catalog':[]}
    for it in items:
        typ = it.get('type', '')
        if typ not in collect:
            continue
        remote = it.get('remoteUrl', '')
        if not remote:
            continue
        # 上游 release tag（从 remoteUrl 取）
        parts = remote.split('/releases/download/')
        up_tag = parts[1].split('/')[0] if len(parts) == 2 else 'stable'
        cat_tag = route_tag(up_tag, typ)
        ver = it.get('verName', os.path.basename(remote).replace('.wcp', ''))
        wcp = os.path.join(OUT, os.path.basename(remote))
        print(f"-> {typ}/{ver}  [{up_tag} -> {cat_tag}]")
        urllib.request.urlretrieve(remote, wcp)

        if typ in wine_types:
            # wine 双包：id 用 verName（bionic identifier 需与 array.xml 一致）
            bid = ver
            _wine_pair(wcp, bid, OUT)
            body = os.path.join(OUT, f"{bid}.tar.zst")
            pat = os.path.join(OUT, f"{bid}_container_pattern.tzst")
            files = [body, pat]
        else:
            dest = os.path.join(OUT, f"{ver}.tzst")
            shutil.copyfile(wcp, dest)
            files = [dest]

        b = bucket.setdefault(cat_tag, {'files': [], 'names': set(), 'catalog': []})
        for f in files:
            if os.path.exists(f):
                b['files'].append(f)
                b['names'].add(os.path.basename(f))
        # 记录 catalog 条目（remoteUrl 指向本仓）
        owner_repo = os.environ['GITHUB_REPOSITORY']
        for f in files:
            name = os.path.basename(f)
            b['catalog'].append({
                'type': typ,
                'verName': ver if not name.endswith('_container_pattern.tzst') else None,
                'file': name,
            })

    # 逐 tag 发布
    repo = os.environ['GITHUB_REPOSITORY']
    for tag, b in bucket.items():
        publish(tag, b['files'], b['names'])
        print(f"published {tag}: {len(b['names'])} files")

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

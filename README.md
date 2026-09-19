# aarch64 build artifacts

Automated packaging pipeline. Cron job pulls upstream release assets,
normalizes filenames, repacks prefix templates, and publishes per-category
releases.

## layout

```
.github/workflows/sync.yml   scheduled pipeline (every 6h + manual)
scripts/convert.py           single-asset normalizer (auto-detect layout)
scripts/sync.py              catalog crawl / download / publish orchestrator
config.yaml                  upstream URL + which categories to collect
contents.json                generated index (refreshed on every run)
```

## categories

| tag          | contents                              |
|--------------|---------------------------------------|
| `latest/*`   | nightly snapshots (rolling)           |
| `stable/*`   | pinned release snapshots              |

Each tag under `latest/` or `stable/` holds one category's current set.
Older assets are pruned automatically on every run — only the newest per
filename survives.

## naming

- runtime packages ship as `<id>.tar.zst`
- matched prefix template ships as `<id>_container_pattern.tzst`
- component packages ship as `<version>.tzst`

## index

`contents.json` is a flat array:

```json
[
  { "type": "...", "verName": "...", "verCode": "0", "remoteUrl": "..." }
]
```

The raw URL is stable and safe to point an app catalog at.

## notes

- No build toolchain here; everything is repackaged from upstream binaries.
- Runs on GitHub-hosted runners. `GITHUB_TOKEN` needs `contents: write`.

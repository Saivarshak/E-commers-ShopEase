from __future__ import annotations
import csv, json
from pathlib import Path
from collections import defaultdict
ROOT = Path(__file__).resolve().parent
rows = list(csv.DictReader((ROOT/'image-audit.csv').open(encoding='utf-8')))
summary = json.loads((ROOT/'image-audit-summary.json').read_text(encoding='utf-8'))
refs = json.loads((ROOT/'image-reference-audit.json').read_text(encoding='utf-8'))
for r in rows:
    r['bytes'] = int(r['bytes']); r['kb'] = float(r['kb']); r['width'] = int(r['width']) if r['width'] else None; r['height'] = int(r['height']) if r['height'] else None
by_path = {r['path']: r for r in rows}
# duplicate storage overhead, counting all but one file per exact content hash
groups = defaultdict(list)
for r in rows: groups[r['sha256']].append(r)
duplicate_overhead = sum(r['bytes'] for group in groups.values() for r in sorted(group, key=lambda x:x['path'])[1:])
# variant savings for static/images same-stem raster -> webp
static = [r for r in rows if r['path'].startswith('static/images/')]
webps = {Path(r['path']).stem: r for r in static if r['extension']=='webp'}
variants=[]
for r in static:
    if r['extension'] in {'jpg','jpeg','png'} and Path(r['path']).stem in webps:
        w=webps[Path(r['path']).stem]
        if r['bytes'] > w['bytes']:
            variants.append((r,w,r['bytes']-w['bytes']))
variants.sort(key=lambda x:x[2], reverse=True)
large=[r for r in rows if r['bytes'] >= 100*1024]
very_large=[r for r in rows if r['bytes'] >= 250*1024]
report=[]
report.append('# ShopEase Comprehensive Image Bandwidth Audit\n')
report.append('## Executive summary\n')
report.append(f"The read-only inventory found **{summary['image_count']} image files** totaling **{summary['total_mb']:.3f} MB** across the repository. This is stored file size, not serving bandwidth. The audit found **{len(groups)} unique content hashes**, with approximately **{duplicate_overhead/1024/1024:.3f} MB** of duplicate storage caused by copied files and collected static artifacts.\n")
report.append('| Measure | Result |')
report.append('|---|---:|')
report.append(f"| Total image files | {summary['image_count']} |")
report.append(f"| Total image storage | {summary['total_mb']:.3f} MB |")
report.append(f"| Files at least 100 KB | {len(large)} |")
report.append(f"| Files at least 250 KB | {len(very_large)} |")
report.append(f"| Exact duplicate storage overhead | {duplicate_overhead/1024/1024:.3f} MB |")
report.append(f"| Static raster-to-WebP pairs found | {len(variants)} |\n")
report.append('## Format distribution\n')
report.append('| Format | Files | Storage |')
report.append('|---|---:|---:|')
for ext,b in sorted(summary['by_extension'].items(), key=lambda x:x[1]['bytes'], reverse=True): report.append(f"| {ext.upper()} | {b['count']} | {b['mb']:.3f} MB |")
report.append('\n## Highest-priority assets\n')
report.append('| Asset | Size | Dimensions | Format reported by decoder | Priority |')
report.append('|---|---:|---:|---|---|')
for r in rows[:20]:
    priority = 'Critical' if r['bytes'] >= 250*1024 else ('High' if r['bytes'] >= 100*1024 else 'Review')
    dims = f"{r['width']}×{r['height']}" if r['width'] else '—'
    report.append(f"| `{r['path']}` | {r['kb']:.1f} KB | {dims} | {r['format'] or r['extension'].upper()} | {priority} |")
report.append('\n## Static raster files with smaller WebP variants\n')
report.append('| Original | Original size | WebP | WebP size | Potential saving per request |')
report.append('|---|---:|---|---:|---:|')
for old,new,saving in variants[:30]: report.append(f"| `{old['path']}` | {old['kb']:.1f} KB | `{new['path']}` | {new['kb']:.1f} KB | {saving/1024:.1f} KB |")
report.append('\n## Template-reference findings\n')
report.append(f"The scanner found **{refs['reference_count']} image-related template references** across **{refs['unique_reference_count']} unique expressions**. Dynamic expressions such as `{{% asset_src item.image_path %}}` cannot be mapped to a concrete file without representative database rows; they require route-level testing.\n")
report.append('The route-aware audit should be used together with this inventory because a file present in the repository is not necessarily requested by a public page, and a database-backed upload may be requested even when it is absent from local static files.\n')
report.append('## Known issues from route testing\n')
report.append('| Issue | Evidence | Action |')
report.append('|---|---|---|')
report.append('| PNG logo reference | A prior route audit measured `static/images/logo1.png` at ~90 KB and the WebP derivative at ~17 KB | Women’s page was changed locally to `logo1.webp`; preserve PNG fallback |')
report.append('| Missing placeholder | `/assets/images/placeholder.svg` returned HTTP 404 during `/womens/` testing | Add the file or remove the stale reference |')
report.append('| Local dotenv warning | The project `.env` contains a malformed line and emits parse warnings | Fix separately; do not expose or commit secrets |')
report.append('\n## Recommended order of work\n')
report.append('First, update every public template that still references a raster file when a verified WebP derivative exists. Second, generate derivatives for the remaining large static JPEG/PNG files while preserving originals. Third, remove duplicate collected/static copies only through the normal `collectstatic` build process rather than deleting source assets manually. Fourth, fix missing image references and add `srcset`/`sizes` for responsive delivery. Finally, run the route-aware audit with `--fail-on-http-error` and a fixed route set before each push.\n')
report.append('Do not treat the 2.639 MB repository image total as an estimate of historical production bandwidth usage. Serving bandwidth depends on request volume multiplied by response bytes, including repeated downloads, bots, and dynamic database-backed media.\n')
(ROOT/'IMAGE_BANDWIDTH_AUDIT.md').write_text('\n'.join(report)+'\n', encoding='utf-8')
print('\n'.join(report[:30]))

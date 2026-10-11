# ShopEase Comprehensive Image Bandwidth Audit

## Executive summary

The read-only inventory found **167 image files** totaling **2.639 MB** across the repository. This is stored file size, not serving bandwidth. The audit found **119 unique content hashes**, with approximately **0.319 MB** of duplicate storage caused by copied files and collected static artifacts.

| Measure | Result |
|---|---:|
| Total image files | 167 |
| Total image storage | 2.639 MB |
| Files at least 100 KB | 4 |
| Files at least 250 KB | 1 |
| Exact duplicate storage overhead | 0.319 MB |
| Static raster-to-WebP pairs found | 40 |

## Format distribution

| Format | Files | Storage |
|---|---:|---:|
| JPG | 61 | 2.144 MB |
| WEBP | 40 | 0.247 MB |
| PNG | 20 | 0.206 MB |
| SVG | 42 | 0.034 MB |
| JPEG | 2 | 0.006 MB |
| GIF | 2 | 0.003 MB |

## Highest-priority assets

| Asset | Size | Dimensions | Format reported by decoder | Priority |
|---|---:|---:|---|---|
| `static/images/shirt.jpg` | 478.0 KB | 1024×683 | PNG | Critical |
| `static/images/loginbg.jpg` | 228.9 KB | 5000×2924 | JPEG | High |
| `staticfiles/images/casual_shirts.jpg` | 168.6 KB | 960×1200 | JPEG | High |
| `staticfiles/images/lounge_wear.jpg` | 115.6 KB | 960×1200 | JPEG | High |
| `staticfiles/images/jackets.jpg` | 96.7 KB | 960×1200 | JPEG | Review |
| `staticfiles/images/sweatshirts.jpg` | 91.5 KB | 960×1200 | JPEG | Review |
| `static/images/sweatshirts.jpg` | 91.5 KB | 960×1200 | JPEG | Review |
| `staticfiles/images/logo1.dff6f0c10b34.png` | 88.2 KB | 500×500 | PNG | Review |
| `static/images/logo1.png` | 88.2 KB | 500×500 | PNG | Review |
| `staticfiles/images/joggers.jpg` | 85.2 KB | 960×1200 | JPEG | Review |
| `staticfiles/images/cargo_pants.jpg` | 82.3 KB | 960×1200 | JPEG | Review |
| `staticfiles/images/tank_tops_vests.jpg` | 70.9 KB | 960×1200 | JPEG | Review |
| `static/images/tank_tops_vests.jpg` | 70.9 KB | 960×1200 | JPEG | Review |
| `staticfiles/images/track_pants.jpg` | 56.6 KB | 960×1200 | JPEG | Review |
| `static/images/sweatshirts.webp` | 46.0 KB | 960×1200 | WEBP | Review |
| `static/images/tank_tops_vests.webp` | 31.8 KB | 960×1200 | WEBP | Review |
| `static/images/shirt.webp` | 28.9 KB | 1024×683 | WEBP | Review |
| `static/images/loginbg.webp` | 19.7 KB | 1600×936 | WEBP | Review |
| `static/images/allen-solly-formal-shirts.jpg` | 19.2 KB | 800×800 | JPEG | Review |
| `static/images/american-tourister-bags.jpg` | 19.2 KB | 800×800 | JPEG | Review |

## Static raster files with smaller WebP variants

| Original | Original size | WebP | WebP size | Potential saving per request |
|---|---:|---|---:|---:|
| `static/images/shirt.jpg` | 478.0 KB | `static/images/shirt.webp` | 28.9 KB | 449.1 KB |
| `static/images/loginbg.jpg` | 228.9 KB | `static/images/loginbg.webp` | 19.7 KB | 209.2 KB |
| `static/images/logo1.png` | 88.2 KB | `static/images/logo1.webp` | 16.9 KB | 71.3 KB |
| `static/images/sweatshirts.jpg` | 91.5 KB | `static/images/sweatshirts.webp` | 46.0 KB | 45.5 KB |
| `static/images/tank_tops_vests.jpg` | 70.9 KB | `static/images/tank_tops_vests.webp` | 31.8 KB | 39.1 KB |
| `static/images/allen-solly-formal-shirts.jpg` | 19.2 KB | `static/images/allen-solly-formal-shirts.webp` | 4.6 KB | 14.6 KB |
| `static/images/allen-solly-polo-shirts.jpg` | 18.7 KB | `static/images/allen-solly-polo-shirts.webp` | 4.2 KB | 14.5 KB |
| `static/images/american-tourister-bags.jpg` | 19.2 KB | `static/images/american-tourister-bags.webp` | 5.0 KB | 14.2 KB |
| `static/images/allen-solly-blazers.jpg` | 18.0 KB | `static/images/allen-solly-blazers.webp` | 3.8 KB | 14.2 KB |
| `static/images/polaroid-sunglasses.jpg` | 18.4 KB | `static/images/polaroid-sunglasses.webp` | 4.3 KB | 14.1 KB |
| `static/images/allen-solly-belts.jpg` | 17.6 KB | `static/images/allen-solly-belts.webp` | 3.6 KB | 14.0 KB |
| `static/images/bags-accessories.jpg` | 18.5 KB | `static/images/bags-accessories.webp` | 4.5 KB | 14.0 KB |
| `static/images/baggit-belts.jpg` | 17.2 KB | `static/images/baggit-belts.webp` | 3.4 KB | 13.8 KB |
| `static/images/baggit-wallets.jpg` | 17.4 KB | `static/images/baggit-wallets.webp` | 3.7 KB | 13.8 KB |
| `static/images/idee-sunglasses.jpg` | 17.7 KB | `static/images/idee-sunglasses.webp` | 4.0 KB | 13.7 KB |
| `static/images/men-s-accessories.jpg` | 17.5 KB | `static/images/men-s-accessories.webp` | 3.9 KB | 13.7 KB |
| `static/images/biba-kids-ethnic-wear.jpg` | 18.0 KB | `static/images/biba-kids-ethnic-wear.webp` | 4.4 KB | 13.6 KB |
| `static/images/fabindia-sherwanis.jpg` | 17.6 KB | `static/images/fabindia-sherwanis.webp` | 4.1 KB | 13.6 KB |
| `static/images/fastrack-watches.jpg` | 17.5 KB | `static/images/fastrack-watches.webp` | 4.0 KB | 13.5 KB |
| `static/images/cargo-pants.jpg` | 16.6 KB | `static/images/cargo-pants.webp` | 3.2 KB | 13.5 KB |
| `static/images/adidas-sneakers.jpg` | 17.3 KB | `static/images/adidas-sneakers.webp` | 3.8 KB | 13.4 KB |
| `static/images/nike-t-shirts.jpg` | 16.4 KB | `static/images/nike-t-shirts.webp` | 3.0 KB | 13.4 KB |
| `static/images/levis-belts.jpg` | 16.3 KB | `static/images/levis-belts.webp` | 2.9 KB | 13.4 KB |
| `static/images/adidas-t-shirts.jpg` | 16.8 KB | `static/images/adidas-t-shirts.webp` | 3.4 KB | 13.4 KB |
| `static/images/sunglasses.jpg` | 16.6 KB | `static/images/sunglasses.webp` | 3.2 KB | 13.4 KB |
| `static/images/bata-dress-shoes.jpg` | 17.2 KB | `static/images/bata-dress-shoes.webp` | 3.9 KB | 13.3 KB |
| `static/images/fossil-wallets.jpg` | 16.6 KB | `static/images/fossil-wallets.webp` | 3.4 KB | 13.3 KB |
| `static/images/kisah-sherwanis.jpg` | 17.0 KB | `static/images/kisah-sherwanis.webp` | 3.8 KB | 13.2 KB |
| `static/images/aurelia-kurtas.jpg` | 16.5 KB | `static/images/aurelia-kurtas.webp` | 3.3 KB | 13.2 KB |
| `static/images/levis-wallets.jpg` | 16.4 KB | `static/images/levis-wallets.webp` | 3.3 KB | 13.2 KB |

## Template-reference findings

The scanner found **104 image-related template references** across **18 unique expressions**. Dynamic expressions such as `{% asset_src item.image_path %}` cannot be mapped to a concrete file without representative database rows; they require route-level testing.

The route-aware audit should be used together with this inventory because a file present in the repository is not necessarily requested by a public page, and a database-backed upload may be requested even when it is absent from local static files.

## Known issues from route testing

| Issue | Evidence | Action |
|---|---|---|
| PNG logo reference | A prior route audit measured `static/images/logo1.png` at ~90 KB and the WebP derivative at ~17 KB | Women’s page was changed locally to `logo1.webp`; preserve PNG fallback |
| Missing placeholder | `/assets/images/placeholder.svg` returned HTTP 404 during `/womens/` testing | Add the file or remove the stale reference |
| Local dotenv warning | The project `.env` contains a malformed line and emits parse warnings | Fix separately; do not expose or commit secrets |

## Recommended order of work

First, update every public template that still references a raster file when a verified WebP derivative exists. Second, generate derivatives for the remaining large static JPEG/PNG files while preserving originals. Third, remove duplicate collected/static copies only through the normal `collectstatic` build process rather than deleting source assets manually. Fourth, fix missing image references and add `srcset`/`sizes` for responsive delivery. Finally, run the route-aware audit with `--fail-on-http-error` and a fixed route set before each push.

Do not treat the 2.639 MB repository image total as an estimate of historical production bandwidth usage. Serving bandwidth depends on request volume multiplied by response bytes, including repeated downloads, bots, and dynamic database-backed media.


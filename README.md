# OpenGloucester

Source for [OpenGloucester](https://gloucester-ma.publick.org), part of the Publick network of town sites: an independent, read-only site that publishes public data about Gloucester, Massachusetts: how the city responds to 311 requests, what is on upcoming meeting agendas, and more over time.

The site is static HTML built by a small Python script and deployed to GitHub Pages by GitHub Actions. There is no server and no database.

## Layout

```
config/<town>.toml          Everything town-specific: name, domain, sources, sections
pipeline/                   Python package
  fetch_meetings.py         Daily: city calendar -> data/meetings/
  fetch_minutes.py          Daily: Archive Center minutes -> data/meetings/minutes/
  fetch_drive_meetings.py   Daily: School Committee agendas and minutes (Google Drive) -> data/meetings/
  summarize.py              Daily: agenda and minutes PDFs -> readable text + summaries (AI) -> data/summaries/
  fetch_311.py              Daily: SeeClickFix -> data/311/requests.json (also --backfill YYYY-MM)
  compute_311.py            Daily: requests -> data/311/scorecard.json
  fetch_finance.py          Average single-family tax bill (Mass. DLS) -> data/finance/
  fetch_labor.py            Unemployment rate (BLS LAUS) -> data/labor/
  fetch_schools.py          Graduation, absenteeism, MCAS (DESE) -> data/schools/
  fetch_permits.py          Building and demolition permits (city Data Hub) -> data/permits/
  streets.py                Street-name matching for the street lookup
  freshness.py              Daily: fails the run when a data source stops updating
  civicplus.py, seeclickfix.py   Source parsers
  geo.py                    Ward/precinct point-in-polygon lookup
  http.py                   Rate-limited HTTP client with retries
  build_site.py             Renders site/ + data/ into _site/
data/                       Collected data, committed by the daily job
  meetings/meetings.json    One record per meeting, with a history of changes
  meetings/agendas/         Saved copy of every agenda as posted
  summaries/                AI-generated agenda text, cached by document hash
  311/                      311 requests and the computed scorecard
  static/                   Ward and precinct boundaries (see data/README.md)
site/templates/             Shared layout and per-record templates (meeting, board)
site/pages/                 One folder per section; each index.html becomes /<section>/
site/static/                CSS, icons, and other files copied as-is
site/static/vendor/leaflet/ Leaflet 1.9.4 map library, self-hosted (BSD-2-Clause)
tests/                      Pipeline, structure, link, and accessibility checks (offline)
.github/workflows/          Daily update, test, and deploy to GitHub Pages
```

## Build and test locally

Requires Python 3.11 or newer.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium

python -m pipeline.fetch_meetings         # update meetings from the city website
python -m pipeline.fetch_311              # update 311 requests from SeeClickFix
python -m pipeline.compute_311            # recompute the 311 scorecard
ANTHROPIC_API_KEY=... python -m pipeline.summarize   # agenda text and previews (optional)
python -m pipeline.build_site             # writes _site/
python -m http.server -d _site 8000       # browse at http://localhost:8000
python -m pytest                          # runs offline against tests/fixtures/
```

## Starting a site for another town

Each town gets its own copy of this repository, its own `config/<town>.toml`, and its own `data/`.

1. **Copy the repository** (fork it, or push a copy to a new repository). Delete everything in `data/` except `data/README.md` and `data/static/`. Keep `config/gloucester.toml`, `data/static/gloucester-precincts-2022.geojson` and `tests/fixtures/`: the tests run against saved Gloucester data.
2. **Write `config/<town>.toml`**, starting from a copy of `config/gloucester.toml`. `[site]`, `[town]`, `[[sections]]`, `[meetings]` and `[archive]` are required. Every other table is one data source. Leave out a table the town doesn't have and the command that fetches it does nothing:

   | Table | Source | Works for |
   |---|---|---|
   | `[meetings]`, `[archive]` | CivicPlus calendar and Archive Center | Towns whose website runs on CivicPlus |
   | `[drive_meetings]` | Agendas and minutes in public Google Drive folders (Gloucester's School Committee) | Any board whose folders are laid out one per committee, with dates in file names |
   | `[seeclickfix]` | SeeClickFix 311 requests | Towns on SeeClickFix; needs a ward boundary file in `data/static/` whose features carry `ward` and `population_2020`, like Gloucester's from MassGIS |
   | `[finance]` | Tax bill and budget (Mass. DLS) | Massachusetts |
   | `[schools]` | DESE | Massachusetts districts |
   | `[housing]` | Census and the Subsidized Housing Inventory | Massachusetts (the Census parts work anywhere) |
   | `[labor]` | BLS unemployment | Anywhere BLS publishes a local series |
   | `[permits]` | The city's permit spreadsheet | Gloucester's Data Hub layout only |
   | `[summaries]` | AI summaries of agendas and minutes | Anywhere, with `ANTHROPIC_API_KEY` |
   | `[freshness]` | Stale-data alerts | List only the sources the town has |
   | `[storage]` | Keeps agenda and minutes PDFs in a bucket instead of git | Recommended for every town; see [Document storage](#document-storage) |

   Rewrite the hand-written content for the new town from its own sources: `[meetings.aliases]`, `[archive.aliases]`, `[participation.*]`, `[[glossary]]` and `[[seeclickfix.annotations]]`.
3. **List only the town's sections** in `[[sections]]`. Page folders under `site/pages/` for sections that aren't listed are not built, and their data is ignored.
4. **Set `TOWN`** at the top of `.github/workflows/deploy.yml`. Every command reads it; locally, pass `--town <town>` or set `TOWN`.
5. **Fetch and build locally** to see what the town's sources return:

   ```sh
   export TOWN=<town>
   python -m pipeline.fetch_meetings && python -m pipeline.fetch_minutes
   python -m pipeline.fetch_311 --backfill 2024-01 && python -m pipeline.compute_311
   python -m pipeline.build_site && python -m http.server -d _site 8000
   ```

6. **Redraw the icon** in `site/static/favicon.svg` (the first letter of `name_suffix`), then run `python -m pipeline.make_share_image` for the share image and PNG icons. Set up the domain and secrets as under [Deploying](#deploying).

Page text is written for a Massachusetts city. A town (rather than a city), or a town outside Massachusetts, needs a read through the page wording.

## Adding a section

1. Add a `[[sections]]` entry to `config/<town>.toml`. It appears in the main navigation.
2. Create `site/pages/<slug>/index.html` extending `base.html`. It is served at `/<slug>/`.
3. Sub-pages go in sub-folders: `site/pages/<slug>/<name>/index.html` is served at `/<slug>/<name>/`.
4. Pages generated from data (one per record) use a template in `site/templates/` and are added in `pipeline/build_site.py`.

New pages are picked up by the tests automatically.

## Data collection

- The workflow runs every morning, fetches new data, commits any changes under `data/`, then tests, builds, and deploys.
- Requests identify the site in the User-Agent, wait between calls, and back off on errors. The city website rate-limits bursts of requests.
- Records are never deleted. When a source changes something after posting it, the change is recorded in the record's `history`.
- Meeting page URLs are fixed when a meeting is first recorded, so links keep working if the city renames or reschedules it.

## Accessibility

The site targets [WCAG 2.2](https://www.w3.org/TR/WCAG22/) Level AA. Every build runs axe-core against each page in light and dark mode at desktop and 320px widths, and checks reflow, text resizing, and keyboard access. A failing check blocks deployment.

Rules for new pages:

- One `<h1>` per page, with headings in order.
- Every chart has a data table with the same figures. Every map has a list of the same locations, and the map is not the only way to reach any information.
- Don't use color alone to carry meaning. Pair it with text, a pattern, or a shape.
- All controls work with a keyboard and have a visible focus style.
- Link text makes sense on its own (no "click here").
- Pages work without JavaScript wherever possible.
- Check each new section by hand with a keyboard and a screen reader before launch.

## Share image

The picture shown when a page is shared is `site/static/share/<town>.png`. Redraw it after changing the site name or tagline:

```
python -m pipeline.make_share_image --town gloucester
```

## Deploying

Pushes to `main` test, build, and deploy. Pull requests test only. The daily schedule and the **Run workflow** button also fetch new data first.

One-time setup. Each town's site is a subdomain of the network's domain, `<town>-<state>.publick.org`, whose DNS is on Cloudflare:

1. **Verify the network domain** once, so no other account can claim it or its subdomains: GitHub profile **Settings → Pages → Add a domain**, enter `publick.org`, then add the TXT record it gives you in Cloudflare **DNS → Records**.
2. **Repository settings → Pages → Source:** GitHub Actions.
3. **DNS record** in Cloudflare: a `CNAME` named `<town>-<state>` (e.g. `gloucester-ma`) with target `<github-owner>.github.io` (for Publick: `publick-org.github.io`), **Proxy status: DNS only** (grey cloud). Proxied records stop GitHub from issuing the site's certificate.
4. **Repository settings → Pages → Custom domain:** enter the town's domain, matching `domain` in its config. Once the certificate is issued, turn on **Enforce HTTPS**.

### Moving a site to a new domain

GitHub Pages serves one custom domain per repository, so a town's old domain is redirected at Cloudflare. For OpenGloucester's move from `opengloucester.org`:

1. Create the new site's DNS record (step 3 above) and wait for it to resolve.
2. Change `domain` in the town's config and merge; then set the same domain under **Repository settings → Pages → Custom domain** straight away. Until then, pages name the new address while being served from the old one.
3. Add the old domain to Cloudflare (**Add a domain**, Free plan) and switch its nameservers at the registrar to the two Cloudflare gives. Before switching, turn off DNSSEC at the registrar if it's on.
4. In the old domain's **DNS → Records**, replace the GitHub records with one `A` record for `@` and one for `www`, both pointing to `192.0.2.1` with **Proxied** (orange cloud). The address is never reached; Cloudflare answers first.
5. **Rules → Redirect Rules → Create rule:** match all incoming requests, with a **Dynamic** redirect to `concat("https://gloucester-ma.publick.org", http.request.uri.path)`, status **301**, and **Preserve query string** on. Old links, including deep ones like `/meetings/…`, land on the same page at the new address.

## Document storage

Agenda and minutes PDFs average well over a megabyte, git keeps every version forever, and a GitHub Pages site may be at most 1 GB. Without a `[storage]` table they're committed under `data/meetings/` and copied into the site, which works for a small or short-lived town. With one, they go to an S3-compatible bucket and pages link to the bucket's public address. Git keeps each document's text, summary and SHA-256 hash, so the site is still rebuilt entirely from the repository.

One bucket serves every town: each town's files sit under its own prefix (`gloucester/agendas/<id>.pdf`). Cloudflare R2 is the suggested host: no charge for downloads, and the first 10 GB are free.

1. **Create the bucket** in Cloudflare: **R2 → Create bucket**, e.g. `publick-documents`.
2. **Give it a public address:** the bucket's **Settings → Custom Domains → Add**, e.g. `files.publick.org`. The domain's DNS must be on Cloudflare, in the same account. Keep GitHub Pages' own records set to *DNS only* so GitHub can still issue the site's certificate. The bucket's `r2.dev` address is rate-limited and meant only for testing.
3. **Create an API token:** **R2 → Manage API tokens → Create**, with *Object Read & Write* on that bucket only. Save its access key ID and secret as the repository secrets `STORAGE_ACCESS_KEY_ID` and `STORAGE_SECRET_ACCESS_KEY`.
4. **Add the table** to `config/<town>.toml`, with the account ID from the R2 overview page:

   ```toml
   [storage]
   endpoint = "https://<account id>.r2.cloudflarestorage.com"
   bucket = "publick-documents"
   public_url = "https://files.publick.org"
   ```

5. **Run the workflow.** New PDFs go straight to the bucket. The **Move saved documents to storage** step uploads the ones already in `data/meetings/`, checks each copy, and commits their removal. Until a file is moved, the site keeps linking to its copy in the repository.

The files remain in the repository's git history. Shrinking the history means rewriting it, which is a separate decision.

## Data and licenses

The code is under the [MIT License](LICENSE). Data keeps the terms of its source. See [`data/README.md`](data/README.md). 311 data comes from [SeeClickFix](https://seeclickfix.com) under [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/). Other sources are listed on the site's [About page](https://gloucester-ma.publick.org/about/).

## Secrets

- `ANTHROPIC_API_KEY` (repository secret, optional): enables agenda and minutes text and summaries. Without it the step is skipped.
- `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY` (repository secrets, needed with `[storage]`): an R2 API token for the documents bucket. See [Document storage](#document-storage).
- `BLS_API_KEY` (repository secret, optional): free key from bls.gov/developers for the unemployment rate. Without it the job uses BLS's keyless limit, then falls back to the bulk data file.

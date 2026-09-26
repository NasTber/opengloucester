# OpenGloucester

Source for [opengloucester.org](https://opengloucester.org), an independent, read-only site that publishes public data about Gloucester, Massachusetts: how the city responds to 311 requests, what is on upcoming meeting agendas, and more over time.

The site is static HTML built by a small Python script and deployed to GitHub Pages by GitHub Actions. There is no server and no database.

## Layout

```
config/<town>.toml          Everything town-specific: name, domain, sources, sections
pipeline/                   Python package
  fetch_meetings.py         Daily: city calendar -> data/meetings/
  fetch_minutes.py          Daily: Archive Center minutes -> data/meetings/minutes/
  summarize.py              Daily: agenda and minutes PDFs -> readable text + summaries (AI) -> data/summaries/
  fetch_311.py              Daily: SeeClickFix -> data/311/requests.json (also --backfill YYYY-MM)
  compute_311.py            Daily: requests -> data/311/scorecard.json
  fetch_finance.py          Average single-family tax bill (Mass. DLS) -> data/finance/
  fetch_labor.py            Unemployment rate (BLS LAUS) -> data/labor/
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

## Adding a section

1. Add a `[[sections]]` entry to `config/gloucester.toml`. It appears in the main navigation.
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

## Deploying

Pushes to `main` test, build, and deploy. Pull requests test only. The daily schedule and the **Run workflow** button also fetch new data first.

One-time setup:

1. **Verify the domain** so no other account can claim it: GitHub profile **Settings → Pages → Add a domain**, then add the TXT record it gives you at the DNS provider.
2. **Repository settings → Pages → Source:** GitHub Actions.
3. **DNS records:**
   - Apex `A` records: `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
   - Optional apex `AAAA` records: `2606:50c0:8000::153`, `2606:50c0:8001::153`, `2606:50c0:8002::153`, `2606:50c0:8003::153`
   - `www` `CNAME` → `<github-username>.github.io`
4. **Repository settings → Pages → Custom domain:** enter the domain. Once the certificate is issued, turn on **Enforce HTTPS**.

## Data and licenses

See [`data/README.md`](data/README.md). 311 data comes from [SeeClickFix](https://seeclickfix.com) under [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/). Other sources are listed on the site's [About page](https://opengloucester.org/about/).

## Secrets

- `ANTHROPIC_API_KEY` (repository secret, optional): enables agenda and minutes text and summaries. Without it the step is skipped.
- `BLS_API_KEY` (repository secret, optional): free key from bls.gov/developers for the unemployment rate. Without it the job uses BLS's keyless limit, then falls back to the bulk data file.

# Gloucester Publick

Source for [Gloucester Publick](https://gloucester-ma.publick.org), part of the Publick network of town sites: an independent, read-only site that publishes public data about Gloucester, Massachusetts: how the city responds to 311 requests, what is on upcoming meeting agendas, the budget, schools, housing, and more.

The site is built by the [Publick engine](https://github.com/publick-org/publick-engine), which holds the code, page templates, and the daily workflow shared by every town. This repository holds only what is Gloucester's own:

```
config/gloucester.toml          Everything town-specific: name, domain, sources, sections
data/                           Collected data, committed by the daily job (see data/README.md)
site/static/share/gloucester.png  The image shown when a page is shared
.github/workflows/deploy.yml    Runs the engine's workflow, pinned to an engine version
```

Every morning the workflow fetches new data, commits it, builds the site, checks every page (structure, links, and WCAG 2.2 AA accessibility), and deploys it to GitHub Pages. Pull requests build and check only. The engine version is the `@v1` in `deploy.yml`.

## Build locally

With the engine cloned next to this repository:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r ../publick-engine/requirements.txt -r ../publick-engine/requirements-dev.txt
python -m playwright install chromium
export PYTHONPATH=../publick-engine

python -m pipeline.build_site             # writes _site/
python -m http.server -d _site 8000       # browse at http://localhost:8000
python -m pytest ../publick-engine/site_checks
```

Agenda and minutes PDFs are kept in the Publick documents bucket (`[storage]` in the config). A local fetch needs the bucket's keys (`STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`), or set `DOCUMENTS_LOCAL=1` to keep PDFs under `data/meetings/`. Building needs no keys.

Everything else, including how to add a section, deploy, set up secrets, or start a site for another town, is in the [engine's README](https://github.com/publick-org/publick-engine#readme).

## Data and licenses

The code is under the [MIT License](LICENSE). Data keeps the terms of its source. See [`data/README.md`](data/README.md). 311 data comes from [SeeClickFix](https://seeclickfix.com) under [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/). Other sources are listed on the site's [About page](https://gloucester-ma.publick.org/about/).

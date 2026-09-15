# monitor-ofert

A small Python tool that tracks real-estate listings from Polish classifieds
portals (**OLX** and **Otodom**) and tells you what actually changed since the
last time you looked.

The first run downloads every listing matching your search and stores it in a
local SQLite database. Every run after that only reports:

- **new** listings,
- **price changes** (with the full price history kept in the database),
- listings that **disappeared** (sold or withdrawn),
- listings that **came back** after disappearing.

It can also export the active listings to CSV, or render them as a
single-file **interactive map** with filters.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) — manages the virtualenv and dependencies
  declared in `pyproject.toml` (locked in `uv.lock`):
  - `curl_cffi` mimics a real browser's TLS fingerprint, which is what OLX
    uses to detect scripted requests,
  - `jinja2` renders the interactive map,
  - `rich` formats the terminal report (tables, colours, clickable links),
  - `python-dotenv` loads optional settings from a `.env` file.

```bash
uv sync            # creates .venv and installs the locked dependencies
```

`uv run` syncs the environment automatically, so the step above is optional.

### Map tiles API key (optional)

The map uses CARTO basemap tiles, which since August 2026 need an API key —
without one the map still works, but every tile shows an "API KEY REQUIRED"
watermark (and the script prints a warning). Request a free key at
<https://carto.com/basemaps/apikey>, then put it in a `.env` file in the
repository root (it is git-ignored):

```bash
cp .env.example .env   # then fill in CARTO_API_KEY
```

A `CARTO_API_KEY` variable already set in the environment takes precedence.
Note that the key ends up inside the generated HTML file.

## Usage

```bash
# all sources (OLX + Otodom), default search: flats for sale in Kraków
uv run src/monitor_ofert.py

# a single source
uv run src/monitor_ofert.py --source olx

# quick mode: only check for NEW listings (skips price/removal detection)
uv run src/monitor_ofert.py --quick

# dump active listings to CSV
uv run src/monitor_ofert.py --export oferty.csv

# render the interactive map
uv run src/monitor_ofert.py --html mapa.html

# your own searches, into a separate database file
uv run src/monitor_ofert.py --db tanie.db \
    --url "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/krakow/?search[filter_float_price:to]=700000" \
    --url "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/malopolskie/krakow/krakow/krakow?priceMax=700000"

# no network at all — just export what's already in the database
uv run src/monitor_ofert.py --offline --html mapa.html
```

### Options

| Option | Meaning |
| --- | --- |
| `--url URL` | Search URL from a supported portal; repeatable |
| `--source NAME` | Restrict to one source (`olx`, `otodom`); repeatable |
| `--db FILE` | SQLite database file (default `oferty.db`) |
| `--quick` | Only look for new listings |
| `--export FILE.csv` | Write active listings to CSV |
| `--html FILE.html` | Generate the standalone interactive map |
| `--offline` | Don't query the portals (export only) |
| `--delay SECONDS` | Pause between requests (default `0.6`) |
| `--category-id` / `--city-id` / `--region-id` | Manual OLX ids, if auto-detection fails |
| `--force` | Allow reusing a database created for a different search |

Note: the script's console output and `--help` text are in Polish.

## Included data

This repo ships a real snapshot so you can look at the output without
scraping anything yourself. Both files are gzipped to stay under GitHub's file
size limits — unpack them first:

```bash
gunzip -k oferty.db.gz     # -> oferty.db  (239 MB)
gunzip -k data.html.gz     # -> data.html  (70 MB)
```

- **`oferty.db.gz`** — SQLite database, snapshot from 2026-09-01: 15 627
  listings (13 217 Otodom, 2 410 OLX), 14 832 of them still active, plus
  15 763 price-history rows. Tables: `offers`, `price_history`, `meta`.
- **`data.html.gz`** — the generated map: one self-contained HTML file
  (Leaflet and Apache ECharts are inlined, no CDN needed) with all listings plotted and filters
  for price, area, rooms, floor and district. Open it in a browser.

You can regenerate the HTML from the database at any time:

```bash
uv run src/monitor_ofert.py --offline --html data.html
```

## Formatting

Code is formatted with [ruff](https://docs.astral.sh/ruff/) (installed with
the dev dependencies):

```bash
uv run ruff format
```

## Tests

`tests/contract/` holds contract tests that send real requests to the OLX and
Otodom endpoints the script depends on, and check that the responses still
contain the fields the parsers read. They need network access and take about
a minute (requests are deliberately paced).

```bash
uv run pytest
```

If a portal blocks a request (HTTP 403), the affected tests are skipped rather
than failed — a block says nothing about the contract.

The same tests run daily on GitHub Actions (`.github/workflows/contract-tests.yml`)
and can be started by hand from the Actions tab (*Run workflow*). The job
summary lists passed, failed and skipped tests; a run where everything was
skipped is marked as failed, so a blocked runner never looks green.

## How it works

- **OLX** is read through the same JSON API (`/api/v1/offers/`) the website
  itself calls in the background; **Otodom** through the `__NEXT_DATA__` blob
  embedded in its result pages. Both are unofficial and can change without
  notice.
- A single query returns a limited number of results, so for larger searches
  the script automatically splits the fetch into price ranges.
- Adding another portal means writing a `Source` subclass and registering it in
  `SOURCES`. The database, reporting, CSV and map layers need no changes.

## Fair use

This is a personal monitoring tool, not a bulk scraper. There is a deliberate
pause between requests (`--delay`, 0.6 s by default) — don't lower it
aggressively. The portals' terms of service restrict automated access, so use
it accordingly.

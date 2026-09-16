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
contain the fields the parsers read. They also download the ZTP Kraków GTFS
timetables the map takes its stops, lines, departures and routes from
(`GTFS_KRK_A.zip`, `GTFS_KRK_M.zip`, `GTFS_KRK_T.zip` from `gtfs.ztp.krakow.pl`)
and check the files and columns the parser reads, plus the assumptions that
would silently break the map rather than the script: `stop_code` in the
`NNN-NN` form with one stop name per number across all archives, times past
midnight written as 24:xx+, a reference weekday with service in every archive,
route shapes for all trips, and a plausible number of stops, lines and night
lines. A server that stops answering `304 Not Modified` is reported as a
skipped test (the map still works, it just re-downloads ~30 MB each time). They need network access and take about
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
- Bus and tram stops on the map come from the public GTFS timetables of ZTP
  Kraków (MPK and Mobilis buses, trams). The archives (~30 MB) are cached in
  `.cache/gtfs/` and downloaded again only when the server has a newer version;
  with `--offline` the cached copy is used, and without one the stop layers are
  left out. Stops have their own *Komunikacja miejska* button on the map: bus
  and tram stops are off by default, and the panel switches between one marker
  per stop and one per stop post, and between showing them only from a chosen
  zoom level (15 by default) or always. Sliders filter stops by a minimum
  number of lines and a minimum number of departures on the reference day. Each marker shows how many lines stop
  there; hovering splits them into day and night lines. Clicking a stop lists
  its lines grouped by the number of departures (100+, 50–99, 20–49, 1–19, not
  running that day), and hovering a line shows the exact count. Departures are
  counted for one reference day: the nearest Tuesday or Wednesday on which every
  timetable has its usual weekday service (so mid-week public holidays are
  skipped). For a whole stop they add up all its posts, i.e. both directions.
  A line counts as a night line when at least half of its trips start between
  23:00 and 4:00. Clicking a line number draws its route (the most common
  variant in each direction, with its stops). Routes are not embedded in the
  map: generating it writes them to `.cache/gtfs/routes.js`, which the page
  loads on the first click. If the map is opened on another computer or the
  cache was cleared, it says the route data is missing — generate the map again.
- Adding another portal means writing a `Source` subclass and registering it in
  `SOURCES`. The database, reporting, CSV and map layers need no changes.

## Fair use

This is a personal monitoring tool, not a bulk scraper. There is a deliberate
pause between requests (`--delay`, 0.6 s by default) — don't lower it
aggressively. The portals' terms of service restrict automated access, so use
it accordingly.

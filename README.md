# monitor-ofert

A small Python tool that tracks real-estate listings from Polish classifieds
portals (**OLX**, **Otodom** and **Gratka**) and tells you what actually changed since the
last time you looked.

The first run downloads every listing matching your search and stores it in a
local SQLite database. Every run after that only reports:

- **new** listings,
- **price changes** (with the full price history kept in the database),
- listings that **disappeared** (sold or withdrawn),
- listings that **came back** after disappearing.

It can also export the active listings to CSV, or render them as a
single-file **interactive map** with filters, where a listing posted on several
portals can be shown once, with links to all of them, and every offer has an
on-demand **report** on the flat and its neighbourhood.

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

The map's other basemap — *Zdjęcia lotnicze*, the GUGiK orthophoto served as
WMTS tiles from <https://mapy.geoportal.gov.pl> — needs no key at all, so it is
also a way around the watermark. Pick it under *Podkład* in the map's
*Podkład i granice* panel. Imagery goes down to zoom 19 (about 10 cm per pixel
over Kraków); zoom 20 just stretches the last level, because the service has no
tiles above 19.

## Usage

```bash
# all sources (OLX + Otodom + Gratka), default search: flats for sale in Kraków
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
    --url "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/malopolskie/krakow/krakow/krakow?priceMax=700000" \
    --url "https://gratka.pl/nieruchomosci/mieszkania/krakow?cena-calkowita:max=700000"

# no network at all — just export what's already in the database
uv run src/monitor_ofert.py --offline --html mapa.html

# generate the map and open it through the local server, so it can save your changes
uv run src/monitor_ofert.py --offline --html mapa.html --serve
```

### Options

| Option | Meaning |
| --- | --- |
| `--url URL` | Search URL from a supported portal; repeatable |
| `--source NAME` | Restrict to one source (`olx`, `otodom`, `gratka`); repeatable |
| `--db FILE` | SQLite database file (default `oferty.db`) |
| `--quick` | Only look for new listings |
| `--export FILE.csv` | Write active listings to CSV |
| `--html FILE.html` | Generate the standalone interactive map |
| `--offline` | Don't query the portals (export only) |
| `--serve` | After generating the map (`--html`), serve it at `http://127.0.0.1:8000/` so the page can save tracked offers and corrected locations |
| `--port N` | Port for `--serve` (default `8000`) |
| `--delay SECONDS` | Pause between requests (default `0.6`) |
| `--category-id` / `--city-id` / `--region-id` | Manual OLX ids, if auto-detection fails |
| `--force` | Allow reusing a database created for a different search |

Note: the script's console output and `--help` text are in Polish.

## Tracked offers and corrected locations

Opened straight from disk, the map is read-only. Started with `--serve`, it is
served by a small local server (Python's standard library, bound to `127.0.0.1`
only) that writes your changes to the database:

- **Tracked offers.** *☆ śledź* on a card, or *Śledź ofertę* in the detail
  panel, marks an offer. Tracked offers get a violet ring on the map, and the
  *tylko śledzone* filter shows only them. The **Śledzone** tab is the report
  on all of them. At the top: counts (active, gone, price cuts and rises,
  changes in the last 7 days). Below, one tile per offer: photo, state (active /
  gone / came back), price with the change since its first recorded price, a
  small step chart of the price over time, the latest event, the status and the
  note. *Szczegóły* (or the photo) opens the offer's detail panel over the tab,
  *Na mapie* jumps to it on the map. Tiles can be narrowed to active or gone
  offers or to one status, and sorted by latest change, biggest price cut,
  price or status. Offers that left the portals stay in the tab — greyed out,
  with the data from the last scan in their detail panel and a link to the old
  listing — until you untrack them with ✕.
  *Historia zmian* is a panel on the right: one chronological feed of price
  changes, disappearances and returns across all tracked offers. *Historia* on
  a tile narrows it to that offer's full history; *zwiń* folds the panel away
  (the page remembers that).
- **Status and note.** A tracked offer has a status (*do kontaktu*, *umówione
  oględziny*, *po oględzinach*, *w negocjacjach*, *rezygnacja*) and a free-text
  note. Both are edited in the offer's detail panel; the status also right on
  its tile in the *Śledzone* tab. They save when the field is left. The status shows on
  the offer's card; untracking keeps both in the database, so they are back if
  you track the offer again.
- **Hidden offers.** *ukryj* on a card, or *Ukryj ofertę* in the detail panel,
  takes a rejected offer off the list and the map (the toast has *Cofnij*).
  *pokaż ukryte* in the filters brings hidden offers back, dimmed, with
  *przywróć ukrytą*. Hiding does not untrack.
- **Corrected location.** *Popraw lokalizację na mapie* in the detail panel
  (or *Wskaż lokalizację na mapie* for an offer that has none), then a click on
  the map, moves the pin. The corrected position counts as exact and is what
  the flat report and the comparison use. *Przywróć z ogłoszenia* brings the
  portal's location back; `Esc` cancels picking.

All of them apply to every copy of a listing posted on several portals. They are kept
in a separate `user_edits` table, so later scans never overwrite them, and are
read fresh from the database on every page load. A map generated afterwards
shows them even when opened from disk — it just cannot change them. Duplicate
matching still uses the portals' own coordinates. An offer that disappears from
the portals leaves the map like any other, but a tracked one stays in the
*Śledzone* tab. Each portal's copy of a listing is a separate row there, since
prices change per portal.

Disappearances and returns are logged in the `offer_events` table from now on,
for every offer. For scans made before that table existed, a disappearance is
dated to the next scan of the portal after the offer was last seen, and earlier
returns are not known.

## Duplicates across portals

The same listing is often posted on two or three portals, where it gets a
different id each time, so the database holds it as separate rows. When the map
is generated, `src/dedup.py` groups those rows, and a switch above the list
chooses between **Bez duplikatów** (one entry per listing — the default) and
**Wszystkie oferty** (every row, as before). In both views the offer's detail
panel links to every portal the listing is on, with each portal's price when
they differ. A collapsed group is shown through its cheapest copy, and it stays
in the results as long as any of its copies passes the filters. The database
itself is not touched, and the *Historia rynku* tab still counts raw listings.

The matching is deliberately conservative — a missed duplicate costs one extra
card, a wrong merge hides a flat:

- A pair always needs the same area (to 0.01 m²) and room count, plus one of:
  the link OLX keeps to the Otodom offer it was imported from; the same price
  and a matching description; the same price and floor confirmed by location,
  street or seller (stricter for round prices); or a near-identical description
  with a price up to 10% apart or the floor off by one — those two only for
  second-hand flats in buildings at least three years old. A matching
  description is never enough on its own, because every unit of a development
  shares one.
- The pair must be unambiguous: clearly better than any other candidate of both
  offers. Identical units of one development (same area, price and floor) stay
  separate unless coordinates, title, date or text tell them apart.
- A group holds at most one offer per portal, so two listings from the same
  portal are never merged — not even through a third one.

On the November 2026 database (31 088 active listings) this finds 7 024 groups
and leaves 23 337 unique listings, in about a second. Run without the OLX links,
the same rules rediscover 95% of those 2 116 known pairs, with one pairing that
differs from the link.

## Flat report

Every offer's detail panel has a **Raport o mieszkaniu i okolicy** button. It
opens a full-screen report written for the person who would live there. The
report is built in the browser, only when the button is pressed and only for
that one offer, from data the page already carries; the one thing it fetches is
the noise level at the flat's pin.

It is laid out to be read fast. The top card holds the overall 0–10 score, a
bar per scored section, six "at a glance" tiles (price against similar offers,
travel time to your first destination, nearest stop, supermarket and park, the
noise level) and the strongest points and warnings. Below it every section is
folded to one line — its conclusion and its score — and opens on a click, so
the whole report fits one screen until you ask for detail. A map stays next to
the text the whole time: it shows how far you get on foot in the chosen time,
and hovering a row draws the walking route to that place or the whole transit
trip.

| Section | What it answers |
| --- | --- |
| *Cena na tle rynku* | Price per m² against comparable active offers (same room count, area ±20%, within 1.5 km), the district median, time on the market, price history, and portals that disagree on the price. |
| *Koszty zakupu i miesięczne* | A calculator: down payment, interest rate, loan period, PCC, other transaction costs and the monthly service charge (read from the description when the seller gives it). |
| *Dojazdy do wybranych miejsc* | Door-to-door time to each chosen destination by public transport, with up to two transfers, next to walking and cycling time. |
| *Przystanki i linie* | Nearest bus, tram and rail stops, the lines and daily departures within walking range. |
| *Zakupy i codzienne sprawy*, *Zdrowie*, *Dzieci i edukacja*, *Zieleń, sport i rekreacja*, *Gastronomia i życie towarzyskie* | The nearest place of each kind (supermarket, pharmacy, clinic, nursery, school, park…), the walk to it, and how many are in range; a row opens to the five nearest. |
| *Wybrane obiekty z mapy* | The nearest object of each kind the main map has a layer for — Lidl, Kaufland, Auchan, Good Lood, shopping centre, cinema, theatre, marketplace, zoo — on foot and, when far, by public transport. |
| *Hałas i uciążliwości* | Road, rail and industrial noise at the pin, day and night, from the city's 2022 noise map, plus nightclubs, bars, tracks or the airport close by. |
| *Budynek i mieszkanie* | What the build year usually means, and which features the description mentions, mentions as missing, or does not mention at all. |
| *Podobne oferty w pobliżu* | The cheapest comparable offers; clicking one opens its report. |
| *Przed oględzinami* | A checklist of questions chosen for this particular listing. |

**Dostosuj** opens the settings:

- a profile that sets how much each section weighs in the overall score
  (universal, family with children, single/couple/student, senior);
- the walking range (5–20 minutes);
- the destinations travel is computed for — Bonarka, the main station and
  Galeria Krakowska to start with. Type a few letters to add any named object
  from the map's layers (a shopping centre, a cinema, a theatre, the airport…),
  or add your own place: name it, press *wskaż na mapie* and click the spot;
- which kinds of map objects the report looks up the nearest of;
- which sections to show.

Settings are kept in the browser's local storage. **Drukuj / PDF** prints the
report alone, as it is on screen (opened sections stay open), with the map at
the end. The defaults live in `src/map/html_map.py` (`REPORT_DESTINATIONS`,
`REPORT_KINDS_OFF`).

What the numbers mean:

- Walking times follow the real network of streets, pavements and paths
  (shortest route from the listing's pin, at 4.8 km/h), so a river, a railway
  or a fenced estate counts. Where the network is missing, or the place is more
  than 5 km away, the time is estimated from the straight-line distance plus
  30% and marked with "≈". When the seller gave only an approximate location,
  the report says so at the top — everything in it is then a rough guide.
- Travel by public transport is door to door: the walk to the stop, the average
  wait (half the interval between departures, at most 12 minutes), the ride time
  from the timetable, 2 minutes of margin per transfer, and the walk to the
  destination. It uses the most common route variant of each line and leaves
  night lines out, so it is a typical daytime trip, not a timetable lookup.
- A category scores 10 when the walk to its nearest place is within its "at
  hand" distance (500 m for a supermarket, 300 m for a playground, …; see
  `CATEGORIES` in `src/osm.py`) and falls linearly to 0 at three times that. A
  section's score is the weighted average of its categories.
- Prices are asking prices from this map, after duplicates are collapsed.
- A flat outside the area the neighbourhood data was fetched for gets no
  neighbourhood sections rather than misleading zeros.

The report is a way to compare offers, not a valuation or a legal or technical
opinion on the flat.

## Comparing offers

Two to four offers can be put side by side, with the same figures the flat
report computes.

- **Any offers, from the map.** Every card in the list and every offer's detail
  panel has a *+ porównaj* button. The comparison button on the map (two
  columns, with a counter) lists what has been chosen, lets you remove offers,
  and has a switch — *wybieraj klikaniem w oferty na mapie* — that makes a click
  on an offer add it instead of opening it. Chosen offers get an orange ring on
  the map; the list survives a reload. *Porównaj* opens the comparison.
- **Similar offers, from a report.** *Porównaj z podobnymi* in the report's
  header compares the flat with comparable ones nearby. Which ones is set in the
  report's *Podobne oferty w pobliżu* section: the three cheapest per m² are
  ticked to start with, and up to three can be chosen.

The comparison is one table: a column per offer (photo, title, links to the
report and the listing), rows grouped into scores, price and flat, travel to
your destinations, walks to the nearest stop, shop, clinic, school and park,
the map objects you chose, noise, features from the description, and the top
strengths and warnings. In every row where "better" has a meaning the best
value has a green background, and each column says in how many rows it wins. A
small map above the table shows where the offers are. Travel times and noise
levels arrive a moment after the rest. Profile, walking range, destinations and
the other settings are the report's — change them in its *Dostosuj* panel.
*Raport* in a column opens that offer's report and closing it returns to the
comparison; *Drukuj / PDF* prints the table.

## Included data

This repo ships a real snapshot so you can look at the output without
scraping anything yourself. Both files are gzipped to stay under GitHub's file
size limits — unpack them first:

```bash
gunzip -k oferty.db.gz     # -> oferty.db  (391 MB)
gunzip -k data.html.gz     # -> data.html  (141 MB)
```

- **`oferty.db.gz`** — SQLite database, snapshot from 2026-09-18: 35 145
  listings (17 454 Otodom, 14 302 Gratka, 3 389 OLX), 29 967 of them still
  active, plus 35 090 price-history rows. Tables: `offers`, `price_history`,
  `meta`.
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

`tests/contract/` holds contract tests that send real requests to the OLX,
Otodom and Gratka endpoints the script depends on, and check that the responses still
contain the fields the parsers read. They also download the ZTP Kraków GTFS
timetables the map takes its stops, lines, departures and routes from
(`GTFS_KRK_A.zip`, `GTFS_KRK_M.zip`, `GTFS_KRK_T.zip` from `gtfs.ztp.krakow.pl`)
and check the files and columns the parser reads, plus the assumptions that
would silently break the map rather than the script: `stop_code` in the
`NNN-NN` form with one stop name per number across all archives, times past
midnight written as 24:xx+, a reference weekday with service in every archive,
route shapes for all trips, and a plausible number of stops, lines and night
lines. The nationwide rail feed gets the same treatment (platforms attached to
their station, train vs replacement-bus routes, lines shared between operators,
a calendar covering the coming weekdays). `test_osm.py` runs the report's
OpenStreetMap queries (places, green areas, the walking network) for a slice of the city (skipped, not failed, when the
Overpass servers are busy) and asks the city noise map for one point the way
the page does. The portal tests also cover the
fields only the duplicate matching reads: the Otodom link OLX keeps, the id at
the end of an Otodom address, and seller, street and development names. A server that stops answering `304 Not Modified` is reported as a
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
  embedded in its result pages; **Gratka** through the GraphQL API
  (`gratka.pl/api-gratka`) its own pages query. All three are unofficial and
  can change without notice.
- On OLX and Otodom a single query returns a limited number of results, so for
  larger searches the script automatically splits the fetch into price ranges.
  Gratka serves results down to the last page, so its listings are read page by
  page instead.
- **Gratka** keeps the market type (primary/secondary), the build year, the
  description and the photos on the offer page only, so the script fetches those
  once per listing and reuses them on later scans.
- **Build year** comes from Otodom and Gratka only — OLX does not collect it at
  all (its listings carry just a building type: block, tenement, apartment
  building). Coverage on the two portals that do have it is around 94–95%; for
  developments still under construction the year is the planned completion date,
  which is why values a few years into the future are normal. The map's *Rok
  budowy* filter is a two-handle slider: left handle at the far end means "no
  lower bound", so the oldest tenements stay in the results until the bound is
  deliberately raised, and narrowing the range drops every listing without a
  year (all of OLX among them). Listings whose Gratka details were saved before
  this field existed are re-fetched once, on the first full scan after the
  upgrade — that run is noticeably longer and says so.
- Bus and tram stops on the map come from the public GTFS timetables of ZTP
  Kraków (MPK and Mobilis buses, trams). The archives (~30 MB) are cached in
  `.cache/gtfs/` and downloaded again only when the server has a newer version;
  with `--offline` the cached copy is used, and without one the stop layers are
  left out. Stops have their own *Komunikacja* button on the map: bus
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
- Railway stations and stops are a third layer of the same *Komunikacja* panel
  (off by default, like the others). They come from *Polish Trains*, the
  nationwide GTFS feed Mikołaj Kuranowski builds from PKP PLK data
  (<https://mkuran.pl/gtfs/>): one archive (~27 MB, refreshed daily, cached like
  the ZTP ones) covering every operator, Koleje Małopolskie, Polregio and PKP
  Intercity included. Only stations inside the rectangle around the listings
  (plus about 10 km) are read. A station is one marker — platforms are not
  shown separately, so the stop/post switch does not change it. Its "lines" are
  what the timetable calls them: line numbers (SKA1, K63) and, for long-distance
  trains, the train category (IC, EIP, TLK). Replacement buses are left out.
  Clicking a numbered line draws its whole route; a category has no single
  route, and the map says so. Departures use the same reference day as the city
  stops. With the layer on, the map's corner credits the feed, as its licence
  asks.
- The same panel has a *Lotnisko* checkbox (on by default): one marker at the
  passenger terminal of Kraków Airport. Its position is hardcoded in
  `TRANSIT_POINTS` (`src/map/html_map.py`), so it needs no data source and is
  not affected by the stop filters or the zoom threshold.
- The flat report's neighbourhood data comes from OpenStreetMap through the
  Overpass API (`src/osm.py`): about 10 000 points in 29 categories and some
  700 parks and forests with their outlines, for the rectangle around the
  listings plus roughly 2.5 km. It is cached in `.cache/osm/` and refreshed at
  most once every 30 days. The public Overpass servers are often busy, so the
  download retries for up to five minutes per query and says what it is waiting
  for; if it still fails, the previous copy is used, and without one the map is
  generated normally and the report leaves those sections out. With `--offline`
  only the cached copy is read. The data adds about 2 MB to the HTML file.
- Walking routes use the network of streets, pavements and paths from the same
  source: about 260 000 nodes for Kraków, another 7 MB in the HTML file. It is
  downloaded in twelve tiles, because the whole city at once is over 100 MB of
  response — too much for one request to a busy server. Each tile has its own
  copy in `.cache/osm/siec/` (refreshed every 90 days), so an interrupted
  download continues on the next run instead of starting over; one run spends
  at most 15 minutes on it. The report works with whatever tiles are there and
  falls back to straight-line estimates elsewhere. The shortest routes are
  computed in the browser when a report is opened (about 0.1 s).
- Ride times in the report come from the timetables: each route variant stored
  in `.cache/gtfs/routes.js` now carries, for every stop, the minutes from the
  start of the route.
- Noise levels are not embedded. When a report is opened, the page asks the MSIP
  Kraków noise-map server which noise zone the flat's pin falls in (six small
  requests, as JSONP so they work from a local file). Without a network
  connection that part of the report says it could not be loaded; the rest
  works.
- The 18 Kraków district boundaries come from the city's open data portal and
  live in `src/map/template/layers/districts.geojson`. They get their own
  *Granice dzielnic* section in the map's *Podkład i granice* panel (next to the
  basemap choice and the city outline) and are **all off by default** — tick a
  district to outline it and label it on the map, or tick the
  section header to show all of them at once. Every section of that panel folds
  away with the arrow next to its header, and a section with all its layers off
  (the districts one, right after opening the map) starts folded. The file in the repo is already
  reprojected from the source PUWG 2000 zone 7 grid (EPSG:2178) to WGS84 and
  simplified to about 6 m, so generating the map needs no projection library.
- The third basemap, *Hałas*, is the city's 2022 strategic noise map: road,
  tram and rail, and industrial noise (L<sub>DWN</sub>, the day–evening–night
  indicator) drawn over a plain light CARTO map, with street names on top and a
  dB legend in the corner. Where sources overlap the image shows road noise on
  top rather than a combined level. Nothing is embedded: the page asks the MSIP
  Kraków ArcGIS server (`msip.um.krakow.pl`) for images in the map's projection,
  which needs no key and works from a local file. The server renders every image
  on demand, so after panning or zooming the noise takes a moment to catch up.
- Adding another portal means writing a `Source` subclass and registering it in
  `SOURCES`. The database, reporting, CSV and map layers need no changes.

## Fair use

This is a personal monitoring tool, not a bulk scraper. There is a deliberate
pause between requests (`--delay`, 0.6 s by default) — don't lower it
aggressively. The portals' terms of service restrict automated access, so use
it accordingly.

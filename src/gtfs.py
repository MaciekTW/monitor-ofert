# -*- coding: utf-8 -*-
"""Przystanki komunikacji miejskiej w Krakowie z rozkładów GTFS ZTP
oraz stacje i przystanki kolejowe z ogólnopolskiego rozkładu „Polish Trains”.

Archiwa (autobusy MPK, autobusy Mobilis, tramwaje) są pobierane do lokalnej
kopii w .cache/gtfs/ i odświeżane tylko wtedy, gdy serwer ma nowszą wersję
(If-Modified-Since). Przystanek to grupa słupków o wspólnym prefiksie stop_code
(„920-01”, „920-02” → „920”); ten numer jest zgodny między archiwami,
w przeciwieństwie do stop_id.

Liczba odjazdów dotyczy jednego dnia odniesienia — najbliższego wtorku lub
środy z typowym rozkładem dnia roboczego (reference_day).

Przebiegi tras linii (główny wariant w każdym kierunku) są zapisywane osobno do
.cache/gtfs/routes.js, który mapa wczytuje dopiero po kliknięciu linii (write_routes).

Kolej (read_rail) pochodzi z jednego archiwum na całą Polskę, które Mikołaj
Kuranowski buduje z danych PKP PLK (https://mkuran.pl/gtfs/): wszyscy przewoźnicy
naraz, w tym Koleje Małopolskie, Polregio i PKP Intercity. Z archiwum brane są
tylko stacje w podanym prostokącie (okolica ofert), a „linią” jest oznaczenie
z rozkładu — numer linii (SKA1, K63) albo kategoria pociągu (IC, EIP, TLK)."""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from collections import Counter
from datetime import date, timedelta
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GTFS_URL = "https://gtfs.ztp.krakow.pl/"
CACHE_DIR = Path(__file__).parents[1] / ".cache" / "gtfs"
ROUTES_FILE = CACHE_DIR / "routes.js"

# Kolej: jedno archiwum na całą Polskę, odświeżane codziennie (~27 MB). Licencja
# wymaga podania źródeł, stąd RAIL_ATTRIBUTION na mapie przy włączonych stacjach.
RAIL_URL = "https://mkuran.pl/gtfs/"
RAIL_FEED = "polish_trains.zip"
RAIL_ATTRIBUTION = (
    'kolej: <a href="https://mkuran.pl/gtfs/">Mikołaj Kuranowski</a>, dane '
    '<a href="https://www.plk-sa.pl/klienci-i-kontrahenci/api-otwarte-dane">PKP PLK</a>'
)
RAIL_ROUTE_TYPE = "2"  # w routes.txt: pociąg; 3 to zastępcza komunikacja autobusowa
# linia o stałej trasie ma ją w nazwie („Kraków Lotnisko — Kraków Główny — Wieliczka”),
# kategoria pociągu (IC, REG) nie — i dla niej jedna trasa na mapie nic by nie znaczyła
RAIL_FIXED_ROUTE = " — "

# archiwum → rodzaj przystanku
FEEDS = {
    "GTFS_KRK_A.zip": "bus",  # autobusy MPK
    "GTFS_KRK_M.zip": "bus",  # autobusy Mobilis
    "GTFS_KRK_T.zip": "tram",  # tramwaje
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def download(name: str, offline: bool = False, base: str = GTFS_URL) -> Path | None:
    """Zwraca ścieżkę do aktualnej kopii archiwum; pobiera je (spod adresu base)
    tylko, gdy na serwerze jest nowsza wersja. Bez sieci używa starej kopii, jeśli jest."""
    path = CACHE_DIR / name
    if offline:
        return path if path.exists() else None
    headers = {"User-Agent": "monitor-ofert"}
    if path.exists():
        headers["If-Modified-Since"] = formatdate(path.stat().st_mtime, usegmt=True)
    try:
        with urlopen(Request(base + name, headers=headers), timeout=120) as resp:
            data = resp.read()
            modified = resp.headers.get("Last-Modified")
    except HTTPError as exc:
        if exc.code == 304:
            return path
        log(f"⚠ {name}: HTTP {exc.code}" + (" — używam poprzedniej kopii" if path.exists() else ""))
        return path if path.exists() else None
    except (URLError, OSError) as exc:
        log(f"⚠ {name}: {exc}" + (" — używam poprzedniej kopii" if path.exists() else ""))
        return path if path.exists() else None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    if modified:
        # data serwera jako mtime — przy następnym pobraniu idzie w If-Modified-Since
        ts = parsedate_to_datetime(modified).timestamp()
        os.utime(path, (ts, ts))
    return path


# linia jest nocna, gdy co najmniej połowa jej kursów rusza między 23:00 a 4:00
# (w danych nocne mają ≥85% takich kursów, dzienne ≤12%)
NIGHT_HOURS = (23, 4)
NIGHT_SHARE = 0.5

# dzień odniesienia dla liczby odjazdów: wtorek albo środa (pon.–śr. mają zwykle
# wspólny rozkład, czwartek i piątek bywają inne) z najbliższych czterech tygodni
REFERENCE_WEEKDAYS = (1, 2)
REFERENCE_HORIZON_DAYS = 28
# archiwum przed zmianą rozkładu zawiera też poprzednią wersję (inne service_id,
# przemianowane przystanki z nowym stop_id) — liczą się tylko kursy jeżdżące
# w tygodniu od dnia odniesienia, żeby linie weekendowe zostały na mapie
RUNNING_DAYS = 7
WEEKDAY_COLUMNS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def table(z: zipfile.ZipFile, name: str):
    """Wiersze pliku z archiwum (csv.reader — przy ~2 mln wierszy kilka razy
    szybszy od DictReader) i indeksy kolumn po nazwie; brak pliku = brak wierszy."""
    if name not in z.namelist():
        return iter(()), {}
    rows = csv.reader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))
    return rows, {col: i for i, col in enumerate(next(rows, []))}


def ymd(text: str) -> date:
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def read_calendar(archive: Path):
    """Funkcja dzień → zbiór service_id kursujących tego dnia, według
    calendar.txt (dni tygodnia w zakresie dat) i wyjątków z calendar_dates.txt."""
    with zipfile.ZipFile(archive) as z:
        rows, c = table(z, "calendar.txt")
        regular = [
            (
                row[c["service_id"]],
                [row[c[d]] == "1" for d in WEEKDAY_COLUMNS],
                ymd(row[c["start_date"]]),
                ymd(row[c["end_date"]]),
            )
            for row in rows
        ]
        added: dict[date, set] = {}
        removed: dict[date, set] = {}
        rows, c = table(z, "calendar_dates.txt")
        for row in rows:
            target = added if row[c["exception_type"]] == "1" else removed
            target.setdefault(ymd(row[c["date"]]), set()).add(row[c["service_id"]])

    def active(day: date) -> frozenset:
        services = {sid for sid, days, start, end in regular if days[day.weekday()] and start <= day <= end}
        return frozenset((services | added.get(day, set())) - removed.get(day, set()))

    return active


def reference_day(calendars: list, today: date) -> date:
    """Najbliższy wtorek lub środa, w które każde archiwum ma swój najczęstszy
    zestaw kursów na te dni tygodnia — pomija święta w środku tygodnia
    (wtedy obowiązuje rozkład świąteczny, więc zestaw jest inny)."""
    days = [
        today + timedelta(n)
        for n in range(REFERENCE_HORIZON_DAYS)
        if (today + timedelta(n)).weekday() in REFERENCE_WEEKDAYS
    ]
    typical = [Counter(active(day) for day in days).most_common(1)[0][0] for active in calendars]
    for day in days:
        if all(active(day) == common and common for active, common in zip(calendars, typical)):
            return day
    return days[0]


def read_feed(archive: Path, active, day: date) -> tuple[dict[str, dict], set[str], dict[str, list]]:
    """Jedno archiwum → (słupki, linie nocne, trasy linii); active to kalendarz
    archiwum z read_calendar, day to dzień odniesienia.

    Brane są tylko kursy jeżdżące w ciągu RUNNING_DAYS od dnia odniesienia —
    pomija to poprzednią wersję rozkładu, którą archiwum zawiera przed zmianą.

    Słupki to te, z których faktycznie odjeżdża jakiś kurs (pomija np. zajezdnie):
    {stop_code: {"name", "lat", "lon", "lines": {linia: liczba odjazdów}}}. Linie
    zatrzymujące się na słupku są ze wszystkich tych kursów, a odjazdy tylko
    z kursów w dniu odniesienia i tylko tam, gdzie można wsiąść — linia, która
    tego dnia nie kursuje, ma 0 odjazdów. Nazwa i położenie słupka są z tego
    stop_id, który ma najwięcej odjazdów (przemianowany słupek ma nowe stop_id).

    Trasy: {linia: [{"dir", "to", "shape": [[lat, lon], …], "stops": [[lat, lon, nazwa, stop_code], …]}]}
    — w każdym kierunku wariant z najczęstszym przebiegiem (shape_id), z przystankami
    jednego jego kursu."""
    services = active(day)
    running = frozenset().union(*(active(day + timedelta(n)) for n in range(RUNNING_DAYS)))
    with zipfile.ZipFile(archive) as z:
        rows, c = table(z, "routes.txt")
        route_line = {row[c["route_id"]]: row[c["route_short_name"]] for row in rows}
        rows, c = table(z, "trips.txt")
        trip_line, counted = {}, set()
        variants: Counter = Counter()  # (linia, kierunek, shape_id) → liczba kursów
        variant_trip: dict[tuple, tuple] = {}  # (linia, kierunek, shape_id) → (trip_id, cel)
        for row in rows:
            if row[c["service_id"]] not in running:
                continue
            trip, line = row[c["trip_id"]], route_line[row[c["route_id"]]]
            trip_line[trip] = line
            if row[c["service_id"]] in services:
                counted.add(trip)
            key = (
                line,
                row[c["direction_id"]] if "direction_id" in c else "",
                row[c["shape_id"]] if "shape_id" in c else "",
            )
            variants[key] += 1
            variant_trip.setdefault(key, (trip, row[c["trip_headsign"]] if "trip_headsign" in c else ""))
        main_variant: dict[tuple, tuple] = {}  # (linia, kierunek) → (shape_id, trip_id, cel)
        for (line, direction, shape), _ in variants.most_common():
            main_variant.setdefault((line, direction), (shape, *variant_trip[(line, direction, shape)]))
        route_trips = {trip: key for key, (_, trip, _) in main_variant.items()}
        trip_stops: dict[str, list] = {trip: [] for trip in route_trips}

        # godziny „HH:MM:SS” (także 24:xx i dalej) porównują się poprawnie jako tekst
        rows, c = table(z, "stop_times.txt")
        trip_i, stop_i, dep_i, pickup_i, seq_i = (
            c["trip_id"],
            c["stop_id"],
            c["departure_time"],
            c.get("pickup_type"),
            c["stop_sequence"],
        )
        stop_lines: dict[str, Counter] = {}
        trip_start: dict[str, str] = {}
        for row in rows:
            trip, dep = row[trip_i], row[dep_i]
            if trip not in trip_line:
                continue
            lines = stop_lines.setdefault(row[stop_i], Counter())
            # pickup_type 1 = nie można wsiąść (np. przystanek końcowy) — to nie odjazd
            boarding = trip in counted and (pickup_i is None or row[pickup_i] != "1")
            lines[trip_line[trip]] += boarding
            if dep and (trip not in trip_start or dep < trip_start[trip]):
                trip_start[trip] = dep
            if trip in trip_stops:
                trip_stops[trip].append((int(row[seq_i]), row[stop_i]))

        rows, c = table(z, "stops.txt")
        posts: dict[str, dict] = {}
        post_departures: dict[str, int] = {}  # stop_code → odjazdy stop_id, z którego jest nazwa
        stop_info: dict[str, list] = {}
        for row in rows:
            stop_info[row[c["stop_id"]]] = [
                float(row[c["stop_lat"]]),
                float(row[c["stop_lon"]]),
                row[c["stop_name"]],
                row[c["stop_code"]],
            ]
            lines = stop_lines.get(row[c["stop_id"]])
            if not lines:
                continue
            code, departures = row[c["stop_code"]], sum(lines.values())
            post = posts.setdefault(code, {"lines": Counter()})
            post["lines"].update(lines)
            if departures > post_departures.get(code, -1):
                post_departures[code] = departures
                post.update(
                    name=row[c["stop_name"]],
                    lat=float(row[c["stop_lat"]]),
                    lon=float(row[c["stop_lon"]]),
                )

        wanted_shapes = {shape for shape, _, _ in main_variant.values() if shape}
        shape_points: dict[str, list] = {}
        rows, c = table(z, "shapes.txt")
        for row in rows:
            if row[c["shape_id"]] in wanted_shapes:
                shape_points.setdefault(row[c["shape_id"]], []).append(
                    (
                        int(row[c["shape_pt_sequence"]]),
                        float(row[c["shape_pt_lat"]]),
                        float(row[c["shape_pt_lon"]]),
                    )
                )

    routes: dict[str, list] = {}
    for (line, direction), (shape, trip, headsign) in sorted(main_variant.items()):
        stops = [stop_info[stop] for _, stop in sorted(trip_stops[trip]) if stop in stop_info]
        points = [[lat, lon] for _, lat, lon in sorted(shape_points.get(shape, []))]
        routes.setdefault(line, []).append(
            {
                "dir": direction,
                "to": headsign,
                # bez przebiegu w shapes.txt — linia prosto od przystanku do przystanku
                "shape": points or [stop[:2] for stop in stops],
                "stops": stops,
            }
        )

    starts: dict[str, list[bool]] = {}
    for trip, dep in trip_start.items():
        hour = int(dep.split(":")[0]) % 24
        starts.setdefault(trip_line[trip], []).append(hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1])
    night = {line for line, flags in starts.items() if sum(flags) >= NIGHT_SHARE * len(flags)}
    return posts, night, routes


def read_rail(archive: Path, active, day: date, bounds: tuple[float, float, float, float]) -> dict:
    """Ogólnopolskie archiwum kolejowe → stacje i przystanki w prostokącie
    bounds = (lat min, lon min, lat max, lon max), w tej samej postaci co sieć
    z load(): {"posts": {id stacji: {"name", "lat", "lon", "lines"}}, "night": set(),
    "routes": {linia: trasy}}.

    Stacja to parent_station z archiwum — perony i tory (osobne stop_id o tych
    samych współrzędnych) zlewają się w jeden punkt. Linie łączą przewoźników
    (SKA2 jeżdżą Koleje Małopolskie i Polregio), a autobusy zastępcze są pomijane.
    Odjazdy liczone jak w read_feed: kursy z dnia odniesienia, na których można
    wsiąść; linia kursująca tylko w inne dni tygodnia ma 0.

    Trasy są tylko dla linii o stałej trasie (RAIL_FIXED_ROUTE) i w całości —
    także poza prostokątem — żeby było widać, dokąd pociąg jedzie."""
    lat_min, lon_min, lat_max, lon_max = bounds
    services = active(day)
    running = frozenset().union(*(active(day + timedelta(n)) for n in range(RUNNING_DAYS)))
    with zipfile.ZipFile(archive) as z:
        rows, c = table(z, "stops.txt")
        station_of: dict[str, str] = {}  # stop_id peronu → id stacji
        stations: dict[str, list] = {}  # id stacji → [lat, lon, nazwa, id]
        for row in rows:
            stop, parent = row[c["stop_id"]], row[c["parent_station"]]
            station_of[stop] = parent or stop
            if not parent:
                stations[stop] = [
                    float(row[c["stop_lat"]]),
                    float(row[c["stop_lon"]]),
                    row[c["stop_name"]],
                    stop,
                ]
        local = {
            sid
            for sid, (lat, lon, _, _) in stations.items()
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
        }

        rows, c = table(z, "routes.txt")
        route_line, fixed = {}, set()
        for row in rows:
            if row[c["route_type"]] != RAIL_ROUTE_TYPE:
                continue
            route_line[row[c["route_id"]]] = row[c["route_short_name"]]
            if RAIL_FIXED_ROUTE in row[c["route_long_name"]]:
                fixed.add(row[c["route_short_name"]])

        rows, c = table(z, "trips.txt")
        trip_line, counted = {}, set()
        trip_variant: dict[str, tuple] = {}  # trip_id → (linia, kierunek, shape_id, cel)
        for row in rows:
            line = route_line.get(row[c["route_id"]])
            if line is None or row[c["service_id"]] not in running:
                continue
            trip = row[c["trip_id"]]
            trip_line[trip] = line
            if row[c["service_id"]] in services:
                counted.add(trip)
            if line in fixed:
                trip_variant[trip] = (
                    line,
                    row[c["direction_id"]],
                    row[c["shape_id"]],
                    row[c["trip_headsign"]],
                )

        # pierwsze przejście: odjazdy ze stacji w prostokącie i kursy, które tam stają
        rows, c = table(z, "stop_times.txt")
        trip_i, stop_i, pickup_i = c["trip_id"], c["stop_id"], c["pickup_type"]
        station_lines: dict[str, Counter] = {}
        calling = set()
        for row in rows:
            trip = row[trip_i]
            if trip not in trip_line:
                continue
            station = station_of.get(row[stop_i])
            if station not in local:
                continue
            calling.add(trip)
            station_lines.setdefault(station, Counter())[trip_line[trip]] += (
                trip in counted and row[pickup_i] != "1"
            )

        # główny wariant linii w każdym kierunku: najczęstszy przebieg wśród kursów stąd
        variants: Counter = Counter()
        variant_trip: dict[tuple, tuple] = {}
        for trip in calling:
            if trip in trip_variant:
                line, direction, shape, headsign = trip_variant[trip]
                variants[line, direction, shape] += 1
                variant_trip.setdefault((line, direction, shape), (trip, headsign))
        main_variant: dict[tuple, tuple] = {}  # (linia, kierunek) → (shape_id, trip_id, cel)
        for (line, direction, shape), _ in sorted(variants.items(), key=lambda kv: (-kv[1], kv[0])):
            main_variant.setdefault((line, direction), (shape, *variant_trip[line, direction, shape]))

        # drugie przejście: stacje na trasie kursów-wzorców (tylko tam, gdzie pociąg staje)
        trip_stops: dict[str, list] = {trip: [] for _, trip, _ in main_variant.values()}
        if trip_stops:
            rows, c = table(z, "stop_times.txt")
            seq_i, drop_i = c["stop_sequence"], c.get("drop_off_type")
            for row in rows:
                stops = trip_stops.get(row[trip_i])
                if stops is None:
                    continue
                passing = row[pickup_i] == "1" and drop_i is not None and row[drop_i] == "1"
                if not passing:
                    stops.append((int(row[seq_i]), station_of.get(row[stop_i])))

        wanted_shapes = {shape for shape, _, _ in main_variant.values() if shape}
        shape_points: dict[str, list] = {}
        if wanted_shapes:
            rows, c = table(z, "shapes.txt")
            id_i, seq_i, lat_i, lon_i = (
                c["shape_id"],
                c["shape_pt_sequence"],
                c["shape_pt_lat"],
                c["shape_pt_lon"],
            )
            for row in rows:
                if row[id_i] in wanted_shapes:
                    shape_points.setdefault(row[id_i], []).append(
                        (int(row[seq_i]), float(row[lat_i]), float(row[lon_i]))
                    )

    routes: dict[str, list] = {}
    for (line, direction), (shape, trip, headsign) in sorted(main_variant.items()):
        stops = [stations[sid] for _, sid in sorted(trip_stops[trip]) if sid in stations]
        points = [[lat, lon] for _, lat, lon in sorted(shape_points.get(shape, []))]
        routes.setdefault(line, []).append(
            {
                "dir": direction,
                "to": headsign,
                "shape": points or [stop[:2] for stop in stops],
                "stops": stops,
            }
        )
    posts = {
        sid: {"name": stations[sid][2], "lat": stations[sid][0], "lon": stations[sid][1], "lines": lines}
        for sid, lines in station_lines.items()
    }
    return {"posts": posts, "night": set(), "routes": routes}


def load(
    offline: bool = False,
    today: date | None = None,
    rail_bounds: tuple[float, float, float, float] | None = None,
) -> tuple[dict[str, dict], date | None]:
    """({"bus": {"posts": {stop_code: słupek}, "night": {linie nocne}, "routes": {linia: trasy}}, "tram": {...},
    "rail": {...}}, dzień odniesienia). Słupek wspólny dla autobusów MPK i Mobilis
    ma zsumowane linie i odjazdy obu przewoźników. Brak archiwum (np. bez sieci) =
    brak jego słupków i linii. Kolej (read_rail) jest dokładana tylko wtedy, gdy
    podano rail_bounds — prostokąt, z którego mają być stacje."""
    rail = download(RAIL_FEED, offline, RAIL_URL) if rail_bounds else None
    if rail_bounds and rail is None:
        log(f"⚠ Brak rozkładu kolejowego ({RAIL_FEED}) — mapa będzie bez stacji kolejowych.")
    rail_calendar = read_calendar(rail) if rail else None
    archives = {}
    for name in FEEDS:
        archive = download(name, offline)
        if archive is not None:
            archives[name] = archive
    missing = [name for name in FEEDS if name not in archives]
    if missing:
        log(f"⚠ Brak rozkładów GTFS ({', '.join(missing)}) — mapa będzie bez części przystanków.")
    if not archives and not rail:
        return {}, None

    calendars = {name: read_calendar(archive) for name, archive in archives.items()}
    # dzień odniesienia wyznaczają rozkłady miejskie; kolej (inny wydawca, inny rytm
    # aktualizacji) dostosowuje się do niego i decyduje tylko wtedy, gdy jest sama
    day = reference_day(list(calendars.values()) or [rail_calendar], today or date.today())
    result: dict[str, dict] = {}
    if rail:
        network = read_rail(rail, rail_calendar, day, rail_bounds)
        if network["posts"]:
            result["rail"] = network
    for name, archive in archives.items():
        posts, night, routes = read_feed(archive, calendars[name], day)
        network = result.setdefault(FEEDS[name], {"posts": {}, "night": set(), "routes": {}})
        network["night"] |= night
        network["routes"].update(routes)
        for code, post in posts.items():
            if code in network["posts"]:
                network["posts"][code]["lines"].update(post["lines"])
            else:
                network["posts"][code] = post
    return result, day


def write_routes(networks: dict[str, dict]) -> Path:
    """Zapisuje trasy linii jako skrypt ustawiający window.TRANSIT_ROUTES =
    {"bus": {linia: trasy}, "tram": {...}, "rail": {...}}. Skrypt, a nie JSON, bo mapa otwierana
    z pliku (file://) może dołączyć lokalny <script src>, ale nie może pobrać
    lokalnego pliku przez fetch()."""
    data = {
        kind: {
            line: [
                {
                    **variant,
                    "shape": [[round(lat, 5), round(lon, 5)] for lat, lon in variant["shape"]],
                    "stops": [
                        [round(lat, 5), round(lon, 5), name, code]
                        for lat, lon, name, code in variant["stops"]
                    ],
                }
                for variant in variants
            ]
            for line, variants in network["routes"].items()
        }
        for kind, network in networks.items()
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ROUTES_FILE.with_suffix(".part")
    tmp.write_text(
        "window.TRANSIT_ROUTES = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    tmp.replace(ROUTES_FILE)
    return ROUTES_FILE


def merge_lines(posts: list[dict]) -> Counter:
    lines = Counter()
    for post in posts:
        lines.update(post["lines"])
    return lines


def group_posts(posts: dict[str, dict]) -> dict[str, dict]:
    """Słupki → przystanki: {numer: {"name", "lat", "lon", "lines"}}, gdzie numer to część
    stop_code przed myślnikiem, współrzędne to średnia ze słupków, a odjazdy linii
    to suma ze wszystkich słupków (oba kierunki)."""
    groups: dict[str, list] = {}
    for code, post in posts.items():
        groups.setdefault(code.split("-")[0], []).append(post)
    return {
        number: {
            "name": members[0]["name"],
            "lat": sum(p["lat"] for p in members) / len(members),
            "lon": sum(p["lon"] for p in members) / len(members),
            # update, nie sum(): dodawanie Counterów gubi linie z zerem odjazdów
            "lines": merge_lines(members),
        }
        for number, members in groups.items()
    }

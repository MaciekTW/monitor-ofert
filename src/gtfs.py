# -*- coding: utf-8 -*-
"""Przystanki komunikacji miejskiej w Krakowie z rozkładów GTFS ZTP.

Archiwa (autobusy MPK, autobusy Mobilis, tramwaje) są pobierane do lokalnej
kopii w .cache/gtfs/ i odświeżane tylko wtedy, gdy serwer ma nowszą wersję
(If-Modified-Since). Przystanek to grupa słupków o wspólnym prefiksie stop_code
(„920-01”, „920-02” → „920”); ten numer jest zgodny między archiwami,
w przeciwieństwie do stop_id.

Liczba odjazdów dotyczy jednego dnia odniesienia — najbliższego wtorku lub
środy z typowym rozkładem dnia roboczego (reference_day).

Przebiegi tras linii (główny wariant w każdym kierunku) są zapisywane osobno do
.cache/gtfs/routes.js, który mapa wczytuje dopiero po kliknięciu linii (write_routes)."""

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

# archiwum → rodzaj przystanku
FEEDS = {
    "GTFS_KRK_A.zip": "bus",  # autobusy MPK
    "GTFS_KRK_M.zip": "bus",  # autobusy Mobilis
    "GTFS_KRK_T.zip": "tram",  # tramwaje
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def download(name: str, offline: bool = False) -> Path | None:
    """Zwraca ścieżkę do aktualnej kopii archiwum; pobiera je tylko, gdy na
    serwerze jest nowsza wersja. Bez sieci używa starej kopii, jeśli jest."""
    path = CACHE_DIR / name
    if offline:
        return path if path.exists() else None
    headers = {"User-Agent": "monitor-ofert"}
    if path.exists():
        headers["If-Modified-Since"] = formatdate(path.stat().st_mtime, usegmt=True)
    try:
        with urlopen(Request(GTFS_URL + name, headers=headers), timeout=60) as resp:
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


def load(offline: bool = False, today: date | None = None) -> tuple[dict[str, dict], date | None]:
    """({"bus": {"posts": {stop_code: słupek}, "night": {linie nocne}, "routes": {linia: trasy}}, "tram": {...}},
    dzień odniesienia). Słupek wspólny dla autobusów MPK i Mobilis ma zsumowane
    linie i odjazdy obu przewoźników. Brak archiwum (np. bez sieci) = brak jego
    słupków i linii."""
    archives = {}
    for name in FEEDS:
        archive = download(name, offline)
        if archive is not None:
            archives[name] = archive
    missing = [name for name in FEEDS if name not in archives]
    if missing:
        log(f"⚠ Brak rozkładów GTFS ({', '.join(missing)}) — mapa będzie bez części przystanków.")
    if not archives:
        return {}, None

    calendars = {name: read_calendar(archive) for name, archive in archives.items()}
    day = reference_day(list(calendars.values()), today or date.today())
    result: dict[str, dict] = {}
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
    {"bus": {linia: trasy}, "tram": {...}}. Skrypt, a nie JSON, bo mapa otwierana
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

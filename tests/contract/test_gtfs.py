# -*- coding: utf-8 -*-
"""Testy kontraktów GTFS ZTP Kraków — prawdziwe pobranie archiwów rozkładów,
z których src/gtfs.py bierze przystanki, linie, odjazdy i trasy. Sprawdzają,
czy strona linkuje archiwa, czy dają się pobrać tak jak w skrypcie (gtfs.download),
czy mają pliki i kolumny czytane przez parser oraz czy dane spełniają założenia,
których brak nie wywróciłby skryptu, ale po cichu popsułby mapę (grupowanie
słupków po stop_code, godziny po północy, kalendarz, przebiegi tras)."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import Counter
from datetime import date

import pytest

import gtfs
from tests.contract.helpers import assert_all, fetch

# pliki i kolumny czytane przez gtfs.read_feed / gtfs.read_calendar
REQUIRED_COLUMNS = {
    "routes.txt": {"route_id", "route_short_name"},
    "trips.txt": {"trip_id", "route_id", "service_id", "direction_id", "shape_id", "trip_headsign"},
    "stop_times.txt": {"trip_id", "stop_id", "stop_sequence", "departure_time", "pickup_type"},
    "stops.txt": {"stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"},
    "shapes.txt": {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"},
}
# kalendarz: wystarczy jeden z dwóch plików, ale obecny musi mieć te kolumny
CALENDAR_COLUMNS = {
    "calendar.txt": {"service_id", "start_date", "end_date", *gtfs.WEEKDAY_COLUMNS},
    "calendar_dates.txt": {"service_id", "date", "exception_type"},
}
# dolne granice rozmiaru (ok. połowa stanu z września 2026) — mniej znaczy
# obcięte albo niepełne archiwum: (słupki z kursami, linie)
MIN_SIZE = {
    "GTFS_KRK_A.zip": (1200, 80),
    "GTFS_KRK_M.zip": (350, 15),
    "GTFS_KRK_T.zip": (150, 12),
}
FEED_NAMES = sorted(gtfs.FEEDS)


@pytest.fixture(scope="module")
def listed_files() -> set[str]:
    resp = fetch(gtfs.GTFS_URL, as_json=False)
    assert resp.status_code == 200, f"{gtfs.GTFS_URL}: HTTP {resp.status_code}"
    return {href.removeprefix("./") for href in re.findall(r'href=["\']([^"\']+)["\']', resp.text)}


@pytest.fixture(scope="module")
def archives(tmp_path_factory) -> dict:
    """Wszystkie archiwa pobrane raz, funkcją skryptu, do pustego katalogu
    zamiast .cache/gtfs/ (żeby test nie korzystał z lokalnej kopii)."""
    cache = tmp_path_factory.mktemp("gtfs")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gtfs, "CACHE_DIR", cache)
        paths = {name: gtfs.download(name) for name in FEED_NAMES}
    failed = [name for name, path in paths.items() if path is None]
    assert not failed, f"nie udało się pobrać {failed} z {gtfs.GTFS_URL} (szczegóły w logu wyżej)"
    return {"dir": cache, "paths": paths}


@pytest.fixture(scope="module")
def parsed(archives) -> dict:
    """Wynik parsera dla każdego archiwum w dniu odniesienia:
    {nazwa: {"calendar", "posts", "night", "routes"}} oraz "day"."""
    paths = archives["paths"]
    calendars = {name: gtfs.read_calendar(paths[name]) for name in FEED_NAMES}
    day = gtfs.reference_day(list(calendars.values()), date.today())
    result = {"day": day}
    for name in FEED_NAMES:
        posts, night, routes = gtfs.read_feed(paths[name], calendars[name], day)
        result[name] = {"calendar": calendars[name], "posts": posts, "night": night, "routes": routes}
    return result


def rows(path, member: str):
    """(nagłówek, wiersze) pliku z archiwum."""
    z = zipfile.ZipFile(path)
    reader = csv.reader(io.TextIOWrapper(z.open(member), encoding="utf-8-sig"))
    return next(reader, []), reader


@pytest.fixture(scope="module")
def stop_times_stats(archives) -> dict:
    """Jedno przejście po stop_times każdego archiwum: niepoprawne godziny,
    najpóźniejsza godzina i wartości pickup_type."""
    time_re = re.compile(r"\d{2}:\d{2}:\d{2}")
    stats = {}
    for name, path in archives["paths"].items():
        header, reader = rows(path, "stop_times.txt")
        dep_i, pickup_i = header.index("departure_time"), header.index("pickup_type")
        bad, max_hour, pickups = [], 0, Counter()
        for row in reader:
            dep = row[dep_i]
            if time_re.fullmatch(dep):
                max_hour = max(max_hour, int(dep[:2]))
            elif len(bad) < 10:
                bad.append(dep)
            pickups[row[pickup_i]] += 1
        stats[name] = {"bad": bad, "max_hour": max_hour, "pickups": pickups}
    return stats


# ------------------------------------------------------------- strona i pobieranie


@pytest.mark.parametrize("name", FEED_NAMES)
def test_index_lists_gtfs_archive(listed_files, name):
    assert name in listed_files, f"brak linku do {name}; są: {sorted(listed_files)}"


@pytest.mark.parametrize("name", FEED_NAMES)
def test_archive_is_valid_zip(archives, name):
    path = archives["paths"][name]
    assert zipfile.is_zipfile(path), f"{name} nie jest archiwum ZIP"
    assert zipfile.ZipFile(path).testzip() is None, f"{name}: uszkodzony plik w archiwum"


@pytest.mark.parametrize("name", FEED_NAMES)
def test_server_answers_not_modified(archives, name):
    """gtfs.download pyta z If-Modified-Since i przy 304 nie pobiera archiwum
    ponownie. Bez tego mapa działa, ale każde generowanie ściąga ~30 MB —
    dlatego brak obsługi to pominięcie z opisem, a nie błąd."""
    path = archives["paths"][name]
    before = path.stat().st_ino
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gtfs, "CACHE_DIR", archives["dir"])
        again = gtfs.download(name)
    assert again == path
    if again.stat().st_ino != before:
        pytest.skip(
            f"{name}: serwer nie odpowiada 304 na If-Modified-Since — archiwum pobierane za każdym razem"
        )


# ------------------------------------------------------------- pliki i kolumny


@pytest.mark.parametrize("name", FEED_NAMES)
def test_archive_has_required_files(archives, name):
    members = set(zipfile.ZipFile(archives["paths"][name]).namelist())
    missing = set(REQUIRED_COLUMNS) - members
    assert not missing, f"{name}: brak plików {sorted(missing)}"
    assert members & set(CALENDAR_COLUMNS), f"{name}: brak calendar.txt i calendar_dates.txt"


@pytest.mark.parametrize("member", sorted(REQUIRED_COLUMNS) + sorted(CALENDAR_COLUMNS))
@pytest.mark.parametrize("name", FEED_NAMES)
def test_file_has_required_columns(archives, name, member):
    path = archives["paths"][name]
    if member in CALENDAR_COLUMNS and member not in zipfile.ZipFile(path).namelist():
        return  # jeden z plików kalendarza może nie istnieć — pilnuje tego test_archive_has_required_files
    header, _ = rows(path, member)
    missing = (REQUIRED_COLUMNS.get(member) or CALENDAR_COLUMNS[member]) - set(header)
    assert not missing, f"{name}/{member}: brak kolumn {sorted(missing)}; są: {header}"


# ------------------------------------------------------------- założenia o danych


@pytest.mark.parametrize("name", FEED_NAMES)
def test_stop_codes_are_group_and_post(archives, name):
    """Przystanek to część stop_code przed myślnikiem („920-01” → „920”) —
    na tym opiera się grupowanie słupków i łączenie autobusów z tramwajami."""
    header, reader = rows(archives["paths"][name], "stops.txt")
    codes = [row[header.index("stop_code")] for row in reader]
    assert_all(codes, lambda code: re.fullmatch(r"\d+-\d+", code), f"{name}: stop_code w postaci NNN-NN")


def test_stop_group_has_one_name_across_feeds(parsed):
    """Numer przystanku oznacza to samo miejsce we wszystkich archiwach. Sprawdzane
    na słupkach z parsera, a nie na całym stops.txt: przed zmianą rozkładu archiwum
    ma też poprzednią wersję, w której przemianowany słupek ma starą nazwę.

    Różne nazwy w jednym archiwum to błąd (mapa pokazałaby przystanek pod dwiema
    nazwami). Różne między archiwami to zwykle zmiana nazwy, której przewoźnik nie
    wprowadził jeszcze w swoim archiwum — to pominięcie z opisem, a nie błąd."""
    names: dict[str, dict[str, set]] = {}  # numer → {archiwum: nazwy}
    for name in FEED_NAMES:
        for code, post in parsed[name]["posts"].items():
            names.setdefault(code.split("-")[0], {}).setdefault(name, set()).add(post["name"])
    within = {
        f"{feed}/{group}": sorted(n)
        for group, feeds in names.items()
        for feed, n in feeds.items()
        if len(n) > 1
    }
    assert not within, f"przystanki z różnymi nazwami w jednym archiwum: {dict(list(within.items())[:10])}"
    across = {
        group: {feed: min(n) for feed, n in feeds.items()}
        for group, feeds in names.items()
        if len(set().union(*feeds.values())) > 1
    }
    if across:
        pytest.skip(f"przystanki z różnymi nazwami w różnych archiwach: {dict(list(across.items())[:10])}")


@pytest.mark.parametrize("name", FEED_NAMES)
def test_departure_times_format(stop_times_stats, name):
    """Godziny HH:MM:SS, po północy liczone dalej (24:xx, 25:xx) — parser porównuje
    je jako tekst i z nich rozpoznaje linie nocne."""
    stats = stop_times_stats[name]
    assert not stats["bad"], f"{name}: departure_time nie w formacie HH:MM:SS, np. {stats['bad']}"
    assert stats["max_hour"] >= 24, (
        f"{name}: brak godzin po północy (najpóźniejsza {stats['max_hour']}:xx) — "
        "kursy nocne zapisane inaczej niż 24:xx+"
    )


@pytest.mark.parametrize("name", FEED_NAMES)
def test_pickup_type_values(stop_times_stats, name):
    """Odjazd to zatrzymanie z pickup_type ≠ 1; nieznane wartości mogą zmieniać znaczenie."""
    unknown = set(stop_times_stats[name]["pickups"]) - {"", "0", "1", "2", "3"}
    assert not unknown, f"{name}: nieznane wartości pickup_type {sorted(unknown)}"


@pytest.mark.parametrize("name", FEED_NAMES)
def test_trip_shapes_exist(archives, name):
    """Trasy linii rysowane są z shapes.txt; bez przebiegu zostają proste odcinki."""
    path = archives["paths"][name]
    header, reader = rows(path, "trips.txt")
    used = {row[header.index("shape_id")] for row in reader}
    header, reader = rows(path, "shapes.txt")
    known = {row[header.index("shape_id")] for row in reader}
    missing = used - known
    assert "" not in used, f"{name}: kursy bez shape_id"
    assert not missing, (
        f"{name}: {len(missing)}/{len(used)} shape_id z trips.txt nie ma w shapes.txt, np. {sorted(missing)[:5]}"
    )


# ------------------------------------------------------------- wynik parsera


def test_reference_day_has_service_in_every_feed(parsed):
    """Kalendarze obejmują najbliższe tygodnie i reference_day trafia w dzień,
    w którym każde archiwum ma kursy."""
    day = parsed["day"]
    empty = [name for name in FEED_NAMES if not parsed[name]["calendar"](day)]
    assert not empty, f"w dniu odniesienia {day} brak kursów w {empty}"


@pytest.mark.parametrize("name", FEED_NAMES)
def test_feed_size(parsed, name):
    posts = parsed[name]["posts"]
    lines = set().union(*(p["lines"] for p in posts.values()))
    min_posts, min_lines = MIN_SIZE[name]
    assert len(posts) >= min_posts, f"{name}: tylko {len(posts)} słupków z kursami (oczekiwano ≥ {min_posts})"
    assert len(lines) >= min_lines, f"{name}: tylko {len(lines)} linii (oczekiwano ≥ {min_lines})"


@pytest.mark.parametrize("name", FEED_NAMES)
def test_most_lines_run_on_reference_day(parsed, name):
    """Liczba odjazdów pochodzi z dnia odniesienia — jeśli większość linii ma 0,
    kalendarz albo wybór dnia rozjechał się z danymi."""
    totals = Counter()
    for post in parsed[name]["posts"].values():
        totals.update(post["lines"])
    running = [line for line, n in totals.items() if n > 0]
    assert len(running) >= 0.7 * len(totals), (
        f"{name}: w dniu {parsed['day']} kursuje tylko {len(running)}/{len(totals)} linii"
    )


@pytest.mark.parametrize("name", FEED_NAMES)
def test_night_lines_are_minority(parsed, name):
    """Rozpoznawanie linii nocnych po godzinach kursów: są jakieś, ale nie większość."""
    night = parsed[name]["night"]
    lines = parsed[name]["routes"].keys()
    assert night, f"{name}: nie rozpoznano żadnej linii nocnej"
    assert len(night) < len(lines) / 2, f"{name}: {len(night)}/{len(lines)} linii uznanych za nocne"


@pytest.mark.parametrize("name", FEED_NAMES)
def test_routes_have_geometry(parsed, name):
    """Każda linia ma trasę z przystankami, a przebiegi są gęstsze niż same przystanki
    (czyli pochodzą z shapes.txt, a nie z awaryjnych prostych odcinków)."""
    routes = parsed[name]["routes"]
    variants = [v for vs in routes.values() for v in vs]
    assert_all(variants, lambda v: len(v["stops"]) >= 2, f"{name}: trasy z co najmniej 2 przystankami")
    detailed = [v for v in variants if len(v["shape"]) > len(v["stops"])]
    assert len(detailed) >= 0.9 * len(variants), (
        f"{name}: tylko {len(detailed)}/{len(variants)} tras ma przebieg gęstszy niż przystanki"
    )

# -*- coding: utf-8 -*-
"""Test kontraktu rozkładu kolejowego (mkuran.pl, „Polish Trains”) — prawdziwe
pobranie archiwum, z którego src/gtfs.py bierze stacje i przystanki kolejowe.
Sprawdza, czy strona linkuje archiwum i czy da się je pobrać tak jak w skrypcie,
pliki i kolumny czytane przez gtfs.read_rail oraz założenia, których brak nie
wywróciłby skryptu, ale po cichu popsułby mapę: perony podpięte pod stację
(parent_station) w tym samym miejscu, typ trasy „pociąg”, linie o stałej trasie
rozpoznawane po nazwie i wspólne dla przewoźników, znaczenie pickup_type,
kalendarz obejmujący najbliższe dni robocze i sensowny wynik dla okolic Krakowa."""

from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from collections import Counter
from datetime import date, timedelta

import pytest

import gtfs
from tests.contract.helpers import fetch

# pliki i kolumny czytane przez gtfs.read_rail / gtfs.read_calendar
REQUIRED_COLUMNS = {
    "routes.txt": {"route_id", "route_short_name", "route_long_name", "route_type"},
    "trips.txt": {"trip_id", "route_id", "service_id", "direction_id", "shape_id", "trip_headsign"},
    "stop_times.txt": {"trip_id", "stop_id", "stop_sequence", "pickup_type"},
    "stops.txt": {"stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station"},
    "shapes.txt": {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"},
    "calendar_dates.txt": {"service_id", "date", "exception_type"},
}
KRAKOW = (49.9, 19.7, 50.2, 20.3)  # (lat min, lon min, lat max, lon max)
# mapa rysuje stację jednym markerem, więc peron nie może leżeć daleko od niej
MAX_PLATFORM_DISTANCE_M = 1000
# dzień odniesienia wybierają rozkłady miejskie spośród wtorków i śród — kolej
# musi mieć kursy w każdy taki dzień najbliższych tygodni
CALENDAR_HORIZON_DAYS = 14


def members(archive, member: str) -> list[dict]:
    with zipfile.ZipFile(archive) as z:
        return list(csv.DictReader(io.TextIOWrapper(z.open(member), encoding="utf-8-sig")))


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """Archiwum pobrane funkcją skryptu do pustego katalogu zamiast .cache/gtfs/."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gtfs, "CACHE_DIR", tmp_path_factory.mktemp("rail"))
        path = gtfs.download(gtfs.RAIL_FEED, base=gtfs.RAIL_URL)
    assert path is not None, f"nie udało się pobrać {gtfs.RAIL_URL}{gtfs.RAIL_FEED} (szczegóły w logu wyżej)"
    return path


@pytest.fixture(scope="module")
def rail(archive) -> dict:
    calendar = gtfs.read_calendar(archive)
    day = gtfs.reference_day([calendar], date.today())
    return gtfs.read_rail(archive, calendar, day, KRAKOW)


def test_archive_has_files_and_columns(archive):
    assert zipfile.is_zipfile(archive), "archiwum nie jest plikiem ZIP"
    with zipfile.ZipFile(archive) as z:
        for member, columns in REQUIRED_COLUMNS.items():
            assert member in z.namelist(), f"brak pliku {member}"
            header = next(csv.reader(io.TextIOWrapper(z.open(member), encoding="utf-8-sig")))
            assert columns <= set(header), f"{member}: brak kolumn {sorted(columns - set(header))}"


def test_routes_mark_trains_and_fixed_lines(archive):
    """Pociągi mają route_type 2 (autobusy zastępcze 3), a linie o stałej trasie
    — jej przebieg w route_long_name, po czym gtfs.read_rail je rozpoznaje."""
    with zipfile.ZipFile(archive) as z:
        routes = list(csv.DictReader(io.TextIOWrapper(z.open("routes.txt"), encoding="utf-8-sig")))
    trains = [r for r in routes if r["route_type"] == gtfs.RAIL_ROUTE_TYPE]
    assert len(trains) >= 50, f"tylko {len(trains)} tras kolejowych"
    fixed = {r["route_short_name"] for r in trains if gtfs.RAIL_FIXED_ROUTE in r["route_long_name"]}
    assert {"SKA1", "SKA2", "SKA3"} <= fixed, f"linie SKA bez trasy w nazwie; ze stałą trasą: {sorted(fixed)}"
    assert not {"IC", "EIP", "TLK"} & fixed, "kategorie pociągów rozpoznane jako linie o stałej trasie"


def test_krakow_stations_lines_and_routes(rail):
    stations = {post["name"]: post for post in rail["posts"].values()}
    assert len(stations) >= 30, f"tylko {len(stations)} stacji w okolicach Krakowa"
    main = stations.get("Kraków Główny")
    assert main is not None, f"brak stacji Kraków Główny; są: {sorted(stations)[:20]}"
    assert abs(main["lat"] - 50.068) < 0.01 and abs(main["lon"] - 19.947) < 0.01, (
        "Kraków Główny w złym miejscu"
    )
    assert {"SKA1", "IC"} <= set(main["lines"]), f"linie na Krakowie Głównym: {sorted(main['lines'])}"
    assert sum(main["lines"].values()) >= 100, "podejrzanie mało odjazdów z Krakowa Głównego"
    for line in ("SKA1", "SKA2", "SKA3"):
        variants = rail["routes"].get(line)
        assert variants, f"brak trasy linii {line}"
        assert all(len(v["shape"]) >= 20 and len(v["stops"]) >= 5 for v in variants), (
            f"{line}: szczątkowa trasa"
        )
    assert "IC" not in rail["routes"], "kategoria pociągu dostała trasę"


# ------------------------------------------------------------- strona i pobieranie


def test_index_lists_rail_archive():
    resp = fetch(gtfs.RAIL_URL, as_json=False)
    assert resp.status_code == 200, f"{gtfs.RAIL_URL}: HTTP {resp.status_code}"
    links = re.findall(r'href=["\']([^"\']+)["\']', resp.text)
    assert any(link.endswith("/" + gtfs.RAIL_FEED) or link == gtfs.RAIL_FEED for link in links), (
        f"strona nie linkuje {gtfs.RAIL_FEED}"
    )


def test_server_answers_not_modified(archive):
    """gtfs.download pyta z If-Modified-Since i przy 304 nie pobiera archiwum
    ponownie. Bez tego mapa działa, ale każde generowanie ściąga ~27 MB —
    dlatego brak obsługi to pominięcie z opisem, a nie błąd."""
    before = archive.stat().st_ino
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gtfs, "CACHE_DIR", archive.parent)
        again = gtfs.download(gtfs.RAIL_FEED, base=gtfs.RAIL_URL)
    assert again == archive
    if again.stat().st_ino != before:
        pytest.skip("serwer nie odpowiada 304 na If-Modified-Since — archiwum pobierane za każdym razem")


# ------------------------------------------------------------- założenia o danych


def test_platforms_belong_to_stations(archive):
    """read_rail zlewa perony w stację po parent_station: każdy peron wskazuje
    istniejącą stację, stacja sama nie ma rodzica, a peron leży tam, gdzie ona."""
    stops = {row["stop_id"]: row for row in members(archive, "stops.txt")}
    children = [row for row in stops.values() if row["parent_station"]]
    assert len(children) >= 3000, f"tylko {len(children)} peronów z parent_station"
    orphans = [row["stop_id"] for row in children if row["parent_station"] not in stops]
    assert not orphans, f"perony bez stacji w stops.txt: {orphans[:10]}"
    nested = [row["stop_id"] for row in children if stops[row["parent_station"]]["parent_station"]]
    assert not nested, f"stacje, które same mają rodzica: {nested[:10]}"

    def distance(a: dict, b: dict) -> float:
        dy = (float(a["stop_lat"]) - float(b["stop_lat"])) * 111_320
        dx = (
            (float(a["stop_lon"]) - float(b["stop_lon"]))
            * 111_320
            * math.cos(math.radians(float(a["stop_lat"])))
        )
        return math.hypot(dx, dy)

    far = [
        row["stop_id"]
        for row in children
        if distance(row, stops[row["parent_station"]]) > MAX_PLATFORM_DISTANCE_M
    ]
    assert not far, f"perony dalej niż {MAX_PLATFORM_DISTANCE_M} m od swojej stacji: {far[:10]}"


def test_stop_times_use_platforms_and_known_pickup_types(archive):
    """Odjazd liczy się tam, gdzie pickup_type nie jest „1” (nie można wsiąść);
    a kursy zatrzymują się na peronach, nie wprost na stacjach."""
    stops = {row["stop_id"]: row["parent_station"] for row in members(archive, "stops.txt")}
    pickups: Counter = Counter()
    unknown, on_station = set(), 0
    with zipfile.ZipFile(archive) as z:
        reader = csv.reader(io.TextIOWrapper(z.open("stop_times.txt"), encoding="utf-8-sig"))
        header = next(reader)
        stop_i, pickup_i = header.index("stop_id"), header.index("pickup_type")
        for row in reader:
            pickups[row[pickup_i]] += 1
            if row[stop_i] not in stops:
                unknown.add(row[stop_i])
            elif not stops[row[stop_i]]:
                on_station += 1
    assert not unknown, f"stop_times odwołuje się do nieznanych stop_id: {sorted(unknown)[:10]}"
    assert set(pickups) <= {"", "0", "1", "2", "3"}, f"nieznane wartości pickup_type: {sorted(pickups)}"
    assert pickups["1"], "żaden postój nie ma pickup_type=1 — stacje końcowe liczyłyby się jako odjazdy"
    assert pickups["0"] + pickups[""] > pickups["1"], f"większość postojów bez wsiadania: {dict(pickups)}"
    total = sum(pickups.values())
    assert on_station < 0.01 * total, f"{on_station}/{total} postojów wprost na stacji, a nie na peronie"


def test_lines_are_shared_between_operators_and_buses_are_marked(archive):
    """Linie łączą się po route_short_name (SKA2 jeżdżą dwaj przewoźnicy),
    a zastępcza komunikacja autobusowa odpada po route_type."""
    routes = members(archive, "routes.txt")
    agencies: dict[str, set] = {}
    for row in routes:
        if row["route_type"] == gtfs.RAIL_ROUTE_TYPE:
            agencies.setdefault(row["route_short_name"], set()).add(row["agency_id"])
    assert len(agencies.get("SKA2", ())) >= 2, f"SKA2 ma przewoźników: {sorted(agencies.get('SKA2', ()))}"
    buses = [row for row in routes if row["route_short_name"].startswith("ZKA")]
    assert buses, "brak tras zastępczej komunikacji autobusowej (ZKA) — zmieniło się ich oznaczenie?"
    wrong = [row["route_id"] for row in buses if row["route_type"] == gtfs.RAIL_ROUTE_TYPE]
    assert not wrong, f"autobusy zastępcze oznaczone jako pociągi: {wrong[:10]}"


def test_calendar_covers_upcoming_reference_days(archive):
    """Dzień odniesienia wyznaczają rozkłady miejskie (wtorek albo środa z najbliższych
    tygodni) — archiwum kolejowe musi mieć wtedy kursy, inaczej stacje pokazują zera."""
    active = gtfs.read_calendar(archive)
    days = [
        date.today() + timedelta(n)
        for n in range(CALENDAR_HORIZON_DAYS)
        if (date.today() + timedelta(n)).weekday() in gtfs.REFERENCE_WEEKDAYS
    ]
    empty = [day.isoformat() for day in days if len(active(day)) < 10]
    assert not empty, f"brak kursów w dniach: {empty}"


def test_stations_are_single_markers(rail):
    """Peron, który nie zlał się ze stacją, dałby drugi marker o tej samej nazwie."""
    names = Counter(post["name"] for post in rail["posts"].values())
    doubled = sorted(name for name, count in names.items() if count > 1)
    assert not doubled, f"stacje z więcej niż jednym markerem: {doubled}"

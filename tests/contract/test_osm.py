# -*- coding: utf-8 -*-
"""Testy kontraktów danych do raportu o mieszkaniu: Overpass API (OpenStreetMap),
z którego src/osm.py bierze sklepy, placówki i tereny zielone, oraz miejska mapa
hałasu (MSIP Kraków, ArcGIS), o którą raport pyta z przeglądarki dla jednego punktu.

Zapytania są te same co w skrypcie, tylko dla wycinka miasta. Przeciążony serwer
Overpass (zdarza się często) to pominięcie testu, a nie błąd — nic nie mówi o kontrakcie."""

from __future__ import annotations

import json

import pytest

import osm
from map import html_map
from tests.contract.helpers import fetch

# (lat min, lon min, lat max, lon max)
OLD_TOWN = (50.055, 19.925, 50.070, 19.950)  # Stare Miasto: jest tu wszystko po trochu
AVIATORS_PARK = (50.062, 19.980, 50.082, 20.015)  # Park Lotników Polskich — relacja z kilku odcinków
MAIN_STATION = (50.0686, 19.9478)  # lat, lon: Kraków Główny, w zasięgu hałasu drogowego i szynowego
# kategorie, które w centrum dużego miasta muszą mieć choć jeden punkt
EXPECTED = (
    "supermarket",
    "convenience",
    "pharmacy",
    "bank",
    "clinic",
    "kindergarten",
    "school",
    "restaurant",
    "cafe",
)


def ask(query: str) -> list[dict]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(osm, "BUDGET", 90)  # w teście krócej niż przy generowaniu mapy
        try:
            return osm.overpass(query)
        except RuntimeError as exc:
            pytest.skip(f"Overpass nie odpowiada ({exc}) — kontraktu nie da się sprawdzić")


@pytest.fixture(scope="module")
def point_elements() -> list[dict]:
    return ask(osm.points_query(OLD_TOWN))


@pytest.fixture(scope="module")
def green_elements() -> list[dict]:
    return ask(osm.green_query(AVIATORS_PARK))


# ------------------------------------------------------------- punkty


def test_point_elements_have_tags_and_position(point_elements):
    """classify_points czyta tags oraz lat/lon (punkt) albo center (droga, relacja)."""
    assert len(point_elements) >= 300, f"tylko {len(point_elements)} elementów w centrum Krakowa"
    without_tags = [e.get("id") for e in point_elements if not isinstance(e.get("tags"), dict)]
    assert not without_tags, f"elementy bez tags: {without_tags[:10]}"
    kinds = {e["type"] for e in point_elements}
    assert {"node", "way"} <= kinds, f"typy elementów: {sorted(kinds)}"
    lost = [e["id"] for e in point_elements if osm._center(e) is None]
    assert len(lost) <= 0.01 * len(point_elements), f"elementy bez położenia: {lost[:10]}"


def test_points_fall_into_categories(point_elements):
    points = osm.classify_points(point_elements)
    empty = [cat for cat in EXPECTED if not points[cat]]
    assert not empty, f"puste kategorie w centrum miasta: {empty}"
    for cat, items in points.items():
        outside = [
            p
            for p in items
            if not (
                OLD_TOWN[0] - 0.01 <= p[0] <= OLD_TOWN[2] + 0.01
                and OLD_TOWN[1] - 0.01 <= p[1] <= OLD_TOWN[3] + 0.01
            )
        ]
        assert not outside, f"{cat}: punkty poza prostokątem zapytania: {outside[:3]}"
    named = sum(1 for items in points.values() for p in items if p[2])
    total = sum(len(items) for items in points.values())
    assert named >= 0.7 * total, f"tylko {named}/{total} punktów ma nazwę (name, brand albo operator)"
    # podstawówki rozpoznajemy po nazwie albo isced:level — musi się to udawać dla części szkół
    assert points["school"] and points["highschool"], "szkoły nie dzielą się na podstawowe i średnie"


# ------------------------------------------------------------- tereny zielone


def test_green_relations_come_with_member_geometry(green_elements):
    """Duże parki są relacjami: obrys składa się z odcinków (members) z geometrią."""
    relations = [e for e in green_elements if e["type"] == "relation"]
    assert relations, "w wycinku nie ma żadnej relacji — zmienił się sposób mapowania parku?"
    for relation in relations:
        ways = [m for m in relation.get("members") or [] if m.get("type") == "way"]
        assert ways, f"relacja {relation['id']} bez odcinków (members)"
        assert all(m.get("geometry") for m in ways), f"relacja {relation['id']}: odcinki bez geometrii"
    ways = [e for e in green_elements if e["type"] == "way"]
    assert ways and all(e.get("geometry") for e in ways), "drogi bez geometrii"


def test_green_areas_get_outline_and_area(green_elements):
    areas = {name: (hectares, outline) for name, hectares, outline in osm.classify_green(green_elements)}
    park = areas.get("Park Lotników Polskich")
    assert park is not None, f"brak Parku Lotników Polskich; są: {sorted(areas)[:15]}"
    hectares, outline = park
    assert 30 <= hectares <= 80, f"Park Lotników Polskich ma {hectares} ha"
    assert len(outline) >= 50, f"obrys parku ma tylko {len(outline)} punktów"


# ------------------------------------------------------------- sieć piesza

WAWEL = (50.050, 19.930, 50.058, 19.942)  # mały wycinek: zamek, bulwary, kawałek Starego Miasta


@pytest.fixture(scope="module")
def walk_elements() -> list[dict]:
    return ask(osm.walk_query(WAWEL))


def test_walk_ways_come_with_node_ids_and_geometry(walk_elements):
    """compact_ways łączy drogi po wspólnych węzłach: każda ma listę id węzłów
    i tyle samo punktów geometrii."""
    assert len(walk_elements) >= 300, f"tylko {len(walk_elements)} dróg wokół Wawelu"
    assert all(e["type"] == "way" for e in walk_elements), "zapytanie zwróciło coś poza drogami"
    broken = [e["id"] for e in walk_elements if len(e.get("nodes") or []) != len(e.get("geometry") or [])]
    assert not broken, f"drogi bez kompletu węzłów albo geometrii: {broken[:10]}"
    assert len(osm.compact_ways(walk_elements)) == len({e["id"] for e in walk_elements})


def test_walk_graph_is_connected_and_measured(walk_elements):
    """Graf do raportu: jedna spójna sieć, krawędzie z długością w metrach."""
    graph = osm.build_walk_graph(osm.compact_ways(walk_elements))
    count = len(graph["nodes"]) // 2
    assert count >= 1000, f"graf ma tylko {count} wierzchołków"
    starts, ends, lengths = graph["edges"][0::3], graph["edges"][1::3], graph["edges"][2::3]
    assert max(max(starts), max(ends)) < count, "krawędź wskazuje wierzchołek spoza grafu"
    assert all(1 <= length <= 2000 for length in lengths), (
        f"długości krawędzi: {min(lengths)}–{max(lengths)} m"
    )
    # współrzędne zapisane różnicowo wracają w okolice Wawelu
    lat = lon = 0
    for i in range(count):
        lat += graph["nodes"][2 * i]
        lon += graph["nodes"][2 * i + 1]
        assert 50.0 < lat / 1e5 < 50.1 and 19.88 < lon / 1e5 < 20.0, (
            f"wierzchołek {i} poza okolicą: {lat / 1e5}, {lon / 1e5}"
        )
    # spójność: z pierwszego wierzchołka da się dojść do wszystkich
    neighbours: dict[int, list] = {}
    for a, b in zip(starts, ends):
        neighbours.setdefault(a, []).append(b)
        neighbours.setdefault(b, []).append(a)
    seen, stack = {0}, [0]
    while stack:
        for nxt in neighbours.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    assert len(seen) == count, f"sieć rozpada się na części: osiągalne {len(seen)} z {count} wierzchołków"


def test_walk_tiles_cover_bounds():
    tiles = osm.walk_tiles((49.95, 19.8, 50.15, 20.1))
    assert len(tiles) == 12
    assert min(t[0] for t in tiles) == 49.95 and max(t[2] for t in tiles) == 50.15
    assert min(t[1] for t in tiles) == 19.8 and max(t[3] for t in tiles) == 20.1


# ------------------------------------------------------------- mapa hałasu


def noise_query(layer: int, **extra) -> str:
    """Adres zapytania o punkt — taki sam, jaki składa report.js (noiseAt)."""
    lat, lon = MAIN_STATION
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ISOV1,ISOV2",
        "returnGeometry": "false",
        "f": "json",
        **extra,
    }
    return f"{html_map.NOISE_SERVICE}/{layer}/query?" + "&".join(f"{k}={v}" for k, v in params.items())


def test_noise_service_lists_expected_layers():
    resp = fetch(html_map.NOISE_SERVICE + "?f=json")
    assert resp.status_code == 200, f"HTTP {resp.status_code}"
    layers = {layer["id"]: layer["name"] for layer in resp.json()["layers"]}
    for _, day, night in html_map.NOISE_LAYERS:
        assert "LDWN" in layers.get(day, ""), f"warstwa {day} to „{layers.get(day)}”, a nie hałas całodobowy"
        assert layers.get(night, "").endswith("LN"), (
            f"warstwa {night} to „{layers.get(night)}”, a nie hałas nocny"
        )
    road, rail, industry = (layers[day] for _, day, _ in html_map.NOISE_LAYERS)
    assert "drogow" in road and "szynow" in rail and "przemysłow" in industry, [road, rail, industry]


def test_noise_point_query_returns_decibel_band():
    """Przy dworcu głównym jest głośno: zapytanie o punkt zwraca strefę z przedziałem dB."""
    road_day = html_map.NOISE_LAYERS[0][1]
    resp = fetch(noise_query(road_day))
    assert resp.status_code == 200, f"HTTP {resp.status_code}"
    data = resp.json()
    assert "error" not in data, data.get("error")
    bands = [f["attributes"] for f in data["features"]]
    assert bands, "brak strefy hałasu drogowego przy dworcu głównym"
    assert all({"ISOV1", "ISOV2"} <= set(band) for band in bands), bands
    loudest = max(bands, key=lambda band: band["ISOV1"])
    assert 50 <= loudest["ISOV1"] < loudest["ISOV2"] <= 100, loudest
    assert (loudest["ISOV2"] - loudest["ISOV1"]) == 5, f"strefy nie są już co 5 dB: {loudest}"


def test_noise_query_supports_jsonp():
    """Raport otwarty z pliku (file://) czyta odpowiedź przez JSONP — parametr callback."""
    resp = fetch(noise_query(html_map.NOISE_LAYERS[0][1], callback="reportCb0"), as_json=False)
    assert resp.status_code == 200, f"HTTP {resp.status_code}"
    text = resp.text.strip()
    assert text.startswith("reportCb0(") and text.endswith((")", ");")), text[:80]
    assert "features" in json.loads(text[text.index("(") + 1 : text.rindex(")")])

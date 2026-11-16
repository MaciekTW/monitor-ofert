# -*- coding: utf-8 -*-
"""Okolica mieszkań z OpenStreetMap — dane do raportu o mieszkaniu (report.js).

Dla prostokąta wokół ofert pobierane są przez Overpass API punkty, których szuka
przyszły mieszkaniec: sklepy, apteki, przychodnie, żłobki, szkoły, place zabaw,
restauracje itd. (CATEGORIES) oraz tereny zielone — parki i lasy — z obrysami,
bo do dużego parku liczy się odległość do jego granicy, a nie do środka.

Dane zmieniają się powoli, a publiczne serwery Overpass bywają przeciążone, więc
wynik leży w .cache/osm/ i jest odświeżany najwyżej raz na MAX_AGE_DAYS dni.
Gdy serwery nie odpowiadają, zostaje poprzednia kopia; bez niej (albo z --offline
bez kopii) mapa powstaje normalnie, a raport pomija sekcje o okolicy."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# publiczne instancje Overpass API, próbowane po kolei
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
BUDGET = 300  # sekundy na jedno zapytanie razem z ponawianiem; potem zostaje poprzednia kopia
RETRY_PAUSE = 5  # sekundy przed drugą próbą; potem dwa razy dłużej, najwyżej minuta
MIRROR_EVERY = 4  # co która próba idzie do serwera zapasowego
MIRROR_TIMEOUT = 60  # sekundy — serwer zapasowy, który milczy, nie może zjeść całego budżetu
QUERY_TIMEOUT = 180  # sekundy, które serwer może liczyć zapytanie
CACHE_FILE = Path(__file__).parents[1] / ".cache" / "osm" / "okolica.json"
MAX_AGE_DAYS = 30
# prostokąt zapytania jest zaokrąglany na zewnątrz do tylu stopni — żeby nowa
# oferta na skraju miasta nie unieważniała kopii przy każdym uruchomieniu
BOUNDS_STEP = 0.05

# tereny zielone: najmniejsza powierzchnia [ha] i odstęp punktów obrysu [m]
MIN_PARK_HA = 0.3
MIN_FOREST_HA = 2.0
OUTLINE_STEP_M = 50
M_PER_DEG = 111_320


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _fold(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower().replace("ł", "l"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _is_primary_school(tags: dict) -> bool:
    """Podstawówka po nazwie albo po poziomie ISCED 1 — OSM nie ma na nią osobnego tagu."""
    return "podstawow" in _fold(tags.get("name")) or "1" in tags.get("isced:level", "")


def _is_nursery(tags: dict) -> bool:
    return tags.get("amenity") == "childcare" or "zlob" in _fold(tags.get("name"))


# Kategorie punktów: id, etykieta w raporcie, sekcja raportu, klucz i wartości tagu OSM,
# "near" — odległość [m], do której punkt liczy się jako „pod ręką” (pełna ocena;
# dalej ocena maleje liniowo do zera przy trzykrotności), "w" — waga w ocenie sekcji
# (0 = tylko informacja, np. kluby nocne jako uciążliwość). "only" zawęża wartości tagu
# do elementów spełniających warunek; kategorie są sprawdzane po kolei i element trafia
# do pierwszej pasującej, więc żłobek nie jest liczony drugi raz jako przedszkole.
CATEGORIES = [
    {"id": "supermarket", "label": "Supermarket / dyskont", "sec": "daily", "near": 500, "w": 3,
     "key": "shop", "values": ("supermarket",)},
    {"id": "convenience", "label": "Sklep osiedlowy", "sec": "daily", "near": 300, "w": 2,
     "key": "shop", "values": ("convenience",)},
    {"id": "bakery", "label": "Piekarnia", "sec": "daily", "near": 400, "w": 1,
     "key": "shop", "values": ("bakery",)},
    {"id": "pharmacy", "label": "Apteka", "sec": "daily", "near": 500, "w": 2,
     "key": "amenity", "values": ("pharmacy",)},
    {"id": "parcel", "label": "Automat paczkowy", "sec": "daily", "near": 300, "w": 1,
     "key": "amenity", "values": ("parcel_locker",)},
    {"id": "post", "label": "Poczta", "sec": "daily", "near": 800, "w": 1,
     "key": "amenity", "values": ("post_office",)},
    {"id": "bank", "label": "Bank / bankomat", "sec": "daily", "near": 500, "w": 1,
     "key": "amenity", "values": ("atm", "bank")},
    {"id": "mall", "label": "Centrum handlowe", "sec": "daily", "near": 1500, "w": 1,
     "key": "shop", "values": ("mall",)},
    {"id": "marketplace", "label": "Targowisko", "sec": "daily", "near": 1200, "w": 1,
     "key": "amenity", "values": ("marketplace",)},
    {"id": "clinic", "label": "Przychodnia / lekarz", "sec": "health", "near": 600, "w": 3,
     "key": "amenity", "values": ("clinic", "doctors")},
    {"id": "dentist", "label": "Dentysta", "sec": "health", "near": 600, "w": 1,
     "key": "amenity", "values": ("dentist",)},
    {"id": "hospital", "label": "Szpital", "sec": "health", "near": 2000, "w": 2,
     "key": "amenity", "values": ("hospital",)},
    {"id": "vet", "label": "Weterynarz", "sec": "health", "near": 1000, "w": 0.5,
     "key": "amenity", "values": ("veterinary",)},
    {"id": "nursery", "label": "Żłobek", "sec": "education", "near": 700, "w": 2,
     "key": "amenity", "values": ("childcare", "kindergarten"), "only": _is_nursery},
    {"id": "kindergarten", "label": "Przedszkole", "sec": "education", "near": 600, "w": 3,
     "key": "amenity", "values": ("kindergarten",)},
    {"id": "school", "label": "Szkoła podstawowa", "sec": "education", "near": 800, "w": 3,
     "key": "amenity", "values": ("school",), "only": _is_primary_school},
    {"id": "highschool", "label": "Szkoła średnia", "sec": "education", "near": 1200, "w": 1,
     "key": "amenity", "values": ("school",)},
    {"id": "university", "label": "Uczelnia", "sec": "education", "near": 2000, "w": 1,
     "key": "amenity", "values": ("university", "college")},
    {"id": "library", "label": "Biblioteka", "sec": "education", "near": 800, "w": 1,
     "key": "amenity", "values": ("library",)},
    {"id": "playground", "label": "Plac zabaw", "sec": "education", "near": 300, "w": 2,
     "key": "leisure", "values": ("playground",)},
    {"id": "fitness", "label": "Siłownia / fitness", "sec": "leisure", "near": 800, "w": 1.5,
     "key": "leisure", "values": ("fitness_centre",)},
    {"id": "sports", "label": "Obiekt sportowy / basen", "sec": "leisure", "near": 1200, "w": 1,
     "key": "leisure", "values": ("sports_centre", "water_park")},
    {"id": "dogpark", "label": "Wybieg dla psów", "sec": "leisure", "near": 800, "w": 0.5,
     "key": "leisure", "values": ("dog_park",)},
    {"id": "restaurant", "label": "Restauracja", "sec": "fun", "near": 500, "w": 2,
     "key": "amenity", "values": ("restaurant",)},
    {"id": "cafe", "label": "Kawiarnia", "sec": "fun", "near": 500, "w": 1.5,
     "key": "amenity", "values": ("cafe",)},
    {"id": "bar", "label": "Bar / pub", "sec": "fun", "near": 600, "w": 1,
     "key": "amenity", "values": ("bar", "pub")},
    {"id": "cinema", "label": "Kino", "sec": "fun", "near": 1500, "w": 1,
     "key": "amenity", "values": ("cinema",)},
    {"id": "theatre", "label": "Teatr", "sec": "fun", "near": 2000, "w": 0.5,
     "key": "amenity", "values": ("theatre",)},
    {"id": "nightclub", "label": "Klub nocny", "sec": "noise", "near": 0, "w": 0,
     "key": "amenity", "values": ("nightclub",)},
]  # fmt: skip
# tereny zielone są osobno (obrysy zamiast punktów), ale w raporcie oceniane tak samo
GREEN = {"id": "green", "label": "Park / las (od 1 ha)", "sec": "leisure", "near": 500, "w": 4}


def _bbox(bounds: tuple[float, float, float, float]) -> str:
    return ",".join(f"{value:.4f}" for value in bounds)


def points_query(bounds: tuple[float, float, float, float]) -> str:
    """Zapytanie Overpass o wszystkie kategorie naraz; bounds = (lat min, lon min, lat max, lon max)."""
    by_key: dict[str, set] = {}
    for cat in CATEGORIES:
        by_key.setdefault(cat["key"], set()).update(cat["values"])
    filters = "".join(
        f'nwr[{key}~"^({"|".join(sorted(values))})$"];' for key, values in sorted(by_key.items())
    )
    return f"[out:json][timeout:{QUERY_TIMEOUT}][bbox:{_bbox(bounds)}];({filters});out center tags;"


def green_query(bounds: tuple[float, float, float, float]) -> str:
    return (
        f"[out:json][timeout:{QUERY_TIMEOUT}][bbox:{_bbox(bounds)}];"
        # łąki i błonia tylko nazwane (Błonia Krakowskie), bo bezimiennych łąk są tysiące;
        # „out geom” bez „tags”, bo skrócona postać gubi odcinki, z których składają się relacje
        '(wr[leisure~"^(park|nature_reserve)$"];wr[landuse=forest];wr[natural=wood];'
        'wr[landuse~"^(meadow|recreation_ground|village_green)$"][name];);out geom;'
    )


def overpass(query: str) -> list[dict]:
    """Elementy z odpowiedzi Overpass. Serwery publiczne często odpowiadają „zajęte”
    (HTTP 429/502/504) — wtedy zapytanie jest ponawiane z rosnącą przerwą, na zmianę
    na kolejnych instancjach, ale łącznie nie dłużej niż BUDGET sekund: generowanie
    mapy nie może utknąć na danych, bez których raport i tak sobie poradzi."""
    body = urlencode({"data": query}).encode()
    deadline = time.monotonic() + BUDGET
    pause, problem = RETRY_PAUSE, "brak odpowiedzi"
    for attempt in range(1000):
        # główny serwer kilka razy z rzędu (odmawia od razu, więc to tanie), zapasowy co MIRROR_EVERY próbę
        mirror = attempt % MIRROR_EVERY == MIRROR_EVERY - 1
        url = (
            OVERPASS_URLS[1 + (attempt // MIRROR_EVERY) % (len(OVERPASS_URLS) - 1)]
            if mirror
            else OVERPASS_URLS[0]
        )
        left = deadline - time.monotonic()
        if left <= 5:
            break
        try:
            request = Request(url, data=body, headers={"User-Agent": "monitor-ofert"})
            with urlopen(
                request, timeout=min(left, MIRROR_TIMEOUT if mirror else QUERY_TIMEOUT + 30)
            ) as resp:
                data = json.load(resp)
            # serwer, któremu skończył się czas, odpowiada 200 z pustą listą i uwagą
            if data.get("elements") or not data.get("remark"):
                return data.get("elements") or []
            problem = f"{data['remark']} ({url})"
        except HTTPError as exc:
            problem = f"HTTP {exc.code} ({url})"
        except (URLError, OSError, ValueError) as exc:
            problem = f"{exc} ({url})"
        wait = min(pause, deadline - time.monotonic() - 5)
        if wait <= 0:
            break
        log(f"  … {problem}; ponawiam za {wait:.0f} s")
        time.sleep(wait)
        pause = min(pause * 2, 60)
    raise RuntimeError(problem)


def _center(element: dict) -> tuple[float, float] | None:
    spot = element if element.get("type") == "node" else element.get("center")
    if not spot or "lat" not in spot:
        return None
    return spot["lat"], spot["lon"]


def classify_points(elements: list[dict]) -> dict[str, list]:
    """Elementy Overpass → {id kategorii: [[lat, lon, nazwa], …]}. Ten sam obiekt
    zmapowany dwa razy (punkt i budynek) zlewa się po nazwie i miejscu (~10 m)."""
    result: dict[str, list] = {cat["id"]: [] for cat in CATEGORIES}
    seen = set()
    for element in elements:
        tags, center = element.get("tags") or {}, _center(element)
        if center is None or tags.get("access") == "private":
            continue
        for cat in CATEGORIES:
            if tags.get(cat["key"]) in cat["values"] and ("only" not in cat or cat["only"](tags)):
                break
        else:
            continue
        name = tags.get("name") or tags.get("brand") or tags.get("operator") or ""
        key = (cat["id"], _fold(name), round(center[0], 4), round(center[1], 4))
        if key not in seen:
            seen.add(key)
            result[cat["id"]].append([round(center[0], 5), round(center[1], 5), name])
    for points in result.values():
        points.sort()
    return result


def _rings(element: dict) -> list[list[tuple[float, float]]]:
    """Zamknięte obrysy elementu: droga jest jednym, relacja składa swoje zewnętrzne
    odcinki w pierścienie (łącząc je końcami)."""
    if element.get("type") == "way":
        parts = [[(p["lat"], p["lon"]) for p in element.get("geometry") or []]]
    else:
        parts = [
            [(p["lat"], p["lon"]) for p in member.get("geometry") or []]
            for member in element.get("members") or []
            if member.get("type") == "way" and member.get("role") in ("outer", "")
        ]
    parts = [part for part in parts if len(part) >= 2]
    rings = []
    while parts:
        ring = parts.pop()
        while ring[0] != ring[-1]:
            for i, part in enumerate(parts):
                if part[0] == ring[-1]:
                    ring += parts.pop(i)[1:]
                    break
                if part[-1] == ring[-1]:
                    ring += parts.pop(i)[-2::-1]
                    break
            else:
                break  # niedomknięty obrys (ucięty granicą zapytania) — pomijamy
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    return lon * M_PER_DEG * math.cos(math.radians(lat0)), lat * M_PER_DEG


def _area_m2(ring: list[tuple[float, float]]) -> float:
    points = [_xy(lat, lon, ring[0][0]) for lat, lon in ring]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:]))) / 2


def _outline(ring: list[tuple[float, float]]) -> list[list[float]]:
    """Obrys jako punkty co najwyżej co OUTLINE_STEP_M — także na długich prostych
    odcinkach, żeby odległość do granicy parku nie zależała od tego, jak go narysowano."""
    points = []
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:]):
        (x1, y1), (x2, y2) = _xy(lat1, lon1, lat1), _xy(lat2, lon2, lat1)
        steps = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / OUTLINE_STEP_M))
        for step in range(steps):
            share = step / steps
            points.append([round(lat1 + (lat2 - lat1) * share, 5), round(lon1 + (lon2 - lon1) * share, 5)])
    return points


def classify_green(elements: list[dict]) -> list[list]:
    """Parki i lasy → [[nazwa, powierzchnia w ha, [[lat, lon], …] punkty obrysu], …],
    od największych; za małe (MIN_PARK_HA, MIN_FOREST_HA) są pomijane."""
    areas = []
    for element in elements:
        tags = element.get("tags") or {}
        if tags.get("access") == "private":
            continue
        park = tags.get("landuse") not in ("forest",) and tags.get("natural") != "wood"
        rings = _rings(element)
        hectares = sum(_area_m2(ring) for ring in rings) / 10_000
        if not rings or hectares < (MIN_PARK_HA if park else MIN_FOREST_HA):
            continue
        outline = [point for ring in rings for point in _outline(ring)]
        areas.append([tags.get("name") or ("park" if park else "las"), round(hectares, 1), outline])
    areas.sort(key=lambda area: (-area[1], area[0]))
    return [area for area in areas if walkable_green(area[0])]


def walkable_green(name: str) -> bool:
    """Park krajobrazowy to obszar ochrony obejmujący całe dzielnice razem z zabudową,
    a nie miejsce na spacer — w OSM bywa jednak oznaczony tak samo jak rezerwat."""
    return "krajobrazow" not in _fold(name)


def _version() -> str:
    """Odcisk tego, co wpływa na zawartość kopii — zmiana kategorii albo progów ją unieważnia."""
    spec = [points_query((0, 0, 0, 0)), green_query((0, 0, 0, 0)), MIN_PARK_HA, MIN_FOREST_HA, OUTLINE_STEP_M]
    spec += [(cat["id"], "only" in cat) for cat in CATEGORIES]
    return hashlib.sha1(json.dumps(spec).encode()).hexdigest()[:12]


def query_bounds(bounds: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    lat_min, lon_min, lat_max, lon_max = bounds
    return (
        math.floor(lat_min / BOUNDS_STEP) * BOUNDS_STEP,
        math.floor(lon_min / BOUNDS_STEP) * BOUNDS_STEP,
        math.ceil(lat_max / BOUNDS_STEP) * BOUNDS_STEP,
        math.ceil(lon_max / BOUNDS_STEP) * BOUNDS_STEP,
    )


def _covers(outer: list, inner: tuple) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _read_cache() -> dict | None:
    try:
        cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cached if cached.get("version") == _version() else None


# Kategorie, które raport bierze z warstw mapy (centra handlowe, targowiska, kina, teatry
# — sekcja „Wybrane obiekty z mapy”), więc nie powtarza ich w sekcjach z OpenStreetMap.
# Zostają w zapytaniu i w kopii, żeby zmiana tej listy nie wymuszała pobierania od nowa.
FROM_MAP_LAYERS = frozenset({"mall", "marketplace", "cinema", "theatre"})


def categories() -> list[dict]:
    """Opis kategorii dla strony (bez części dotyczącej zapytania)."""
    return [
        {k: cat[k] for k in ("id", "label", "sec", "near", "w")}
        for cat in [*CATEGORIES, GREEN]
        if cat["id"] not in FROM_MAP_LAYERS
    ]


def load(bounds: tuple[float, float, float, float] | None, offline: bool = False) -> dict | None:
    """Dane okolicy dla raportu: {"fetched": data pobrania, "bounds": prostokąt danych,
    "cats": opis kategorii, "pts": {id: [[lat, lon, nazwa], …]}, "green": tereny zielone}
    albo None, gdy danych nie ma i nie da się ich pobrać. bounds to prostokąt wokół ofert."""
    cached = _read_cache()
    if bounds is None:
        return None
    if cached is not None:
        cached["cats"] = categories()  # etykiety i progi zawsze z bieżącego kodu
        cached["green"] = [area for area in cached["green"] if walkable_green(area[0])]
    wanted = query_bounds(bounds)
    usable = cached is not None and _covers(cached["bounds"], wanted)
    if offline:
        if cached is None:
            log("⚠ Brak zapisanych danych OpenStreetMap — raport o mieszkaniu będzie bez sekcji o okolicy.")
        return cached
    if usable and (date.today() - date.fromisoformat(cached["fetched"])).days < MAX_AGE_DAYS:
        return cached
    log(
        "Pobieram dane o okolicy z OpenStreetMap (sklepy, szkoły, parki…) — zwykle minutę, przy zajętych serwerach do 10."
    )
    try:
        data = {
            "version": _version(),
            "fetched": date.today().isoformat(),
            "saved": datetime.now().isoformat(timespec="seconds"),
            "bounds": list(wanted),
            "cats": categories(),
            "pts": classify_points(overpass(points_query(wanted))),
            "green": classify_green(overpass(green_query(wanted))),
        }
    except RuntimeError as exc:
        log(
            f"⚠ OpenStreetMap (Overpass) nie odpowiada: {exc} — "
            + ("używam poprzedniej kopii." if cached else "raport o mieszkaniu będzie bez sekcji o okolicy.")
        )
        return cached
    if not any(data["pts"].values()):
        log(
            "⚠ OpenStreetMap zwrócił pusty wynik — " + ("używam poprzedniej kopii." if cached else "pomijam.")
        )
        return cached
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(CACHE_FILE)
    return data


# ---------------------------------------------------------------- sieć piesza
# Raport liczy odległości pieszo po prawdziwych ulicach, chodnikach i ścieżkach, a nie
# w linii prostej: rzeka, tory czy ogrodzone osiedle potrafią zrobić z „300 m” kwadrans
# marszu. Sieć pochodzi z OpenStreetMap i jest pobierana kawałkami (WALK_TILE), bo całe
# miasto naraz to ponad 100 MB odpowiedzi — za dużo na jedno zapytanie do zajętego
# serwera. Każdy kawałek ma własną kopię w .cache/osm/siec/, więc przerwane pobieranie
# jest przy następnym generowaniu mapy kontynuowane, a nie zaczynane od nowa.
WALK_DIR = CACHE_FILE.parent / "siec"
WALK_GRAPH = CACHE_FILE.parent / "siec.json"
WALK_TILE = (0.05, 0.1)  # rozmiar kawałka w stopniach (szerokość, długość geograficzna)
WALK_MAX_AGE_DAYS = 90  # ulice zmieniają się wolniej niż sklepy
WALK_BUDGET = 900  # sekundy na pobieranie sieci w jednym uruchomieniu; reszta następnym razem
# po czym da się iść: wszystko poza autostradami i drogami ekspresowymi
WALK_HIGHWAYS = (
    "footway", "path", "pedestrian", "steps", "living_street", "residential", "service", "unclassified",
    "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link", "track",
    "cycleway", "road", "platform", "corridor",
)  # fmt: skip
# Na stronę trafia graf: wierzchołki co najwyżej co tyle metrów wzdłuż drogi (plus wszystkie
# skrzyżowania) — gęściej nie trzeba, bo punkt i tak jest przypinany do najbliższego wierzchołka
WALK_NODE_STEP_M = 35


def walk_query(bounds: tuple[float, float, float, float]) -> str:
    """Drogi, po których wolno iść: bez zakazu ruchu pieszych i bez dróg prywatnych
    (chyba że pieszym wyraźnie wolno), bez podjazdów do posesji."""
    closed = '"^(no|private)$"'
    return (
        f"[out:json][timeout:{QUERY_TIMEOUT}][bbox:{_bbox(bounds)}];("
        f'way[highway~"^({"|".join(WALK_HIGHWAYS)})$"][foot!~{closed}][access!~{closed}][service!=driveway];'
        f'way[highway][access~{closed}][foot~"^(yes|designated|permissive)$"];'
        ");out skel geom qt;"
    )


def walk_tiles(bounds: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """Kawałki siatki WALK_TILE pokrywające prostokąt bounds."""
    lat_step, lon_step = WALK_TILE
    lat_min, lon_min, lat_max, lon_max = bounds
    tiles = []
    for i in range(math.floor(lat_min / lat_step + 1e-9), math.ceil(lat_max / lat_step - 1e-9)):
        for j in range(math.floor(lon_min / lon_step + 1e-9), math.ceil(lon_max / lon_step - 1e-9)):
            tiles.append(
                (
                    round(i * lat_step, 4),
                    round(j * lon_step, 4),
                    round((i + 1) * lat_step, 4),
                    round((j + 1) * lon_step, 4),
                )
            )
    return tiles


def _tile_file(tile: tuple[float, float, float, float]) -> Path:
    return WALK_DIR / f"{tile[0]:.2f}_{tile[1]:.2f}.json.gz"


def compact_ways(elements: list[dict]) -> dict[str, list]:
    """Odpowiedź Overpass → {id drogi: [id węzła, lat, lon, id węzła, lat, lon, …]}
    (współrzędne w milionowych stopnia) — tyle wystarcza, żeby złożyć graf."""
    ways = {}
    for element in elements:
        nodes, geometry = element.get("nodes") or [], element.get("geometry") or []
        if element.get("type") != "way" or len(nodes) < 2 or len(nodes) != len(geometry):
            continue
        flat = []
        for node, point in zip(nodes, geometry):
            if point is None:  # węzeł poza zasięgiem odpowiedzi — nie powinno się zdarzać przy „out geom”
                flat = []
                break
            flat += [node, round(point["lat"] * 1e6), round(point["lon"] * 1e6)]
        if flat:
            ways[str(element["id"])] = flat
    return ways


def build_walk_graph(ways: dict[str, list]) -> dict:
    """Drogi → graf do raportu: {"nodes": [lat, lon, …] w stutysięcznych stopnia,
    zapisane różnicowo (każda para to przyrost względem poprzedniej), "edges":
    [a, b, metry, …]}. Zostaje największy spójny kawałek sieci — alejka na zamkniętym
    podwórku, z której nie da się nigdzie dojść, tylko myliłaby przypinanie punktów."""
    used: dict[int, int] = {}
    for flat in ways.values():
        for node in flat[::3]:
            used[node] = used.get(node, 0) + 1
    coords: dict[int, tuple[int, int]] = {}
    edges: list[tuple[int, int, float]] = []
    for flat in ways.values():
        last, since = None, 0.0
        for k in range(0, len(flat), 3):
            node, lat, lon = flat[k], flat[k + 1], flat[k + 2]
            if k:
                plat, plon = flat[k - 2], flat[k - 1]
                dy = (lat - plat) * M_PER_DEG / 1e6
                dx = (lon - plon) * M_PER_DEG / 1e6 * math.cos(math.radians(lat / 1e6))
                since += math.hypot(dx, dy)
            end = k == 0 or k == len(flat) - 3
            if end or used[node] > 1 or since >= WALK_NODE_STEP_M:
                coords[node] = (lat, lon)
                if last is not None and last != node:
                    edges.append((last, node, since))
                last, since = node, 0.0

    # największa spójna składowa (union-find)
    parent = {node: node for node in coords}

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a, b, _ in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    sizes: dict[int, int] = {}
    for node in coords:
        root = find(node)
        sizes[root] = sizes.get(root, 0) + 1
    if not sizes:
        return {"nodes": [], "edges": []}
    main = max(sizes, key=sizes.get)
    kept = sorted((node for node in coords if find(node) == main), key=lambda node: coords[node])
    index = {node: i for i, node in enumerate(kept)}
    nodes, plat, plon = [], 0, 0
    for node in kept:
        lat, lon = round(coords[node][0] / 10), round(coords[node][1] / 10)
        nodes += [lat - plat, lon - plon]
        plat, plon = lat, lon
    flat_edges = []
    seen = set()
    for a, b, meters in edges:
        if a not in index or b not in index:
            continue
        pair = (min(index[a], index[b]), max(index[a], index[b]))
        length = max(1, round(meters))
        if (pair, length) in seen:
            continue
        seen.add((pair, length))
        flat_edges += [pair[0], pair[1], length]
    return {"nodes": nodes, "edges": flat_edges}


def _fresh(path: Path, days: int) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < days * 86400


def load_walk(bounds: tuple[float, float, float, float] | None, offline: bool = False) -> dict | None:
    """Graf sieci pieszej dla prostokąta wokół ofert (postać jak w build_walk_graph,
    plus "tiles": ile kawałków jest w grafie i ile powinno być) albo None, gdy nie ma
    ani jednego kawałka. Brakujące i przeterminowane kawałki są dociągane, dopóki
    starcza WALK_BUDGET; z tym, co jest, raport liczy trasy, a poza pobranym obszarem
    wraca do odległości w linii prostej."""
    if bounds is None:
        return None
    tiles = walk_tiles(query_bounds(bounds))
    if not offline:
        deadline = time.monotonic() + WALK_BUDGET
        missing = [tile for tile in tiles if not _fresh(_tile_file(tile), WALK_MAX_AGE_DAYS)]
        if missing:
            log(
                f"Pobieram sieć ulic i chodników z OpenStreetMap: {len(missing)} z {len(tiles)} fragmentów do pobrania."
            )
        for number, tile in enumerate(missing, 1):
            if time.monotonic() > deadline:
                log(
                    f"⚠ Koniec czasu na sieć ulic — pozostałe {len(missing) - number + 1} fragmenty pobiorę przy następnym generowaniu mapy."
                )
                break
            try:
                ways = compact_ways(overpass(walk_query(tile)))
            except RuntimeError as exc:
                log(f"⚠ Fragment sieci ulic {number}/{len(missing)} niepobrany ({exc}).")
                continue
            if not ways:
                continue  # pusty wynik to raczej kłopot serwera niż miasto bez ulic
            WALK_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _tile_file(tile).with_suffix(".part")
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                json.dump(ways, f, separators=(",", ":"))
            tmp.replace(_tile_file(tile))
            log(f"  fragment {number}/{len(missing)}: {len(ways)} dróg")
    have = [tile for tile in tiles if _tile_file(tile).exists()]
    if not have:
        log("⚠ Brak sieci ulic z OpenStreetMap — raport policzy dojścia w linii prostej.")
        return None
    # złożony graf też ma kopię: zależy tylko od tego, które kawałki są i z kiedy
    stamp = [
        WALK_NODE_STEP_M,
        *((_tile_file(tile).name, int(_tile_file(tile).stat().st_mtime)) for tile in have),
    ]
    try:
        cached = json.loads(WALK_GRAPH.read_text(encoding="utf-8"))
        if cached.get("stamp") == json.loads(json.dumps(stamp)):
            return cached["graph"]
    except (OSError, ValueError):
        pass
    ways: dict[str, list] = {}
    for tile in have:
        with gzip.open(_tile_file(tile), "rt", encoding="utf-8") as f:
            ways.update(json.load(f))  # droga na granicy kawałków jest w obu — po id zostaje raz
    graph = {**build_walk_graph(ways), "tiles": [len(have), len(tiles)]}
    tmp = WALK_GRAPH.with_suffix(".part")
    tmp.write_text(json.dumps({"stamp": stamp, "graph": graph}, separators=(",", ":")), encoding="utf-8")
    tmp.replace(WALK_GRAPH)
    return graph

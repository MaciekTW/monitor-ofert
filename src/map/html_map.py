# -*- coding: utf-8 -*-
"""
Interaktywna mapa HTML — generuje pojedynczy, samowystarczalny plik HTML
z mapą (Leaflet), listą i filtrami aktywnych ofert zapisanych w bazie
oraz drugą zakładką z historią rynku (liczba ofert, ceny i ich zmiany w czasie).

Wygląd i logika strony są w katalogu template/ (szablon Jinja2 index.html
oraz style.css, app.js, history.js i krakow_boundary.js wklejane do niego bez zmian;
dodatkowe warstwy punktów, np. lodziarnie, i granice dzielnic w template/layers/;
przystanki komunikacji miejskiej z modułu gtfs).
Zewnętrzne biblioteki
(Leaflet, Apache ECharts, Tailwind CSS w wersji przeglądarkowej) leżą w katalogu deps/
w głównym katalogu repozytorium.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

import gtfs


def log(msg: str = "") -> None:
    print(msg, flush=True)


def meta_get(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


TEMPLATE_DIR = Path(__file__).parent / "template"
DEPS_DIR = Path(__file__).parents[2] / "deps"  # np. js/leaflet.js, css/leaflet.css
LAYERS_DIR = TEMPLATE_DIR / "layers"

# Dodatkowe warstwy punktów na mapie, włączane i wyłączane w całości przyciskiem
# warstw. Każda to plik GeoJSON z punktami (np. eksport z overpass-turbo)
# i ikona SVG z katalogu template/layers/. Warstwy z tym samym "group" trafiają
# w panelu do wspólnej sekcji z nagłówkiem; bez "group" są na górze panelu.
# "shape": "square" rysuje marker jako zaokrąglony kwadrat zamiast koła
# (dla kwadratowych logo, którym koło ucina rogi), a "plain" zdejmuje białą
# podkładkę i zostawia sam rysunek (dla ikon, które nie są logotypem).
# "visible": False sprawia, że warstwa jest po otwarciu mapy wyłączona
# i trzeba ją zaznaczyć w panelu. "brands" i "brands_except" zawężają plik do
# punktów z danym tagiem brand:wikidata (albo do całej reszty), więc jeden
# GeoJSON może dać kilka osobno przełączanych warstw. Warstwa o kilku punktach
# (jak zoo) może zamiast "geojson" podać "features" z obiektami GeoJSON wpisanymi
# wprost — czytane są tak samo, więc osobny plik nie jest potrzebny.

# Kina sieciowe rozpoznawane po tagu brand:wikidata z OSM: Cinema City
# i Multikino. Kina bez tej marki (albo bez tagu) są na mapie jako studyjne.
MULTIPLEX_BRANDS = ("Q543651", "Q1144802")

# Zoo jest w Krakowie jedno, więc zamiast pliku wystarczy jeden obiekt GeoJSON
# (way/25171269 z OSM). Z tagów zostały te, które mapa pokazuje w dymku:
# nazwa, adres i godziny otwarcia.
ZOO = [
    {
        "type": "Feature",
        "properties": {
            "@id": "way/25171269",
            "addr:street": "Aleja Kasy Oszczędności Miasta Krakowa",
            "name": "Ogród Zoologiczny w Krakowie",
            "opening_hours": "Mo-Su 09:00-18:00",
        },
        "geometry": {"type": "Point", "coordinates": [19.8500484, 50.0531577]},
    },
]

POI_LAYERS = [
    {
        "id": "goodlood",
        "label": "Lodziarnie Good Lood",
        "geojson": "goodlood.geojson",
        "icon": "goodlood.svg",
    },
    {
        "id": "lidl",
        "label": "Lidl",
        "group": "Markety",
        "visible": False,
        "geojson": "lidl.geojson",
        "icon": "lidl.svg",
    },
    {
        "id": "kaufland",
        "label": "Kaufland",
        "group": "Markety",
        "visible": False,
        "shape": "square",
        "geojson": "kaufland.geojson",
        "icon": "kaufland.svg",
    },
    {
        "id": "auchan",
        "label": "Auchan",
        "group": "Markety",
        "geojson": "auchan.geojson",
        "icon": "auchan.svg",
    },
    {
        "id": "marketplace",
        "label": "Targowiska",
        "group": "Handel",
        "visible": False,
        # rysunek pinezki, nie logotyp — bez białej podkładki pod ikoną
        "shape": "plain",
        "geojson": "marketplaces.geojson",
        "icon": "pin-targowisko.svg",
    },
    {
        "id": "theatre",
        "label": "Teatry",
        "group": "Rozrywka",
        "visible": False,
        # rysunek budynku, nie logotyp — bez białej podkładki pod ikoną
        "shape": "plain",
        "geojson": "theatres.geojson",
        "icon": "theatre.svg",
    },
    {
        "id": "multiplex",
        "label": "Kina — multipleksy",
        "group": "Rozrywka",
        "visible": False,
        "shape": "plain",
        "geojson": "cinemas.geojson",
        "icon": "kino-popcorn.svg",
        "brands": MULTIPLEX_BRANDS,
    },
    {
        "id": "arthouse",
        "label": "Kina studyjne",
        "group": "Rozrywka",
        "visible": False,
        "shape": "plain",
        "geojson": "cinemas.geojson",
        "icon": "kino-popcorn.svg",
        "brands_except": MULTIPLEX_BRANDS,
    },
    {
        "id": "zoo",
        "label": "Zoo",
        "group": "Rozrywka",
        "visible": False,
        # rysunek zwierzęcia, nie logotyp — bez białej podkładki pod ikoną
        "shape": "plain",
        "features": ZOO,
        "icon": "zoo-akcent.svg",
    },
]


# Przystanki komunikacji miejskiej z rozkładów GTFS ZTP (moduł gtfs), z ikonami
# z template/layers/. Na mapie mają osobny przycisk i panel (app.js).
TRANSIT_LAYERS = [
    {"id": "bus", "label": "Przystanki autobusowe", "icon": "bus.svg"},
    {"id": "tram", "label": "Przystanki tramwajowe", "icon": "tram.svg"},
]

# Granice dzielnic Krakowa: obrysy 18 dzielnic z miejskiego portalu danych
# (template/layers/districts.geojson). Plik jest już w WGS84 — przeliczenie
# z układu 2000 strefa 7 (EPSG:2178), uproszczenie obrysów (~6 m) i obcięcie
# zbędnych atrybutów zrobiono raz, przy dodawaniu pliku do repozytorium.
# Na mapie mają w panelu warstw własną sekcję i domyślnie są wyłączone.
DISTRICTS_GEOJSON = "districts.geojson"


def make_env() -> Environment:
    """Środowisko Jinja2: szablony z template/, biblioteki z deps/."""
    env = Environment(
        loader=FileSystemLoader([TEMPLATE_DIR, DEPS_DIR]),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    # tojson zamienia <, >, & i ' na sekwencje \uXXXX, więc dane są bezpieczne
    # wewnątrz <script>; ensure_ascii=False trzyma polskie znaki w UTF-8
    env.policies["json.dumps_kwargs"] = {
        "ensure_ascii": False,
        "separators": (",", ":"),
    }

    def include_raw(name: str) -> Markup:
        """Wkleja plik szablonu bez przetwarzania przez Jinja (CSS/JS mogą
        zawierać sekwencje w rodzaju {# czy {{, które Jinja by zinterpretowała)."""
        return Markup(env.loader.get_source(env, name)[0])

    env.globals["include_raw"] = include_raw
    return env


def strip_html(text: str, limit: int = 4000) -> str:
    """HTML opisu → czysty tekst (z zachowaniem akapitów), przycięty do limitu."""
    if not text:
        return ""
    text = re.sub(r"<\s*br\s*/?\s*>|</\s*p\s*>|</\s*li\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " […]"
    return text


def photo_urls(raw_offer: dict, max_photos: int = 8) -> list[str]:
    urls = []
    for photo in (raw_offer.get("photos") or [])[:max_photos]:
        link = photo.get("link") if isinstance(photo, dict) else None
        if not link:
            continue
        urls.append(link.replace("{width}", "1000").replace("{height}", "700"))
    return urls


def poi_address(props: dict) -> str:
    """Adres z tagów OSM: ulica (albo osiedle/plac) z numerem,
    a dla punktów spoza Krakowa także miejscowość."""
    street = props.get("addr:street") or props.get("addr:place") or ""
    address = " ".join(part for part in (street, props.get("addr:housenumber")) if part)
    city = props.get("addr:city")
    if city and city != "Kraków":
        address = f"{address}, {city}" if address else city
    return address


def icon_svg(name: str) -> str:
    """Ikona SVG z template/layers/ bez bloku <metadata> (manifest pochodzenia
    pliku), który tylko zwiększa rozmiar strony."""
    svg = (LAYERS_DIR / name).read_text(encoding="utf-8")
    return re.sub(r"<metadata>.*?</metadata>", "", svg, flags=re.DOTALL)


def icon_data_uri(name: str) -> str:
    """Ikona SVG z template/layers/ jako data URI."""
    return "data:image/svg+xml;base64," + base64.b64encode(icon_svg(name).encode("utf-8")).decode("ascii")


def poi_layers() -> list[dict]:
    """Warstwy z POI_LAYERS: ikona jako data URI i kompaktowa lista punktów
    [lat, lon, nazwa, adres, godziny otwarcia]."""
    layers = []
    for spec in POI_LAYERS:
        if "geojson" in spec:
            features = json.loads((LAYERS_DIR / spec["geojson"]).read_text(encoding="utf-8"))["features"]
        else:
            features = spec["features"]
        points = []
        for feature in features:
            geometry = feature.get("geometry") or {}
            if geometry.get("type") != "Point":
                continue
            lon, lat = geometry["coordinates"][:2]
            props = feature.get("properties") or {}
            brand = props.get("brand:wikidata")
            if spec.get("brands") and brand not in spec["brands"]:
                continue
            if spec.get("brands_except") and brand in spec["brands_except"]:
                continue
            name = " ".join(part for part in (props.get("name"), props.get("branch")) if part)
            points.append(
                [round(lat, 6), round(lon, 6), name, poi_address(props), props.get("opening_hours") or ""]
            )
        layers.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "group": spec.get("group"),
                "visible": spec.get("visible", True),
                "shape": spec.get("shape", "circle"),
                "icon": icon_data_uri(spec["icon"]),
                "pts": points,
            }
        )
    return layers


def point_in_ring(lat: float, lon: float, ring: list[list[float]]) -> bool:
    """Czy punkt leży wewnątrz obrysu (algorytm promienia poziomego)."""
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[-1:] + ring[:-1]):
        if (lat1 > lat) != (lat2 > lat) and lon < (lon2 - lon1) * (lat - lat1) / (lat2 - lat1) + lon1:
            inside = not inside
    return inside


def label_point(ring: list[list[float]]) -> list[float]:
    """Miejsce na podpis dzielnicy: środek ciężkości obrysu, a gdy wypada poza nim
    (dzielnice w kształcie podkowy) — środek najszerszego poziomego przekroju."""
    area = lat = lon = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[-1:] + ring[:-1]):
        cross = lon1 * lat2 - lon2 * lat1
        area += cross
        lat += (lat1 + lat2) * cross
        lon += (lon1 + lon2) * cross
    if area and point_in_ring(lat / (3 * area), lon / (3 * area), ring):
        return [round(lat / (3 * area), 6), round(lon / (3 * area), 6)]

    lats = [p[0] for p in ring]
    best = (0.0, ring[0])
    for step in range(1, 64):
        y = min(lats) + (max(lats) - min(lats)) * step / 64
        crossings = sorted(
            lon1 + (lon2 - lon1) * (y - lat1) / (lat2 - lat1)
            for (lat1, lon1), (lat2, lon2) in zip(ring, ring[-1:] + ring[:-1])
            if (lat1 > y) != (lat2 > y)
        )
        for left, right in zip(crossings[::2], crossings[1::2]):
            if right - left > best[0]:
                best = (right - left, [round(y, 6), round((left + right) / 2, 6)])
    return best[1]


def district_layers() -> list[dict]:
    """Granice dzielnic na mapę: [{"nr": numer, "name": nazwa,
    "rings": [[[lat, lon], …], …], "c": [lat, lon] pod podpis}, …]."""
    geojson = json.loads((LAYERS_DIR / DISTRICTS_GEOJSON).read_text(encoding="utf-8"))
    layers = []
    for feature in sorted(geojson["features"], key=lambda f: f["properties"]["id"]):
        props = feature["properties"]
        rings = [[[lat, lon] for lon, lat in ring] for ring in feature["geometry"]["coordinates"]]
        layers.append(
            {
                "nr": props["nr"],
                "name": props["nazwa"],
                "rings": rings,
                "c": label_point(max(rings, key=len)),
            }
        )
    return layers


def line_order(line: str):
    """Numery linii po kolei jak liczby (2 < 10 < 102); nieliczbowe, np. LR0, na końcu."""
    return (0, int(line), "") if line.isdigit() else (1, 0, line)


def transit_layers(offline: bool = False) -> dict:
    """Przystanki na mapę: {"day": dzień odniesienia dla odjazdów (RRRR-MM-DD),
    "routes": adres file:// skryptu z trasami linii (gtfs.write_routes), "layers": [...]},
    gdzie każda warstwa ma:
    - svg: ikonę z „{n}” w miejscu liczby linii (app.js wstawia liczbę),
    - lines: posortowane numery linii i night: indeksy linii nocnych,
    - stops: przystanki zagregowane [lat, lon, nazwa, numer przystanku, [[indeks linii, odjazdy], …]],
    - posts: pojedyncze słupki [lat, lon, nazwa, stop_code, [[indeks linii, odjazdy], …]]."""
    networks, day = gtfs.load(offline)
    routes = gtfs.write_routes(networks).as_uri() if networks else None
    layers = []
    for spec in TRANSIT_LAYERS:
        network = networks.get(spec["id"])
        if not network or not network["posts"]:
            continue
        posts = network["posts"]
        lines = sorted(set().union(*(p["lines"] for p in posts.values())), key=line_order)
        index = {line: i for i, line in enumerate(lines)}

        def departures(item: dict) -> list[list[int]]:
            return sorted([index[line], count] for line, count in item["lines"].items())

        svg = re.sub(
            r'(<text id="liczba-linii"[^>]*>).*?(</text>)',
            r"\1{n}\2",
            icon_svg(spec["icon"]),
            flags=re.DOTALL,
        )
        layers.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "svg": svg,
                "lines": lines,
                "night": sorted(index[line] for line in network["night"] if line in index),
                "stops": [
                    [round(s["lat"], 6), round(s["lon"], 6), s["name"], number, departures(s)]
                    for number, s in sorted(gtfs.group_posts(posts).items(), key=lambda item: item[1]["name"])
                ],
                "posts": [
                    [round(p["lat"], 6), round(p["lon"], 6), p["name"], code, departures(p)]
                    for code, p in sorted(posts.items())
                ],
            }
        )
    return {"day": day.isoformat() if day else None, "routes": routes, "layers": layers}


def market_history(con: sqlite3.Connection) -> dict:
    """Dane dla zakładki z historią rynku: lista skanów i kompaktowy opis
    każdej oferty (także wycofanych) z numerami skanów zamiast dat.

    Baza nie ma osobnego logu skanów, ale każdy skan zapisuje ten sam znacznik
    czasu w first_seen, last_seen i price_history.ts — zbiór tych wartości to
    lista skanów. Oferta była aktywna we wszystkich skanach od first_seen do
    last_seen, a cena w danym skanie to ostatni wpis historii nie późniejszy."""
    scans = sorted(
        ts
        for (ts,) in con.execute(
            "SELECT first_seen FROM offers UNION SELECT last_seen FROM offers UNION SELECT ts FROM price_history"
        )
        if ts
    )
    idx = {ts: i for i, ts in enumerate(scans)}
    prices: dict[str, list] = {}
    for offer_uid, ts, price in con.execute("SELECT offer_uid, ts, price FROM price_history ORDER BY ts"):
        if ts in idx and price is not None:
            prices.setdefault(offer_uid, []).append([idx[ts], price])

    rows = []
    query = "SELECT uid, source, market, district, business, area, first_seen, last_seen, active FROM offers"
    for uid, source, market, district, business, area, first_seen, last_seen, active in con.execute(query):
        if first_seen not in idx or last_seen not in idx:
            continue
        rows.append(
            [
                source,
                market,
                district,
                business,
                area,
                idx[first_seen],
                idx[last_seen],
                active,
                prices.get(uid, []),
            ]
        )
    return {"scans": scans, "offers": rows}


def export_html(con: sqlite3.Connection, path: str, offline: bool = False) -> None:
    """Buduje pojedynczy, samowystarczalny plik HTML z mapą, listą i filtrami.
    Lekkie dane (ceny, metraże, współrzędne, opisy) siedzą w pliku;
    zdjęcia dociągają się z serwerów OLX dopiero po otwarciu oferty.
    offline: przystanki tylko z zapisanej wcześniej kopii rozkładów GTFS."""
    history: dict[str, list] = {}
    for offer_uid, ts, price in con.execute("SELECT offer_uid, ts, price FROM price_history ORDER BY ts"):
        history.setdefault(offer_uid, []).append([ts[:10], price])

    offers = []
    query = """SELECT uid, source, url, title, price, negotiable, area,
                      price_per_m, rooms, floor, market, district, business,
                      created_at, first_seen, lat, lon, map_radius, raw
               FROM offers WHERE active = 1"""
    for (
        uid,
        source,
        url,
        title,
        price,
        negotiable,
        area,
        ppm,
        rooms,
        floor,
        market,
        district,
        business,
        created,
        first_seen,
        lat,
        lon,
        radius,
        raw,
    ) in con.execute(query):
        try:
            raw_offer = json.loads(raw) if raw else {}
        except ValueError:
            raw_offer = {}
        if source in ("otodom", "gratka"):
            # oba portale mają opis i zdjęcia dopiero na stronie oferty —
            # skrypt dociąga je raz i chowa w surowym JSON-ie
            detail = raw_offer.get("_ad") or raw_offer.get("_detail") or {}
            photos = [u for u in (detail.get("images") or [])[:8] if isinstance(u, str)]
            desc = strip_html(detail.get("description") or "")
        else:
            photos = photo_urls(raw_offer)
            desc = strip_html(raw_offer.get("description") or "")
        item = {
            "id": uid,
            "s": source,
            "u": url,
            "t": title,
            "p": price,
            "ng": negotiable,
            "a": area,
            "pm": ppm,
            "r": rooms,
            "f": floor,
            "mk": market,
            "d": district,
            "b": business,
            "c": created,
            "fs": first_seen,
            "lat": lat,
            "lon": lon,
            "rad": radius or 0,
            "ph": photos,
            "dsc": desc,
        }
        hist = history.get(uid) or []
        if len(hist) > 1:
            item["h"] = hist
        offers.append(item)

    if not offers:
        raise RuntimeError(
            "Baza nie zawiera aktywnych ofert — najpierw uruchom skrypt bez --offline, żeby pobrać dane."
        )

    cities = Counter(row[0] for row in con.execute("SELECT city FROM offers WHERE active = 1") if row[0])
    meta = {
        "city": cities.most_common(1)[0][0] if cities else "OLX",
        "gen": datetime.now().isoformat(timespec="minutes"),
        "url": meta_get(con, "search_url") or "",
        "total": len(offers),
        # klucz do kafelków CARTO; bez niego kafelki mają napis „API KEY REQUIRED”
        "carto": os.environ.get("CARTO_API_KEY", "").strip(),
    }
    if not meta["carto"]:
        log(
            "⚠ Brak zmiennej CARTO_API_KEY (np. w pliku .env) — mapa zadziała, ale kafelki "
            "będą miały napis „API KEY REQUIRED”. Darmowy klucz: https://carto.com/basemaps/apikey"
        )

    page = (
        make_env()
        .get_template("index.html")
        .render(
            meta=meta,
            offers=offers,
            hist=market_history(con),
            pois=poi_layers(),
            districts=district_layers(),
            transit=transit_layers(offline),
        )
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    size_mb = len(page.encode("utf-8")) / 1_048_576
    log(
        f"\n✔ Zapisano interaktywną mapę {len(offers)} ofert do „{path}” "
        f"({size_mb:.1f} MB). Otwórz ten plik w przeglądarce."
    )

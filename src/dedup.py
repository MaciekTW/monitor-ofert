# -*- coding: utf-8 -*-
"""
Deduplikacja ofert między portalami — to samo ogłoszenie wystawione na OLX,
Otodomie i Gratce ma na każdym portalu inny identyfikator, więc w bazie są to
osobne wiersze. Ten moduł łączy je w grupy (najwyżej jedna oferta z portalu
w grupie), nie dotykając bazy: wynik liczy się od nowa przy każdym eksporcie.

Zasada nadrzędna: lepiej nie połączyć duplikatu niż skleić dwa różne mieszkania.
Stąd trzy bezpieczniki, z których każdy działa niezależnie od pozostałych:

1. Para musi przejść twarde warunki. Metraż zgodny co do 0,01 m² i ta sama liczba
   pokoi są wymagane zawsze, a dalej jedna z reguł:
   - link:    OLX podaje adres oferty na Otodomie, z którego została przeniesiona
              (pole external_url) — pewne połączenie, bez zgadywania;
   - tekst:   ta sama cena i ten sam (albo bardzo podobny) opis, piętro zgodne;
   - cechy:   ta sama cena i piętro, opisy różne (deweloper pisze sam na jednym
              portalu, a na drugi trafia przez pośrednika) — wtedy potrzebne jest
              potwierdzenie lokalizacją, ulicą albo sprzedającym, ostrzejsze przy
              okrągłych cenach, bo „650 000 zł za 50 m² na 2. piętrze” to żaden wyróżnik;
   - cena:    niemal identyczny opis, ale cena różni się do 10% (na jednym portalu
              jej nie zaktualizowano) — tylko rynek wtórny i starsze budynki;
   - piętro:  niemal identyczny opis i ta sama cena, piętra różnią się o jedno
              (portale różnie liczą parter) — też tylko starsze budynki z rynku wtórnego.
   Sam opis nigdy nie wystarcza: w jednej inwestycji wszystkie lokale mają ten sam tekst.
2. Para musi być jednoznaczna: wygrywa tylko wtedy, gdy dla obu ofert jest wyraźnie
   lepsza od każdej innej kandydatki. Dwa identyczne lokale w jednej inwestycji
   (ten sam metraż, cena i piętro) zostają więc niepołączone, o ile nie da się ich
   rozróżnić współrzędnymi, tytułem, datą dodania albo opisem.
3. W grupie jest najwyżej jedna oferta z każdego portalu. Dwa ogłoszenia z tego
   samego portalu nigdy się nie łączą, także pośrednio (A–B, B–C, gdzie A i C
   są z jednego portalu) — odpada wtedy słabsze z połączeń.

Koszt: oferty trafiają do kubełków po (metraż, cena) albo samym metrażu, więc
porównywane są tylko te, które w ogóle mogą do siebie pasować — dla ~30 tys.
ofert to kilkadziesiąt tysięcy par zamiast kilkuset milionów.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from datetime import date
from itertools import combinations

# próg podobieństwa opisów: „ten sam tekst z portalowymi dodatkami” i „praktycznie identyczny”
SIM_TEXT = 0.5
SIM_SAME = 0.8
SHINGLE = 4  # długość ciągu słów, z których składa się odcisk opisu
MIN_SHINGLES = 40  # krótszym opisom nie ufamy przy liczeniu zawierania się tekstów
NEAR_M = 150  # „to samo miejsce”: portale geokodują ten sam adres z taką rozbieżnością
SAME_SPOT_M = 5  # współrzędne przepisane 1:1 z jednego systemu sprzedającego
FAR_M = 3000  # bez zgodnego opisu dalej niż tyle to już inne mieszkanie
PRICE_DRIFT = 0.10  # dopuszczalna różnica ceny przy niemal identycznym opisie
NEW_BUILDING_YEARS = 3  # tyle lat od budowy lokale jednej inwestycji bywają nie do odróżnienia
MARGIN = 1.0  # o tyle punktów para musi wyprzedzać kolejną kandydatkę obu ofert
LINK_SCORE = 100.0

_WORD = re.compile(r"[0-9a-z]+")
_OTODOM_ID = re.compile(r"-(ID[0-9A-Za-z]+)/?(?:[?#].*)?$")
# słowa, które w nazwach firm nic nie znaczą („Nieruchomości”, „Sp. z o.o.”…)
_WHO_STOP = frozenset(
    "nieruchomosci nieruchomosc biuro agencja oddzial spolka ograniczona odpowiedzialnoscia komandytowa "
    "krakow krakowie polska poland estate estates group grupa invest investment inwestycje inwestycja "
    "development developer deweloper home house dom domy mieszkania mieszkanie salon sprzedazy osiedle "
    "osiedla etap budynek bud real property properties partner partners test api the and".split()
)


def _fold(text: str | None) -> str:
    """Małe litery bez polskich znaków — portale różnie je zapisują."""
    text = unicodedata.normalize("NFKD", (text or "").lower().replace("ł", "l"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _shingles(text: str) -> frozenset[int]:
    """Odcisk opisu: zbiór skrótów wszystkich ciągów SHINGLE kolejnych słów."""
    words = _WORD.findall(_fold(text))
    return frozenset(hash(tuple(words[i : i + SHINGLE])) for i in range(len(words) - SHINGLE + 1))


def _similarity(a: frozenset[int], b: frozenset[int]) -> float:
    """Podobieństwo opisów 0–1: współczynnik Jaccarda, a gdy jeden opis zawiera
    się w drugim (portal dokleja stopkę biura albo ucina tekst) — obniżony
    udział wspólnych fragmentów w krótszym z nich."""
    if not a or not b:
        return 0.0
    common = len(a & b)
    if not common:
        return 0.0
    jaccard = common / (len(a) + len(b) - common)
    shorter = min(len(a), len(b))
    if shorter < MIN_SHINGLES:
        return jaccard
    return max(jaccard, 0.85 * common / shorter)


def _rooms(label) -> tuple[int, bool] | None:
    """Etykieta pokoi → (liczba, czy „i więcej”): OLX kończy skalę na „4 i więcej”."""
    if not label:
        return None
    text = _fold(str(label))
    if "kawaler" in text:
        return 1, False
    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group()), "wiecej" in text or "+" in text


def _rooms_match(a, b) -> bool:
    if a is None or b is None:
        return False
    (na, plus_a), (nb, plus_b) = a, b
    return na == nb or (plus_a and nb >= na) or (plus_b and na >= nb)


def _floor(label) -> int | None:
    """Etykieta piętra → numer (parter 0, suterena −1). Piętra od 11 w górę
    zlewają się w jedno, bo OLX zna tylko „Powyżej 10”; poddasze nie ma numeru."""
    if label is None:
        return None
    text = _fold(str(label)).strip()
    if text.startswith("parter"):
        return 0
    if text.startswith("suteren"):
        return -1
    if text.startswith("powyzej"):
        return 11
    return min(int(text), 11) if text.isdigit() else None


def _who(*names) -> frozenset[str]:
    """Znaczące słowa z nazw sprzedającego i inwestycji."""
    words = set()
    for name in names:
        words.update(w for w in _WORD.findall(_fold(name)) if len(w) > 2 and not w.isdigit())
    return frozenset(words - _WHO_STOP)


def _street(name) -> str | None:
    """Ulica sprowadzona do ostatniego słowa („ul. gen. Tadeusza Kościuszki” →
    „kosciuszki”): imiona i tytuły powtarzają się między różnymi ulicami."""
    words = [w for w in _WORD.findall(_fold(name)) if len(w) > 3 and not w.isdigit()]
    return words[-1] if words else None


def otodom_id(url) -> str | None:
    """Identyfikator z końca adresu oferty Otodomu („…-ID4uaVC”)."""
    match = _OTODOM_ID.search(url or "")
    return match.group(1) if match else None


def _first(*values) -> str | None:
    return next((v for v in values if isinstance(v, str) and v.strip()), None)


def listing(row: dict, raw: dict, description: str) -> dict:
    """Oferta w postaci, na której pracuje find_duplicates.

    row: kolumny tabeli offers (uid, source, url, title, price, area, rooms, floor,
    market, build_year, created_at, lat, lon, map_radius), raw: jej surowy JSON
    z portalu, description: opis jako czysty tekst."""
    source = row["source"]
    street = development = link = None
    if source == "olx":
        user = raw.get("user") or {}
        seller = _first(user.get("company_name"), user.get("name"))
        link = otodom_id(raw.get("external_url"))
    elif source == "otodom":
        seller = (raw.get("agency") or {}).get("name")
        street = (((raw.get("location") or {}).get("address") or {}).get("street") or {}).get("name")
        development = raw.get("developmentTitle")
    else:
        seller = ((raw.get("contact") or {}).get("company") or {}).get("name")
        street = (raw.get("location") or {}).get("street")
        development = (
            (raw.get("development") or {}).get("name") if isinstance(raw.get("development"), dict) else None
        )
    price, area = row.get("price"), row.get("area")
    title = "".join(_WORD.findall(_fold(row.get("title"))))
    return {
        "uid": row["uid"],
        "source": source,
        "price": round(price) if price else None,
        "area": round(area * 100) if area else None,  # w setnych m², żeby porównywać dokładnie
        "rooms": _rooms(row.get("rooms")),
        "floor": _floor(row.get("floor")),
        "primary": row.get("market") == "Pierwotny",
        "year": row.get("build_year"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "radius": row.get("map_radius") or 0,
        "day": (row.get("created_at") or "")[:10] or None,
        "title": title if len(title) >= 10 else None,
        "text": _shingles(description),
        "who": _who(seller, development),
        "street": _street(street),
        "otodom": otodom_id(row.get("url")) if source == "otodom" else None,
        "link": link,
    }


def _distance(a: dict, b: dict) -> float | None:
    """Odległość w metrach, pomniejszona o podany przez portal promień
    przybliżenia lokalizacji (OLX); None, gdy któraś oferta nie ma współrzędnych."""
    if a["lat"] is None or b["lat"] is None or a["lon"] is None or b["lon"] is None:
        return None
    dy = (a["lat"] - b["lat"]) * 111_320
    dx = (a["lon"] - b["lon"]) * 111_320 * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return max(0.0, math.hypot(dx, dy) - a["radius"] - b["radius"])


def _score(a: dict, b: dict, this_year: int) -> float | None:
    """Ocena pary ofert z różnych portali o tym samym metrażu: None, gdy żadna
    reguła nie pozwala ich połączyć, inaczej liczba punktów — im więcej zgodnych
    cech, tym wyżej (służy do wyboru między kandydatkami, nie jako próg)."""
    if not _rooms_match(a["rooms"], b["rooms"]):
        return None
    same_price = abs(a["price"] - b["price"]) <= 1
    sim = _similarity(a["text"], b["text"])
    floors_known = a["floor"] is not None and b["floor"] is not None
    same_floor = floors_known and a["floor"] == b["floor"]
    years = [y for y in (a["year"], b["year"]) if y]
    same_year = len(years) == 2 and years[0] == years[1]
    # w świeżym budynku sąsiednie lokale bywają bliźniacze, a pośrednicy wystawiają
    # je jako rynek wtórny — reguły dopuszczające różnicę ceny lub piętra ich nie obejmują
    settled = (
        not a["primary"] and not b["primary"] and all(y <= this_year - NEW_BUILDING_YEARS for y in years)
    )
    dist = _distance(a, b)
    near = dist is not None and dist <= NEAR_M
    same_street = a["street"] is not None and a["street"] == b["street"]
    same_who = bool(a["who"] & b["who"])

    if not same_price:
        # reguła „cena”
        drift = abs(a["price"] - b["price"]) / max(a["price"], b["price"])
        if not (sim >= SIM_SAME and settled and drift <= PRICE_DRIFT and (same_floor or not floors_known)):
            return None
    elif sim >= SIM_TEXT:
        # reguła „tekst”, a przy różnicy piętra o jedno — „piętro”
        if floors_known and not same_floor:
            if not (sim >= SIM_SAME and settled and years and abs(a["floor"] - b["floor"]) == 1):
                return None
    else:
        # reguła „cechy”: opisy nie pomagają, więc wszystko inne musi się zgadzać
        if not same_floor or (dist is not None and dist > FAR_M):
            return None
        if len(years) == 2 and abs(years[0] - years[1]) > 2:
            return None
        if a["price"] % 1000:
            # cena co do złotówki (cennik dewelopera) sama jest mocnym wyróżnikiem
            if not (near or same_street or same_who):
                return None
        elif not (near and same_year and (a["area"] % 100 or same_street or same_who)):
            return None

    score = 4 * sim + same_floor + same_street + same_who + 0.5 * same_year
    score += 2 * (a["title"] is not None and a["title"] == b["title"])
    score += a["day"] is not None and a["day"] == b["day"]
    if dist is not None and dist <= SAME_SPOT_M and not a["radius"] and not b["radius"]:
        score += 2
    elif near:
        score += 1
    return score - (0 if same_price else 1.5)


def _candidates(items: list[dict], this_year: int) -> list[tuple[float, int, int]]:
    """Pary (punkty, indeks, indeks) ofert z różnych portali, które przeszły _score."""
    exact = defaultdict(list)  # ten sam metraż i cena
    drifted = defaultdict(list)  # ten sam metraż, cena dowolna — tylko dla reguły „cena”
    for i, item in enumerate(items):
        if item["area"] is None or item["price"] is None:
            continue
        exact[item["area"], item["price"]].append(i)
        if not item["primary"] and len(item["text"]) >= MIN_SHINGLES:
            drifted[item["area"]].append(i)

    edges = []
    for bucket in exact.values():
        for i, j in combinations(bucket, 2):
            if items[i]["source"] != items[j]["source"]:
                score = _score(items[i], items[j], this_year)
                if score is not None:
                    edges.append((score, i, j))
    for bucket in drifted.values():
        if len(bucket) < 2:
            continue
        bucket.sort(key=lambda i: items[i]["price"])
        for pos, i in enumerate(bucket):
            a = items[i]
            for j in bucket[pos + 1 :]:
                b = items[j]
                if b["price"] * (1 - PRICE_DRIFT) > a["price"]:
                    break  # posortowane po cenie: dalej są już tylko droższe
                if a["source"] != b["source"] and a["price"] != b["price"]:
                    score = _score(a, b, this_year)
                    if score is not None:
                        edges.append((score, i, j))
    return edges


def _unambiguous(edges: list[tuple[float, int, int]], items: list[dict]) -> list[tuple[float, int, int]]:
    """Zostawia pary jednoznaczne: w obrębie dwóch portali para musi wyprzedzać
    o MARGIN każdą inną kandydatkę obu swoich ofert. Po odłożeniu pewnych par
    konkurencja o pozostałe oferty maleje, więc wybór powtarza się do skutku."""
    accepted = []
    pending = defaultdict(list)
    for edge in edges:
        _, i, j = edge
        pending[frozenset((items[i]["source"], items[j]["source"]))].append(edge)
    for group in pending.values():
        while group:
            # dla każdej oferty dwie najlepsze oceny wśród jej kandydatek
            top = defaultdict(lambda: [-math.inf, -math.inf])
            for score, i, j in group:
                for node in (i, j):
                    best = top[node]
                    if score > best[0]:
                        best[1], best[0] = best[0], score
                    elif score > best[1]:
                        best[1] = score
            sure = [
                (score, i, j)
                for score, i, j in group
                if score >= top[i][0] and score >= top[j][0] and score - max(top[i][1], top[j][1]) >= MARGIN
            ]
            if not sure:
                break
            accepted.extend(sure)
            taken = {node for _, i, j in sure for node in (i, j)}
            group = [e for e in group if e[1] not in taken and e[2] not in taken]
    return accepted


def _uid_order(uid: str) -> tuple[int, str]:
    """Kolejność uid według numeru oferty („oto:68439349” przed „oto:9000062315”)."""
    return len(uid), uid


def find_duplicates(items: list[dict]) -> list[list[str]]:
    """Grupy uid ofert uznanych za to samo ogłoszenie na różnych portalach
    (po co najmniej dwie oferty w grupie, najwyżej jedna z portalu).
    items: oferty przygotowane przez listing()."""
    edges = _unambiguous(_candidates(items, date.today().year), items)

    # Otodom potrafi wydać tę samą ofertę (ten sam adres) drugi raz pod sztucznym, dużo
    # dłuższym numerem — link z OLX ma wtedy trafić w tę właściwą, o krótszym numerze
    by_otodom: dict[str, int] = {}
    for i, item in enumerate(items):
        j = by_otodom.get(item["otodom"]) if item["otodom"] else None
        if item["otodom"] and (j is None or _uid_order(item["uid"]) < _uid_order(items[j]["uid"])):
            by_otodom[item["otodom"]] = i
    for i, item in enumerate(items):
        j = by_otodom.get(item["link"]) if item["link"] else None
        if j is not None:
            edges.append((LINK_SCORE, i, j))

    # łączenie od najpewniejszych par; grupa pamięta swoje portale, więc połączenie,
    # które dołożyłoby drugą ofertę z tego samego portalu, przepada
    parent = list(range(len(items)))
    sources = [{item["source"]} for item in items]

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for _, i, j in sorted(edges, key=lambda e: (-e[0], items[e[1]]["uid"], items[e[2]]["uid"])):
        root_i, root_j = find(i), find(j)
        if root_i == root_j or sources[root_i] & sources[root_j]:
            continue
        parent[root_j] = root_i
        sources[root_i] |= sources[root_j]

    groups = defaultdict(list)
    for i, item in enumerate(items):
        groups[find(i)].append(item["uid"])
    return sorted(sorted(group) for group in groups.values() if len(group) > 1)

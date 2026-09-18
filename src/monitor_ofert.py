#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_ofert.py — monitor ofert nieruchomości z wielu portali (OLX, Otodom, Gratka)

Domyślnie zbiera dane ze WSZYSTKICH zdefiniowanych serwisów (patrz rejestr
SOURCES); dla każdego portala używane jest jego domyślne wyszukiwanie
(mieszkania na sprzedaż, Kraków), chyba że podasz własne adresy --url.

Pierwsze uruchomienie:  pobiera WSZYSTKIE oferty z podanych wyszukiwań
                        i zapisuje je w lokalnej bazie SQLite.
Kolejne uruchomienia:   pobiera oferty ponownie i pokazuje TYLKO:
                          • nowe ogłoszenia,
                          • zmiany cen (z pełną historią cen w bazie),
                          • ogłoszenia, które zniknęły (sprzedane / wycofane),
                          • ogłoszenia, które wróciły po zniknięciu.

Przykłady użycia:
    python src/monitor_ofert.py                      # wszystkie źródła (OLX + Otodom + Gratka)
    python src/monitor_ofert.py --source olx         # tylko jedno źródło
    python src/monitor_ofert.py --quick              # szybki tryb: tylko NOWE oferty
    python src/monitor_ofert.py --export oferty.csv  # zrzut aktywnych ofert do CSV
    python src/monitor_ofert.py --html mapa.html     # interaktywna mapa ofert
    python src/monitor_ofert.py --db tanie.db \\
        --url "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/krakow/?search[filter_float_price:to]=700000" \\
        --url "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/malopolskie/krakow/krakow/krakow?priceMax=700000"

Nowy portal dodaje się, pisząc podklasę Source i dopisując ją do SOURCES —
reszta skryptu (baza, raport, eksporty, mapa) nie wymaga wtedy żadnych zmian.

Wymagania:  Python 3.8+  oraz  pip install curl_cffi
            (zalecane — omija blokady antybotowe; awaryjnie wystarczy
             samo 'pip install requests', ale bywa częściej blokowane)

Uwagi:
  * Skrypt korzysta z tych samych, nieoficjalnych mechanizmów, których używają
    strony portali w przeglądarce (API JSON OLX-a, dane __NEXT_DATA__
    Otodomu, API GraphQL Gratki). Serwisy mogą je w każdej chwili zmienić.
  * W OLX-ie i Otodomie jedno zapytanie zwraca ograniczoną liczbę wyników,
    więc przy większych wyszukiwaniach skrypt automatycznie dzieli pobieranie
    na przedziały cen (Gratka wydaje wyniki do ostatniej strony).
  * Między zapytaniami jest pauza (--delay, domyślnie 0,6 s) — nie zmniejszaj
    jej agresywnie; to narzędzie do prywatnego monitoringu, a nie masowego
    scrapingu. Regulaminy portali ograniczają automatyczny dostęp.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import random
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode

from dotenv import load_dotenv

from map.html_map import export_html
from terminal_report import report

# curl_cffi (jeśli jest zainstalowane) podszywa się pod prawdziwą przeglądarkę
# także na poziomie uścisku dłoni TLS — a właśnie po tym "odcisku palca" OLX
# rozpoznaje od pewnego czasu automatyczne zapytania i odpowiada HTTP 403,
# zanim w ogóle spojrzy na nagłówki. Zwykłe `requests` zostaje jako zapasowe.
try:
    from curl_cffi import requests  # pip install curl_cffi

    IMPERSONATE = True
except ImportError:
    IMPERSONATE = False
    try:
        import requests
    except ImportError:
        sys.exit(
            "Brakuje biblioteki HTTP. Zainstaluj:  pip install curl_cffi\n"
            "(zadziała też samo 'pip install requests', ale bywa częściej "
            "blokowane przez OLX)"
        )

# klasy wyjątków sieciowych różnią się między requests a curl_cffi
NETWORK_ERRORS = tuple(
    dict.fromkeys(
        filter(
            None,
            (
                getattr(getattr(requests, "exceptions", None), "RequestException", None),
                getattr(requests, "RequestsError", None),
            ),
        )
    )
) or (OSError,)

# ---------------------------------------------------------------- konfiguracja

DEFAULT_DB = "oferty.db"
LEGACY_DB = "olx_oferty.db"  # nazwa bazy ze starszych wersji skryptu

API_OFFERS = "https://www.olx.pl/api/v1/offers/"

PAGE_LIMIT = 50  # maks. liczba ofert na jedno zapytanie API
SEGMENT_MAX = 1000  # głębiej niż ~1000 wyników jedno zapytanie nie sięga
MIN_PRICE_STEP = 1000  # nie dziel przedziałów cen drobniej niż co 1000 zł

# Używane tylko, gdy brak curl_cffi. Aktualizuj co kilka miesięcy — mocno
# przestarzała wersja Chrome w User-Agencie sama w sobie wygląda podejrzanie.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Chromium";v="152", "Google Chrome";v="152", "Not_A Brand";v="24"'

# segmenty ścieżki URL, które NIE są nazwą miasta (do wykrycia miasta w adresie)
NON_CITY_SEGMENTS = {
    "nieruchomosci",
    "mieszkania",
    "domy",
    "dzialki",
    "biura-lokale",
    "garaze-parkingi",
    "stancje-pokoje",
    "hale-magazyny",
    "pozostale",
    "sprzedaz",
    "wynajem",
    "zamiana",
}

if IMPERSONATE:
    # impersonate ustawia spójny komplet nagłówków Chrome (User-Agent,
    # sec-ch-ua itd.) dopasowany do odcisku TLS — nie nadpisujemy ich
    # własnym UA, żeby nie wprowadzić rozpoznawalnej niezgodności.
    session = requests.Session(impersonate="chrome")
    session.headers.update({"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5"})
else:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    )

# Nagłówki dobrane do rodzaju zapytania. Interfejs API ma dostawać takie,
# jakie wysyła strona OLX pobierająca dane w tle (fetch/cors), a zwykłe
# strony HTML — nagłówki nawigacji przeglądarki. Wysyłanie nagłówków
# nawigacji do API wygląda jak bot i kończy się blokadą (HTTP 403).
JSON_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.olx.pl/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

DEVTOOLS_HELP = """
Nie udało się automatycznie ustalić parametrów wyszukiwania (OLX mógł zmienić
strukturę strony). Możesz podać je ręcznie:

  1. Otwórz swoje wyszukiwanie na olx.pl w przeglądarce.
  2. Naciśnij F12 → zakładka "Sieć" (Network) → filtr "offers".
  3. Przewiń listę ogłoszeń — pojawi się zapytanie w stylu:
       https://www.olx.pl/api/v1/offers/?offset=40&limit=40&category_id=14&region_id=4&city_id=8959&...
  4. Odczytaj z niego identyfikatory i uruchom skrypt np. tak:
       python src/monitor_ofert.py --category-id 14 --region-id 4 --city-id 8959

Jeśli powyżej widzisz HTTP 403: OLX rozpoznał zapytanie jako automatyczne.
Najskuteczniejsza poprawka to instalacja biblioteki curl_cffi:
      pip install curl_cffi
(skrypt sam ją wykryje i użyje). Pomaga też odczekanie kilkunastu minut
i większa pauza (--delay 1.5). Identyfikatory podane ręcznie omijają ten
etap, bo skrypt nie musi wtedy pobierać strony wyników.
"""


# ------------------------------------------------------------------- narzędzia


def log(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def fmt_price(value) -> str:
    if value is None:
        return "brak ceny"
    return f"{int(round(value)):,}".replace(",", " ") + " zł"


def pause(delay: float) -> None:
    """Pauza między zapytaniami z lekką losowością (równy rytm zdradza bota)."""
    if delay > 0:
        time.sleep(delay * random.uniform(0.75, 1.35))


_warmed_hosts: set[str] = set()


def warm_up(url: str) -> None:
    """Jednorazowo odwiedza stronę główną portalu, tak jak przeglądarka.

    Serwis przy pierwszej wizycie ustawia ciasteczka (m.in. antybotowe),
    bez których zapytania do API są znacznie częściej odrzucane (HTTP 403).
    Sesja przechowuje ciasteczka, więc wystarczy zrobić to raz na domenę."""
    host = urlparse(url).netloc
    if not host or host in _warmed_hosts:
        return
    _warmed_hosts.add(host)
    try:
        session.get(f"https://{host}/", timeout=30, headers=HTML_HEADERS)
        time.sleep(random.uniform(0.8, 1.6))
    except Exception:
        pass  # rozgrzewka tylko zwiększa szanse — bez niej też próbujemy


def http_get(
    url: str,
    params: dict | None = None,
    as_json: bool = True,
    retries: int = 4,
    timeout: int = 30,
    headers: dict | None = None,
):
    """GET z ponawianiem przy 403/429/5xx i czytelnym komunikatem przy blokadzie."""
    if headers is None:
        headers = JSON_HEADERS if as_json else HTML_HEADERS
    warm_up(url)
    last_exc = None
    denied = 0
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout, headers=headers)
        except NETWORK_ERRORS as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 3 * (attempt + 1)
            log(f"  ! HTTP {r.status_code} — czekam {wait} s i ponawiam...", err=True)
            time.sleep(wait)
            continue
        if r.status_code == 403:
            denied += 1
            if denied < 2 and attempt < retries - 1:
                # zdarza się, że blokada dotyczy pojedynczego zapytania
                log("  ! HTTP 403 — próbuję jeszcze raz za chwilę...", err=True)
                time.sleep(random.uniform(4, 7))
                continue
            src = source_for(url)
            portal = src.label if src else "Serwis"
            hint = (
                ""
                if IMPERSONATE
                else "\nNajskuteczniejsza poprawka: zainstaluj bibliotekę curl_cffi "
                "(pip install curl_cffi) — skrypt sam ją wykryje i będzie "
                "przedstawiał się serwerowi jak prawdziwa przeglądarka, "
                "również na poziomie połączenia TLS, po którym serwisy "
                "rozpoznają boty niezależnie od nagłówków."
            )
            raise RuntimeError(
                f"{portal} odrzucił zapytanie (HTTP 403) — serwis uznał je za "
                "automatyczne. Odczekaj kilkanaście minut do godziny i spróbuj "
                "ponownie, najlepiej z większą pauzą (np. --delay 1.5)." + hint
            )
        r.raise_for_status()
        if not as_json:
            return r.text
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"Odpowiedź z {url} nie jest poprawnym JSON-em — OLX mógł zmienić API.")
    raise RuntimeError(f"Nie udało się pobrać {url} ({last_exc})")


# ------------------------------------------- adres wyszukiwania → parametry API


def parse_search_url(url: str):
    """Rozbija adres wyszukiwania OLX na ścieżkę SEO, parametry i slug miasta."""
    u = urlparse(url)
    segments = [s for s in u.path.split("/") if s]
    extra: dict[str, str] = {}
    clean_segments = []
    for seg in segments:
        if seg.startswith("q-"):  # fraza wyszukiwania w ścieżce
            extra["q"] = seg[2:].replace("-", " ")
        else:
            clean_segments.append(seg)
    for key, val in parse_qsl(u.query, keep_blank_values=True):
        # parametry strony wyglądają tak:  search[filter_float_price:to]=700000
        # a API przyjmuje je "gołe":       filter_float_price:to=700000
        m = re.fullmatch(r"search\[([^\]]+)\](.*)", key)
        api_key = (m.group(1) + m.group(2)) if m else key
        if api_key in ("page", "reason", "view", "sl"):
            continue
        extra[api_key] = val
    city_slug = None
    if clean_segments and clean_segments[-1] not in NON_CITY_SEGMENTS:
        city_slug = clean_segments[-1]
    return "/".join(clean_segments), extra, city_slug


def resolve_api_params(url: str, overrides: dict) -> tuple[dict, str | None]:
    """Ustala parametry API (category_id, city_id, ...) dla podanego adresu."""
    _, extra, city_slug = parse_search_url(url)
    resolved: dict[str, str] = {}

    # 1) identyfikatory wyszukane w kodzie HTML strony wyników
    try:
        html = http_get(url, as_json=False)
        for key in ("category_id", "region_id", "city_id", "district_id"):
            camel = re.sub(r"_(\w)", lambda m: m.group(1).upper(), key)
            hits = re.findall(rf'["\']?{key}["\']?\s*[:=]\s*["\']?(\d+)', html)
            hits += re.findall(rf'["\']?{camel}["\']?\s*[:=]\s*["\']?(\d+)', html)
            if hits:
                resolved[key] = Counter(hits).most_common(1)[0][0]
    except Exception as exc:
        log(f"  ! Nie udało się przeanalizować strony wyników: {exc}", err=True)

    # 2) parametry podane ręcznie w linii poleceń mają najwyższy priorytet
    params = {**extra, **resolved}
    params.update({k: v for k, v in overrides.items() if v})

    if "category_id" not in params:
        raise RuntimeError(DEVTOOLS_HELP)
    return params, city_slug


# ------------------------------------------------------------ pobieranie ofert


def api_page(params: dict, offset: int, delay: float) -> dict:
    query = dict(params)
    query["offset"] = offset
    query["limit"] = PAGE_LIMIT
    data = http_get(API_OFFERS, params=query)
    pause(delay)
    return data


def reported_count(params: dict, delay: float) -> int:
    """Liczba wyników, jaką deklaruje API.

    UWAGA: OLX ucina tę liczbę do ~SEGMENT_MAX (stąd na stronie „ponad 1000
    ogłoszeń” zamiast konkretu). Wynik równy SEGMENT_MAX znaczy więc tak
    naprawdę „co najmniej tyle” i nie wolno na jego podstawie uznać,
    że wszystko zmieści się w jednym zapytaniu."""
    query = dict(params)
    query["offset"] = 0
    query["limit"] = 1
    data = http_get(API_OFFERS, params=query)
    pause(delay)
    meta = data.get("metadata") or {}
    counts = [
        meta[k] for k in ("total_elements", "visible_total_count", "total") if isinstance(meta.get(k), int)
    ]
    if counts:
        return max(counts)
    return len(data.get("data") or [])


def check_city(offers: list, city_slug: str | None) -> None:
    """Bezpiecznik: czy pobrane oferty faktycznie pochodzą z miasta z adresu URL."""
    if not city_slug or not offers:
        return
    cities = []
    for offer in offers[:40]:
        loc = offer.get("location") or {}
        city = loc.get("city") or {}
        name = slugify(city.get("name", "")) if isinstance(city, dict) else ""
        if name:
            cities.append(name)
    if not cities:
        return
    hits = sum(1 for c in cities if city_slug in c or c in city_slug)
    if hits / len(cities) < 0.5:
        top = Counter(cities).most_common(1)[0][0]
        raise RuntimeError(
            f"Pobrane oferty pochodzą głównie z lokalizacji „{top}”, a nie "
            f"„{city_slug}” — parametry wyszukiwania zostały źle rozpoznane.\n" + DEVTOOLS_HELP
        )


def fetch_all_olx(params: dict, city_slug: str | None, delay: float) -> list[dict]:
    """Pobiera wszystkie oferty.

    OLX nie pozwala sięgnąć głębiej niż ~SEGMENT_MAX wyników na jedno zapytanie
    i DO TEJ SAMEJ wartości ucina deklarowaną liczbę ogłoszeń. Deklaracji więc
    nie ufamy: gdy dobija do limitu, traktujemy ją jako „co najmniej tyle”
    i tniemy wyszukiwanie na przedziały cenowe tak długo, aż każdy kawałek
    da się pobrać w całości. Dodatkowo crawl() sam zgłasza, gdy uciął na
    limicie — to druga, niezależna linia obrony."""
    base = {k: v for k, v in params.items() if not k.startswith("filter_float_price")}
    base["sort_by"] = params.get("sort_by", "created_at:desc")
    lo = int(to_float(params.get("filter_float_price:from")) or 0)
    hi_raw = to_float(params.get("filter_float_price:to"))
    hi = int(hi_raw) if hi_raw else None  # None = bez górnej granicy cen

    collected: dict[int, dict] = {}
    state = {"city_checked": False}

    def crawl(query: dict) -> bool:
        """Pobiera kolejne strony jednego zapytania.
        True = dotarliśmy do końca wyników, False = ucięło nas na limicie API."""
        offset = 0
        while True:
            data = api_page(query, offset, delay)
            batch = data.get("data") or []
            if not state["city_checked"] and batch:
                check_city(batch, city_slug)
                state["city_checked"] = True
            for offer in batch:
                if isinstance(offer, dict) and offer.get("id") is not None:
                    collected[offer["id"]] = offer
            print(f"\r  Pobrano {len(collected)} ofert...", end="", flush=True)
            if len(batch) < PAGE_LIMIT:
                return True
            offset += PAGE_LIMIT
            if offset >= SEGMENT_MAX:
                return False

    def price_query(a: int, b: int | None) -> dict:
        query = dict(base)
        if a > 0:
            query["filter_float_price:from"] = a
        if b is not None:
            query["filter_float_price:to"] = b
        return query

    def segment(a: int, b: int | None) -> None:
        query = price_query(a, b)
        count = reported_count(query, delay)
        if count == 0:
            return
        splittable = b is None or (b - a) > MIN_PRICE_STEP
        if count < SEGMENT_MAX or not splittable:
            if crawl(query):
                return
            if not splittable:
                rng = f"{fmt_price(a)}–{fmt_price(b)}" if b is not None else f"od {fmt_price(a)}"
                log(
                    f"\n  ! Przedział {rng} ma więcej ofert, niż OLX pozwala "
                    f"pobrać (~{SEGMENT_MAX}) — część z niego pominięto."
                )
                return
            # deklaracja była zaniżona, a crawl uciął → mimo wszystko dzielimy
        # Za dużo wyników → tniemy przedział cen na pół. Granice celowo
        # zachodzą na siebie (a–mid i mid–b), żeby nie zgubić ofert z ceną
        # niecałkowitą tuż przy granicy; duplikaty i tak odpadają po id.
        if b is not None:
            mid = (a + b) // 2
            segment(a, mid)
            segment(mid, b)
        else:
            pivot = max(a * 2, a + 500_000)  # otwarty koniec: rosnący pivot
            segment(a, pivot)
            segment(pivot, None)

    count = reported_count(params, delay)
    if count >= SEGMENT_MAX:
        log(
            f"OLX deklaruje „ponad {SEGMENT_MAX}” ogłoszeń (dokładnej liczby "
            f"powyżej limitu nie zdradza) — pobieram partiami wg przedziałów cen."
        )
        segment(lo, hi)
    else:
        log(f"W tym wyszukiwaniu jest łącznie ok. {count} ogłoszeń.")
        if not crawl(dict(params, sort_by=base["sort_by"])):
            segment(lo, hi)  # deklaracja okazała się zaniżona
    print()
    log(f"Pobrano łącznie {len(collected)} unikalnych ofert.")
    return list(collected.values())


def fetch_new_quick_olx(params: dict, known_ids: set, city_slug: str | None, delay: float) -> list[dict]:
    """Szybki tryb: idzie od najnowszych i kończy, gdy trafi na same znane oferty."""
    query = dict(params)
    query["sort_by"] = "created_at:desc"
    fresh: list[dict] = []
    seen: set = set()
    offset = 0
    city_checked = False
    while offset < SEGMENT_MAX:
        data = api_page(query, offset, delay)
        batch = data.get("data") or []
        if not batch:
            break
        if not city_checked:
            check_city(batch, city_slug)
            city_checked = True
        page_new = [
            o for o in batch if o.get("id") is not None and o["id"] not in known_ids and o["id"] not in seen
        ]
        seen.update(o["id"] for o in page_new)
        fresh.extend(page_new)
        print(
            f"\r  Sprawdzono {offset + len(batch)} najnowszych ofert, nowych: {len(fresh)}...",
            end="",
            flush=True,
        )
        if not page_new:  # cała strona to już znane oferty → koniec
            break
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    print()
    return fresh


# ---------------------------------------------------------------------- otodom

OTO_LIMIT = 72  # tyle ofert na stronę pozwala ustawić Otodom
OTO_DETAIL_KEYS = ("description", "createdAt", "market", "advertType")


def next_data(html: str) -> dict:
    """Wyciąga JSON __NEXT_DATA__ (Otodom to aplikacja Next.js —
    wszystkie dane strony siedzą w tym jednym znaczniku)."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json"[^>]*>'
        r"(.*?)</script>",
        html,
        re.S,
    )
    if not m:
        raise RuntimeError(
            "Strona Otodom nie zawiera danych __NEXT_DATA__ — serwis mógł "
            "zmienić strukturę albo zwrócił stronę blokady."
        )
    return json.loads(m.group(1))


def _oto_flatten(items: list) -> list[dict]:
    """Inwestycje deweloperskie mają mieszkania schowane w relatedAds —
    rozpakowujemy je do płaskiej listy zwykłych ofert."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        related = item.get("relatedAds")
        if related:
            for sub in related:
                if isinstance(sub, dict) and sub.get("id") is not None:
                    out.append(sub)
        elif item.get("id") is not None:
            out.append(item)
    return out


def otodom_page(base_url: str, extra: dict, delay: float) -> tuple[list, int, int]:
    """Jedna strona wyników: (oferty, liczba_stron, deklarowana_liczba_wyników)."""
    parsed = urlparse(base_url)
    params = dict(parse_qsl(parsed.query))
    params.update({"viewType": "listing", "limit": OTO_LIMIT})
    params.update(extra)
    clean = parsed._replace(query="").geturl()
    html = http_get(clean, params=params, as_json=False)
    pause(delay)
    data = next_data(html)
    props = (data.get("props") or {}).get("pageProps") or {}
    items = _oto_flatten(((props.get("data") or {}).get("searchAds") or {}).get("items"))
    listing = (props.get("tracking") or {}).get("listing") or {}
    pages = listing.get("page_count") or 0
    total = listing.get("result_count") or 0
    return items, int(pages or 0), int(total or 0)


def fetch_all_otodom(url: str, delay: float) -> list[dict]:
    """Pobiera wszystkie oferty z wyszukiwania Otodom.
    Gdyby wyników było więcej, niż serwis wydaje w jednym przebiegu stron,
    dzieli wyszukiwanie po cenach (priceMin/priceMax) — tak samo jak dla OLX."""
    user_q = dict(parse_qsl(urlparse(url).query))
    lo = int(to_float(user_q.get("priceMin")) or 0)
    hi_raw = to_float(user_q.get("priceMax"))
    hi = int(hi_raw) if hi_raw else None

    collected: dict[int, dict] = {}

    def crawl(extra: dict) -> tuple[int, bool]:
        """Zwraca (deklarowana_liczba, czy_pobrano_komplet_segmentu)."""
        items, pages, total = otodom_page(url, dict(extra, page=1), delay)
        for it in items:
            collected[it["id"]] = it
        print(f"\r  [Otodom] pobrano {len(collected)} ofert...", end="", flush=True)
        got = len(items)
        for page in range(2, pages + 1):
            items, _, _ = otodom_page(url, dict(extra, page=page), delay)
            if not items:
                break
            got += len(items)
            for it in items:
                collected[it["id"]] = it
            print(f"\r  [Otodom] pobrano {len(collected)} ofert...", end="", flush=True)
        # niepełny segment = serwis pokazał mniej stron, niż ma wyników
        return total, (total == 0 or got >= total * 0.9)

    def segment(a: int, b: int | None) -> None:
        extra = {}
        if a > 0:
            extra["priceMin"] = a
        if b is not None:
            extra["priceMax"] = b
        total, complete = crawl(extra)
        if complete:
            return
        if b is None:
            pivot = max(a * 2, a + 500_000)
            segment(a, pivot)
            segment(pivot, None)
        elif (b - a) > MIN_PRICE_STEP:
            mid = (a + b) // 2
            segment(a, mid)
            segment(mid, b)
        else:
            log(
                f"\n  ! [Otodom] przedział {fmt_price(a)}–{fmt_price(b)} ma "
                f"{total} ofert — nie wszystkie dało się pobrać."
            )

    segment(lo, hi)
    print()
    log(f"[Otodom] pobrano łącznie {len(collected)} unikalnych ofert.")
    return list(collected.values())


def fetch_new_quick_otodom(url: str, known_ids: set, delay: float) -> list[dict]:
    """Szybki tryb dla Otodom: od najnowszych, aż trafimy na same znane."""
    fresh, seen = [], set()
    page = 1
    while True:
        items, pages, _ = otodom_page(url, {"by": "LATEST", "direction": "DESC", "page": page}, delay)
        if not items:
            break
        page_new = [it for it in items if it["id"] not in known_ids and it["id"] not in seen]
        seen.update(it["id"] for it in page_new)
        fresh.extend(page_new)
        print(
            f"\r  [Otodom] sprawdzono {page} str., nowych: {len(fresh)}...",
            end="",
            flush=True,
        )
        if not page_new or page >= pages:
            break
        page += 1
    print()
    return fresh


def fetch_otodom_detail(offer_url: str, delay: float) -> dict | None:
    """Strona pojedynczej oferty: współrzędne, pełny opis, zdjęcia, szczegóły.
    Zwraca odchudzony słownik (pełny JSON `ad` jest ogromny) albo None, gdy
    oferty nie da się pobrać — np. właśnie zniknęła z serwisu (404), serwer
    odmówił, albo strona nie zawiera już danych ogłoszenia."""
    try:
        html = http_get(offer_url, as_json=False)
    except (RuntimeError,) + NETWORK_ERRORS:
        return None  # oferta mogła właśnie zniknąć — trudno
    pause(delay)
    try:
        ad = ((next_data(html).get("props") or {}).get("pageProps") or {}).get("ad") or {}
    except (RuntimeError, ValueError):
        return None
    slim = {k: ad.get(k) for k in OTO_DETAIL_KEYS if ad.get(k) is not None}
    coords = (ad.get("location") or {}).get("coordinates") or {}
    if coords.get("latitude") is not None:
        slim["coordinates"] = {
            "latitude": coords.get("latitude"),
            "longitude": coords.get("longitude"),
        }
    geo = ((ad.get("location") or {}).get("reverseGeocoding") or {}).get("locations") or []
    for node in geo:
        if isinstance(node, dict) and node.get("locationLevel") == "district":
            slim["district"] = node.get("name")
            break
    target = ad.get("target") or {}
    target_slim = {
        k: target.get(k) for k in ("Rent", "Floor_no", "Rooms_num", "Build_year") if target.get(k) is not None
    }
    if target_slim:
        slim["target"] = target_slim
    images = []
    for img in (ad.get("images") or [])[:8]:
        if isinstance(img, dict):
            link = img.get("large") or img.get("medium") or img.get("small")
            if link:
                images.append(link)
    if images:
        slim["images"] = images
    return slim or None


def enrich_otodom(items: list[dict], have_detail: set, delay: float) -> None:
    """Dociąga szczegóły (współrzędne, opisy) dla ofert, które ich nie mają.
    Wynik wkłada do item["_ad"] — trafi do bazy razem z surowymi danymi."""
    todo = [it for it in items if it["id"] not in have_detail]
    if not todo:
        return
    est = int(len(todo) * (delay + 0.35) / 60) + 1
    log(f"[Otodom] dociągam szczegóły {len(todo)} ofert (współrzędne i opisy — ok. {est} min)...")
    for done, item in enumerate(todo, 1):
        slug = item.get("slug")
        if slug:
            detail = fetch_otodom_detail(f"https://www.otodom.pl/pl/oferta/{slug}", delay)
            if detail:
                item["_ad"] = detail
        print(f"\r  [Otodom] szczegóły {done}/{len(todo)}...", end="", flush=True)
    print()


_OTO_ROOMS = {
    "ONE": "1 pokój",
    "TWO": "2 pokoje",
    "THREE": "3 pokoje",
    "FOUR": "4 pokoje",
    "FIVE": "5 pokoi",
    "SIX": "6 pokoi",
    "SEVEN": "7 pokoi",
    "EIGHT": "8 pokoi",
    "NINE": "9 pokoi",
    "TEN": "10 pokoi",
    "MORE": "10+ pokoi",
}
_OTO_MARKET = {
    "primary": "Pierwotny",
    "secondary": "Wtórny",
    "PRIMARY": "Pierwotny",
    "SECONDARY": "Wtórny",
}


def _oto_floor(value) -> str | None:
    raw = str(value or "")
    m = re.search(r"floor_(\d+)", raw)
    if m:
        return m.group(1)
    if "ground" in raw:
        return "Parter"
    if "cellar" in raw or "basement" in raw:
        return "Suterena"
    return None


def _oto_created_at(item: dict, ad: dict) -> str | None:
    """Data pierwszego dodania oferty Otodom.

    Najdokładniejsza jest ad.createdAt ze strony oferty (prawdziwy UTC).
    Lista wyników ma createdAtFirst — to samo, ale w czasie warszawskim
    z błędnym sufiksem „Z” (a bywa też wartością zastępczą w rodzaju
    „1999-02-29 00:00:01”), więc odcinamy „Z” i zostawiamy czas lokalny.
    dateCreated to data ostatniego odświeżenia — celowo jej nie używamy."""
    if ad.get("createdAt"):
        return ad["createdAt"]
    first = str(item.get("createdAtFirst") or "").removesuffix("Z")
    try:
        parsed = datetime.fromisoformat(first)
    except ValueError:
        return None
    return first if parsed.year >= 2000 else None


def parse_offer_otodom(item: dict) -> dict:
    """Oferta Otodom (wynik wyszukiwania + ew. szczegóły) → wspólny format."""
    ad = item.get("_ad") or {}
    target = ad.get("target") or {}

    def money(node):
        return to_float(node.get("value")) if isinstance(node, dict) else None

    price = money(item.get("totalPrice"))
    area = to_float(item.get("areaInSquareMeters"))
    ppm = money(item.get("pricePerSquareMeter"))
    if ppm is None and price and area:
        ppm = round(price / area)

    rooms = _OTO_ROOMS.get(str(item.get("roomsNumber")))
    if rooms is None:
        m = re.search(r"\d+", str(target.get("Rooms_num") or ""))
        if m:
            n = int(m.group())
            rooms = "1 pokój" if n == 1 else (f"{n} pokoje" if n < 5 else f"{n} pokoi")

    loc = item.get("location") or {}
    city = district = None
    addr = loc.get("address") or {}
    if isinstance(addr.get("city"), dict):
        city = addr["city"].get("name")
    for node in (loc.get("reverseGeocoding") or {}).get("locations") or []:
        if isinstance(node, dict) and node.get("locationLevel") == "district":
            district = node.get("name")
            break
    district = district or ad.get("district")

    coords = ad.get("coordinates") or {}
    agency = item.get("agency")
    advert = str(ad.get("advertType") or "").lower()
    business = 1 if (agency or advert in ("agency", "developer", "business")) else 0

    offer_id = item.get("id")
    slug = item.get("slug") or ""
    return {
        "uid": f"oto:{offer_id}",
        "source": "otodom",
        "id": offer_id,
        "url": f"https://www.otodom.pl/pl/oferta/{slug}",
        "title": (item.get("title") or "").strip(),
        "price": price,
        "currency": "PLN",
        "negotiable": 0,
        "area": area,
        "price_per_m": ppm,
        "rooms": rooms,
        "floor": _oto_floor(target.get("Floor_no")),
        "market": _OTO_MARKET.get(str(item.get("market") or ad.get("market"))),
        "city": city,
        "district": district,
        "business": business,
        "created_at": _oto_created_at(item, ad),
        "last_refresh": item.get("pushedUpAt"),
        "lat": to_float(coords.get("latitude")),
        "lon": to_float(coords.get("longitude")),
        "map_radius": 0,
    }


# ---------------------------------------------------------------------- gratka

API_GRATKA = "https://gratka.pl/api-gratka"

GRA_PAGE = 35  # tyle ofert na stronę wydaje API (liczby nie da się zmienić)
# zdjęcia serwuje osobny CDN; w API jest sam identyfikator kadru i nazwa pliku
GRA_THUMB = "https://thumbs.cdngr.pl/thumb/{id}/3x2_m:fill_and_crop/{name}.jpg"

# Gratka to aplikacja Nuxt rozmawiająca z własnym API GraphQL — wysyłamy
# dokładnie te zapytania, co strona otwarta w przeglądarce. Serwis nie wymaga
# do nich ani tokenu, ani zalogowanej sesji.
GRA_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://gratka.pl",
    "Referer": "https://gratka.pl/",
    "X-MZN-Client": "GRATKA",
    "X-MZN-Type": "GRATKA",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# adres wyszukiwania → parametry, które serwis z niego odczytał
GRA_DECODE_QUERY = """query decodeListingUrl($url: String!) {
  decodeListingUrl(url: $url) {
    url
    totalCount
    listingParameters {
      locations { name type }
      searchParameters { transaction type }
    }
  }
}"""

# jedna strona wyników; pola dobrane pod kolumny bazy (patrz parse_offer_gratka).
# Celowo NIE pobieramy relatedProperties ani topPromoted — to oferty spoza
# wyszukiwania, które serwis dokleja do listy jako promowane wstawki.
GRA_SEARCH_QUERY = """query getPropertyListingData($url: String!) {
  searchProperties(url: $url) {
    properties {
      totalCount
      nodes {
        idOnFrontend
        title
        advertisementText
        url
        addedAt
        refreshedAt
        area
        numberOfRooms
        floorFormatted
        price { amount currency }
        priceM2 { amount }
        location { location street map { center { latitude longitude } } }
        contact { company { name type } person { type } }
        development { id name }
      }
    }
  }
}"""

# strona pojedynczej oferty: rynek, dokładne piętro i powierzchnia, opis, zdjęcia
GRA_DETAIL_QUERY = """query getPropertyDetails($url: String!) {
  getProperty(url: $url) {
    marketType
    floor
    area
    description
    photos { id name }
  }
}"""


class GratkaQueryError(RuntimeError):
    """Błąd zgłoszony przez samo API Gratki (HTTP 200 + pole „errors”)."""


def gratka_gql(query: str, variables: dict, delay: float, retries: int = 4) -> dict:
    """Zapytanie do API GraphQL Gratki — odpowiednik http_get dla POST-a.

    Zwraca zawartość pola „data”. Błędy GraphQL przychodzą ze statusem 200,
    więc sprawdzamy je osobno."""
    warm_up(API_GRATKA)
    payload = {"query": query, "variables": variables}
    last_exc = None
    for attempt in range(retries):
        try:
            r = session.post(API_GRATKA, json=payload, timeout=30, headers=GRA_HEADERS)
        except NETWORK_ERRORS as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 3 * (attempt + 1)
            log(f"  ! HTTP {r.status_code} — czekam {wait} s i ponawiam...", err=True)
            time.sleep(wait)
            continue
        if r.status_code == 403:
            raise RuntimeError(
                "Gratka odrzuciła zapytanie (HTTP 403) — serwis uznał je za "
                "automatyczne. Odczekaj kilkanaście minut i spróbuj ponownie, "
                "najlepiej z większą pauzą (np. --delay 1.5)."
            )
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError("Odpowiedź API Gratki nie jest poprawnym JSON-em — serwis mógł zmienić API.")
        pause(delay)
        errors = data.get("errors")
        if errors:
            first = errors[0].get("message") if isinstance(errors[0], dict) else errors[0]
            raise GratkaQueryError(f"API Gratki odrzuciło zapytanie: {first}")
        return data.get("data") or {}
    raise RuntimeError(f"Nie udało się odpytać API Gratki ({last_exc})")


def gratka_path(url: str, **extra) -> str:
    """Adres wyszukiwania → ścieżka w postaci, jakiej oczekuje API.

    API przyjmuje dokładnie to, co widać w pasku adresu przeglądarki (np.
    „/nieruchomosci/mieszkania/krakow?cena-calkowita:max=700000”), więc
    zachowujemy parametry użytkownika i dokładamy tylko własne (page, sort)."""
    parsed = urlparse(url if "//" in url else "https://gratka.pl" + url)
    params = dict(parse_qsl(parsed.query))
    params.update({k: str(v) for k, v in extra.items() if v is not None})
    # dwukropek w nazwach filtrów Gratki („cena-calkowita:max”) zostawiamy
    # nieprzekodowany — tak zapisuje go sam serwis
    query = urlencode(params, safe=":")
    return (parsed.path or "/") + (f"?{query}" if query else "")


def decode_gratka(url: str, delay: float) -> dict:
    """Parametry, które Gratka odczytała z adresu wyszukiwania.

    Pozwala sprawdzić adres, zanim zaczniemy pobieranie, i poznać deklarowaną
    liczbę ofert — odpowiednik reported_count() dla OLX-a."""
    try:
        data = gratka_gql(GRA_DECODE_QUERY, {"url": gratka_path(url)}, delay)
    except GratkaQueryError as exc:
        raise RuntimeError(
            f"Gratka nie rozpoznaje tego wyszukiwania:\n  {url}\n"
            "Otwórz listę ofert na gratka.pl i skopiuj adres z paska przeglądarki.\n"
            f"(odpowiedź serwisu: {exc})"
        )
    info = data.get("decodeListingUrl")
    if not info:
        raise RuntimeError(f"Gratka nie zwróciła parametrów wyszukiwania dla adresu {url}.")
    return info


def gratka_page(url: str, extra: dict, delay: float) -> tuple[list, int]:
    """Jedna strona wyników: (oferty, deklarowana liczba wszystkich ofert)."""
    data = gratka_gql(GRA_SEARCH_QUERY, {"url": gratka_path(url, **extra)}, delay)
    props = ((data.get("searchProperties") or {}).get("properties")) or {}
    nodes = [n for n in (props.get("nodes") or []) if isinstance(n, dict) and _gra_id(n) is not None]
    return nodes, int(props.get("totalCount") or 0)


def fetch_all_gratka(url: str, delay: float) -> list[dict]:
    """Pobiera wszystkie oferty z wyszukiwania Gratki.

    W odróżnieniu od OLX-a i Otodomu serwis wydaje wyniki do ostatniej strony,
    więc nie trzeba dzielić wyszukiwania na przedziały cenowe — wystarczy
    przejść kolejne strony."""
    collected: dict[int, dict] = {}
    nodes, total = gratka_page(url, {}, delay)
    pages = -(-total // GRA_PAGE) if total else 1
    page = 1
    while nodes:
        for node in nodes:
            collected[_gra_id(node)] = node
        print(f"\r  [Gratka] pobrano {len(collected)} z {total} ofert...", end="", flush=True)
        page += 1
        if page > pages:
            break
        nodes, _ = gratka_page(url, {"page": page}, delay)
    print()
    if total and len(collected) < total * 0.9:
        log(f"  ! [Gratka] serwis zapowiadał {total} ofert, a wydał {len(collected)}.")
    log(f"[Gratka] pobrano łącznie {len(collected)} unikalnych ofert.")
    return list(collected.values())


def fetch_new_quick_gratka(url: str, known_ids: set, delay: float) -> list[dict]:
    """Szybki tryb dla Gratki: od najnowszych, aż trafimy na same znane."""
    fresh, seen = [], set()
    page = 1
    while True:
        nodes, total = gratka_page(url, {"sort": "newest", "page": page}, delay)
        if not nodes:
            break
        page_new = [n for n in nodes if _gra_id(n) not in known_ids and _gra_id(n) not in seen]
        seen.update(_gra_id(n) for n in page_new)
        fresh.extend(page_new)
        print(f"\r  [Gratka] sprawdzono {page} str., nowych: {len(fresh)}...", end="", flush=True)
        if not page_new or page * GRA_PAGE >= total:
            break
        page += 1
    print()
    return fresh


def fetch_gratka_detail(offer_path: str, delay: float) -> dict | None:
    """Strona pojedynczej oferty: rynek, piętro, opis, zdjęcia.
    Zwraca odchudzony słownik albo None, gdy oferty nie da się pobrać — np.
    właśnie zniknęła z serwisu (API zwraca wtedy getProperty: null)."""
    if not offer_path:
        return None
    try:
        data = gratka_gql(GRA_DETAIL_QUERY, {"url": offer_path}, delay)
    except (RuntimeError,) + NETWORK_ERRORS:
        return None  # oferta mogła właśnie zniknąć — trudno
    prop = data.get("getProperty")
    if not prop:
        return None
    slim = {k: prop.get(k) for k in ("marketType", "floor", "area", "description") if prop.get(k) is not None}
    images = [
        GRA_THUMB.format(id=photo["id"], name=photo.get("name") or "zdjecie")
        for photo in (prop.get("photos") or [])[:8]
        if isinstance(photo, dict) and photo.get("id")
    ]
    if images:
        slim["images"] = images
    return slim or None


def enrich_gratka(items: list[dict], stored: dict, delay: float) -> None:
    """Dociąga szczegóły ofert, które ich jeszcze nie mają (rynek, opis, zdjęcia).
    Wynik wkłada do item["_detail"] — trafi do bazy razem z surowymi danymi.

    Ofertom znanym z poprzednich uruchomień podkładamy szczegóły zapisane
    wtedy w bazie: rynek, piętro i dokładna powierzchnia są kolumnami bazy,
    więc bez tego zwykły skan listy wyczyściłby je przy aktualizacji."""
    todo = []
    for item in items:
        detail = stored.get(_gra_id(item))
        if detail:
            item["_detail"] = detail
        else:
            todo.append(item)
    if not todo:
        return
    est = int(len(todo) * (delay + 0.25) / 60) + 1
    log(f"[Gratka] dociągam szczegóły {len(todo)} ofert (rynek, opisy, zdjęcia — ok. {est} min)...")
    for done, item in enumerate(todo, 1):
        detail = fetch_gratka_detail(item.get("url") or "", delay)
        if detail:
            item["_detail"] = detail
        print(f"\r  [Gratka] szczegóły {done}/{len(todo)}...", end="", flush=True)
    print()


_GRA_MARKET = {"Rynek pierwotny": "Pierwotny", "Rynek wtórny": "Wtórny"}
_GRA_VOIVODESHIPS = {
    "dolnośląskie",
    "kujawsko-pomorskie",
    "lubelskie",
    "lubuskie",
    "łódzkie",
    "małopolskie",
    "mazowieckie",
    "opolskie",
    "podkarpackie",
    "podlaskie",
    "pomorskie",
    "śląskie",
    "świętokrzyskie",
    "warmińsko-mazurskie",
    "wielkopolskie",
    "zachodniopomorskie",
}
# tylko do komunikatu o rozpoznanym wyszukiwaniu (patrz GratkaSource.prepare)
_GRA_TRANSACTION = {"SALE": "sprzedaż", "RENT": "wynajem"}
_GRA_TYPE = {
    "FLAT": "mieszkania",
    "HOUSE": "domy",
    "PLOT": "działki",
    "COMMERCIAL_PROPERTY": "lokale użytkowe",
    "GARAGE": "garaże",
    "ROOM": "pokoje",
}


def _gra_id(item: dict) -> int | None:
    """Numer ogłoszenia widoczny w adresie oferty (pole „id” to klucz wewnętrzny)."""
    try:
        return int(item.get("idOnFrontend"))
    except (TypeError, ValueError):
        return None


def _gra_title(item: dict) -> str:
    """Tytuł oferty. Lista wyników podaje w „title” samą kategorię („mieszkanie
    na sprzedaż”), więc bierzemy nagłówek ogłoszenia, a gdy go brak —
    dopisujemy do kategorii ulicę, żeby oferty dało się od siebie odróżnić."""
    headline = " ".join((item.get("advertisementText") or "").split())
    if headline:
        return headline
    title = " ".join((item.get("title") or "").split())
    street = " ".join(((item.get("location") or {}).get("street") or "").split())
    return f"{title}, {street}" if title and street else (title or street)


def _gra_place(item: dict) -> tuple[str | None, str | None]:
    """(miasto, dzielnica) z hierarchii lokalizacji oferty.

    Gratka podaje dwa poziomy, ale nie zawsze te same: przy ofercie z podaną
    dzielnicą jest to [miasto, dzielnica], a bez niej — [województwo, miasto].
    Pola county/commune bywają puste, więc rozpoznajemy województwo po nazwie."""
    names = [n.strip() for n in ((item.get("location") or {}).get("location") or []) if isinstance(n, str)]
    names = [n for n in names if n]
    if names and names[0].lower() in _GRA_VOIVODESHIPS:
        names = names[1:]
    city = names[0] if names else None
    district = names[1] if len(names) > 1 else None
    return city, district


def _gra_floor(item: dict, detail: dict) -> str | None:
    """Piętro w formacie wspólnym dla portali: „3”, „Parter”, „Suterena”."""
    floor = detail.get("floor")
    if isinstance(floor, int):
        if floor < 0:
            return "Suterena"
        return "Parter" if floor == 0 else str(floor)
    # z listy wyników przychodzi etykieta w stylu „piętro 2/3” albo „parter/4”
    # (piętro oferty / liczba pięter w budynku)
    label = str(item.get("floorFormatted") or "").split("/")[0].strip().lower()
    m = re.search(r"\d+", label)
    if m:
        return m.group()
    return label.capitalize() or None


def parse_offer_gratka(item: dict) -> dict:
    """Oferta Gratki (wynik wyszukiwania + ew. szczegóły) → wspólny format."""
    detail = item.get("_detail") or {}

    def money(node):
        return to_float(node.get("amount")) if isinstance(node, dict) else None

    price = money(item.get("price"))
    # szczegóły oferty podają powierzchnię dokładniej (27,75 zamiast 27)
    area = to_float(detail.get("area")) or to_float(item.get("area"))
    ppm = money(item.get("priceM2"))
    if ppm is None and price and area:
        ppm = round(price / area)

    loc = item.get("location") or {}
    center = (loc.get("map") or {}).get("center") or {}
    city, district = _gra_place(item)
    contact = item.get("contact") or {}
    offer_id = _gra_id(item)
    return {
        "uid": f"gra:{offer_id}",
        "source": "gratka",
        "id": offer_id,
        "url": "https://gratka.pl" + (item.get("url") or ""),
        "title": _gra_title(item),
        "price": price,
        "currency": (item.get("price") or {}).get("currency") or "PLN",
        "negotiable": 0,
        "area": area,
        "price_per_m": ppm,
        "rooms": item.get("numberOfRooms"),
        "floor": _gra_floor(item, detail),
        "market": _GRA_MARKET.get(detail.get("marketType")),
        "city": city,
        "district": district,
        # ogłoszenia prywatne są na Gratce rzadkością — niemal wszystko
        # wystawiają biura i deweloperzy (contact.company / development)
        "business": 1 if (contact.get("company") or item.get("development")) else 0,
        "created_at": item.get("addedAt"),
        "last_refresh": item.get("refreshedAt"),
        "lat": to_float(center.get("latitude")),
        "lon": to_float(center.get("longitude")),
        # API podaje dokładny punkt, bez promienia przybliżenia
        "map_radius": 0,
    }


# ----------------------------------------------------- interpretacja ofert OLX


def parse_offer_olx(offer: dict) -> dict:
    """Wyciąga z surowego JSON-a oferty pola, które trzymamy w bazie."""
    by_key: dict[str, dict | str] = {}
    for p in offer.get("params") or []:
        if isinstance(p, dict) and p.get("key"):
            by_key[p["key"]] = p.get("value")

    def val(key: str, field: str | None = None):
        v = by_key.get(key)
        if isinstance(v, dict):
            if field:
                return v.get(field)
            return v.get("key", v.get("label"))
        return v

    price = currency = None
    negotiable = 0
    pv = by_key.get("price")
    if isinstance(pv, dict):
        price = to_float(pv.get("value"))
        currency = pv.get("currency")
        negotiable = 1 if pv.get("negotiable") else 0

    area = to_float(val("m"))
    price_per_m = to_float(val("price_per_m"))
    if price_per_m is None and price and area:
        price_per_m = round(price / area)

    location = offer.get("location") or {}

    def loc_name(key: str):
        node = location.get(key)
        return node.get("name") if isinstance(node, dict) else None

    map_node = offer.get("map") if isinstance(offer.get("map"), dict) else {}

    return {
        "uid": f"olx:{offer.get('id')}",
        "source": "olx",
        "id": offer.get("id"),
        "url": offer.get("url"),
        "title": (offer.get("title") or "").strip(),
        "price": price,
        "currency": currency or "PLN",
        "negotiable": negotiable,
        "area": area,
        "price_per_m": price_per_m,
        "rooms": val("rooms", "label") or val("rooms"),
        "floor": val("floor_select", "label") or val("floor", "label"),
        "market": val("market", "label") or val("market"),
        "city": loc_name("city"),
        "district": loc_name("district"),
        "business": 1 if offer.get("business") else 0,
        "created_at": offer.get("created_time"),
        "last_refresh": offer.get("last_refresh_time"),
        "lat": to_float(map_node.get("lat")),
        "lon": to_float(map_node.get("lon")),
        # promień > 0 = ogłoszeniodawca podał tylko przybliżoną lokalizację
        "map_radius": to_float(map_node.get("radius")) or 0,
    }


# ------------------------------------------------------------------------ baza

OFFER_COLUMNS = (
    "uid",
    "source",
    "id",
    "url",
    "title",
    "price",
    "currency",
    "negotiable",
    "area",
    "price_per_m",
    "rooms",
    "floor",
    "market",
    "city",
    "district",
    "business",
    "created_at",
    "last_refresh",
    "lat",
    "lon",
    "map_radius",
)


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS offers(
            uid TEXT PRIMARY KEY,
            source TEXT, id INTEGER,
            url TEXT, title TEXT,
            price REAL, currency TEXT, negotiable INTEGER,
            area REAL, price_per_m REAL, rooms TEXT, floor TEXT, market TEXT,
            city TEXT, district TEXT, business INTEGER,
            created_at TEXT, last_refresh TEXT,
            lat REAL, lon REAL, map_radius REAL,
            first_seen TEXT, last_seen TEXT,
            active INTEGER DEFAULT 1,
            raw TEXT
        );
        CREATE TABLE IF NOT EXISTS price_history(
            offer_uid TEXT,
            ts TEXT,
            price REAL
        );
        CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)


def init_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    init_schema(con)
    migrate_db(con)
    return con


def migrate_db(con: sqlite3.Connection) -> None:
    """Dostosowuje bazy założone starszymi wersjami skryptu — bez ponownego
    pobierania czegokolwiek. Trzy etapy:
    1) baza jednoportalowa (klucz = numer oferty OLX) → wieloportalowa
       (klucz uid „olx:123”/„oto:456” + kolumna source), razem z historią cen,
    2) uzupełnienie współrzędnych z zapisanego surowego JSON-a ofert,
    3) jednorazowa poprawka dat dodania ofert Otodom (patrz _oto_created_at)."""
    offer_cols = {row[1] for row in con.execute("PRAGMA table_info(offers)")}
    if "uid" not in offer_cols:
        log("(dostosowuję bazę do obsługi wielu portali — chwilka...)")
        old_cols = [c for c in OFFER_COLUMNS if c in offer_cols]
        con.executescript("""
            ALTER TABLE offers RENAME TO offers_v1;
            ALTER TABLE price_history RENAME TO price_history_v1;
        """)
        init_schema(con)
        con.execute(
            f"INSERT INTO offers(uid, source, {', '.join(old_cols)}, "
            f"first_seen, last_seen, active, raw) "
            f"SELECT 'olx:' || id, 'olx', {', '.join(old_cols)}, "
            f"first_seen, last_seen, active, raw FROM offers_v1"
        )
        con.execute(
            "INSERT INTO price_history(offer_uid, ts, price) "
            "SELECT 'olx:' || offer_id, ts, price FROM price_history_v1"
        )
        con.executescript("DROP TABLE offers_v1; DROP TABLE price_history_v1;")
        con.commit()
    rows = con.execute(
        "SELECT uid, raw FROM offers WHERE lat IS NULL AND raw IS NOT NULL AND source = 'olx'"
    ).fetchall()
    filled = 0
    for uid, raw in rows:
        try:
            map_node = json.loads(raw).get("map") or {}
        except (ValueError, AttributeError):
            continue
        lat, lon = to_float(map_node.get("lat")), to_float(map_node.get("lon"))
        if lat is None or lon is None:
            continue
        con.execute(
            "UPDATE offers SET lat = ?, lon = ?, map_radius = ? WHERE uid = ?",
            (lat, lon, to_float(map_node.get("radius")) or 0, uid),
        )
        filled += 1
    if filled:
        con.commit()
        log(f"(uzupełniono współrzędne {filled} ofert z danych już zapisanych w bazie)")

    # starsze wersje zapisywały jako datę dodania Otodom datę odświeżenia
    if meta_get(con, "fix:otodom_created_at") is None:
        fixed = 0
        for uid, created, raw in con.execute(
            "SELECT uid, created_at, raw FROM offers WHERE source = 'otodom' AND raw IS NOT NULL"
        ).fetchall():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            value = _oto_created_at(item, item.get("_ad") or {})
            if value != created:
                con.execute("UPDATE offers SET created_at = ? WHERE uid = ?", (value, uid))
                fixed += 1
        meta_set(con, "fix:otodom_created_at", "1")
        con.commit()
        if fixed:
            log(
                f"(poprawiono datę dodania {fixed} ofert Otodom — wcześniej "
                "zapisywana była data ostatniego odświeżenia)"
            )


def meta_get(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def sync(con: sqlite3.Connection, records: list[tuple[dict, dict]], full_scan_sources: set) -> dict:
    """Zapisuje pobrane oferty i zwraca różnice względem poprzedniego stanu.

    records: pary (rozparsowana_oferta, surowy_json_dict).
    full_scan_sources: portale przeskanowane w całości — tylko dla nich
    wolno uznać nieobecne oferty za wycofane."""
    now = datetime.now().isoformat(timespec="seconds")
    first_run = con.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0

    new, price_changes, returned = [], [], []
    fetched_uids: set = set()
    update_cols = [c for c in OFFER_COLUMNS[1:] if c not in ("lat", "lon", "map_radius")]

    for o, raw in records:
        if o["id"] is None:
            continue
        fetched_uids.add(o["uid"])
        row = con.execute("SELECT price, active, raw FROM offers WHERE uid = ?", (o["uid"],)).fetchone()
        if row is None:
            con.execute(
                f"INSERT INTO offers({', '.join(OFFER_COLUMNS)}, "
                f"first_seen, last_seen, active, raw) "
                f"VALUES ({', '.join('?' * len(OFFER_COLUMNS))}, ?, ?, 1, ?)",
                tuple(o[c] for c in OFFER_COLUMNS) + (now, now, json.dumps(raw, ensure_ascii=False)),
            )
            if o["price"] is not None:
                con.execute(
                    "INSERT INTO price_history VALUES (?, ?, ?)",
                    (o["uid"], now, o["price"]),
                )
            new.append(o)
        else:
            old_price, was_active, old_raw_txt = row
            changed = o["price"] is not None and (
                old_price is None or int(round(o["price"])) != int(round(old_price))
            )
            if changed:
                price_changes.append((o, old_price))
                con.execute(
                    "INSERT INTO price_history VALUES (?, ?, ?)",
                    (o["uid"], now, o["price"]),
                )
            # szczegóły ze stron ofert (Otodom: opis, zdjęcia, współrzędne;
            # Gratka: rynek, opis, zdjęcia) dociągamy raz — przy zwykłym
            # skanie listy przenosimy je ze starego rekordu
            detail_key = DETAIL_KEYS.get(o["source"])
            if detail_key and detail_key not in raw and old_raw_txt:
                try:
                    old_detail = json.loads(old_raw_txt).get(detail_key)
                    if old_detail:
                        raw = dict(raw, **{detail_key: old_detail})
                except ValueError:
                    pass
            con.execute(
                f"UPDATE offers SET "
                f"{', '.join(c + ' = ?' for c in update_cols)}, "
                f"lat = COALESCE(?, lat), lon = COALESCE(?, lon), "
                f"map_radius = COALESCE(?, map_radius), "
                f"last_seen = ?, active = 1, raw = ? WHERE uid = ?",
                tuple(o[c] for c in update_cols)
                + (
                    o["lat"],
                    o["lon"],
                    o["map_radius"] if o["lat"] is not None else None,
                    now,
                    json.dumps(raw, ensure_ascii=False),
                    o["uid"],
                ),
            )
            if not was_active:
                returned.append(o)

    removed = []
    if full_scan_sources and not first_run:
        marks = ", ".join("?" * len(full_scan_sources))
        for row in con.execute(
            f"SELECT uid, title, price, area, district, url, source "
            f"FROM offers WHERE active = 1 AND source IN ({marks})",
            tuple(full_scan_sources),
        ):
            if row[0] not in fetched_uids:
                removed.append(
                    dict(
                        zip(
                            (
                                "uid",
                                "title",
                                "price",
                                "area",
                                "district",
                                "url",
                                "source",
                            ),
                            row,
                        )
                    )
                )
        for item in removed:
            con.execute("UPDATE offers SET active = 0 WHERE uid = ?", (item["uid"],))

    con.commit()
    return {
        "first_run": first_run,
        "new": new,
        "price_changes": price_changes,
        "removed": removed,
        "returned": returned,
    }


# ------------------------------------------------------------- źródła danych


class Source:
    """Wspólny interfejs źródła ofert.

    Nowy portal = nowa podklasa + wpis w SOURCES. Reszta skryptu (main, baza,
    raport, eksporty) nie zna szczegółów żadnego serwisu — rozmawia z nimi
    wyłącznie przez poniższe metody."""

    name = ""  # klucz źródła: w bazie, w uid ofert i we fladze --source
    label = ""  # nazwa wyświetlana w komunikatach
    domains: tuple = ()  # domeny rozpoznawane w adresach --url
    default_url = ""  # wyszukiwanie używane, gdy nie podano --url
    detail_key = ""  # klucz w surowym JSON-ie, pod którym siedzą dociągnięte
    # szczegóły oferty; ustaw, gdy enrich() pobiera je raz na ofertę

    def __init__(self, url: str, delay: float):
        self.url = url
        self.delay = delay

    @classmethod
    def handles(cls, url: str) -> bool:
        host = urlparse(url).netloc
        return any(domain in host for domain in cls.domains)

    # --- do nadpisania w podklasach (prepare/enrich/save_state opcjonalnie) ---

    def prepare(self, con: sqlite3.Connection, overrides: dict) -> None:
        """Jednorazowe przygotowania przed pobieraniem (np. parametry API)."""

    def fetch_all(self) -> list[dict]:
        """Pełny skan: wszystkie oferty z wyszukiwania."""
        raise NotImplementedError

    def fetch_new(self, known_ids: set) -> list[dict]:
        """Szybki skan: tylko oferty spoza known_ids, od najnowszych."""
        raise NotImplementedError

    def enrich(self, items: list[dict], con: sqlite3.Connection) -> None:
        """Opcjonalne dociągnięcie szczegółów (współrzędne, opisy)."""

    def parse(self, raw: dict) -> dict:
        """Surowa oferta z portalu → wspólny format bazy (OFFER_COLUMNS)."""
        raise NotImplementedError

    def save_state(self, con: sqlite3.Connection) -> None:
        """Zapis stanu do bazy — wołane dopiero PO udanym pobraniu."""


class OlxSource(Source):
    name = "olx"
    label = "OLX"
    domains = ("olx.pl",)
    default_url = "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/krakow/"

    def __init__(self, url: str, delay: float):
        super().__init__(url, delay)
        self.params: dict = {}
        self.city_slug = None

    def prepare(self, con, overrides):
        cached = meta_get(con, f"api_params::{self.url}")
        if (
            cached is None
            and not con.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'api_params::%'").fetchone()[0]
        ):
            # bazy ze starszych wersji trzymały parametry pod wspólnym kluczem
            cached = meta_get(con, "api_params")
            if cached:
                log("(korzystam z parametrów zapamiętanych w bazie)")
        if cached and not any(overrides.values()):
            self.params = json.loads(cached)
            _, _, self.city_slug = parse_search_url(self.url)
        else:
            log("Ustalam parametry wyszukiwania...")
            self.params, self.city_slug = resolve_api_params(self.url, overrides)
        log("Parametry: " + ", ".join(f"{k}={v}" for k, v in sorted(self.params.items())))

    def fetch_all(self):
        return fetch_all_olx(self.params, self.city_slug, self.delay)

    def fetch_new(self, known_ids):
        return fetch_new_quick_olx(self.params, known_ids, self.city_slug, self.delay)

    def parse(self, raw):
        return parse_offer_olx(raw)

    def save_state(self, con):
        # dopiero po udanym pobraniu — złych parametrów nie chcemy zapamiętać
        meta_set(con, f"api_params::{self.url}", json.dumps(self.params, ensure_ascii=False))


class OtodomSource(Source):
    name = "otodom"
    detail_key = "_ad"
    label = "Otodom"
    domains = ("otodom.pl",)
    default_url = "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/malopolskie/krakow/krakow/krakow"

    def fetch_all(self):
        return fetch_all_otodom(self.url, self.delay)

    def fetch_new(self, known_ids):
        return fetch_new_quick_otodom(self.url, known_ids, self.delay)

    def enrich(self, items, con):
        have_detail = {
            row[0]
            for row in con.execute(
                "SELECT id FROM offers WHERE source = ? AND lat IS NOT NULL",
                (self.name,),
            )
        }
        enrich_otodom(items, have_detail, self.delay)

    def parse(self, raw):
        return parse_offer_otodom(raw)


class GratkaSource(Source):
    name = "gratka"
    label = "Gratka"
    domains = ("gratka.pl",)
    default_url = "https://gratka.pl/nieruchomosci/mieszkania/krakow"
    detail_key = "_detail"

    def prepare(self, con, overrides):
        """Sprawdza adres wyszukiwania i mówi, co serwis z niego odczytał.
        Zły adres kończy się tu czytelnym błędem, a nie pustym wynikiem."""
        info = decode_gratka(self.url, self.delay)
        params = (info.get("listingParameters") or {}).get("searchParameters") or {}
        places = [
            loc.get("name")
            for loc in (info.get("listingParameters") or {}).get("locations") or []
            if loc.get("name")
        ]
        what = ", ".join(_GRA_TYPE.get(t, str(t).lower()) for t in params.get("type") or []) or "?"
        deal = _GRA_TRANSACTION.get(params.get("transaction"), str(params.get("transaction") or "?").lower())
        where = ", ".join(places) or "?"
        log(f"Wyszukiwanie: {what}, {deal}, {where} — {info.get('totalCount') or 0} ofert.")

    def fetch_all(self):
        return fetch_all_gratka(self.url, self.delay)

    def fetch_new(self, known_ids):
        return fetch_new_quick_gratka(self.url, known_ids, self.delay)

    def enrich(self, items, con):
        stored = {}
        for offer_id, raw in con.execute(
            "SELECT id, raw FROM offers WHERE source = ? AND raw IS NOT NULL",
            (self.name,),
        ):
            try:
                detail = json.loads(raw).get(self.detail_key)
            except ValueError:
                continue
            if detail:
                stored[offer_id] = detail
        enrich_gratka(items, stored, self.delay)

    def parse(self, raw):
        return parse_offer_gratka(raw)


SOURCES: dict[str, type] = {cls.name: cls for cls in (OlxSource, OtodomSource, GratkaSource)}

# portale, które dociągają szczegóły ofert osobnym zapytaniem (patrz sync)
DETAIL_KEYS = {name: cls.detail_key for name, cls in SOURCES.items() if cls.detail_key}


def source_for(url: str):
    """Klasa źródła obsługująca dany adres (None = adres nieobsługiwany)."""
    return next((cls for cls in SOURCES.values() if cls.handles(url)), None)


PORTAL_NAME = {name: cls.label for name, cls in SOURCES.items()}


# ------------------------------------------------------------------ eksport CSV


def export_csv(con: sqlite3.Connection, path: str) -> None:
    """Zapis aktywnych ofert do CSV (średnik + BOM → otwiera się wprost w Excelu)."""
    rows = con.execute("""
        SELECT source, id, title, price, price_per_m, area, rooms, floor,
               market, district, city, business, negotiable, lat, lon,
               created_at, first_seen, url
        FROM offers WHERE active = 1
        ORDER BY price_per_m IS NULL, price_per_m
    """).fetchall()
    headers = (
        "portal",
        "id",
        "tytul",
        "cena",
        "cena_za_m2",
        "metraz_m2",
        "pokoje",
        "pietro",
        "rynek",
        "dzielnica",
        "miasto",
        "od_firmy",
        "do_negocjacji",
        "szer_geo",
        "dl_geo",
        "data_dodania",
        "pierwszy_raz_widziana",
        "link",
    )
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)
    log(f"\n✔ Wyeksportowano {len(rows)} aktywnych ofert do „{path}”.")


# ------------------------------------------------------------------------ main


def parse_cli(argv: list[str] | None):
    portals = ", ".join(cls.label for cls in SOURCES.values())
    parser = argparse.ArgumentParser(
        description=f"Monitor ofert nieruchomości z portali: {portals}. "
        "Pierwszy raz pobiera wszystko, potem pokazuje tylko "
        "nowe oferty i zmiany.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        action="append",
        help="adres wyszukiwania z obsługiwanego portalu; można "
        "podać kilka razy (domyślnie: domyślne wyszukiwanie "
        "każdego źródła — mieszkania na sprzedaż w Krakowie)",
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=sorted(SOURCES),
        help="zbieraj dane tylko z tego źródła; można podać kilka razy (domyślnie: wszystkie zdefiniowane)",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="plik bazy SQLite")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="szybki tryb: sprawdza tylko NOWE oferty (bez zmian cen i zniknięć)",
    )
    parser.add_argument(
        "--export",
        metavar="PLIK.csv",
        help="po zakończeniu zapisz aktywne oferty do pliku CSV",
    )
    parser.add_argument(
        "--html",
        metavar="PLIK.html",
        help="wygeneruj interaktywną mapę ofert z filtrami (jeden samodzielny plik HTML)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="nie odpytuj portali (np. sam eksport CSV/HTML z bazy)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.6,
        help="pauza w sekundach między zapytaniami do portali",
    )
    parser.add_argument("--category-id", help="ręcznie: id kategorii (tylko OLX)")
    parser.add_argument("--city-id", help="ręcznie: id miasta (tylko OLX)")
    parser.add_argument("--region-id", help="ręcznie: id województwa (tylko OLX)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="pozwól użyć bazy utworzonej dla innego adresu URL",
    )
    return parser.parse_args(argv)


def select_sources(args) -> list[tuple[type, str]]:
    """Zamienia --url/--source na listę par (klasa_źródła, adres)."""
    urls = list(dict.fromkeys(args.url or [cls.default_url for cls in SOURCES.values()]))
    chosen = []
    for url in urls:
        cls = source_for(url)
        if cls is None:
            supported = ", ".join(d for c in SOURCES.values() for d in c.domains)
            sys.exit(f"Nieobsługiwany adres: {url}\nSkrypt rozumie wyszukiwania z: {supported}.")
        chosen.append((cls, url))
    if args.source:
        wanted = set(args.source)
        chosen = [(cls, url) for cls, url in chosen if cls.name in wanted]
        if not chosen:
            sys.exit(
                "Po zawężeniu --source nie został żaden adres do "
                "sprawdzenia. Wybrane źródła: "
                + ", ".join(sorted(wanted))
                + ". Sprawdź, czy pasują do adresów podanych w --url."
            )
    return chosen


def check_db_urls(con, db_path: str, urls: list, force: bool) -> list:
    """Pilnuje, żeby nie mieszać różnych wyszukiwań w jednej bazie.
    Zwraca listę adresów zapamiętanych w bazie (do późniejszego scalenia)."""
    stored_raw = meta_get(con, "search_url")
    if not stored_raw:
        return []
    try:
        stored = json.loads(stored_raw)
        stored = [stored] if isinstance(stored, str) else list(stored)
    except ValueError:
        stored = [stored_raw]
    new, old = set(urls), set(stored)
    if new == old or force:
        return stored
    if new < old:
        log(
            "(tym razem sprawdzam tylko część zapisanych wyszukiwań — "
            "dane pozostałych źródeł zostają w bazie bez zmian)"
        )
    elif new > old:
        log(
            "(rozszerzasz to wyszukiwanie o nowy adres — istniejące "
            "dane zostają, dojdą oferty z nowego adresu)"
        )
    else:
        sys.exit(
            f"Ta baza ({db_path}) była utworzona dla innego wyszukiwania:\n"
            + "\n".join(f"  {u}" for u in stored)
            + "\nUżyj osobnego pliku bazy (--db inna_nazwa.db) albo dodaj "
            "--force, jeśli świadomie zmieniasz wyszukiwanie."
        )
    return stored


def collect(source, con, quick: bool) -> tuple[list, bool]:
    """Pobiera oferty z jednego źródła. Zwraca (rekordy, czy_pełny_skan)."""
    known = {row[0] for row in con.execute("SELECT id FROM offers WHERE source = ?", (source.name,))}
    if quick and known:
        log("Szybki tryb: szukam tylko nowych ofert (od najnowszych)...")
        items, full_scan = source.fetch_new(known), False
    else:
        if quick and not known:
            log("Pierwszy skan tego źródła — muszę pobrać wszystko (--quick zadziała od następnego razu).")
        items, full_scan = source.fetch_all(), True
    source.enrich(items, con)
    return [(source.parse(it), it) for it in items], full_scan


def main(argv: list[str] | None = None) -> None:
    # opcjonalne ustawienia (np. CARTO_API_KEY) z pliku .env w głównym katalogu repozytorium;
    # zmienne ustawione już w środowisku mają pierwszeństwo
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    args = parse_cli(argv)
    chosen = select_sources(args)

    if not args.offline and not IMPERSONATE:
        log(
            "Wskazówka: 'pip install curl_cffi' znacząco zmniejsza ryzyko "
            "blokady HTTP 403 (skrypt użyje tej biblioteki automatycznie)."
        )

    if args.db == DEFAULT_DB and not os.path.exists(DEFAULT_DB) and os.path.exists(LEGACY_DB):
        os.replace(LEGACY_DB, DEFAULT_DB)
        log(
            f"(znalazłem bazę ze starszej wersji skryptu — zmieniam nazwę "
            f"{LEGACY_DB} → {DEFAULT_DB}, wszystkie dane zostają)"
        )

    con = init_db(args.db)
    urls = [url for _, url in chosen]
    stored_urls = [] if args.offline else check_db_urls(con, args.db, urls, args.force)

    if args.offline:
        if args.export:
            export_csv(con, args.export)
        if args.html:
            export_html(con, args.html, offline=True)
        if not args.export and not args.html:
            log(
                "Tryb --offline: nic nie pobrano. Dodaj --export PLIK.csv "
                "lub --html PLIK.html, aby wyeksportować dane z bazy."
            )
        return

    overrides = {
        "category_id": args.category_id,
        "city_id": args.city_id,
        "region_id": args.region_id,
    }
    records: list[tuple[dict, dict]] = []
    full_scan_sources: set = set()

    for source_cls, url in chosen:
        source = source_cls(url, args.delay)
        log(f"\n[{source.label}] {url}")
        source.prepare(con, overrides)
        items, full_scan = collect(source, con, args.quick)
        records += items
        if full_scan:
            full_scan_sources.add(source.name)
        source.save_state(con)

    result = sync(con, records, full_scan_sources)
    final_urls = set(urls) if args.force else set(urls) | set(stored_urls)
    meta_set(con, "search_url", json.dumps(sorted(final_urls)))
    meta_set(con, "last_run", datetime.now().isoformat(timespec="seconds"))
    con.commit()

    report(result, args.db, con, PORTAL_NAME)
    if args.export:
        export_csv(con, args.export)
    if args.html:
        export_html(con, args.html)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nPrzerwano — baza nie została zmieniona.")
    except RuntimeError as exc:
        sys.exit(f"\nBłąd: {exc}")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_ofert.py — monitor ofert nieruchomości z wielu portali (OLX, Otodom)

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
    python monitor_ofert.py                      # wszystkie źródła (OLX + Otodom)
    python monitor_ofert.py --source olx         # tylko jedno źródło
    python monitor_ofert.py --quick              # szybki tryb: tylko NOWE oferty
    python monitor_ofert.py --export oferty.csv  # zrzut aktywnych ofert do CSV
    python monitor_ofert.py --html mapa.html     # interaktywna mapa ofert
    python monitor_ofert.py --db tanie.db \\
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
    Otodomu). Serwisy mogą je w każdej chwili zmienić.
  * Jedno zapytanie zwraca ograniczoną liczbę wyników, więc przy większych
    wyszukiwaniach skrypt automatycznie dzieli pobieranie na przedziały cen.
  * Między zapytaniami jest pauza (--delay, domyślnie 0,6 s) — nie zmniejszaj
    jej agresywnie; to narzędzie do prywatnego monitoringu, a nie masowego
    scrapingu. Regulaminy portali ograniczają automatyczny dostęp.
"""

from __future__ import annotations

import argparse
import csv
import html as html_lib
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import time
import random
import unicodedata
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, quote

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
        sys.exit("Brakuje biblioteki HTTP. Zainstaluj:  pip install curl_cffi\n"
                 "(zadziała też samo 'pip install requests', ale bywa częściej "
                 "blokowane przez OLX)")

# klasy wyjątków sieciowych różnią się między requests a curl_cffi
NETWORK_ERRORS = tuple(dict.fromkeys(filter(None, (
    getattr(getattr(requests, "exceptions", None), "RequestException", None),
    getattr(requests, "RequestsError", None),
)))) or (OSError,)

# ---------------------------------------------------------------- konfiguracja

DEFAULT_DB = "oferty.db"
LEGACY_DB = "olx_oferty.db"   # nazwa bazy ze starszych wersji skryptu

API_OFFERS = "https://www.olx.pl/api/v1/offers/"
API_FRIENDLY = "https://www.olx.pl/api/v1/friendly-links/query-params/"

PAGE_LIMIT = 50        # maks. liczba ofert na jedno zapytanie API
SEGMENT_MAX = 1000     # głębiej niż ~1000 wyników jedno zapytanie nie sięga
MIN_PRICE_STEP = 1000  # nie dziel przedziałów cen drobniej niż co 1000 zł
LIST_CAP = 30          # maks. liczba pozycji wypisywanych w każdej sekcji raportu

# Używane tylko, gdy brak curl_cffi. Aktualizuj co kilka miesięcy — mocno
# przestarzała wersja Chrome w User-Agencie sama w sobie wygląda podejrzanie.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
SEC_CH_UA = '"Chromium";v="152", "Google Chrome";v="152", "Not_A Brand";v="24"'

# segmenty ścieżki URL, które NIE są nazwą miasta (do wykrycia miasta w adresie)
NON_CITY_SEGMENTS = {
    "nieruchomosci", "mieszkania", "domy", "dzialki", "biura-lokale",
    "garaze-parkingi", "stancje-pokoje", "hale-magazyny", "pozostale",
    "sprzedaz", "wynajem", "zamiana",
}

if IMPERSONATE:
    # impersonate ustawia spójny komplet nagłówków Chrome (User-Agent,
    # sec-ch-ua itd.) dopasowany do odcisku TLS — nie nadpisujemy ich
    # własnym UA, żeby nie wprowadzić rozpoznawalnej niezgodności.
    session = requests.Session(impersonate="chrome")
    session.headers.update({"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5"})
else:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })

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
       python monitor_ofert.py --category-id 14 --region-id 4 --city-id 8959

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


def http_get(url: str, params: dict | None = None, as_json: bool = True,
             retries: int = 4, timeout: int = 30, headers: dict | None = None):
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
            hint = ("" if IMPERSONATE else
                    "\nNajskuteczniejsza poprawka: zainstaluj bibliotekę curl_cffi "
                    "(pip install curl_cffi) — skrypt sam ją wykryje i będzie "
                    "przedstawiał się serwerowi jak prawdziwa przeglądarka, "
                    "również na poziomie połączenia TLS, po którym serwisy "
                    "rozpoznają boty niezależnie od nagłówków.")
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
            raise RuntimeError(
                f"Odpowiedź z {url} nie jest poprawnym JSON-em — OLX mógł zmienić API."
            )
    raise RuntimeError(f"Nie udało się pobrać {url} ({last_exc})")


# ------------------------------------------- adres wyszukiwania → parametry API

def parse_search_url(url: str):
    """Rozbija adres wyszukiwania OLX na ścieżkę SEO, parametry i slug miasta."""
    u = urlparse(url)
    segments = [s for s in u.path.split("/") if s]
    extra: dict[str, str] = {}
    clean_segments = []
    for seg in segments:
        if seg.startswith("q-"):                    # fraza wyszukiwania w ścieżce
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
    path, extra, city_slug = parse_search_url(url)
    resolved: dict[str, str] = {}

    # 1) endpoint OLX zamieniający adresy SEO na parametry wyszukiwania
    try:
        data = http_get(API_FRIENDLY + quote(path) + "/")
        flat: dict = {}

        def flatten(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, (dict, list)):
                        flatten(v)
                    else:
                        flat.setdefault(k, v)
            elif isinstance(node, list):
                for item in node:
                    flatten(item)

        flatten(data)
        for key in ("category_id", "region_id", "city_id", "district_id"):
            if flat.get(key) not in (None, "", 0, "0"):
                resolved[key] = str(flat[key])
    except Exception as exc:
        # endpoint bywa niedostępny — logujemy powód i przechodzimy do planu B
        note = " ".join(str(exc).split())
        if len(note) > 140:
            note = note[:140] + "…"
        log(f"  (endpoint parametrów nie odpowiedział: {note})", err=True)

    # 2) plan B: identyfikatory wyszukane w kodzie HTML strony wyników
    if "category_id" not in resolved:
        try:
            html = http_get(url, as_json=False)
            for key in ("category_id", "region_id", "city_id", "district_id"):
                camel = re.sub(r"_(\w)", lambda m: m.group(1).upper(), key)
                hits = re.findall(rf'["\']?{key}["\']?\s*[:=]\s*["\']?(\d+)', html)
                hits += re.findall(rf'["\']?{camel}["\']?\s*[:=]\s*["\']?(\d+)', html)
                if hits:
                    resolved.setdefault(key, Counter(hits).most_common(1)[0][0])
        except Exception as exc:
            log(f"  ! Nie udało się przeanalizować strony wyników: {exc}", err=True)

    # 3) parametry podane ręcznie w linii poleceń mają najwyższy priorytet
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
    counts = [meta[k] for k in ("total_elements", "visible_total_count", "total")
              if isinstance(meta.get(k), int)]
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
            f"„{city_slug}” — parametry wyszukiwania zostały źle rozpoznane.\n"
            + DEVTOOLS_HELP
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
    hi = int(hi_raw) if hi_raw else None        # None = bez górnej granicy cen

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
                rng = (f"{fmt_price(a)}–{fmt_price(b)}" if b is not None
                       else f"od {fmt_price(a)}")
                log(f"\n  ! Przedział {rng} ma więcej ofert, niż OLX pozwala "
                    f"pobrać (~{SEGMENT_MAX}) — część z niego pominięto.")
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
            pivot = max(a * 2, a + 500_000)     # otwarty koniec: rosnący pivot
            segment(a, pivot)
            segment(pivot, None)

    count = reported_count(params, delay)
    if count >= SEGMENT_MAX:
        log(f"OLX deklaruje „ponad {SEGMENT_MAX}” ogłoszeń (dokładnej liczby "
            f"powyżej limitu nie zdradza) — pobieram partiami wg przedziałów cen.")
        segment(lo, hi)
    else:
        log(f"W tym wyszukiwaniu jest łącznie ok. {count} ogłoszeń.")
        if not crawl(dict(params, sort_by=base["sort_by"])):
            segment(lo, hi)     # deklaracja okazała się zaniżona
    print()
    log(f"Pobrano łącznie {len(collected)} unikalnych ofert.")
    return list(collected.values())


def fetch_new_quick_olx(params: dict, known_ids: set, city_slug: str | None,
                    delay: float) -> list[dict]:
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
        page_new = [o for o in batch
                    if o.get("id") is not None
                    and o["id"] not in known_ids and o["id"] not in seen]
        seen.update(o["id"] for o in page_new)
        fresh.extend(page_new)
        print(f"\r  Sprawdzono {offset + len(batch)} najnowszych ofert, "
              f"nowych: {len(fresh)}...", end="", flush=True)
        if not page_new:          # cała strona to już znane oferty → koniec
            break
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    print()
    return fresh


# ---------------------------------------------------------------------- otodom

OTO_LIMIT = 72                # tyle ofert na stronę pozwala ustawić Otodom
OTO_DETAIL_KEYS = ("description", "createdAt", "market", "advertType")


def next_data(html: str) -> dict:
    """Wyciąga JSON __NEXT_DATA__ (Otodom to aplikacja Next.js —
    wszystkie dane strony siedzą w tym jednym znaczniku)."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>'
                  r'(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError(
            "Strona Otodom nie zawiera danych __NEXT_DATA__ — serwis mógł "
            "zmienić strukturę albo zwrócił stronę blokady.")
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
    items = _oto_flatten(((props.get("data") or {}).get("searchAds") or {})
                         .get("items"))
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
            print(f"\r  [Otodom] pobrano {len(collected)} ofert...", end="",
                  flush=True)
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
            log(f"\n  ! [Otodom] przedział {fmt_price(a)}–{fmt_price(b)} ma "
                f"{total} ofert — nie wszystkie dało się pobrać.")

    segment(lo, hi)
    print()
    log(f"[Otodom] pobrano łącznie {len(collected)} unikalnych ofert.")
    return list(collected.values())


def fetch_new_quick_otodom(url: str, known_ids: set, delay: float) -> list[dict]:
    """Szybki tryb dla Otodom: od najnowszych, aż trafimy na same znane."""
    fresh, seen = [], set()
    page = 1
    while True:
        items, pages, _ = otodom_page(
            url, {"by": "LATEST", "direction": "DESC", "page": page}, delay)
        if not items:
            break
        page_new = [it for it in items
                    if it["id"] not in known_ids and it["id"] not in seen]
        seen.update(it["id"] for it in page_new)
        fresh.extend(page_new)
        print(f"\r  [Otodom] sprawdzono {page} str., nowych: {len(fresh)}...",
              end="", flush=True)
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
        return None            # oferta mogła właśnie zniknąć — trudno
    pause(delay)
    try:
        ad = ((next_data(html).get("props") or {}).get("pageProps") or {})\
            .get("ad") or {}
    except (RuntimeError, ValueError):
        return None
    slim = {k: ad.get(k) for k in OTO_DETAIL_KEYS if ad.get(k) is not None}
    coords = ((ad.get("location") or {}).get("coordinates") or {})
    if coords.get("latitude") is not None:
        slim["coordinates"] = {"latitude": coords.get("latitude"),
                               "longitude": coords.get("longitude")}
    geo = ((ad.get("location") or {}).get("reverseGeocoding") or {})\
        .get("locations") or []
    for node in geo:
        if isinstance(node, dict) and node.get("locationLevel") == "district":
            slim["district"] = node.get("name")
            break
    target = ad.get("target") or {}
    target_slim = {k: target.get(k) for k in
                   ("Rent", "Floor_no", "Rooms_num", "Build_year")
                   if target.get(k) is not None}
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
    log(f"[Otodom] dociągam szczegóły {len(todo)} ofert "
        f"(współrzędne i opisy — ok. {est} min)...")
    for done, item in enumerate(todo, 1):
        slug = item.get("slug")
        if slug:
            detail = fetch_otodom_detail(
                f"https://www.otodom.pl/pl/oferta/{slug}", delay)
            if detail:
                item["_ad"] = detail
        print(f"\r  [Otodom] szczegóły {done}/{len(todo)}...", end="", flush=True)
    print()


_OTO_ROOMS = {"ONE": "1 pokój", "TWO": "2 pokoje", "THREE": "3 pokoje",
              "FOUR": "4 pokoje", "FIVE": "5 pokoi", "SIX": "6 pokoi",
              "SEVEN": "7 pokoi", "EIGHT": "8 pokoi", "NINE": "9 pokoi",
              "TEN": "10 pokoi", "MORE": "10+ pokoi"}
_OTO_MARKET = {"primary": "Pierwotny", "secondary": "Wtórny",
               "PRIMARY": "Pierwotny", "SECONDARY": "Wtórny"}


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
            rooms = "1 pokój" if n == 1 else (f"{n} pokoje" if n < 5
                                              else f"{n} pokoi")

    loc = item.get("location") or {}
    city = district = None
    addr = loc.get("address") or {}
    if isinstance(addr.get("city"), dict):
        city = addr["city"].get("name")
    for node in ((loc.get("reverseGeocoding") or {}).get("locations") or []):
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
        "created_at": item.get("dateCreatedFirst") or item.get("dateCreated")
                      or ad.get("createdAt"),
        "last_refresh": item.get("pushedUpAt"),
        "lat": to_float(coords.get("latitude")),
        "lon": to_float(coords.get("longitude")),
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

OFFER_COLUMNS = ("uid", "source", "id", "url", "title", "price", "currency",
                 "negotiable", "area", "price_per_m", "rooms", "floor", "market",
                 "city", "district", "business", "created_at", "last_refresh",
                 "lat", "lon", "map_radius")


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
    pobierania czegokolwiek. Dwa etapy:
    1) baza jednoportalowa (klucz = numer oferty OLX) → wieloportalowa
       (klucz uid „olx:123”/„oto:456” + kolumna source), razem z historią cen,
    2) uzupełnienie współrzędnych z zapisanego surowego JSON-a ofert."""
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
            f"first_seen, last_seen, active, raw FROM offers_v1")
        con.execute(
            "INSERT INTO price_history(offer_uid, ts, price) "
            "SELECT 'olx:' || offer_id, ts, price FROM price_history_v1")
        con.executescript("DROP TABLE offers_v1; DROP TABLE price_history_v1;")
        con.commit()
    rows = con.execute(
        "SELECT uid, raw FROM offers WHERE lat IS NULL AND raw IS NOT NULL "
        "AND source = 'olx'").fetchall()
    filled = 0
    for uid, raw in rows:
        try:
            map_node = (json.loads(raw).get("map") or {})
        except (ValueError, AttributeError):
            continue
        lat, lon = to_float(map_node.get("lat")), to_float(map_node.get("lon"))
        if lat is None or lon is None:
            continue
        con.execute("UPDATE offers SET lat = ?, lon = ?, map_radius = ? WHERE uid = ?",
                    (lat, lon, to_float(map_node.get("radius")) or 0, uid))
        filled += 1
    if filled:
        con.commit()
        log(f"(uzupełniono współrzędne {filled} ofert z danych już zapisanych w bazie)")


def meta_get(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def sync(con: sqlite3.Connection, records: list[tuple[dict, dict]],
         full_scan_sources: set) -> dict:
    """Zapisuje pobrane oferty i zwraca różnice względem poprzedniego stanu.

    records: pary (rozparsowana_oferta, surowy_json_dict).
    full_scan_sources: portale przeskanowane w całości — tylko dla nich
    wolno uznać nieobecne oferty za wycofane."""
    now = datetime.now().isoformat(timespec="seconds")
    first_run = con.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0

    new, price_changes, returned = [], [], []
    fetched_uids: set = set()
    update_cols = [c for c in OFFER_COLUMNS[1:]
                   if c not in ("lat", "lon", "map_radius")]

    for o, raw in records:
        if o["id"] is None:
            continue
        fetched_uids.add(o["uid"])
        row = con.execute(
            "SELECT price, active, raw FROM offers WHERE uid = ?",
            (o["uid"],)).fetchone()
        if row is None:
            con.execute(
                f"INSERT INTO offers({', '.join(OFFER_COLUMNS)}, "
                f"first_seen, last_seen, active, raw) "
                f"VALUES ({', '.join('?' * len(OFFER_COLUMNS))}, ?, ?, 1, ?)",
                tuple(o[c] for c in OFFER_COLUMNS)
                + (now, now, json.dumps(raw, ensure_ascii=False)))
            if o["price"] is not None:
                con.execute("INSERT INTO price_history VALUES (?, ?, ?)",
                            (o["uid"], now, o["price"]))
            new.append(o)
        else:
            old_price, was_active, old_raw_txt = row
            changed = (o["price"] is not None
                       and (old_price is None
                            or int(round(o["price"])) != int(round(old_price))))
            if changed:
                price_changes.append((o, old_price))
                con.execute("INSERT INTO price_history VALUES (?, ?, ?)",
                            (o["uid"], now, o["price"]))
            # szczegóły Otodom (_ad: opis, zdjęcia, współrzędne) dociągamy raz —
            # przy zwykłym skanie listy przenosimy je ze starego rekordu
            if o["source"] == "otodom" and "_ad" not in raw and old_raw_txt:
                try:
                    old_ad = json.loads(old_raw_txt).get("_ad")
                    if old_ad:
                        raw = dict(raw, _ad=old_ad)
                except ValueError:
                    pass
            con.execute(
                f"UPDATE offers SET "
                f"{', '.join(c + ' = ?' for c in update_cols)}, "
                f"lat = COALESCE(?, lat), lon = COALESCE(?, lon), "
                f"map_radius = COALESCE(?, map_radius), "
                f"last_seen = ?, active = 1, raw = ? WHERE uid = ?",
                tuple(o[c] for c in update_cols)
                + (o["lat"], o["lon"],
                   o["map_radius"] if o["lat"] is not None else None,
                   now, json.dumps(raw, ensure_ascii=False), o["uid"]))
            if not was_active:
                returned.append(o)

    removed = []
    if full_scan_sources and not first_run:
        marks = ", ".join("?" * len(full_scan_sources))
        for row in con.execute(
                f"SELECT uid, title, price, area, district, url, source "
                f"FROM offers WHERE active = 1 AND source IN ({marks})",
                tuple(full_scan_sources)):
            if row[0] not in fetched_uids:
                removed.append(dict(zip(
                    ("uid", "title", "price", "area", "district", "url",
                     "source"), row)))
        for item in removed:
            con.execute("UPDATE offers SET active = 0 WHERE uid = ?",
                        (item["uid"],))

    con.commit()
    return {"first_run": first_run, "new": new, "price_changes": price_changes,
            "removed": removed, "returned": returned}


# ---------------------------------------------------------------------- raport

# ------------------------------------------------------------- źródła danych

class Source:
    """Wspólny interfejs źródła ofert.

    Nowy portal = nowa podklasa + wpis w SOURCES. Reszta skryptu (main, baza,
    raport, eksporty) nie zna szczegółów żadnego serwisu — rozmawia z nimi
    wyłącznie przez poniższe metody."""

    name = ""            # klucz źródła: w bazie, w uid ofert i we fladze --source
    label = ""           # nazwa wyświetlana w komunikatach
    domains: tuple = ()  # domeny rozpoznawane w adresach --url
    default_url = ""     # wyszukiwanie używane, gdy nie podano --url

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
        if cached is None and not con.execute(
                "SELECT COUNT(*) FROM meta WHERE key LIKE 'api_params::%'"
        ).fetchone()[0]:
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
        log("Parametry: "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.params.items())))

    def fetch_all(self):
        return fetch_all_olx(self.params, self.city_slug, self.delay)

    def fetch_new(self, known_ids):
        return fetch_new_quick_olx(self.params, known_ids, self.city_slug,
                                   self.delay)

    def parse(self, raw):
        return parse_offer_olx(raw)

    def save_state(self, con):
        # dopiero po udanym pobraniu — złych parametrów nie chcemy zapamiętać
        meta_set(con, f"api_params::{self.url}",
                 json.dumps(self.params, ensure_ascii=False))


class OtodomSource(Source):
    name = "otodom"
    label = "Otodom"
    domains = ("otodom.pl",)
    default_url = ("https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/"
                   "malopolskie/krakow/krakow/krakow")

    def fetch_all(self):
        return fetch_all_otodom(self.url, self.delay)

    def fetch_new(self, known_ids):
        return fetch_new_quick_otodom(self.url, known_ids, self.delay)

    def enrich(self, items, con):
        have_detail = {row[0] for row in con.execute(
            "SELECT id FROM offers WHERE source = ? AND lat IS NOT NULL",
            (self.name,))}
        enrich_otodom(items, have_detail, self.delay)

    def parse(self, raw):
        return parse_offer_otodom(raw)


SOURCES: dict[str, type] = {cls.name: cls for cls in (OlxSource, OtodomSource)}


def source_for(url: str):
    """Klasa źródła obsługująca dany adres (None = adres nieobsługiwany)."""
    return next((cls for cls in SOURCES.values() if cls.handles(url)), None)


# ----------------------------------------------------------------------- raport

PORTAL_NAME = {name: cls.label for name, cls in SOURCES.items()}


def offer_details(o: dict) -> str:
    parts = [fmt_price(o.get("price"))]
    if o.get("source"):
        parts.insert(0, PORTAL_NAME.get(o["source"], o["source"]))
    if o.get("area"):
        parts.append(f"{o['area']:g} m²")
    if o.get("price_per_m"):
        parts.append(f"{int(o['price_per_m'])} zł/m²")
    if o.get("rooms"):
        parts.append(str(o["rooms"]))
    if o.get("district"):
        parts.append(str(o["district"]))
    if o.get("business"):
        parts.append("biuro/deweloper")
    return " · ".join(parts)


def print_section(title: str, items: list, render) -> None:
    log(f"\n=== {title} ({len(items)}) " + "=" * max(0, 46 - len(title)))
    for item in items[:LIST_CAP]:
        render(item)
    if len(items) > LIST_CAP:
        log(f"  … i {len(items) - LIST_CAP} kolejnych (pełna lista jest w bazie).")


def report(result: dict, db_path: str, con: sqlite3.Connection) -> None:
    if result["first_run"]:
        log(f"\n✔ Pierwsze uruchomienie: zapisano {len(result['new'])} ofert "
            f"do bazy „{db_path}”.")
        log("  Przy kolejnych uruchomieniach zobaczysz już tylko nowe oferty i zmiany.")
        return

    def render_offer(o: dict) -> None:
        log(f"  • {o['title'][:90]}")
        log(f"    {offer_details(o)}")
        log(f"    {o.get('url') or ''}")

    def render_change(item) -> None:
        o, old_price = item
        pct = ""
        if old_price and o.get("price"):
            diff = (o["price"] - old_price) / old_price * 100
            pct = f" ({diff:+.1f}%)"
        log(f"  • {fmt_price(old_price)} → {fmt_price(o.get('price'))}{pct}"
            f"  {o['title'][:70]}")
        log(f"    {offer_details(o)}")
        log(f"    {o.get('url') or ''}")

    def render_removed(o: dict) -> None:
        details = [fmt_price(o.get("price"))]
        if o.get("source"):
            details.insert(0, PORTAL_NAME.get(o["source"], o["source"]))
        if o.get("area"):
            details.append(f"{o['area']:g} m²")
        if o.get("district"):
            details.append(str(o["district"]))
        log(f"  • {o['title'][:70]} — {' · '.join(details)}")

    anything = False
    if result["new"]:
        anything = True
        print_section("NOWE OGŁOSZENIA", result["new"], render_offer)
    if result["price_changes"]:
        anything = True
        print_section("ZMIANY CEN", result["price_changes"], render_change)
    if result["returned"]:
        anything = True
        print_section("WRÓCIŁY DO SPRZEDAŻY", result["returned"], render_offer)
    if result["removed"]:
        anything = True
        print_section("ZNIKNĘŁY (sprzedane / wycofane)", result["removed"],
                      render_removed)
    if not anything:
        log("\nBrak zmian od ostatniego uruchomienia.")

    active, total = con.execute(
        "SELECT SUM(active), COUNT(*) FROM offers").fetchone()
    log(f"\nW bazie: {active or 0} aktywnych ofert ({total} łącznie) — plik „{db_path}”.")


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
    headers = ("portal", "id", "tytul", "cena", "cena_za_m2", "metraz_m2",
               "pokoje", "pietro", "rynek", "dzielnica", "miasto", "od_firmy",
               "do_negocjacji", "szer_geo", "dl_geo",
               "data_dodania", "pierwszy_raz_widziana", "link")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)
    log(f"\n✔ Wyeksportowano {len(rows)} aktywnych ofert do „{path}”.")


# ------------------------------------------------------- interaktywna mapa HTML

LEAFLET_VERSION = "1.9.4"
_LEAFLET_CACHE: dict[str, str] = {}


def fetch_leaflet() -> tuple[str, str]:
    """Pobiera bibliotekę map Leaflet z rejestru npm i zwraca (js, css),
    żeby wkleić ją w całości do generowanego pliku HTML."""
    if _LEAFLET_CACHE:
        return _LEAFLET_CACHE["js"], _LEAFLET_CACHE["css"]
    url = f"https://registry.npmjs.org/leaflet/-/leaflet-{LEAFLET_VERSION}.tgz"
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        wanted = {"package/dist/leaflet.js": "js", "package/dist/leaflet.css": "css"}
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                key = wanted.get(member.name)
                if key:
                    _LEAFLET_CACHE[key] = tar.extractfile(member).read().decode("utf-8")
    except Exception as exc:
        raise RuntimeError(
            "Nie udało się pobrać biblioteki map (Leaflet) z registry.npmjs.org — "
            "do wygenerowania pliku HTML potrzebny jest internet.\n"
            f"Szczegóły: {exc}") from exc
    if set(_LEAFLET_CACHE) != {"js", "css"}:
        raise RuntimeError("Paczka Leaflet ma nieoczekiwaną zawartość.")
    if "</script" in _LEAFLET_CACHE["js"].lower():
        raise RuntimeError("Kod Leaflet zawiera sekwencję łamiącą osadzanie w HTML.")
    return _LEAFLET_CACHE["js"], _LEAFLET_CACHE["css"]


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


def export_html(con: sqlite3.Connection, path: str) -> None:
    """Buduje pojedynczy, samowystarczalny plik HTML z mapą, listą i filtrami.
    Lekkie dane (ceny, metraże, współrzędne, opisy) siedzą w pliku;
    zdjęcia dociągają się z serwerów OLX dopiero po otwarciu oferty."""
    history: dict[str, list] = {}
    for offer_uid, ts, price in con.execute(
            "SELECT offer_uid, ts, price FROM price_history ORDER BY ts"):
        history.setdefault(offer_uid, []).append([ts[:10], price])

    offers = []
    query = """SELECT uid, source, url, title, price, negotiable, area,
                      price_per_m, rooms, floor, market, district, business,
                      created_at, first_seen, lat, lon, map_radius, raw
               FROM offers WHERE active = 1"""
    for (uid, source, url, title, price, negotiable, area, ppm, rooms, floor,
         market, district, business, created, first_seen, lat, lon, radius,
         raw) in con.execute(query):
        try:
            raw_offer = json.loads(raw) if raw else {}
        except ValueError:
            raw_offer = {}
        if source == "otodom":
            ad = raw_offer.get("_ad") or {}
            photos = [u for u in (ad.get("images") or [])[:8]
                      if isinstance(u, str)]
            desc = strip_html(ad.get("description") or "")
        else:
            photos = photo_urls(raw_offer)
            desc = strip_html(raw_offer.get("description") or "")
        item = {
            "id": uid, "s": source, "u": url, "t": title, "p": price,
            "ng": negotiable, "a": area, "pm": ppm, "r": rooms, "f": floor,
            "mk": market, "d": district, "b": business, "c": created,
            "fs": first_seen, "lat": lat, "lon": lon, "rad": radius or 0,
            "ph": photos, "dsc": desc,
        }
        hist = history.get(uid) or []
        if len(hist) > 1:
            item["h"] = hist
        offers.append(item)

    if not offers:
        raise RuntimeError("Baza nie zawiera aktywnych ofert — najpierw uruchom "
                           "skrypt bez --offline, żeby pobrać dane.")

    cities = Counter(row[0] for row in
                     con.execute("SELECT city FROM offers WHERE active = 1")
                     if row[0])
    meta = {
        "city": cities.most_common(1)[0][0] if cities else "OLX",
        "gen": datetime.now().isoformat(timespec="minutes"),
        "url": meta_get(con, "search_url") or "",
        "total": len(offers),
    }

    log("Pobieram bibliotekę map (Leaflet) do wklejenia w plik...")
    leaflet_js, leaflet_css = fetch_leaflet()
    data_json = json.dumps(offers, ensure_ascii=False,
                           separators=(",", ":")).replace("</", "<\\/")
    meta_json = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")

    page = (HTML_TEMPLATE
            .replace("/*__LEAFLET_CSS__*/", leaflet_css)
            .replace("/*__LEAFLET_JS__*/", leaflet_js)
            .replace("/*__META__*/null", meta_json)
            .replace("/*__DATA__*/null", data_json))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    size_mb = len(page.encode("utf-8")) / 1_048_576
    log(f"\n✔ Zapisano interaktywną mapę {len(offers)} ofert do „{path}” "
        f"({size_mb:.1f} MB). Otwórz ten plik w przeglądarce.")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oferty OLX — mapa</title>
<style>/*__LEAFLET_CSS__*/</style>
<style>
:root{--bg:#f4f5f7;--panel:#fff;--line:#e2e5ea;--txt:#1c2733;--mut:#68758a;
 --acc:#2456c7;--accbg:#e9effc;--green:#178a4c;--red:#c92a2a;--rad:10px}
*{box-sizing:border-box}
html,body{margin:0;height:100%;font:14px/1.45 -apple-system,"Segoe UI",Roboto,
 "Helvetica Neue",Arial,sans-serif;color:var(--txt);background:var(--bg)}
button,input,select{font:inherit;color:inherit}
header{display:flex;gap:14px;align-items:center;padding:9px 14px;background:var(--panel);
 border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:16px;margin:0;white-space:nowrap}
header .sub{color:var(--mut);font-size:12px}
#q{flex:1;min-width:180px;max-width:460px;padding:7px 11px;border:1px solid var(--line);
 border-radius:8px;background:#fbfcfd}
#q:focus{outline:2px solid var(--accbg);border-color:var(--acc)}
label.chk{display:flex;gap:6px;align-items:center;color:var(--mut);font-size:12.5px;
 cursor:pointer;user-select:none;white-space:nowrap}
#layout{display:flex;height:calc(100% - 52px)}
aside#filters{width:252px;min-width:252px;overflow-y:auto;background:var(--panel);
 border-right:1px solid var(--line);padding:12px}
#mapwrap{flex:1;min-width:0;position:relative}
#map{position:absolute;inset:0}
aside#list{width:396px;min-width:300px;display:flex;flex-direction:column;
 background:var(--panel);border-left:1px solid var(--line)}
.f-group{margin-bottom:14px}
.f-group>h3{margin:0 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
 color:var(--mut)}
.range{display:flex;gap:6px}
.range input{width:50%;padding:6px 8px;border:1px solid var(--line);border-radius:7px}
select{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:7px;
 background:#fff}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{padding:5px 11px;border:1px solid var(--line);border-radius:99px;background:#fff;
 cursor:pointer}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
#districts{max-height:168px;overflow-y:auto;border:1px solid var(--line);
 border-radius:7px;padding:6px 8px;background:#fbfcfd}
#districts label{display:flex;gap:6px;align-items:center;padding:2px 0;cursor:pointer}
#districts .cnt{margin-left:auto;color:var(--mut);font-size:11.5px}
.mini{font-size:12px;color:var(--acc);background:none;border:none;cursor:pointer;
 padding:0 6px 0 0}
#stats{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:12px}
#stats div{background:var(--accbg);border-radius:8px;padding:7px 9px}
#stats b{display:block;font-size:15px}
#stats span{font-size:11px;color:var(--mut)}
#histo{width:100%;height:74px;margin:2px 0 12px;display:block}
#clear{width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;
 background:#fff;cursor:pointer}
#clear:hover{background:var(--accbg)}
#listhead{display:flex;gap:8px;align-items:center;padding:9px 12px;
 border-bottom:1px solid var(--line)}
#listhead .n{font-size:12.5px;color:var(--mut);white-space:nowrap}
#cards{overflow-y:auto;flex:1}
.card{display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);
 cursor:pointer}
.card:hover{background:#f7f9fc}
.card.sel{background:var(--accbg)}
.card img{width:92px;height:70px;object-fit:cover;border-radius:7px;background:#e8ebf0;
 flex-shrink:0}
.card .noimg{width:92px;height:70px;border-radius:7px;background:#eef0f4;color:#b6bdc9;
 display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0}
.card h4{margin:0 0 3px;font-size:13px;line-height:1.3;font-weight:600;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .pr{font-size:14.5px;font-weight:700}
.card .meta{font-size:12px;color:var(--mut)}
.badge{display:inline-block;font-size:10.5px;font-weight:700;border-radius:5px;
 padding:1px 6px;margin-left:6px;vertical-align:1px}
.badge.new{background:#e3f4ea;color:var(--green)}
.badge.drop{background:#fdecec;color:var(--red)}
#more{padding:12px;text-align:center;color:var(--mut)}
#detail{position:fixed;top:0;right:-620px;width:min(600px,100vw);height:100%;
 background:var(--panel);box-shadow:-8px 0 28px rgba(15,25,40,.18);z-index:1200;
 transition:right .22s ease;display:flex;flex-direction:column}
#detail.open{right:0}
#dbody{overflow-y:auto;padding:16px 18px}
#dclose{position:absolute;top:10px;right:12px;width:34px;height:34px;border-radius:50%;
 border:none;background:rgba(20,28,40,.55);color:#fff;font-size:17px;cursor:pointer;
 z-index:5}
#backdrop{position:fixed;inset:0;background:rgba(15,22,33,.35);z-index:1100;
 opacity:0;pointer-events:none;transition:opacity .2s}
#backdrop.open{opacity:1;pointer-events:auto}
#gal{position:relative;background:#0d1117;border-radius:var(--rad);overflow:hidden}
#gal .main{width:100%;height:320px;object-fit:contain;display:block}
#thumbs{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px}
#thumbs img{width:72px;height:54px;object-fit:cover;border-radius:6px;cursor:pointer;
 opacity:.65;flex-shrink:0}
#thumbs img.on{opacity:1;outline:2px solid var(--acc)}
#detail h2{margin:12px 0 4px;font-size:18px;line-height:1.3}
#dprice{font-size:22px;font-weight:800}
#dprice small{font-size:13px;color:var(--mut);font-weight:400}
#dgrid{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px;margin:13px 0;
 padding:12px;background:#f7f8fa;border-radius:var(--rad)}
#dgrid span{color:var(--mut);font-size:11.5px;display:block}
#dgrid b{font-weight:600}
#dhist table{width:100%;border-collapse:collapse;font-size:13px}
#dhist td{padding:4px 0;border-bottom:1px dashed var(--line)}
#dhist td:last-child{text-align:right}
.up{color:var(--red)}.down{color:var(--green)}
#ddesc{white-space:pre-line;margin-top:12px;font-size:13.5px}
.olxbtn{display:block;text-align:center;margin:16px 0 6px;padding:11px;
 background:var(--acc);color:#fff;text-decoration:none;border-radius:9px;font-weight:600}
.approx{font-size:12px;color:var(--mut);margin-top:8px}
.leaflet-container{font:inherit}
.legend{background:#fff;padding:8px 10px;border-radius:8px;
 box-shadow:0 1px 5px rgba(0,0,0,.25);font-size:11.5px;line-height:1.55}
.legend i{display:inline-block;width:11px;height:11px;border-radius:50%;
 margin-right:6px;vertical-align:-1px}
@media(max-width:960px){
 #layout{flex-direction:column;height:auto}
 aside#filters{width:100%;min-width:0;order:2;border-right:none;
  border-top:1px solid var(--line)}
 #mapwrap{order:1;height:46vh;flex:none}
 aside#list{width:100%;min-width:0;order:3;border-left:none;height:60vh}
 body,html{height:auto}
}
</style>
</head>
<body>
<header>
 <div><h1 id="hd">Oferty OLX</h1><div class="sub" id="hdsub"></div></div>
 <input id="q" type="search" placeholder="Szukaj: np. balkon, garaż, Ruczaj…">
 <label class="chk"><input type="checkbox" id="qdesc" checked> szukaj też w opisach</label>
</header>
<div id="layout">
 <aside id="filters">
  <div id="stats"></div>
  <canvas id="histo" title="Rozkład ceny za m² (po filtrach)"></canvas>
  <div class="f-group"><h3>Cena [zł]</h3>
   <div class="range"><input id="pmin" type="number" placeholder="od" step="10000">
    <input id="pmax" type="number" placeholder="do" step="10000"></div></div>
  <div class="f-group"><h3>Metraż [m²]</h3>
   <div class="range"><input id="amin" type="number" placeholder="od">
    <input id="amax" type="number" placeholder="do"></div></div>
  <div class="f-group"><h3>Cena za m² [zł]</h3>
   <div class="range"><input id="mmin" type="number" placeholder="od" step="500">
    <input id="mmax" type="number" placeholder="do" step="500"></div></div>
  <div class="f-group"><h3>Pokoje</h3><div class="chips" id="rooms">
   <button class="chip" data-r="1">1</button><button class="chip" data-r="2">2</button>
   <button class="chip" data-r="3">3</button><button class="chip" data-r="4">4+</button>
  </div></div>
  <div class="f-group"><h3>Portal</h3><div class="chips" id="portals">
   <button class="chip on" data-s="olx">OLX</button>
   <button class="chip on" data-s="otodom">Otodom</button>
  </div></div>
  <div class="f-group"><h3>Rynek</h3><select id="market">
   <option value="">wszystkie</option><option>Pierwotny</option><option>Wtórny</option>
  </select></div>
  <div class="f-group"><h3>Sprzedający</h3><select id="seller">
   <option value="">wszyscy</option><option value="0">osoba prywatna</option>
   <option value="1">firma / deweloper</option>
  </select></div>
  <div class="f-group"><h3>Dodane</h3><select id="fresh">
   <option value="">kiedykolwiek</option><option value="1">ostatnie 24 h</option>
   <option value="3">ostatnie 3 dni</option><option value="7">ostatnie 7 dni</option>
   <option value="14">ostatnie 14 dni</option><option value="30">ostatnie 30 dni</option>
  </select></div>
  <div class="f-group"><h3>Dzielnica</h3>
   <div><button class="mini" id="dall">wszystkie</button>
    <button class="mini" id="dnone">żadna</button></div>
   <div id="districts"></div></div>
  <div class="f-group">
   <label class="chk"><input type="checkbox" id="onlydrop"> tylko z obniżką ceny</label>
   <label class="chk"><input type="checkbox" id="onlygeo"> tylko z lokalizacją na mapie</label>
   <label class="chk"><input type="checkbox" id="bounds"> zawęź do widocznego obszaru mapy</label>
  </div>
  <button id="clear">Wyczyść filtry</button>
 </aside>
 <div id="mapwrap"><div id="map"></div></div>
 <aside id="list">
  <div id="listhead">
   <select id="sort">
    <option value="new">najnowsze</option>
    <option value="pm-asc">cena za m² ↑</option>
    <option value="pm-desc">cena za m² ↓</option>
    <option value="p-asc">cena ↑</option>
    <option value="p-desc">cena ↓</option>
    <option value="a-desc">metraż ↓</option>
    <option value="a-asc">metraż ↑</option>
    <option value="drop">największa obniżka</option>
   </select>
   <span class="n" id="cnt"></span>
  </div>
  <div id="cards"></div>
 </aside>
</div>
<div id="backdrop"></div>
<section id="detail"><button id="dclose" title="Zamknij">✕</button>
 <div id="dbody"></div></section>
<script>/*__LEAFLET_JS__*/</script>
<script>
"use strict";
const META = /*__META__*/null;
const OFFERS = /*__DATA__*/null;
const GEN = new Date(META.gen);
const PORTAL = {olx: "OLX", otodom: "Otodom"};
const $ = s => document.querySelector(s);
const esc = s => (s == null ? "" : String(s))
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;");
const fmtP = v => v == null ? "brak ceny"
  : Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g,"\u202f") + " zł";
const fmtN = v => v == null ? "—"
  : Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g,"\u202f");
function fmtDate(iso){
  if(!iso) return "—";
  const d = new Date(iso), days = Math.floor((GEN - d)/864e5);
  if(days <= 0) return "dziś";
  if(days === 1) return "wczoraj";
  if(days < 14) return days + " dni temu";
  return d.toLocaleDateString("pl-PL");
}
function roomBucket(r){
  if(!r) return null;
  if(/kawaler/i.test(r)) return 1;
  const m = r.match(/\d+/);
  return m ? Math.min(+m[0], 4) : null;
}
// obniżka: cena aktualna vs najwyższa w historii
for(const o of OFFERS){
  o._rb = roomBucket(o.r);
  o._drop = 0;
  if(o.h && o.p != null){
    const top = Math.max(...o.h.map(x => x[1]));
    if(top > o.p) o._drop = (top - o.p) / top;
  }
  o._new = o.fs && (GEN - new Date(o.fs)) < 48*3600e3;
  o._txt = (o.t || "").toLowerCase();
  o._dtxt = (o.dsc || "").toLowerCase();
}
document.title = `Mieszkania ${META.city} — mapa ofert OLX`;
$("#hd").textContent = `Mieszkania — ${META.city}`;
const perSrc = OFFERS.reduce((m,o) => (m[o.s]=(m[o.s]||0)+1, m), {});
$("#hdsub").textContent = `stan z ${GEN.toLocaleString("pl-PL",
  {dateStyle:"medium", timeStyle:"short"})} · ` +
  Object.entries(perSrc).map(([s,n]) => `${PORTAL[s]||s}: ${n}`).join(" · ");

/* ---------- mapa ---------- */
const withGeo = OFFERS.filter(o => o.lat != null && o.lon != null);
const cLat = withGeo.length ?
  withGeo.reduce((s,o)=>s+o.lat,0)/withGeo.length : 50.0614;
const cLon = withGeo.length ?
  withGeo.reduce((s,o)=>s+o.lon,0)/withGeo.length : 19.9366;
const map = L.map("map", {preferCanvas:true}).setView([cLat, cLon], 12);
/* Kafelki OpenStreetMap.org wymagają od 2026 r. nagłówka Referer, którego
   przeglądarki nie wysyłają z plików lokalnych (file://) — każdy kafelek
   wracał jako "Access blocked". Publiczne kafelki CARTO (te same dane OSM,
   styl Voyager) nie mają tego wymogu i działają z pliku lokalnego. */
L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
  {maxZoom:20, subdomains:"abcd",
   attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
              +' &middot; &copy; <a href="https://carto.com/attributions">CARTO</a>'}
).addTo(map);
// skala kolorów: kwintyle ceny za m² liczone raz, z całego zbioru
const PAL = ["#1a9850","#8fce54","#f5c53c","#f2803a","#d73027"];
const ppmAll = OFFERS.map(o=>o.pm).filter(v=>v!=null).sort((a,b)=>a-b);
const BR = [1,2,3,4].map(i => ppmAll[Math.floor(ppmAll.length*i/5)] || 0);
const colorOf = v => v == null ? "#8a93a3"
  : PAL[BR.findIndex(b => v <= b) === -1 ? 4 : BR.findIndex(b => v <= b)];
const legend = L.control({position:"bottomleft"});
legend.onAdd = () => {
  const div = L.DomUtil.create("div","legend");
  div.innerHTML = "<b>cena za m²</b><br>" + PAL.map((c,i)=>{
    const lo = i ? fmtN(BR[i-1]) : null, hi = BR[i] ? fmtN(BR[i]) : null;
    const lbl = i === 0 ? "do " + hi : i === 4 ? "od " + lo : lo + "–" + hi;
    return `<i style="background:${c}"></i>${lbl}`;
  }).join("<br>");
  return div;
};
legend.addTo(map);
const layer = L.layerGroup().addTo(map);
const markers = new Map();
let selId = null;
function baseStyle(o){
  return {radius:6, weight:1, color:"#fff", fillColor:colorOf(o.pm),
          fillOpacity:.88};
}
function rebuildMarkers(list){
  layer.clearLayers(); markers.clear();
  for(const o of list){
    if(o.lat == null) continue;
    const m = L.circleMarker([o.lat, o.lon], baseStyle(o))
      .on("click", () => openDetail(o.id, true))
      .bindTooltip(`${esc(o.t)}<br><b>${fmtP(o.p)}</b>` +
        (o.pm ? ` · ${fmtN(o.pm)} zł/m²` : ""), {direction:"top", opacity:.94});
    m.addTo(layer);
    markers.set(o.id, m);
  }
  if(selId != null) highlight(selId);
}
function highlight(id){
  if(selId != null && markers.has(selId)){
    const prev = OFFERS.find(x => x.id === selId);
    markers.get(selId).setStyle(baseStyle(prev));
  }
  selId = id;
  const m = markers.get(id);
  if(m){ m.setStyle({radius:10, weight:3, color:"#17233b"}); m.bringToFront(); }
}

/* ---------- filtry ---------- */
const districts = {};
for(const o of OFFERS){
  const d = o.d || "(nie podano)";
  districts[d] = (districts[d] || 0) + 1;
}
$("#districts").innerHTML = Object.entries(districts)
  .sort((a,b) => b[1]-a[1])
  .map(([d,n]) => `<label><input type="checkbox" class="dbox" value="${esc(d)}"
     checked> ${esc(d)} <span class="cnt">${n}</span></label>`).join("");
const num = id => { const v = $(id).value.trim(); return v === "" ? null : +v; };
function currentFilter(){
  const roomsOn = [...document.querySelectorAll("#rooms .chip.on")]
    .map(b => +b.dataset.r);
  const boxes = [...document.querySelectorAll(".dbox")];
  const dsel = new Set(boxes.filter(b => b.checked).map(b => b.value));
  const allD = dsel.size === boxes.length;
  const srcOn = [...document.querySelectorAll("#portals .chip.on")]
    .map(b => b.dataset.s);
  const q = $("#q").value.trim().toLowerCase();
  const inDesc = $("#qdesc").checked;
  const freshDays = $("#fresh").value ? +$("#fresh").value : null;
  const bounds = $("#bounds").checked ? map.getBounds() : null;
  return o => {
    if(srcOn.length < 2 && !srcOn.includes(o.s)) return false;
    if(q && !(o._txt.includes(q) || (inDesc && o._dtxt.includes(q)))) return false;
    const pmin=num("#pmin"), pmax=num("#pmax");
    if(pmin != null && (o.p == null || o.p < pmin)) return false;
    if(pmax != null && (o.p == null || o.p > pmax)) return false;
    const amin=num("#amin"), amax=num("#amax");
    if(amin != null && (o.a == null || o.a < amin)) return false;
    if(amax != null && (o.a == null || o.a > amax)) return false;
    const mmin=num("#mmin"), mmax=num("#mmax");
    if(mmin != null && (o.pm == null || o.pm < mmin)) return false;
    if(mmax != null && (o.pm == null || o.pm > mmax)) return false;
    if(roomsOn.length && (o._rb == null || !roomsOn.includes(o._rb))) return false;
    if($("#market").value && o.mk !== $("#market").value) return false;
    if($("#seller").value !== "" && String(o.b) !== $("#seller").value) return false;
    if(freshDays != null){
      const t = o.c || o.fs;
      if(!t || (GEN - new Date(t)) > freshDays*864e5) return false;
    }
    if(!allD && !dsel.has(o.d || "(nie podano)")) return false;
    if($("#onlydrop").checked && !(o._drop > 0)) return false;
    if($("#onlygeo").checked && o.lat == null) return false;
    if(bounds && (o.lat == null || !bounds.contains([o.lat, o.lon]))) return false;
    return true;
  };
}
const SORTS = {
  "new": (a,b) => new Date(b.c || b.fs || 0) - new Date(a.c || a.fs || 0),
  "pm-asc": (a,b) => (a.pm ?? 9e9) - (b.pm ?? 9e9),
  "pm-desc": (a,b) => (b.pm ?? -1) - (a.pm ?? -1),
  "p-asc": (a,b) => (a.p ?? 9e9) - (b.p ?? 9e9),
  "p-desc": (a,b) => (b.p ?? -1) - (a.p ?? -1),
  "a-desc": (a,b) => (b.a ?? -1) - (a.a ?? -1),
  "a-asc": (a,b) => (a.a ?? 9e9) - (b.a ?? 9e9),
  "drop": (a,b) => b._drop - a._drop,
};
let filtered = [], shown = 0;
const CHUNK = 80;
function apply(refitMarkers = true){
  const pass = currentFilter();
  filtered = OFFERS.filter(pass).sort(SORTS[$("#sort").value]);
  $("#cnt").textContent = `${filtered.length} z ${OFFERS.length}`;
  renderStats(); renderHisto();
  if(refitMarkers) rebuildMarkers(filtered);
  $("#cards").scrollTop = 0;
  shown = 0; $("#cards").innerHTML = ""; renderMore();
}
function median(arr){
  if(!arr.length) return null;
  const s = [...arr].sort((a,b)=>a-b), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m-1]+s[m])/2;
}
function renderStats(){
  const ps = filtered.map(o=>o.p).filter(v=>v!=null);
  const ms = filtered.map(o=>o.pm).filter(v=>v!=null);
  const drops = filtered.filter(o=>o._drop>0).length;
  $("#stats").innerHTML =
    `<div><b>${filtered.length}</b><span>ofert po filtrach</span></div>` +
    `<div><b>${fmtN(median(ms))}</b><span>mediana zł/m²</span></div>` +
    `<div><b>${fmtN(median(ps))}</b><span>mediana ceny [zł]</span></div>` +
    `<div><b>${drops}</b><span>z obniżką ceny</span></div>`;
}
function renderHisto(){
  const cv = $("#histo"), ctx = cv.getContext("2d");
  const W = cv.width = cv.clientWidth * devicePixelRatio;
  const H = cv.height = cv.clientHeight * devicePixelRatio;
  ctx.clearRect(0,0,W,H);
  const vals = filtered.map(o=>o.pm).filter(v=>v!=null);
  if(vals.length < 3) return;
  const lo = ppmAll[Math.floor(ppmAll.length*.02)] || Math.min(...vals);
  const hi = ppmAll[Math.floor(ppmAll.length*.98)] || Math.max(...vals);
  const NB = 28, bins = new Array(NB).fill(0);
  for(const v of vals){
    const i = Math.max(0, Math.min(NB-1, Math.floor((v-lo)/(hi-lo)*NB)));
    bins[i]++;
  }
  const top = Math.max(...bins), bw = W/NB;
  for(let i=0;i<NB;i++){
    const h = bins[i]/top*(H-14*devicePixelRatio);
    const mid = lo + (i+.5)*(hi-lo)/NB;
    ctx.fillStyle = colorOf(mid);
    ctx.fillRect(i*bw+1, H-h, bw-2, h);
  }
  ctx.fillStyle = "#68758a";
  ctx.font = `${10*devicePixelRatio}px sans-serif`;
  ctx.fillText(fmtN(lo), 2, 10*devicePixelRatio);
  const t = fmtN(hi);
  ctx.fillText(t, W - ctx.measureText(t).width - 2, 10*devicePixelRatio);
}
function cardHTML(o){
  const img = o.ph.length
    ? `<img loading="lazy" src="${esc(o.ph[0])}"
        onerror="this.outerHTML='<div class=noimg>🏠</div>'">`
    : `<div class="noimg">🏠</div>`;
  const badges = (o._new ? `<span class="badge new">NOWA</span>` : "") +
    (o._drop > 0 ? `<span class="badge drop">-${(o._drop*100).toFixed(0)}%</span>` : "");
  const meta = [PORTAL[o.s] || o.s, o.a ? o.a + " m²" : null,
    o.pm ? fmtN(o.pm) + " zł/m²" : null,
    o.r || null, o.d || null].filter(Boolean).join(" · ");
  return `<div class="card" data-id="${o.id}">${img}<div>
    <h4>${esc(o.t)}${badges}</h4>
    <div class="pr">${fmtP(o.p)}</div>
    <div class="meta">${esc(meta)}</div></div></div>`;
}
function renderMore(){
  const slice = filtered.slice(shown, shown + CHUNK);
  shown += slice.length;
  const sent = $("#more"); if(sent) sent.remove();
  $("#cards").insertAdjacentHTML("beforeend", slice.map(cardHTML).join(""));
  if(shown < filtered.length)
    $("#cards").insertAdjacentHTML("beforeend",
      `<div id="more">… wczytuję (${shown}/${filtered.length})</div>`);
  const m = $("#more");
  if(m) io.observe(m);
}
const io = new IntersectionObserver(es => {
  if(es.some(e => e.isIntersecting)) renderMore();
});
$("#cards").addEventListener("click", e => {
  const card = e.target.closest(".card");
  if(card) openDetail(card.dataset.id, false);
});

/* ---------- szczegóły oferty ---------- */
function openDetail(id, fromMap){
  const o = OFFERS.find(x => x.id === id);
  if(!o) return;
  highlight(id);
  document.querySelectorAll(".card.sel").forEach(c => c.classList.remove("sel"));
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if(card){ card.classList.add("sel");
    if(fromMap) card.scrollIntoView({block:"nearest"}); }
  if(!fromMap && o.lat != null)
    map.flyTo([o.lat, o.lon], Math.max(map.getZoom(), 15), {duration:.5});
  // zdjęcia dociągane z serwerów OLX dopiero teraz — na żądanie
  const gal = o.ph.length ? `<div id="gal">
      <img class="main" id="gmain" src="${esc(o.ph[0])}"
       onerror="this.closest('#gal').style.display='none'"></div>` +
    (o.ph.length > 1 ? `<div id="thumbs">` + o.ph.map((u,i) =>
      `<img src="${esc(u)}" data-i="${i}" class="${i?"":"on"}"
        onerror="this.remove()">`).join("") + `</div>` : "")
    : "";
  const grid = [
    ["Portal", PORTAL[o.s] || o.s],
    ["Metraż", o.a ? o.a + " m²" : null],
    ["Cena za m²", o.pm ? fmtN(o.pm) + " zł" : null],
    ["Pokoje", o.r], ["Piętro", o.f],
    ["Rynek", o.mk], ["Dzielnica", o.d],
    ["Sprzedający", o.b ? "firma / deweloper" : "osoba prywatna"],
    ["Dodane", fmtDate(o.c)],
    ["Pierwszy raz widziana", fmtDate(o.fs)],
  ].filter(x => x[1] != null)
   .map(x => `<div><span>${x[0]}</span><b>${esc(x[1])}</b></div>`).join("");
  let hist = "";
  if(o.h){
    hist = `<div id="dhist"><h3>Historia cen</h3><table>` + o.h.map((x,i) => {
      const prev = i ? o.h[i-1][1] : null;
      const diff = prev == null ? "" :
        `<span class="${x[1] > prev ? "up" : "down"}">
          ${x[1] > prev ? "▲" : "▼"} ${fmtN(Math.abs(x[1]-prev))}</span> `;
      return `<tr><td>${esc(x[0])}</td><td>${diff}${fmtP(x[1])}</td></tr>`;
    }).join("") + `</table></div>`;
  }
  $("#dbody").innerHTML = gal +
    `<h2>${esc(o.t)}</h2>
     <div id="dprice">${fmtP(o.p)}${o.ng ? " <small>do negocjacji</small>" : ""}</div>
     <div id="dgrid">${grid}</div>` + hist +
    (o.rad > 0 ? `<div class="approx">📍 Sprzedający podał lokalizację
       przybliżoną (±${fmtN(o.rad)} m) — pinezka wskazuje okolicę.</div>` : "") +
    (o.dsc ? `<div id="ddesc">${esc(o.dsc)}</div>` : "") +
    `<a class="olxbtn" href="${esc(o.u)}" target="_blank" rel="noopener">
       Otwórz ogłoszenie na ${PORTAL[o.s] || "portalu"} ↗</a>`;
  const th = $("#thumbs");
  if(th) th.addEventListener("click", e => {
    if(e.target.tagName !== "IMG") return;
    $("#gmain").src = o.ph[+e.target.dataset.i];
    th.querySelectorAll("img").forEach(x => x.classList.remove("on"));
    e.target.classList.add("on");
  });
  $("#detail").classList.add("open");
  $("#backdrop").classList.add("open");
}
function closeDetail(){
  $("#detail").classList.remove("open");
  $("#backdrop").classList.remove("open");
}
$("#dclose").onclick = closeDetail;
$("#backdrop").onclick = closeDetail;
addEventListener("keydown", e => { if(e.key === "Escape") closeDetail(); });

/* ---------- zdarzenia ---------- */
let deb;
const soon = () => { clearTimeout(deb); deb = setTimeout(() => apply(), 220); };
["#q","#pmin","#pmax","#amin","#amax","#mmin","#mmax"]
  .forEach(s => $(s).addEventListener("input", soon));
["#qdesc","#market","#seller","#fresh","#sort","#onlydrop","#onlygeo"]
  .forEach(s => $(s).addEventListener("change", () => apply()));
$("#bounds").addEventListener("change", () => apply(false));
map.on("moveend", () => { if($("#bounds").checked) apply(false); });
document.querySelectorAll("#rooms .chip").forEach(b =>
  b.addEventListener("click", () => { b.classList.toggle("on"); apply(); }));
document.querySelectorAll("#portals .chip").forEach(b =>
  b.addEventListener("click", () => { b.classList.toggle("on"); apply(); }));
$("#districts").addEventListener("change", () => apply());
$("#dall").onclick = () => {
  document.querySelectorAll(".dbox").forEach(b => b.checked = true); apply(); };
$("#dnone").onclick = () => {
  document.querySelectorAll(".dbox").forEach(b => b.checked = false); apply(); };
$("#clear").onclick = () => {
  ["#q","#pmin","#pmax","#amin","#amax","#mmin","#mmax"]
    .forEach(s => $(s).value = "");
  ["#market","#seller","#fresh"].forEach(s => $(s).value = "");
  ["#onlydrop","#onlygeo","#bounds"].forEach(s => $(s).checked = false);
  $("#qdesc").checked = true;
  document.querySelectorAll("#rooms .chip").forEach(b => b.classList.remove("on"));
  document.querySelectorAll("#portals .chip").forEach(b => b.classList.add("on"));
  document.querySelectorAll(".dbox").forEach(b => b.checked = true);
  apply();
};
addEventListener("resize", renderHisto);
apply();
</script>
</body>
</html>
"""


# ------------------------------------------------------------------------ main

def parse_cli(argv: list[str] | None):
    portals = ", ".join(cls.label for cls in SOURCES.values())
    parser = argparse.ArgumentParser(
        description=f"Monitor ofert nieruchomości z portali: {portals}. "
                    "Pierwszy raz pobiera wszystko, potem pokazuje tylko "
                    "nowe oferty i zmiany.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--url", action="append",
                        help="adres wyszukiwania z obsługiwanego portalu; można "
                             "podać kilka razy (domyślnie: domyślne wyszukiwanie "
                             "każdego źródła — mieszkania na sprzedaż w Krakowie)")
    parser.add_argument("--source", action="append", choices=sorted(SOURCES),
                        help="zbieraj dane tylko z tego źródła; można podać "
                             "kilka razy (domyślnie: wszystkie zdefiniowane)")
    parser.add_argument("--db", default=DEFAULT_DB, help="plik bazy SQLite")
    parser.add_argument("--quick", action="store_true",
                        help="szybki tryb: sprawdza tylko NOWE oferty "
                             "(bez zmian cen i zniknięć)")
    parser.add_argument("--export", metavar="PLIK.csv",
                        help="po zakończeniu zapisz aktywne oferty do pliku CSV")
    parser.add_argument("--html", metavar="PLIK.html",
                        help="wygeneruj interaktywną mapę ofert z filtrami "
                             "(jeden samodzielny plik HTML)")
    parser.add_argument("--offline", action="store_true",
                        help="nie odpytuj portali (np. sam eksport CSV/HTML z bazy)")
    parser.add_argument("--delay", type=float, default=0.6,
                        help="pauza w sekundach między zapytaniami do portali")
    parser.add_argument("--category-id", help="ręcznie: id kategorii (tylko OLX)")
    parser.add_argument("--city-id", help="ręcznie: id miasta (tylko OLX)")
    parser.add_argument("--region-id", help="ręcznie: id województwa (tylko OLX)")
    parser.add_argument("--force", action="store_true",
                        help="pozwól użyć bazy utworzonej dla innego adresu URL")
    return parser.parse_args(argv)


def select_sources(args) -> list[tuple[type, str]]:
    """Zamienia --url/--source na listę par (klasa_źródła, adres)."""
    urls = list(dict.fromkeys(
        args.url or [cls.default_url for cls in SOURCES.values()]))
    chosen = []
    for url in urls:
        cls = source_for(url)
        if cls is None:
            supported = ", ".join(
                d for c in SOURCES.values() for d in c.domains)
            sys.exit(f"Nieobsługiwany adres: {url}\n"
                     f"Skrypt rozumie wyszukiwania z: {supported}.")
        chosen.append((cls, url))
    if args.source:
        wanted = set(args.source)
        chosen = [(cls, url) for cls, url in chosen if cls.name in wanted]
        if not chosen:
            sys.exit("Po zawężeniu --source nie został żaden adres do "
                     "sprawdzenia. Wybrane źródła: " + ", ".join(sorted(wanted))
                     + ". Sprawdź, czy pasują do adresów podanych w --url.")
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
        log("(tym razem sprawdzam tylko część zapisanych wyszukiwań — "
            "dane pozostałych źródeł zostają w bazie bez zmian)")
    elif new > old:
        log("(rozszerzasz to wyszukiwanie o nowy adres — istniejące "
            "dane zostają, dojdą oferty z nowego adresu)")
    else:
        sys.exit(
            f"Ta baza ({db_path}) była utworzona dla innego wyszukiwania:\n"
            + "\n".join(f"  {u}" for u in stored) +
            "\nUżyj osobnego pliku bazy (--db inna_nazwa.db) albo dodaj "
            "--force, jeśli świadomie zmieniasz wyszukiwanie.")
    return stored


def collect(source, con, quick: bool) -> tuple[list, bool]:
    """Pobiera oferty z jednego źródła. Zwraca (rekordy, czy_pełny_skan)."""
    known = {row[0] for row in con.execute(
        "SELECT id FROM offers WHERE source = ?", (source.name,))}
    if quick and known:
        log("Szybki tryb: szukam tylko nowych ofert (od najnowszych)...")
        items, full_scan = source.fetch_new(known), False
    else:
        if quick and not known:
            log("Pierwszy skan tego źródła — muszę pobrać wszystko "
                "(--quick zadziała od następnego razu).")
        items, full_scan = source.fetch_all(), True
    source.enrich(items, con)
    return [(source.parse(it), it) for it in items], full_scan


def main(argv: list[str] | None = None) -> None:
    args = parse_cli(argv)
    chosen = select_sources(args)

    if not args.offline and not IMPERSONATE:
        log("Wskazówka: 'pip install curl_cffi' znacząco zmniejsza ryzyko "
            "blokady HTTP 403 (skrypt użyje tej biblioteki automatycznie).")

    if (args.db == DEFAULT_DB and not os.path.exists(DEFAULT_DB)
            and os.path.exists(LEGACY_DB)):
        os.replace(LEGACY_DB, DEFAULT_DB)
        log(f"(znalazłem bazę ze starszej wersji skryptu — zmieniam nazwę "
            f"{LEGACY_DB} → {DEFAULT_DB}, wszystkie dane zostają)")

    con = init_db(args.db)
    urls = [url for _, url in chosen]
    stored_urls = [] if args.offline else check_db_urls(
        con, args.db, urls, args.force)

    if args.offline:
        if args.export:
            export_csv(con, args.export)
        if args.html:
            export_html(con, args.html)
        if not args.export and not args.html:
            log("Tryb --offline: nic nie pobrano. Dodaj --export PLIK.csv "
                "lub --html PLIK.html, aby wyeksportować dane z bazy.")
        return

    overrides = {"category_id": args.category_id,
                 "city_id": args.city_id, "region_id": args.region_id}
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

    report(result, args.db, con)
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

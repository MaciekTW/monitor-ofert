# -*- coding: utf-8 -*-
"""Testy kontraktów OLX — prawdziwe zapytania do endpointów, z których korzysta
src/monitor_ofert.py. Sprawdzają, czy endpointy odpowiadają i czy odpowiedzi mają
pola, na których opierają się resolve_api_params, reported_count, fetch_*_olx
oraz parse_offer_olx."""

from __future__ import annotations

import re
from collections import Counter

import pytest

import monitor_ofert as m
from tests.contract.helpers import assert_all, assert_some, dig, fetch

# identyfikatory wyszukiwania „mieszkania / sprzedaż / Kraków” — podane wprost,
# żeby testy endpointu ofert nie zależały od działania mechanizmu ich wykrywania
KRAKOW = {"category_id": "14", "region_id": "4", "city_id": "8959"}
ID_KEYS = ("category_id", "region_id", "city_id")


def offers_query(**extra) -> dict:
    return {
        **KRAKOW,
        "offset": 0,
        "limit": m.PAGE_LIMIT,
        "sort_by": "created_at:desc",
        **extra,
    }


@pytest.fixture(scope="module")
def offers_page() -> dict:
    resp = fetch(m.API_OFFERS, params=offers_query())
    assert resp.status_code == 200, f"{m.API_OFFERS}: HTTP {resp.status_code}"
    return resp.json()


@pytest.fixture(scope="module")
def offers(offers_page) -> list[dict]:
    data = dig(offers_page, "data")
    assert isinstance(data, list) and data, "pole data jest puste albo nie jest listą"
    return data


# ------------------------------------------------ wykrywanie parametrów wyszukiwania


def test_search_page_html_contains_search_ids():
    """resolve_api_params: identyfikatory wyszukane regexem w HTML wyników."""
    resp = fetch(m.OlxSource.default_url, as_json=False)
    assert resp.status_code == 200, f"HTTP {resp.status_code}"
    html = resp.text
    for key in ID_KEYS:
        camel = re.sub(r"_(\w)", lambda g: g.group(1).upper(), key)
        hits = re.findall(rf'["\']?{key}["\']?\s*[:=]\s*["\']?(\d+)', html)
        hits += re.findall(rf'["\']?{camel}["\']?\s*[:=]\s*["\']?(\d+)', html)
        assert hits, f"w HTML strony wyników nie ma {key} ani {camel}"
        top = Counter(hits).most_common(1)[0][0]
        assert top == KRAKOW[key], f"{key}: najczęstsza wartość {top}, oczekiwano {KRAKOW[key]}"


# ------------------------------------------------------------ endpoint ofert


# OLX nie gwarantuje pełnych PAGE_LIMIT wyników organicznych — potrafi wydać
# ich o kilka mniej (np. gdy oferta zniknie między zbudowaniem listy a
# odpowiedzią). Nie sprawdzamy więc równości, tylko czy strona nie została
# realnie obcięta.
ORGANIC_TOLERANCE = 5


def test_offers_endpoint_returns_full_page(offers_page, offers):
    """Paginacja zakłada, że strona, która nie jest ostatnia, ma co najmniej
    PAGE_LIMIT ofert (krótsza = koniec wyników). crawl() liczy przy tym całe
    pole data, do którego OLX dokłada do ofert organicznych kilka promowanych —
    indeksy tych organicznych są w metadata.source."""
    assert len(offers) >= m.PAGE_LIMIT, (
        f"data ma {len(offers)} ofert, oczekiwano co najmniej {m.PAGE_LIMIT} — "
        "przy tylu wynikach crawl() uznałby stronę za ostatnią"
    )
    organic = dig(offers_page, "metadata.source.organic")
    assert len(organic) >= m.PAGE_LIMIT - ORGANIC_TOLERANCE, (
        f"ofert organicznych {len(organic)}, oczekiwano ok. {m.PAGE_LIMIT}"
    )


def test_offers_metadata_reports_count(offers_page):
    """reported_count czyta liczbę wyników z metadata."""
    meta = dig(offers_page, "metadata")
    counts = {k: meta.get(k) for k in ("total_elements", "visible_total_count", "total")}
    assert any(isinstance(v, int) and v > 0 for v in counts.values()), (
        f"brak dodatniej liczby wyników w metadata: {counts}"
    )


def organic_ids(page: dict) -> set:
    data = dig(page, "data")
    return {data[i]["id"] for i in dig(page, "metadata.source.organic")}


# OLX potrafi powtórzyć ofertę z granicy stron — ostatnia oferta organiczna
# strony N bywa pierwszą ofertą strony N+1 (taka strona ma wtedy o jedną ofertę
# organiczną mniej). crawl() zbiera oferty do słownika po id, więc pojedyncze
# powtórzenie mu nie szkodzi; groźne byłoby dopiero, gdyby offset przestał
# przesuwać okno wyników i strony zaczęły się realnie dublować.
OVERLAP_TOLERANCE = 2


def test_offers_offset_returns_next_page(offers_page):
    """offset=PAGE_LIMIT zwraca kolejną stronę wyników organicznych,
    która — poza ofertą ze styku stron — nie pokrywa się z pierwszą."""
    resp = fetch(m.API_OFFERS, params=offers_query(offset=m.PAGE_LIMIT))
    assert resp.status_code == 200
    next_ids = organic_ids(resp.json())
    assert next_ids, "druga strona wyników jest pusta"
    overlap = next_ids & organic_ids(offers_page)
    assert len(overlap) <= OVERLAP_TOLERANCE, (
        f"druga strona powtarza {len(overlap)}/{len(next_ids)} ofert z pierwszej "
        f"(dopuszczamy {OVERLAP_TOLERANCE} ze styku stron): {sorted(overlap)[:10]}"
    )


def test_offers_price_filter_is_applied():
    """Dzielenie na przedziały cen opiera się na filter_float_price:from/to."""
    lo, hi = 400_000, 500_000
    resp = fetch(
        m.API_OFFERS,
        params=offers_query(**{"filter_float_price:from": lo, "filter_float_price:to": hi}),
    )
    assert resp.status_code == 200
    data = dig(resp.json(), "data")
    assert data, "filtr cen zwrócił pustą listę"
    prices = [m.parse_offer_olx(o)["price"] for o in data]
    outside = [p for p in prices if p is not None and not lo <= p <= hi]
    assert not outside, f"ceny poza przedziałem {lo}–{hi}: {outside[:10]}"


# ---------------------------------------------------- pola oferty (parse_offer_olx)


def params_of(offer: dict) -> dict:
    return {p["key"]: p.get("value") for p in offer.get("params") or [] if isinstance(p, dict) and "key" in p}


def test_offer_core_fields(offers):
    assert_all(offers, lambda o: isinstance(o.get("id"), int), "id (int)")
    assert_all(offers, lambda o: str(o.get("url", "")).startswith("https://"), "url")
    assert_all(offers, lambda o: isinstance(o.get("title"), str) and o["title"], "title")
    assert_all(offers, lambda o: isinstance(o.get("params"), list), "params (lista)")
    assert_all(offers, lambda o: "business" in o, "business")
    assert_all(offers, lambda o: isinstance(o.get("created_time"), str), "created_time")
    assert_some(
        offers,
        lambda o: isinstance(o.get("last_refresh_time"), str),
        "last_refresh_time",
    )


def test_offer_price_param(offers):
    def ok(o):
        price = params_of(o).get("price")
        return isinstance(price, dict) and "value" in price and "currency" in price and "negotiable" in price

    assert_all(offers, ok, "params[key=price] z value/currency/negotiable")
    assert_some(
        offers,
        lambda o: isinstance(params_of(o)["price"]["value"], (int, float)),
        "liczbowa cena w params.price.value",
    )


@pytest.mark.parametrize("key", ["m", "price_per_m", "rooms", "floor_select", "market"])
def test_offer_detail_params(offers, key):
    """Parametry ogłoszenia mają postać {key, label}."""
    assert_some(
        offers,
        lambda o: isinstance(params_of(o).get(key), dict) and {"key", "label"} <= set(params_of(o)[key]),
        f"params[key={key}] z polami key/label",
    )


def test_offer_area_is_numeric(offers):
    assert_some(
        offers,
        lambda o: m.to_float((params_of(o).get("m") or {}).get("key")),
        "params.m.key da się zamienić na liczbę (metraż)",
    )


def test_offer_location(offers):
    assert_all(
        offers,
        lambda o: isinstance(dig(o, "location.city.name"), str),
        "location.city.name",
    )
    assert_all(
        offers,
        lambda o: m.slugify(o["location"]["city"]["name"]) == "krakow",
        "oferty z Krakowa (check_city)",
    )
    assert_some(
        offers,
        lambda o: isinstance((o["location"].get("district") or {}).get("name"), str),
        "location.district.name",
    )


def test_offer_map(offers):
    def ok(o):
        node = o.get("map") or {}
        return all(isinstance(node.get(k), (int, float)) for k in ("lat", "lon", "radius"))

    assert_some(offers, ok, "map.lat/lon/radius")


def test_offer_photos_and_description(offers):
    """Używane przez mapę HTML: zdjęcia z szablonem rozmiaru i opis."""

    def photo_ok(o):
        photos = o.get("photos") or []
        return bool(photos) and all(
            "{width}" in p.get("link", "") and "{height}" in p.get("link", "") for p in photos
        )

    assert_some(offers, photo_ok, "photos[].link z {width} i {height}")
    assert_some(
        offers,
        lambda o: isinstance(o.get("description"), str) and o["description"],
        "description",
    )

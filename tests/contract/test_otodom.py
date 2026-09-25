# -*- coding: utf-8 -*-
"""Testy kontraktów Otodom — prawdziwe zapytania do stron, z których
src/monitor_ofert.py wyciąga dane __NEXT_DATA__. Sprawdzają, czy strony odpowiadają
i czy mają pola, na których opierają się otodom_page, fetch_*_otodom,
fetch_otodom_detail oraz parse_offer_otodom."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

import monitor_ofert as m
from tests.contract.helpers import assert_all, assert_some, dig, fetch

SEARCH_URL = m.OtodomSource.default_url


def search(**params) -> dict:
    """Strona wyników z parametrami jak w otodom_page; zwraca pageProps."""
    query = {"viewType": "listing", "limit": m.OTO_LIMIT, **params}
    resp = fetch(SEARCH_URL, params=query, as_json=False)
    assert resp.status_code == 200, f"{SEARCH_URL}: HTTP {resp.status_code}"
    return dig(m.next_data(resp.text), "props.pageProps")


def items_of(page_props: dict) -> list[dict]:
    items = dig(page_props, "data.searchAds.items")
    assert isinstance(items, list) and items, "searchAds.items jest puste albo nie jest listą"
    return m._oto_flatten(items)


def price_of(item: dict):
    return (item.get("totalPrice") or {}).get("value")


@pytest.fixture(scope="module")
def page_props() -> dict:
    return search(page=1)


@pytest.fixture(scope="module")
def items(page_props) -> list[dict]:
    return items_of(page_props)


@pytest.fixture(scope="module")
def ad(items) -> dict:
    slug = next(i["slug"] for i in items if i.get("slug"))
    url = f"https://www.otodom.pl/pl/oferta/{slug}"
    resp = fetch(url, as_json=False)
    assert resp.status_code == 200, f"{url}: HTTP {resp.status_code}"
    return dig(m.next_data(resp.text), "props.pageProps.ad")


# ----------------------------------------------------------- strona wyników


def test_search_listing_tracking_counts(page_props):
    """Liczba stron i wyników sterują pobieraniem i dzieleniem na przedziały cen."""
    listing = dig(page_props, "tracking.listing")
    for key in ("page_count", "result_count"):
        value = dig(listing, key)
        assert isinstance(value, int) and value > 0, f"tracking.listing.{key} = {value!r}"


def test_search_page_parameter_returns_next_page(items):
    next_ids = {i["id"] for i in items_of(search(page=2))}
    first_ids = {i["id"] for i in items}
    assert len(next_ids & first_ids) < len(next_ids) / 2, (
        "parametr page nie przesuwa wyników — druga strona powtarza pierwszą"
    )


def test_search_price_filter_is_applied():
    """Dzielenie na przedziały cen opiera się na priceMin/priceMax."""
    lo, hi = 400_000, 500_000
    found = items_of(search(page=1, priceMin=lo, priceMax=hi))
    outside = [p for p in map(price_of, found) if p is not None and not lo <= p <= hi]
    assert not outside, f"ceny poza przedziałem {lo}–{hi}: {outside[:10]}"


def test_search_latest_sort_is_accepted():
    """Szybki tryb prosi o sortowanie by=LATEST&direction=DESC."""
    assert items_of(search(page=1, by="LATEST", direction="DESC"))


# ------------------------------------------- pola oferty z listy (parse_offer_otodom)


def test_item_core_fields(items):
    assert_all(items, lambda i: isinstance(i.get("id"), int), "id (int)")
    assert_all(items, lambda i: isinstance(i.get("slug"), str) and i["slug"], "slug")
    assert_all(items, lambda i: isinstance(i.get("title"), str) and i["title"], "title")
    assert_all(items, lambda i: "agency" in i, "agency")
    assert_some(items, lambda i: isinstance(i.get("pushedUpAt"), str), "pushedUpAt")


def test_item_prices_and_area(items):
    assert_some(items, lambda i: isinstance(price_of(i), (int, float)), "totalPrice.value")
    assert_some(
        items,
        lambda i: isinstance((i.get("pricePerSquareMeter") or {}).get("value"), (int, float)),
        "pricePerSquareMeter.value",
    )
    assert_some(items, lambda i: m.to_float(i.get("areaInSquareMeters")), "areaInSquareMeters")


def test_item_rooms_use_known_enum(items):
    values = {i.get("roomsNumber") for i in items} - {None}
    assert values, "żadna oferta nie ma roomsNumber"
    unknown = values - set(m._OTO_ROOMS)
    assert not unknown, f"nieznane wartości roomsNumber: {unknown}"


def test_item_floor_uses_known_enum(items):
    """floorNumber z listy to zapasowe piętro, gdy nie ma strony oferty."""
    values = {i.get("floorNumber") for i in items} - {None}
    assert values, "żadna oferta nie ma floorNumber"
    unknown = values - set(m._OTO_FLOOR_NUMBER)
    assert not unknown, f"nieznane wartości floorNumber: {unknown}"


def test_item_location(items):
    assert_all(
        items,
        lambda i: isinstance(dig(i, "location.address.city.name"), str),
        "location.address.city.name",
    )
    assert_some(
        items,
        lambda i: any(
            n.get("locationLevel") == "district" and n.get("name")
            for n in dig(i, "location.reverseGeocoding.locations")
        ),
        "location.reverseGeocoding.locations[] z locationLevel=district",
    )


def test_item_first_created_date(items):
    """Bez strony oferty _oto_created_at bierze datę dodania z createdAtFirst
    (czas warszawski z sufiksem „Z”; dateCreated to data odświeżenia)."""
    assert_some(
        items,
        lambda i: m._oto_created_at(i, {}) is not None,
        "createdAtFirst z poprawną datą",
    )


# ------------------------------------------------ strona oferty (fetch_otodom_detail)


def test_ad_coordinates(ad):
    coords = dig(ad, "location.coordinates")
    for key in ("latitude", "longitude"):
        assert isinstance(coords.get(key), (int, float)), f"coordinates.{key} = {coords.get(key)!r}"


def test_ad_district(ad):
    """parse_offer_otodom czyta dzielnicę z reverseGeocoding. Sama dzielnica
    jest opcjonalna — oferty spoza miasta mają hierarchię powiat/gmina bez
    poziomu district, a kod ma dla nich fallback (district or ad["district"],
    kolumna w bazie jest NULL-owalna). Tu sprawdzamy więc kształt listy;
    obecności locationLevel=district pilnuje test_item_location na całej
    liście wyników."""
    locations = dig(ad, "location.reverseGeocoding.locations")
    assert isinstance(locations, list) and locations, "reverseGeocoding.locations jest puste"
    assert_all(
        locations,
        lambda n: isinstance(n, dict) and isinstance(n.get("locationLevel"), str) and n.get("name"),
        "locations[] z polami locationLevel i name",
    )


def test_ad_target_details(ad):
    target = dig(ad, "target")
    assert isinstance(target, dict)
    for key in ("Floor_no", "Rooms_num"):
        assert key in target, f"brak target.{key}; dostępne: {sorted(target)[:40]}"
    assert m._oto_floor(target["Floor_no"]) is not None, (
        f"_oto_floor nie rozumie target.Floor_no = {target['Floor_no']!r}"
    )
    # roku budowy nie podaje każda oferta (ma go ok. 94%), więc samego klucza
    # nie wymagamy — ale jeśli jest, to musi dać się odczytać
    if target.get("Build_year") is not None:
        assert m.to_year(target["Build_year"]) is not None, (
            f"to_year nie rozumie target.Build_year = {target['Build_year']!r}"
        )


def test_ad_images(ad):
    images = dig(ad, "images")
    assert isinstance(images, list) and images, "brak zdjęć"
    assert_all(
        images,
        lambda img: (
            urlparse(str(img.get("large") or img.get("medium") or img.get("small") or "")).scheme == "https"
        ),
        "images[].large/medium/small",
    )


def test_ad_text_fields(ad):
    assert isinstance(dig(ad, "description"), str) and ad["description"]
    created = dig(ad, "createdAt")
    assert isinstance(created, str) and created.endswith("Z"), f"createdAt nie jest datą UTC: {created!r}"
    assert dig(ad, "market") in m._OTO_MARKET, f"nieznany market: {ad['market']!r}"
    assert isinstance(dig(ad, "advertType"), str)

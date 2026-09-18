# -*- coding: utf-8 -*-
"""Testy kontraktów Gratka — prawdziwe zapytania do API GraphQL, z którego
korzysta src/monitor_ofert.py. Sprawdzają, czy API odpowiada i czy odpowiedzi
mają pola, na których opierają się decode_gratka, gratka_page, fetch_*_gratka,
fetch_gratka_detail oraz parse_offer_gratka."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

import monitor_ofert as m
from tests.contract.helpers import assert_all, assert_some, dig, fetch, post_json

SEARCH_URL = m.GratkaSource.default_url


def gql(query: str, variables: dict) -> dict:
    """Zapytanie tym samym adresem i nagłówkami, których używa skrypt.
    Celowo bez ponawiania z gratka_gql — test ma zobaczyć faktyczną odpowiedź."""
    resp = post_json(m.API_GRATKA, {"query": query, "variables": variables}, headers=m.GRA_HEADERS)
    assert resp.status_code == 200, f"{m.API_GRATKA}: HTTP {resp.status_code}"
    body = resp.json()
    assert not body.get("errors"), f"API zwróciło błędy: {body['errors']}"
    return body.get("data") or {}


def search(**extra) -> dict:
    """Strona wyników w postaci, jakiej używa gratka_page; zwraca properties."""
    data = gql(m.GRA_SEARCH_QUERY, {"url": m.gratka_path(SEARCH_URL, **extra)})
    return dig(data, "searchProperties.properties")


def nodes_of(properties: dict) -> list[dict]:
    items = dig(properties, "nodes")
    assert isinstance(items, list) and items, "nodes jest puste albo nie jest listą"
    return items


def price_of(node: dict):
    return (node.get("price") or {}).get("amount")


@pytest.fixture(scope="module")
def properties() -> dict:
    return search()


@pytest.fixture(scope="module")
def nodes(properties) -> list[dict]:
    return nodes_of(properties)


@pytest.fixture(scope="module")
def detail(nodes) -> dict:
    """Szczegóły pierwszej oferty, która ma adres — tak jak fetch_gratka_detail."""
    path = next(n["url"] for n in nodes if n.get("url"))
    return dig(gql(m.GRA_DETAIL_QUERY, {"url": path}), "getProperty")


# ------------------------------------------------- adres wyszukiwania (decode)


def test_decode_reads_search_parameters():
    """prepare() sprawdza po tym, czy adres w ogóle jest wyszukiwaniem ofert."""
    info = dig(gql(m.GRA_DECODE_QUERY, {"url": m.gratka_path(SEARCH_URL)}), "decodeListingUrl")
    params = dig(info, "listingParameters.searchParameters")
    assert params.get("transaction") in m._GRA_TRANSACTION, (
        f"nieznana transakcja: {params.get('transaction')!r}"
    )
    kinds = params.get("type")
    assert isinstance(kinds, list) and kinds, f"brak typu nieruchomości: {kinds!r}"
    names = [loc.get("name") for loc in dig(info, "listingParameters.locations")]
    assert "Kraków" in names, f"adres domyślnego wyszukiwania nie wskazuje Krakowa: {names}"


def test_decode_reports_total_count():
    """Deklarowana liczba ofert steruje liczbą stron do pobrania."""
    info = dig(gql(m.GRA_DECODE_QUERY, {"url": m.gratka_path(SEARCH_URL)}), "decodeListingUrl")
    total = dig(info, "totalCount")
    assert isinstance(total, int) and total > 0, f"totalCount = {total!r}"


def test_decode_rejects_nonsense_url():
    """Zły adres ma skończyć się błędem, a nie pustą listą ofert."""
    resp = post_json(
        m.API_GRATKA,
        {
            "query": m.GRA_DECODE_QUERY,
            "variables": {"url": "/nieruchomosci/mieszkania/miasto-ktorego-nie-ma"},
        },
        headers=m.GRA_HEADERS,
    )
    body = resp.json()
    assert body.get("errors") or not (body.get("data") or {}).get("decodeListingUrl"), (
        "API przyjęło nieistniejące wyszukiwanie bez błędu"
    )


# ---------------------------------------------------------- strona wyników


def test_search_total_count(properties):
    total = dig(properties, "totalCount")
    assert isinstance(total, int) and total > 0, f"totalCount = {total!r}"


def test_search_page_size_matches_constant(nodes):
    assert len(nodes) == m.GRA_PAGE, (
        f"strona wyników ma {len(nodes)} ofert, a GRA_PAGE = {m.GRA_PAGE} "
        "(od tego zależy liczba pobieranych stron)"
    )


def test_search_page_parameter_returns_next_page(nodes):
    next_ids = {n["idOnFrontend"] for n in nodes_of(search(page=2))}
    first_ids = {n["idOnFrontend"] for n in nodes}
    assert len(next_ids & first_ids) < len(next_ids) / 2, (
        "parametr page nie przesuwa wyników — druga strona powtarza pierwszą"
    )


def test_search_reaches_last_page(properties):
    """fetch_all_gratka idzie stronami do końca — serwis nie ucina głębokości."""
    total = dig(properties, "totalCount")
    last = -(-total // m.GRA_PAGE)
    assert nodes_of(search(page=last)), f"ostatnia strona ({last}) jest pusta"


def test_search_newest_sort_is_applied():
    """Szybki tryb prosi o sortowanie ?sort=newest i ufa kolejności dat."""
    dates = [n.get("addedAt") for n in nodes_of(search(sort="newest"))]
    assert all(isinstance(d, str) for d in dates), f"addedAt nie zawsze jest datą: {dates[:5]}"
    assert dates == sorted(dates, reverse=True), f"oferty nie są od najnowszych: {dates[:5]}"


def test_search_price_filter_is_applied():
    """Filtry z adresu użytkownika muszą dojeżdżać do API nieprzekodowane."""
    limit = 400_000
    found = nodes_of(search(**{"cena-calkowita:max": limit}))
    above = [p for p in map(price_of, found) if m.to_float(p) and m.to_float(p) > limit]
    assert not above, f"ceny powyżej {limit}: {above[:10]}"


# ------------------------------------------- pola oferty z listy (parse_offer_gratka)


def test_node_core_fields(nodes):
    assert_all(nodes, lambda n: m._gra_id(n) is not None, "idOnFrontend (liczba)")
    assert_all(nodes, lambda n: str(n.get("url") or "").startswith("/"), "url (ścieżka oferty)")
    assert_all(nodes, lambda n: isinstance(n.get("addedAt"), str) and n["addedAt"], "addedAt")
    assert_all(nodes, lambda n: m._gra_title(n), "tytuł (advertisementText albo title + ulica)")


def test_node_prices_and_area(nodes):
    assert_some(nodes, lambda n: m.to_float(price_of(n)), "price.amount")
    assert_some(nodes, lambda n: m.to_float((n.get("priceM2") or {}).get("amount")), "priceM2.amount")
    assert_all(nodes, lambda n: m.to_float(n.get("area")), "area")


def test_node_rooms_and_floor(nodes):
    assert_some(nodes, lambda n: isinstance(n.get("numberOfRooms"), str), "numberOfRooms")
    assert_some(nodes, lambda n: m._gra_floor(n, {}) is not None, "floorFormatted zrozumiałe dla _gra_floor")


def test_node_location(nodes):
    """Mapa stoi na współrzędnych, a raport na mieście i dzielnicy."""
    assert_all(
        nodes,
        lambda n: isinstance(dig(n, "location.map.center.latitude"), (int, float)),
        "location.map.center.latitude",
    )
    assert_all(nodes, lambda n: m._gra_place(n)[0], "miasto z location.location")
    assert_some(nodes, lambda n: m._gra_place(n)[1], "dzielnica z location.location")


def test_node_seller_fields(nodes):
    """Po contact.company i development poznajemy ofertę biura/dewelopera."""
    assert_all(nodes, lambda n: "contact" in n and "development" in n, "contact i development")
    assert_some(nodes, lambda n: (n.get("contact") or {}).get("company"), "contact.company")


def test_parse_offer_fills_database_columns(nodes):
    """Kontrakt całości: oferta z listy → komplet kolumn tabeli offers."""
    offer = m.parse_offer_gratka(nodes[0])
    assert set(offer) == set(m.OFFER_COLUMNS), (
        f"brakuje: {set(m.OFFER_COLUMNS) - set(offer)}, nadmiarowe: {set(offer) - set(m.OFFER_COLUMNS)}"
    )
    assert offer["uid"].startswith("gra:") and offer["source"] == "gratka"
    assert urlparse(offer["url"]).netloc == "gratka.pl", offer["url"]
    for key in ("id", "title", "area", "lat", "lon", "city", "created_at"):
        assert offer[key] is not None, f"puste pole {key} w {offer}"


# --------------------------------------------- strona oferty (fetch_gratka_detail)


def test_detail_market_and_floor(detail):
    """Rynek jest tylko tutaj — na liście wyników go nie ma."""
    assert detail.get("marketType") in m._GRA_MARKET, f"nieznany marketType: {detail.get('marketType')!r}"
    assert isinstance(detail.get("floor"), int), f"floor nie jest liczbą: {detail.get('floor')!r}"
    assert m.to_float(detail.get("area")), f"area = {detail.get('area')!r}"


def test_detail_description(detail):
    assert isinstance(detail.get("description"), str) and detail["description"], "pusty opis oferty"


def test_detail_photos(detail):
    photos = detail.get("photos")
    assert isinstance(photos, list) and photos, "oferta bez zdjęć"
    assert_all(photos, lambda p: isinstance(p.get("id"), str) and p["id"], "photos[].id")


def test_detail_photo_url_is_usable(detail):
    """Mapa pokazuje miniatury złożone z identyfikatora kadru (GRA_THUMB)."""
    photo = detail["photos"][0]
    url = m.GRA_THUMB.format(id=photo["id"], name=photo.get("name") or "zdjecie")
    resp = fetch(url, as_json=False)
    assert resp.status_code == 200, f"{url}: HTTP {resp.status_code}"
    assert resp.headers.get("content-type", "").startswith("image/"), resp.headers.get("content-type")


def test_detail_of_missing_offer_is_null():
    """Zniknięta oferta ma dać null, a nie błąd — na tym stoi obsługa 'None'."""
    data = gql(m.GRA_DETAIL_QUERY, {"url": "/nieruchomosci/mieszkanie-krakow-nieistniejace/ob/1"})
    assert data.get("getProperty") is None, "API zwróciło ofertę pod zmyślonym adresem"

# -*- coding: utf-8 -*-
"""Szczegóły ofert Otodom (piętro, rynek, rok budowy) nie mogą ginąć przy
zwykłym skanie listy, a waluta musi pochodzić z oferty — testy offline,
bez zapytań do portalu."""

from __future__ import annotations

import json

import monitor_ofert as m

AD = {
    "market": "SECONDARY",
    "coordinates": {"latitude": 50.06, "longitude": 19.94},
    "target": {"Floor_no": ["floor_3"], "Build_year": "1998"},
}


def listing_item(**extra) -> dict:
    """Oferta tak, jak przychodzi z listy wyników — bez strony oferty."""
    return {"id": 123, "slug": "mieszkanie-ID123", "title": "Mieszkanie", "areaInSquareMeters": 50, **extra}


def db_with_offer(con, ad: dict | None, lat: float | None = 50.06) -> None:
    raw = json.dumps(listing_item(**({"_ad": ad} if ad else {})))
    con.execute(
        "INSERT INTO offers(uid, source, id, lat, raw) VALUES ('oto:123', 'otodom', 123, ?, ?)",
        (lat, raw),
    )


def test_floor_falls_back_to_listing_floor_number():
    assert m.parse_offer_otodom(listing_item(floorNumber="SECOND"))["floor"] == "2"
    assert m.parse_offer_otodom(listing_item(floorNumber="GROUND"))["floor"] == "Parter"
    assert m.parse_offer_otodom(listing_item())["floor"] is None


def test_detail_floor_wins_over_listing():
    parsed = m.parse_offer_otodom(listing_item(floorNumber="FIRST", _ad=AD))
    assert parsed["floor"] == "3"


def test_floor_labels_beyond_numbers():
    assert m._oto_floor(["floor_higher_10"]) == "Powyżej 10"
    assert m._oto_floor(["garret"]) == "Poddasze"


def test_enrich_restores_stored_detail_for_known_offer():
    con = m.init_db(":memory:")
    db_with_offer(con, AD)
    item = listing_item(floorNumber="FIRST")
    m.OtodomSource("https://www.otodom.pl/", 0).enrich([item], con)  # bez sieci: oferta ma współrzędne
    parsed = m.parse_offer_otodom(item)
    assert (parsed["floor"], parsed["market"], parsed["build_year"]) == ("3", "Wtórny", 1998)
    assert parsed["lat"] == 50.06


def test_sync_of_known_offer_keeps_detail_columns():
    con = m.init_db(":memory:")
    item = listing_item(_ad=AD)
    m.sync(con, [(m.parse_offer_otodom(item), item)], set())
    again = listing_item()  # kolejny skan: sama lista wyników
    m.OtodomSource("https://www.otodom.pl/", 0).enrich([again], con)
    m.sync(con, [(m.parse_offer_otodom(again), again)], set())
    row = con.execute("SELECT floor, market, build_year FROM offers WHERE uid = 'oto:123'").fetchone()
    assert row == ("3", "Wtórny", 1998)


def test_migration_backfills_wiped_columns():
    con = m.init_db(":memory:")
    db_with_offer(con, AD)
    con.execute("DELETE FROM meta WHERE key = 'fix:otodom_columns'")
    m.migrate_db(con)
    row = con.execute("SELECT floor, market, build_year FROM offers WHERE uid = 'oto:123'").fetchone()
    assert row == ("3", "Wtórny", 1998)


def test_currency_comes_from_listing_price():
    eur = {"value": 1150000, "currency": "EUR"}
    assert m.parse_offer_otodom(listing_item(totalPrice=eur))["currency"] == "EUR"
    assert m.parse_offer_otodom(listing_item(totalPrice={"value": 500000, "currency": "PLN"}))["currency"] == "PLN"
    assert m.parse_offer_otodom(listing_item())["currency"] == "PLN"  # cena ukryta — „Zapytaj o cenę”


def test_migration_fixes_currency():
    con = m.init_db(":memory:")
    raw = json.dumps(listing_item(totalPrice={"value": 1150000, "currency": "EUR"}))
    con.execute(
        "INSERT INTO offers(uid, source, id, currency, raw) VALUES ('oto:123', 'otodom', 123, 'PLN', ?)",
        (raw,),
    )
    con.execute("DELETE FROM meta WHERE key = 'fix:otodom_columns'")
    m.migrate_db(con)
    assert con.execute("SELECT currency FROM offers WHERE uid = 'oto:123'").fetchone() == ("EUR",)

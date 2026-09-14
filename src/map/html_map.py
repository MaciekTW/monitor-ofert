# -*- coding: utf-8 -*-
"""
Interaktywna mapa HTML — generuje pojedynczy, samowystarczalny plik HTML
z mapą (Leaflet), listą i filtrami aktywnych ofert zapisanych w bazie
oraz drugą zakładką z historią rynku (liczba ofert, ceny i ich zmiany w czasie).

Wygląd i logika strony są w katalogu template/ (szablon Jinja2 index.html
oraz style.css, app.js, history.js i krakow_boundary.js wklejane do niego bez zmian).
Zewnętrzne biblioteki
(Leaflet, Apache ECharts, Tailwind CSS w wersji przeglądarkowej) leżą w katalogu deps/
w głównym katalogu repozytorium.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup


def log(msg: str = "") -> None:
    print(msg, flush=True)


def meta_get(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


TEMPLATE_DIR = Path(__file__).parent / "template"
DEPS_DIR = Path(__file__).parents[2] / "deps"  # np. js/leaflet.js, css/leaflet.css


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


def export_html(con: sqlite3.Connection, path: str) -> None:
    """Buduje pojedynczy, samowystarczalny plik HTML z mapą, listą i filtrami.
    Lekkie dane (ceny, metraże, współrzędne, opisy) siedzą w pliku;
    zdjęcia dociągają się z serwerów OLX dopiero po otwarciu oferty."""
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
        if source == "otodom":
            ad = raw_offer.get("_ad") or {}
            photos = [u for u in (ad.get("images") or [])[:8] if isinstance(u, str)]
            desc = strip_html(ad.get("description") or "")
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
    }

    page = make_env().get_template("index.html").render(meta=meta, offers=offers, hist=market_history(con))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    size_mb = len(page.encode("utf-8")) / 1_048_576
    log(
        f"\n✔ Zapisano interaktywną mapę {len(offers)} ofert do „{path}” "
        f"({size_mb:.1f} MB). Otwórz ten plik w przeglądarce."
    )

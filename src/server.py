# -*- coding: utf-8 -*-
"""
Lokalny serwer mapy (--serve) — podaje wygenerowany plik HTML i zapisuje do bazy
to, co użytkownik zmienia na stronie: oznaczenie oferty jako śledzonej i ręcznie
poprawioną lokalizację. Zmiany siedzą w osobnej tabeli user_edits, więc kolejne
skany portali ich nie nadpisują.

Serwer słucha tylko na 127.0.0.1 i jest przeznaczony wyłącznie do użytku lokalnego.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gtfs

# user_edits: to, co użytkownik ustawia na stronie — śledzenie (ze statusem i notatką),
# ukrycie oferty i poprawiona lokalizacja; status i notatka zostają po zakończeniu śledzenia
# offer_events: zniknięcia i powroty ofert wykryte przy skanach (kind: removed | returned)
SCHEMA = """
CREATE TABLE IF NOT EXISTS user_edits(
    uid TEXT PRIMARY KEY,
    tracked INTEGER DEFAULT 0,
    lat REAL, lon REAL,
    hidden INTEGER DEFAULT 0,
    status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS offer_events(
    uid TEXT,
    ts TEXT,
    kind TEXT
);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # tabela założona, zanim doszły ukrywanie, status i notatka
    have = {row[1] for row in con.execute("PRAGMA table_info(user_edits)")}
    for column, kind in (("hidden", "INTEGER DEFAULT 0"), ("status", "TEXT"), ("note", "TEXT")):
        if column not in have:
            con.execute(f"ALTER TABLE user_edits ADD COLUMN {column} {kind}")


def load_edits(con: sqlite3.Connection) -> dict[str, list]:
    """{uid: [śledzona 0/1, lat, lon, ukryta 0/1, status, notatka]} — lat/lon puste,
    gdy lokalizacji nie poprawiano."""
    ensure_schema(con)
    query = "SELECT uid, tracked, lat, lon, hidden, status, note FROM user_edits"
    return {uid: values for uid, *values in con.execute(query)}


def save_edit(con: sqlite3.Connection, change: dict) -> None:
    """change: {"uids": [...]} oraz dowolne z: "tracked": bool, "hidden": bool,
    "loc": [lat, lon] | None, "status": tekst | None, "note": tekst | None.
    Kilka uid naraz, bo to samo ogłoszenie z kilku portali ma w bazie osobne wiersze."""
    uids = change["uids"]
    if not isinstance(uids, list) or not all(isinstance(uid, str) for uid in uids):
        raise ValueError("uids")
    ensure_schema(con)
    for uid in uids:
        con.execute("INSERT OR IGNORE INTO user_edits(uid) VALUES (?)", (uid,))
        if "tracked" in change:
            con.execute(
                "UPDATE user_edits SET tracked = ? WHERE uid = ?", (int(bool(change["tracked"])), uid)
            )
        for flag in ("hidden",):
            if flag in change:
                con.execute(f"UPDATE user_edits SET {flag} = ? WHERE uid = ?", (int(bool(change[flag])), uid))
        for field in ("status", "note"):
            if field in change:
                text = str(change[field] or "").strip()[:5000] or None
                con.execute(f"UPDATE user_edits SET {field} = ? WHERE uid = ?", (text, uid))
        if "loc" in change:
            lat, lon = map(float, change["loc"]) if change["loc"] else (None, None)
            con.execute("UPDATE user_edits SET lat = ?, lon = ? WHERE uid = ?", (lat, lon, uid))
    con.execute(
        "DELETE FROM user_edits WHERE tracked = 0 AND hidden = 0 AND lat IS NULL AND status IS NULL AND note IS NULL"
    )
    con.commit()


def tracked_report(con: sqlite3.Connection) -> list[dict]:
    """Śledzone oferty — także te, które już zniknęły z portali — w tej samej postaci co oferty
    na mapie (html_map.offer_item), a do tego: "active", status "st", notatka "nt" i historia
    zdarzeń "ev": [[czas, rodzaj, cena, poprzednia cena], …]. Rodzaj: new (pierwszy raz widziana),
    price (zmiana ceny), removed (zniknęła), returned (wróciła)."""
    from map.html_map import OFFER_FIELDS, offer_item  # tutaj, bo html_map importuje ten moduł

    ensure_schema(con)
    tracked = "(SELECT uid FROM user_edits WHERE tracked = 1)"
    events: dict[str, list] = {}
    last_price: dict[str, float] = {}
    query = f"SELECT offer_uid, ts, price FROM price_history WHERE offer_uid IN {tracked} ORDER BY ts"
    for uid, ts, price in con.execute(query):
        kind = "price" if uid in last_price else "new"
        events.setdefault(uid, []).append([ts, kind, price, last_price.get(uid)])
        last_price[uid] = price
    for uid, ts, kind in con.execute(f"SELECT uid, ts, kind FROM offer_events WHERE uid IN {tracked}"):
        events.setdefault(uid, []).append([ts, kind, None, None])

    result = []
    notes = {uid: rest for uid, *rest in con.execute("SELECT uid, status, note FROM user_edits")}
    query = f"SELECT {OFFER_FIELDS}, active, last_seen FROM offers WHERE uid IN {tracked}"
    for *row, active, last_seen in con.execute(query).fetchall():
        item, _ = offer_item(tuple(row))
        uid, source = item["id"], item["s"]
        ev = events.get(uid, [])
        prices = [[e[0][:10], e[2]] for e in ev if e[2] is not None]
        if len(prices) > 1:
            item["h"] = prices
        if not any(e[1] == "new" for e in ev):
            ev.append([item["fs"], "new", None, None])
        if not active and not any(e[1] == "removed" and e[0] > last_seen for e in ev):
            # zniknięcie sprzed wprowadzenia dziennika zdarzeń: wyszło na jaw w kolejnym skanie portalu
            (ts,) = con.execute(
                "SELECT MIN(last_seen) FROM offers WHERE source = ? AND last_seen > ?", (source, last_seen)
            ).fetchone()
            ev.append([ts or last_seen, "removed", None, None])
        status, note = notes[uid]
        result.append(dict(item, active=active, st=status, nt=note, ev=sorted(ev, key=lambda e: e[0] or "")))
    return result


def serve(db_path: str, html_path: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # bez wpisu w terminalu na każde zapytanie
            pass

        def reply(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path, content_type: str) -> None:
            try:
                f = open(path, "rb")
            except OSError:
                return self.reply(404)
            with f:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                shutil.copyfileobj(f, self.wfile)

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/":
                self.send_file(html_path, "text/html; charset=utf-8")
            elif path == "/edits.js":
                with sqlite3.connect(db_path) as con:
                    edits = load_edits(con)
                self.reply(200, f"window.EDITS = {json.dumps(edits)};".encode(), "text/javascript")
            elif path == "/api/tracked":
                with sqlite3.connect(db_path) as con:
                    report = tracked_report(con)
                self.reply(200, json.dumps(report, ensure_ascii=False).encode())
            elif path == "/routes.js":
                self.send_file(gtfs.ROUTES_FILE, "text/javascript; charset=utf-8")
            else:
                self.reply(404)

        def do_POST(self) -> None:
            # wymóg JSON-a sprawia, że obca strona otwarta w przeglądarce nie wyśle tu zapisu
            if self.path != "/api/edit" or self.headers.get("Content-Type") != "application/json":
                return self.reply(404)
            try:
                change = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                with sqlite3.connect(db_path) as con:
                    save_edit(con, change)
            except (ValueError, TypeError, KeyError):
                return self.reply(400)
            self.reply(200, b"{}")

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise RuntimeError(
            f"Nie mogę uruchomić serwera na porcie {port} ({exc}) — podaj inny w --port."
        ) from exc
    url = f"http://127.0.0.1:{port}/"
    print(f"\nMapa działa pod adresem {url} — zmiany zapisują się w „{db_path}”. Ctrl+C kończy.", flush=True)
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

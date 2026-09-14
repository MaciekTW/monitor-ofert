# -*- coding: utf-8 -*-
"""Pomocnicze funkcje do sprawdzania kształtu odpowiedzi portali."""

from __future__ import annotations

import random
import time

import pytest

import monitor_ofert as m

REQUEST_PAUSE = 1.5   # sekundy między zapytaniami — nie obciążamy portali


def fetch(url: str, params: dict | None = None, as_json: bool = True):
    """Surowe zapytanie tą samą sesją i nagłówkami, których używa skrypt.

    Celowo bez ponawiania z http_get — test ma zobaczyć faktyczny status.
    HTTP 403 oznacza blokadę antybotową, a nie zmianę kontraktu, więc test
    jest wtedy pomijany (skip), a nie oblewany."""
    m.warm_up(url)
    headers = m.JSON_HEADERS if as_json else m.HTML_HEADERS
    resp = m.session.get(url, params=params, timeout=30, headers=headers)
    time.sleep(REQUEST_PAUSE * random.uniform(0.9, 1.3))
    if resp.status_code == 403:
        pytest.skip(f"HTTP 403: {url} — portal odrzucił zapytanie jako "
                    "automatyczne, kontraktu nie da się sprawdzić")
    return resp


def dig(node, path: str):
    """Zwraca wartość spod ścieżki "a.b.c"; jeśli jej brak, zgłasza czytelny błąd
    z miejscem, w którym struktura się urywa, i kluczami dostępnymi w tym miejscu."""
    walked = []
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            where = ".".join(walked) or "<korzeń>"
            available = sorted(node)[:40] if isinstance(node, dict) else type(node).__name__
            raise AssertionError(
                f"brak klucza „{key}” w {where} (ścieżka {path}); "
                f"dostępne: {available}")
        node = node[key]
        walked.append(key)
    return node


def assert_all(items: list, check, what: str) -> None:
    """Warunek musi być spełniony dla każdego elementu listy."""
    bad = [i for i, item in enumerate(items) if not check(item)]
    assert not bad, f"{what}: niespełnione dla {len(bad)}/{len(items)} elementów (indeksy {bad[:10]})"


def assert_some(items: list, check, what: str) -> None:
    """Warunek musi być spełniony dla co najmniej jednego elementu listy
    (pola opcjonalne — nie każda oferta je ma, ale gdy nie ma ich żadna,
    portal najpewniej zmienił format)."""
    assert any(check(item) for item in items), \
        f"{what}: niespełnione dla żadnego z {len(items)} elementów"

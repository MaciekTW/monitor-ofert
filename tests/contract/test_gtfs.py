# -*- coding: utf-8 -*-
"""Testy kontraktów GTFS ZTP Kraków — prawdziwe zapytanie do strony z plikami
rozkładów jazdy. Sprawdzają, czy strona odpowiada i czy linkuje do archiwów
GTFS, z których src/gtfs.py bierze przystanki."""

from __future__ import annotations

import re

import pytest

import gtfs
from tests.contract.helpers import fetch


@pytest.fixture(scope="module")
def listed_files() -> set[str]:
    resp = fetch(gtfs.GTFS_URL, as_json=False)
    assert resp.status_code == 200, f"{gtfs.GTFS_URL}: HTTP {resp.status_code}"
    return {href.removeprefix("./") for href in re.findall(r'href=["\']([^"\']+)["\']', resp.text)}


@pytest.mark.parametrize("name", sorted(gtfs.FEEDS))
def test_index_lists_gtfs_archive(listed_files, name):
    assert name in listed_files, f"brak linku do {name}; są: {sorted(listed_files)}"

# -*- coding: utf-8 -*-
"""
Raport w terminalu — wypisuje nowe ogłoszenia, zmiany cen, powroty i zniknięcia
wykryte przy synchronizacji bazy, jako czytelne tabele (biblioteka rich).
"""

from __future__ import annotations

import sqlite3

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text


def fmt_price(value) -> str:
    if value is None:
        return "brak ceny"
    return f"{int(round(value)):,}".replace(",", " ") + " zł"


LIST_CAP = 30  # maks. liczba pozycji wypisywanych w każdej sekcji raportu
PORTAL_STYLE = {"olx": "cyan", "otodom": "magenta"}


def offer_cell(o: dict, console: Console, with_url: bool = True) -> Group:
    """Komórka „Ogłoszenie”: klikalny tytuł, pod nim szczegóły i (opcjonalnie) adres."""
    url = o.get("url") or ""
    lines = [Text(o.get("title") or "(bez tytułu)", style=f"bold link {url}" if url else "bold")]
    details = [str(o[k]) for k in ("rooms", "district") if o.get(k)]
    if o.get("business"):
        details.append("biuro/deweloper")
    if details:
        lines.append(Text(" · ".join(details)))
    if with_url and url:
        # w terminalu adres mieści się w jednej linii (link i tak prowadzi pod pełny adres);
        # poza terminalem (cron, mail, plik) linki nie są klikalne, więc wypisujemy go w całości
        on_terminal = console.is_terminal
        lines.append(
            Text(
                url,
                style=f"dim link {url}",
                no_wrap=on_terminal,
                overflow="ellipsis" if on_terminal else "fold",
            )
        )
    return Group(*lines)


def portal_cell(o: dict, portal_names: dict[str, str]) -> Text:
    source = o.get("source") or ""
    return Text(portal_names.get(source, source), style=PORTAL_STYLE.get(source, ""))


def area_cell(o: dict) -> str:
    return f"{o['area']:g} m²".replace(".", ",") if o.get("area") else "—"


def price_cell(value) -> Text:
    return Text(fmt_price(value), style="bold" if value is not None else "dim")


def ppm_cell(o: dict) -> str:
    return fmt_price(o["price_per_m"]).removesuffix(" zł") if o.get("price_per_m") else "—"


def change_cell(old_price, new_price) -> Text:
    if not old_price or not new_price:
        return Text("—", style="dim")
    diff = (new_price - old_price) / old_price * 100
    delta = fmt_price(abs(new_price - old_price))
    # z perspektywy kupującego obniżka to dobra wiadomość
    arrow, sign, style = ("▼", "-", "bold green") if diff < 0 else ("▲", "+", "bold red")
    return Text(f"{arrow} {diff:+.1f}%\n{sign}{delta}".replace(".", ","), style=style)


def section_table(title: str, count: int, style: str) -> Table:
    table = Table(
        title=f"{title} [dim]({count})[/]",
        title_style=f"bold {style}",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        header_style="bold dim",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Portal", no_wrap=True)
    table.add_column("Ogłoszenie", ratio=1, overflow="fold")
    return table


def print_section(console: Console, table: Table, items: list, add_row) -> None:
    for item in items[:LIST_CAP]:
        add_row(item)
    if len(items) > LIST_CAP:
        table.caption = f"… i {len(items) - LIST_CAP} kolejnych (pełna lista jest w bazie)"
        table.caption_justify = "left"
    console.print()
    console.print(table)


def report(result: dict, db_path: str, con: sqlite3.Connection, portal_names: dict[str, str]) -> None:
    """Wypisuje w terminalu wynik synchronizacji (sync) jako tabele sekcji i podsumowanie.

    portal_names: nazwy portali do wyświetlenia, np. {"olx": "OLX", "otodom": "Otodom"}."""
    console = Console(highlight=False)
    if result["first_run"]:
        console.print(
            f"\n[bold green]✔[/] Pierwsze uruchomienie: zapisano [bold]{len(result['new'])}[/] "
            f"ofert do bazy „{db_path}”."
        )
        console.print("  [dim]Przy kolejnych uruchomieniach zobaczysz już tylko nowe oferty i zmiany.[/]")
        return

    def offers_section(title: str, items: list, style: str) -> None:
        table = section_table(title, len(items), style)
        table.add_column("Cena", justify="right", no_wrap=True)
        table.add_column("Metraż", justify="right", no_wrap=True)
        table.add_column("zł/m²", justify="right", no_wrap=True)

        def add_row(o: dict) -> None:
            table.add_row(
                portal_cell(o, portal_names),
                offer_cell(o, console),
                price_cell(o.get("price")),
                area_cell(o),
                ppm_cell(o),
            )
            table.add_section()

        print_section(console, table, items, add_row)

    def changes_section(items: list) -> None:
        table = section_table("Zmiany cen", len(items), "yellow")
        table.add_column("Cena", justify="right", no_wrap=True)
        table.add_column("Zmiana", justify="right", no_wrap=True)

        def add_row(item) -> None:
            o, old_price = item
            # nowa cena, a pod nią przekreślona poprzednia
            price = price_cell(o.get("price"))
            price.append("\n" + fmt_price(old_price), style="dim strike")
            table.add_row(
                portal_cell(o, portal_names),
                offer_cell(o, console),
                price,
                change_cell(old_price, o.get("price")),
            )
            table.add_section()

        # największe obniżki na górze
        ordered = sorted(items, key=lambda it: (it[0].get("price") or 0) - (it[1] or 0))
        print_section(console, table, ordered, add_row)

    def removed_section(items: list) -> None:
        table = section_table("Zniknęły (sprzedane / wycofane)", len(items), "red")
        table.add_column("Cena", justify="right", no_wrap=True)
        table.add_column("Metraż", justify="right", no_wrap=True)

        def add_row(o: dict) -> None:
            # link do wycofanego ogłoszenia zwykle już nie działa — sam tytuł wystarczy
            table.add_row(
                portal_cell(o, portal_names),
                offer_cell(o, console, with_url=False),
                fmt_price(o.get("price")),
                area_cell(o),
            )

        print_section(console, table, items, add_row)

    if result["new"]:
        offers_section("Nowe ogłoszenia", result["new"], "green")
    if result["price_changes"]:
        changes_section(result["price_changes"])
    if result["returned"]:
        offers_section("Wróciły do sprzedaży", result["returned"], "blue")
    if result["removed"]:
        removed_section(result["removed"])

    counts = [
        ("nowe", len(result["new"]), "green"),
        ("zmiany cen", len(result["price_changes"]), "yellow"),
        ("wróciły", len(result["returned"]), "blue"),
        ("zniknęły", len(result["removed"]), "red"),
    ]
    active, total = con.execute("SELECT SUM(active), COUNT(*) FROM offers").fetchone()
    console.print()
    if any(n for _, n, _ in counts):
        summary = "   ".join(f"[{style}]{label}: [bold]{n}[/][/]" for label, n, style in counts if n)
        console.print(f"[bold]Podsumowanie[/]   {summary}")
    else:
        console.print("[bold]Brak zmian od ostatniego uruchomienia.[/]")
    console.print(
        f"[dim]W bazie: [/][bold]{active or 0}[/][dim] aktywnych ofert ({total} łącznie) — plik „{db_path}”.[/]"
    )

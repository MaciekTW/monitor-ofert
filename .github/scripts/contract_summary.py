# -*- coding: utf-8 -*-
"""Podsumowanie testów kontraktów dla GitHub Actions.

Czyta raport JUnit z pytest i dopisuje tabelę wyników do $GITHUB_STEP_SUMMARY
(lokalnie: wypisuje na stdout). Kończy się błędem, gdy żaden test nie został
faktycznie sprawdzony — np. wszystkie pominięte przez blokadę portali (HTTP 403)
— żeby taki przebieg nie wyglądał na zielony."""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


def main(report_path: str) -> int:
    try:
        root = ET.parse(report_path).getroot()
    except (OSError, ET.ParseError) as exc:
        write(f"## Testy kontraktów\n\n❌ Brak raportu z pytest ({exc}).\n")
        return 1

    per_file: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    failed, skipped = [], []
    for case in root.iter("testcase"):
        module = case.get("classname", "?").rsplit(".", 1)[-1]
        name = f"{module}::{case.get('name')}"
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            per_file[module]["failed"] += 1
            failed.append((name, problem.get("message") or ""))
        elif case.find("skipped") is not None:
            per_file[module]["skipped"] += 1
            skipped.append((name, case.find("skipped").get("message") or ""))
        else:
            per_file[module]["passed"] += 1

    passed = sum(c["passed"] for c in per_file.values())
    lines = ["## Testy kontraktów", ""]
    if failed:
        lines.append(f"❌ **{len(failed)} niezgodności z kontraktem**")
    elif not passed:
        lines.append("⚠️ **Nic nie zostało sprawdzone** — wszystkie testy pominięte "
                     "(najpewniej portale zablokowały zapytania z runnera).")
    elif skipped:
        lines.append(f"⚠️ Kontrakty zgodne, ale {len(skipped)} testów pominięto.")
    else:
        lines.append("✅ Wszystkie kontrakty zgodne.")

    lines += ["", "| Plik | ✅ | ❌ | ⏭️ |", "| --- | --: | --: | --: |"]
    for module, counts in sorted(per_file.items()):
        lines.append(f"| `{module}` | {counts['passed']} | {counts['failed']} "
                     f"| {counts['skipped']} |")

    for title, items in (("Niezgodności", failed), ("Pominięte", skipped)):
        if items:
            lines += ["", f"### {title}", ""]
            lines += [f"- `{name}` — {one_line(message)}" for name, message in items]

    write("\n".join(lines) + "\n")
    return 1 if not passed and not failed else 0


def one_line(text: str, limit: int = 300) -> str:
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else text[:limit] + "…"


def write(markdown: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(markdown)
    print(markdown)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "report.xml"))

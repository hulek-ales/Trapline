"""Vypíše runtime závislosti z pyproject.toml, jednu na řádek.

Jediný zdroj pravdy o závislostech je ``[project].dependencies``; tenhle
skript z něj udělá vstup pro ``pip install -r``, aby image nemusel držet
druhý, ručně synchronizovaný requirements.txt.
"""

from __future__ import annotations

import sys
import tomllib


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "pyproject.toml"
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    for dep in data.get("project", {}).get("dependencies", []):
        print(dep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

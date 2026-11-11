"""``python -m scripts.sim.fixtures``: answer pending Apify runs of the portals with the repository's live fixtures.

``tests/fixtures/portals_live`` holds rows of real portal runs (Madrid, 2026-10-16, phones replaced). A pending run
whose actor and input match one of those runs (same portal, Madrid, same deal and property kind) gets that file as
its record in ``sim/apify/<actor>/<hash>.json``; every other pending run is left for a recorded run (MCP, within the
budget). The rows the actor's own input filters would not return (``FILTERS``: ``minPrice``, ``maxPrice``,
``minSize``/``minArea``, as ``bot/web_search/sources/portals.py`` sends them) are dropped first: the bench gets no
listing the production run would not get. A row without the value (a plot whose area is only in the text) is kept:
what the portal does with it is unknown. Prints what it answered and what is left. ``--dry-run``: print only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "tests" / "fixtures" / "portals_live"


def _madrid(value: Any) -> bool:
    return str(value or "").strip().lower() in ("madrid", "madrid_capital")


def _milanuncios(data: dict[str, Any]) -> str | None:
    slugs = {"solares": "madrid_solares", "venta-de-pisos": "madrid_pisos", "venta-de-casas": "madrid_casas",
             "alquiler-de-pisos": "madrid_alquiler_pisos", "alquiler-de-casas": "madrid_alquiler_casas",
             "locales-comerciales": "madrid_locales"}
    name = slugs.get(str(data.get("categorySlug")))
    return f"milanuncios_items_{name}.json" if name and _madrid(data.get("province")) else None


def _yaencontre(data: dict[str, Any]) -> str | None:
    kind, deal = str(data.get("propertyType")), str(data.get("operation")).lower()
    files = {("land", "buy"): "yaencontre_items.json", ("home", "buy"): "yaencontre_flat_items.json",
             ("home", "rent"): "yaencontre_rent_flat_items.json"}
    return files.get((kind, deal))


# actor (URL form) -> the fixture that fits an input, or None
RULES: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "igolaizola~idealista-scraper": lambda d: "idealista_items.json"
    if d.get("propertyType") == "lands" and d.get("operation") == "sale" and _madrid(d.get("location")) else None,
    "trev0n~habitaclia-com-spain-scraper": lambda d: "habitaclia_items.json"
    if d.get("propertyType") == "land" and d.get("transactionType") == "sale" and _madrid(d.get("location")) else None,
    "logiover~pisos-com-property-scraper": lambda d: "pisos_items.json"
    if d.get("propertyType") == "land" and d.get("transactionType") == "sale" and _madrid(d.get("location")) else None,
    "rastriq~milanuncios-scraper": _milanuncios,
    "igolaizola~yaencontre-scraper": _yaencontre,
}


# actor -> the input filter -> (the row's field, "min" | "max"); Milanuncios has no filters in its input.
FILTERS: dict[str, dict[str, tuple[str, str]]] = {
    "igolaizola~idealista-scraper": {"minPrice": ("price", "min"), "maxPrice": ("price", "max"),
                                     "minSize": ("size", "min")},
    "trev0n~habitaclia-com-spain-scraper": {"minPrice": ("price", "min"), "maxPrice": ("price", "max"),
                                            "minArea": ("areaSqm", "min")},
    "logiover~pisos-com-property-scraper": {"minPrice": ("price", "min"), "maxPrice": ("price", "max"),
                                            "minArea": ("areaSqm", "min")},
    "igolaizola~yaencontre-scraper": {"minPrice": ("price", "min"), "maxPrice": ("price", "max"),
                                      "minArea": ("area", "min")},
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def passes(actor: str, actor_input: dict[str, Any], row: Any) -> bool:
    """False when the row fails one of the input's filters the actor applies; a row without the value passes."""
    if not isinstance(row, dict):
        return True
    for key, (name, side) in FILTERS.get(actor, {}).items():
        limit, value = _number(actor_input.get(key)), _number(row.get(name))
        if limit is None or value is None:
            continue
        if (side == "min" and value < limit) or (side == "max" and value > limit):
            return False
    return True


def answer(sim_root: Path, *, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """(answered, left) pending Apify files."""
    answered, left = [], []
    for path in sorted((sim_root / "pending_apify").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        actor = str(data.get("actor", "")).replace("/", "~")
        rule = RULES.get(actor)
        name = rule(data.get("input") or {}) if rule else None
        if name is None or not (LIVE / name).is_file():
            left.append(path.name)
            continue
        target = sim_root / "apify" / actor / f"{data['key']}.json"
        text = (LIVE / name).read_text(encoding="utf-8")
        rows = json.loads(text)
        rows = rows if isinstance(rows, list) else [rows]
        kept = [row for row in rows if passes(actor, data.get("input") or {}, row)]
        if len(kept) < len(rows):  # the rows the actor would have returned for this input
            text = json.dumps(kept, ensure_ascii=False, indent=2) + "\n"
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            path.unlink()
        answered.append(f"{path.name} <- tests/fixtures/portals_live/{name}"
                        + (f" ({len(kept)} of {len(rows)} rows pass the input's filters)" if len(kept) < len(rows)
                           else ""))
    return answered, left


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.sim.fixtures", description=__doc__.splitlines()[0])
    parser.add_argument("--sim", default=str(ROOT / "sim"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    answered, left = answer(Path(args.sim).resolve(), dry_run=args.dry_run)
    for line in answered:
        print("answered", line)
    for name in left:
        print("left    ", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

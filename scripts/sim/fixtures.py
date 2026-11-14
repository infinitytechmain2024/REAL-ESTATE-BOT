"""``python -m scripts.sim.fixtures``: answer pending Apify runs of the portals with the repository's live fixtures.

``tests/fixtures/portals_live`` holds rows of real portal runs (Madrid, 2026-10-16, phones replaced). A fixture
answers a pending run only when the run's input is the search the fixture was recorded with (``RULES``: the portal,
the place and its level, the deal and the property kind, no price or area filter, the default order): a run with a
price or area filter, another place level or another order asks the portal another question, and the newest rows of
an unfiltered search are not its answer. Such a run, and every other one, is left for a recorded run (MCP, within
the budget). The rows the actor's own input filters would not return (``FILTERS``: ``minPrice``, ``maxPrice``,
``minSize``/``minArea``, as ``bot/web_search/sources/portals.py`` sends them) are dropped before the record is
written; a row without the value (a plot whose area is only in the text) is kept: what the portal does with it is
unknown. When no row is left of a non-empty fixture, no record is written and the run is left (a recorded run gives
the real answer, an empty record would hide it). Prints what it answered and what is left. ``--dry-run``: print only.
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


def _unfiltered(actor: str, data: dict[str, Any]) -> bool:
    """No price or area filter in the input (``FILTERS`` of the actor): the fixtures were recorded without one."""
    return not any(data.get(key) not in (None, "", 0) for key in FILTERS.get(actor, {}))


def _sorted(data: dict[str, Any], key: str, *default: str) -> bool:
    """The input's order is the default one the bot sends (or none): another order returns other rows."""
    return data.get(key) in (None, *default)


def _idealista(data: dict[str, Any]) -> str | None:
    """Recorded 2026-10-16: plots for sale, ``location`` «Madrid» by name only (no ``latitude``/``longitude``/
    ``distanceKm``: no radius, no city circle of Q17, no district of Q26), no ``minPrice``/``maxPrice``/``minSize``,
    newest first (not ``biggest``). A province task sends the same name: the actor gets the same search."""
    if (data.get("propertyType") == "lands" and data.get("operation") == "sale" and _madrid(data.get("location"))
            and not any(key in data for key in ("latitude", "longitude", "distanceKm"))
            and _unfiltered("igolaizola~idealista-scraper", data) and _sorted(data, "sortBy", "mostRecent", "newest")):
        return "idealista_items.json"
    return None


def _habitaclia(data: dict[str, Any]) -> str | None:
    """Recorded 2026-10-16: plots for sale, ``location`` «Madrid» (free text; a province task sends the same text),
    no ``minPrice``/``maxPrice``/``minArea``, ``sort`` newest."""
    if (data.get("propertyType") == "land" and data.get("transactionType") == "sale" and _madrid(data.get("location"))
            and _unfiltered("trev0n~habitaclia-com-spain-scraper", data) and _sorted(data, "sort", "newest")):
        return "habitaclia_items.json"
    return None


def _pisos(data: dict[str, Any]) -> str | None:
    """Recorded 2026-10-16: plots for sale in the city (``location`` ``madrid_capital``; the rows say «Madrid
    Capital»), no ``minPrice``/``maxPrice``/``minArea``, ``sortBy`` newest. The province (``madrid``) is another
    search."""
    if (data.get("propertyType") == "land" and data.get("transactionType") == "sale"
            and str(data.get("location") or "").strip().lower() == "madrid_capital"
            and _unfiltered("logiover~pisos-com-property-scraper", data) and _sorted(data, "sortBy", "newest")):
        return "pisos_items.json"
    return None


def _milanuncios(data: dict[str, Any]) -> str | None:
    """Recorded 2026-10-16: one run per category, ``province`` ``madrid``, no filter and no order in the input (the
    actor has none). A city task of Madrid sends the same input as the province: the same search."""
    slugs = {"solares": "madrid_solares", "venta-de-pisos": "madrid_pisos", "venta-de-casas": "madrid_casas",
             "alquiler-de-pisos": "madrid_alquiler_pisos", "alquiler-de-casas": "madrid_alquiler_casas",
             "locales-comerciales": "madrid_locales"}
    name = slugs.get(str(data.get("categorySlug")))
    return f"milanuncios_items_{name}.json" if name and _madrid(data.get("province")) else None


MADRID = (40.4168, -3.7038)  # the centre the bot sends for Madrid (tests/test_apify_portals.py)


def _yaencontre(data: dict[str, Any]) -> str | None:
    """Recorded 2026-10-16 by coordinates around the centre of Madrid with a city radius (flats: 10 km; the plots,
    houses and rents: a city circle, the rows are Madrid, Getafe, Pozuelo), no ``minPrice``/``maxPrice``/``minArea``,
    ``sortBy`` newest. Another centre, a province circle (60 km) or a filter is another search."""
    kind, deal = str(data.get("propertyType")), str(data.get("operation")).lower()
    # the bot's input (``ApifyYaencontreSource``): ``propertyType`` land/flat, ``operation`` sale/rent
    files = {("land", "sale"): "yaencontre_items.json", ("flat", "sale"): "yaencontre_flat_items.json",
             ("flat", "rent"): "yaencontre_rent_flat_items.json"}
    lat, lon, radius = (_number(data.get(key)) for key in ("latitude", "longitude", "radiusKm"))
    near = (lat is not None and lon is not None and abs(lat - MADRID[0]) <= .05 and abs(lon - MADRID[1]) <= .05)
    if not (near and radius is not None and radius <= 15 and _unfiltered("igolaizola~yaencontre-scraper", data)
            and _sorted(data, "sortBy", "newest")):
        return None
    return files.get((kind, deal))


# actor (URL form) -> the fixture recorded with that input, or None (left for a recorded run)
RULES: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "igolaizola~idealista-scraper": _idealista,
    "trev0n~habitaclia-com-spain-scraper": _habitaclia,
    "logiover~pisos-com-property-scraper": _pisos,
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
    """(answered, left) pending Apify files; a left file whose fixture lost every row to the filters carries the
    reason after its name."""
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
        if rows and not kept:  # an empty record would stand for the portal's answer: a recorded run gives it
            left.append(f"{path.name} (0 of {len(rows)} rows of tests/fixtures/portals_live/{name} pass the input's "
                        "filters: left for a recorded run)")
            continue
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

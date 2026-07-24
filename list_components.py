#!/usr/bin/env python3
"""Parse an Altium .SchLib file, list its components, and self-check the parse.

This is the acceptance program for the ``altium_schlib`` parser. It:

  1. Opens a .SchLib (given on the command line, or auto-discovered in ./input).
  2. Prints library metadata and the components it finds.
  3. Runs a name-agnostic self-check: every declared component resolves to a
     distinct OLE storage, and every Data stream round-trips byte-for-byte
     through the parser.

Use --match to search for components and --show to inspect one.
Exit code is 0 when the self-check passes, 1 otherwise.

Usage::

    python list_components.py                    # auto-find in ./input
    python list_components.py path/to/lib.SchLib
    python list_components.py --limit 40         # cap the component listing
    python list_components.py --match CONN       # search names
    python list_components.py --show SOME_PART   # inspect one component
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run directly from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import SchLib, parse_records, serialize_records  # noqa: E402
from cli_common import (  # noqa: E402  (find_default_schlib re-exported)
    add_selector_arguments,
    build_predicate,
    find_default_schlib,
    select_components,
)


def print_library_info(lib: SchLib) -> None:
    print("=" * 72)
    print("Altium Schematic Library")
    print("=" * 72)
    print(f"File          : {lib.path}")
    print(f"Header        : {lib.header.header_string}")
    print(f"Declared comps: {len(lib)}")
    print(f"OLE storages  : {len(lib.storage_names)}")
    print()


def list_components(lib: SchLib, limit: int | None) -> None:
    names = lib.component_names
    # limit is None -> show all; otherwise clamp into [0, len] so a negative or
    # oversized value can't produce a nonsensical count.
    capped = None if limit is None else max(0, min(limit, len(names)))
    shown = names if capped is None else names[:capped]
    truncated = capped is not None and capped < len(names)
    print(f"Components ({len(names)} total"
          + (f", showing first {len(shown)}" if truncated else "") + "):")
    print("-" * 72)
    width = len(str(len(names)))
    for i, name in enumerate(shown, 1):
        try:
            comp = lib.get_component(name)
            detail = f"{comp.pin_count} pins"
            desc = comp.description
            if desc:
                detail += f"  |  {desc[:40]}"
        except Exception as exc:  # pragma: no cover - defensive
            detail = f"<error: {exc}>"
        print(f"  {i:>{width}}. {name:<44} {detail}")
    if truncated:
        print(f"  ... and {len(names) - len(shown)} more (use --limit 0 to show all)")
    print()


def verify_library(lib: SchLib) -> bool:
    """Name-agnostic self-check: unique name->storage resolution + round-trip."""
    print("=" * 72)
    print("Self-check: name resolution + Data-stream round-trip")
    print("=" * 72)

    names = lib.component_names
    unresolved = []
    used: dict = {}
    for name in names:
        storage = lib.storage_name_for(name)
        if storage is None:
            unresolved.append(name)
        else:
            used.setdefault(storage, []).append(name)
    collisions = {k: v for k, v in used.items() if len(v) > 1}

    roundtrip_fail = []
    for storage in lib.storage_names:
        raw = lib._read_stream([storage, "Data"])
        if serialize_records(parse_records(raw)) != raw:
            roundtrip_fail.append(storage)

    n_store = len(lib.storage_names)
    print(f"  Declared components    : {len(names)}")
    print(f"  Resolved to a storage  : {len(names) - len(unresolved)} / {len(names)}")
    print(f"  Distinct storages used : {len(used)}")
    print(f"  Data round-trips exact : {n_store - len(roundtrip_fail)} / {n_store}")

    if unresolved:
        print(f"  ! unresolved names    : {unresolved[:5]}"
              + (" ..." if len(unresolved) > 5 else ""))
    if collisions:
        print(f"  ! storage collisions  : {list(collisions)[:5]}")
    if roundtrip_fail:
        print(f"  ! round-trip failures : {roundtrip_fail[:5]}")

    passed = not unresolved and not collisions and not roundtrip_fail
    print()
    print("RESULT: " + ("PASS - all components resolve uniquely and round-trip."
                        if passed else "FAIL - see issues above."))
    print()
    return passed


def match_components(lib: SchLib, pattern: str) -> list[str]:
    """Component names containing ``pattern`` (case-insensitive substring).

    Matches over declared LibReference names and, for anything not already
    covered, the raw OLE storage names -- so a query still finds a component
    whose storage name was truncated/sanitized. Order follows the FileHeader.
    """
    p = pattern.lower()
    pool = list(dict.fromkeys(lib.component_names + lib.storage_names))
    return [n for n in pool if p in n.lower()]


def _suggest(lib: SchLib, query: str, limit: int = 8) -> list[str]:
    """Case-insensitive substring matches (capped), for 'did you mean' hints."""
    return match_components(lib, query)[:limit]


def print_matches(lib: SchLib, names: list[str], label: str) -> bool:
    """List the given components under a heading. Returns True if any."""
    print("=" * 72)
    print(f"Components matching {label}  ({len(names)} found)")
    print("=" * 72)
    if not names:
        print("  (no matches — try a different query)")
        print()
        return False
    width = len(str(len(names)))
    for i, name in enumerate(names, 1):
        try:
            comp = lib.get_component(name)
            detail = f"{comp.pin_count} pins"
            if comp.description:
                detail += f"  |  {comp.description[:40]}"
        except Exception as exc:  # pragma: no cover - defensive
            detail = f"<error: {exc}>"
        print(f"  {i:>{width}}. {name:<44} {detail}")
    print()
    print(f"  Use --show NAME for full details, e.g.  --show {names[0]}")
    print()
    return True


def list_matches(lib: SchLib, pattern: str) -> bool:
    """List components whose name matches ``pattern``. Returns True if any."""
    return print_matches(lib, match_components(lib, pattern), repr(pattern))


def show_component(lib: SchLib, name: str) -> bool:
    """Print one component's header fields, parameters, and record breakdown.

    Returns True if the component was found. Accepts a LibReference or the raw
    OLE storage name.
    """
    if not lib.has_component(name):
        print(f"Component not found: {name!r}")
        hints = _suggest(lib, name)
        if hints:
            print("Did you mean:")
            for h in hints:
                print(f"    {h}")
        else:
            print("(no similar names; use --limit 0 to see the full list)")
        return False

    comp = lib.get_component(name)
    print("=" * 72)
    print(f"Component: {comp.name}")
    print("=" * 72)
    print(f"  Storage name : {comp.storage_name}")
    print(f"  DesignItemId : {comp.design_item_id}")
    print(f"  Description  : {comp.description}")
    print(f"  Parts        : {comp.part_count}")
    print(f"  Pins         : {comp.pin_count}")
    print()

    header = comp.header
    if header is not None:
        print("Header fields (RECORD=1):")
        for key, value in header.fields:
            print(f"    {key} = {value}")
        print()

    params = comp.parameters
    print(f"Parameters (RECORD=41) [{len(params)}]:")
    if params:
        for p in params:
            pname = p.get("Name", "")
            text = p.get("Text", "")
            print(f"    {pname:<32} = {text}")
    else:
        print("    (none)")
    print()

    # A compact breakdown of every record type in the component.
    counts: dict = {}
    for r in comp.records:
        key = f"RECORD={r.record_id}" if r.is_text else "pin (binary)"
        counts[key] = counts.get(key, 0) + 1
    print("Record breakdown:")
    for key in sorted(counts):
        print(f"    {key:<16} x {counts[key]}")
    print()
    return True


def component_to_dict(comp) -> dict:
    """A JSON-serializable view of a component: identity, header, parameters."""
    counts: dict = {}
    for r in comp.records:
        key = f"RECORD={r.record_id}" if r.is_text else "pin"
        counts[key] = counts.get(key, 0) + 1
    header = comp.header
    return {
        "name": comp.name,
        "storage_name": comp.storage_name,
        "design_item_id": comp.design_item_id,
        "description": comp.description,
        "part_count": comp.part_count,
        "pin_count": comp.pin_count,
        "header": dict(header.fields) if header is not None else {},
        "parameters": [
            {"name": p.get("Name", ""), "text": p.get("Text", "")}
            for p in comp.parameters
        ],
        "record_breakdown": counts,
    }


def library_to_dict(lib: SchLib) -> dict:
    """A JSON-serializable summary of the whole library and its components."""
    components = []
    for name in lib.component_names:
        try:
            comp = lib.get_component(name)
        except Exception:  # pragma: no cover - defensive
            components.append({"name": name, "resolved": False})
            continue
        components.append({
            "name": comp.name,
            "storage_name": comp.storage_name,
            "description": comp.description,
            "part_count": comp.part_count,
            "pin_count": comp.pin_count,
        })
    return {
        "file": lib.path,
        "header": lib.header.header_string,
        "declared_count": lib.declared_count,
        "storage_count": len(lib.storage_names),
        "components": components,
    }


def component_summaries(lib: SchLib, names: list[str]) -> list[dict]:
    """One-line JSON-serializable summaries for the given components."""
    matches = []
    for name in names:
        try:
            comp = lib.get_component(name)
            matches.append({
                "name": comp.name,
                "storage_name": comp.storage_name,
                "description": comp.description,
                "part_count": comp.part_count,
                "pin_count": comp.pin_count,
            })
        except Exception:  # pragma: no cover - defensive
            matches.append({"name": name, "resolved": False})
    return matches


def matches_to_dict(lib: SchLib, pattern: str) -> dict:
    """JSON-serializable summaries of components matching ``pattern``."""
    matches = component_summaries(lib, match_components(lib, pattern))
    return {"pattern": pattern, "count": len(matches), "matches": matches}


def _emit_json(obj) -> None:
    print(json.dumps(obj, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", help="Path to a .SchLib file "
                        "(default: the single file in ./input).")
    parser.add_argument("--limit", type=int, default=25,
                        help="Max components to list (0 = all). Default 25.")
    parser.add_argument("--no-list", action="store_true",
                        help="Skip the component listing, only run verification.")
    parser.add_argument("-m", "--match", metavar="PATTERN",
                        help="List component names containing PATTERN "
                             "(case-insensitive) and exit, so you can pick one "
                             "to pass to --show.")
    parser.add_argument("-s", "--show", metavar="NAME",
                        help="Show one component's fields/parameters by name "
                             "(LibReference or storage name) and exit.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON on stdout (a single "
                             "component with --show, else the whole library) "
                             "and skip the human-readable banner/verification.")
    add_selector_arguments(parser)
    args = parser.parse_args(argv)

    # The full query-selector set (same flags as batch_set_parameter.py) acts
    # as a search: list only the components the query matches.
    selector_pred = build_predicate(args)
    if selector_pred is not None and args.match is not None:
        raise SystemExit("Use either --match or the selector flags, not both.")

    path = args.path or find_default_schlib()
    if not os.path.isfile(path):
        raise SystemExit(f"File not found: {path}")

    with SchLib(path) as lib:
        # JSON mode: clean machine-readable output, no banner/verification.
        # --show (one component) takes precedence over --match (a filtered list).
        if args.json:
            if args.show is not None:
                if not lib.has_component(args.show):
                    _emit_json({"found": False, "query": args.show,
                                "suggestions": _suggest(lib, args.show)})
                    return 1
                _emit_json(component_to_dict(lib.get_component(args.show)))
                return 0
            if selector_pred is not None:
                names = select_components(lib, selector_pred)
                _emit_json({"query": "selectors", "count": len(names),
                            "matches": component_summaries(lib, names)})
                return 0 if names else 1
            if args.match is not None:
                result = matches_to_dict(lib, args.match)
                _emit_json(result)
                return 0 if result["count"] else 1
            _emit_json(library_to_dict(lib))
            return 0

        # Query mode: show a single component and skip listing/verification.
        if args.show is not None:
            print_library_info(lib)
            found = show_component(lib, args.show)
            return 0 if found else 1

        # Search modes: list matching names so the user can then --show one.
        if selector_pred is not None:
            print_library_info(lib)
            names = select_components(lib, selector_pred)
            return 0 if print_matches(lib, names, "the query selectors") else 1

        if args.match is not None:
            print_library_info(lib)
            return 0 if list_matches(lib, args.match) else 1

        print_library_info(lib)
        if not args.no_list:
            list_components(lib, None if args.limit == 0 else args.limit)
        passed = verify_library(lib)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

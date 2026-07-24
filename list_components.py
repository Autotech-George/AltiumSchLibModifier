#!/usr/bin/env python3
"""Parse an Altium .SchLib file, list its components, and verify a known set.

This is the acceptance test for the ``altium_schlib`` parser. It:

  1. Opens a .SchLib (given on the command line, or auto-discovered in ./input).
  2. Prints library metadata and every component it finds.
  3. Verifies the expected connector family CON_KLEMA_2 .. CON_KLEMA_12
     (11 components) is present -- and that the unrelated CON_KLEMA_20 is
     *not* mistaken for one of them.

Exit code is 0 when the verification passes, 1 otherwise.

Usage::

    python list_components.py                    # auto-find in ./input
    python list_components.py path/to/lib.SchLib
    python list_components.py --limit 40         # cap the component listing
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# Make the package importable when run directly from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import SchLib  # noqa: E402

# The connector family the user asked us to confirm: CON_KLEMA_2 .. CON_KLEMA_12.
EXPECTED_KLEMA = [f"CON_KLEMA_{n}" for n in range(2, 13)]  # 11 names


def find_default_schlib() -> str:
    """Locate a single .SchLib under ./input, or raise a helpful error."""
    here = os.path.dirname(os.path.abspath(__file__))
    matches = sorted(glob.glob(os.path.join(here, "input", "*.SchLib")))
    if not matches:
        raise SystemExit(
            "No .SchLib found in ./input. Pass the file path explicitly:\n"
            "    python list_components.py path/to/library.SchLib"
        )
    if len(matches) > 1:
        names = "\n  ".join(os.path.basename(m) for m in matches)
        raise SystemExit(
            f"Multiple .SchLib files in ./input; specify one:\n  {names}"
        )
    return matches[0]


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


def verify_klema(lib: SchLib) -> bool:
    print("=" * 72)
    print("Verification: CON_KLEMA_2 .. CON_KLEMA_12  (expect 11 components)")
    print("=" * 72)
    found = []
    for name in EXPECTED_KLEMA:
        present = lib.has_component(name)
        if present:
            comp = lib.get_component(name)
            # Confirm the resolved component's real name matches exactly,
            # so CON_KLEMA_2 is never satisfied by e.g. CON_KLEMA_20.
            ok = comp.name == name
            found.append(name if ok else None)
            mark = "OK " if ok else "!! "
            extra = "" if ok else f"  (resolved to {comp.name!r}!)"
            print(f"  [{mark}] {name:<16} {comp.pin_count} pins{extra}")
        else:
            found.append(None)
            print(f"  [MISS] {name:<16} not found")

    found_count = sum(1 for f in found if f)
    print("-" * 72)
    print(f"  Found {found_count} of {len(EXPECTED_KLEMA)} expected components.")

    # Sanity check: CON_KLEMA_20 exists in this library but must stay distinct
    # from the 2..12 range (guards against a prefix-matching regression).
    if lib.has_component("CON_KLEMA_20"):
        distinct = lib.get_component("CON_KLEMA_20").name == "CON_KLEMA_20"
        print(f"  Note: CON_KLEMA_20 also exists and resolves "
              f"{'distinctly' if distinct else 'INCORRECTLY'} "
              "(not counted in the range).")

    passed = found_count == len(EXPECTED_KLEMA)
    print()
    print("RESULT: " + ("PASS - all 11 CON_KLEMA components found."
                        if passed else "FAIL - missing components (see above)."))
    print()
    return passed


def _suggest(lib: SchLib, query: str, limit: int = 8) -> list[str]:
    """Case-insensitive substring matches over declared + storage names."""
    q = query.lower()
    pool = list(dict.fromkeys(lib.component_names + lib.storage_names))
    return [n for n in pool if q in n.lower()][:limit]


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
    parser.add_argument("-s", "--show", metavar="NAME",
                        help="Show one component's fields/parameters by name "
                             "(LibReference or storage name) and exit.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON on stdout (a single "
                             "component with --show, else the whole library) "
                             "and skip the human-readable banner/verification.")
    args = parser.parse_args(argv)

    path = args.path or find_default_schlib()
    if not os.path.isfile(path):
        raise SystemExit(f"File not found: {path}")

    with SchLib(path) as lib:
        # JSON mode: clean machine-readable output, no banner/verification.
        if args.json:
            if args.show is not None:
                if not lib.has_component(args.show):
                    _emit_json({"found": False, "query": args.show,
                                "suggestions": _suggest(lib, args.show)})
                    return 1
                _emit_json(component_to_dict(lib.get_component(args.show)))
            else:
                _emit_json(library_to_dict(lib))
            return 0

        # Query mode: show a single component and skip listing/verification.
        if args.show is not None:
            print_library_info(lib)
            found = show_component(lib, args.show)
            return 0 if found else 1

        print_library_info(lib)
        if not args.no_list:
            list_components(lib, None if args.limit == 0 else args.limit)
        passed = verify_klema(lib)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

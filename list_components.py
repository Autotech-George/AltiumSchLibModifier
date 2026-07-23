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
    shown = names if limit is None else names[:limit]
    print(f"Components ({len(names)} total"
          + (f", showing first {len(shown)}" if limit else "") + "):")
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
    if limit and len(names) > limit:
        print(f"  ... and {len(names) - limit} more (use --limit 0 to show all)")
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

    # Sanity check: CON_KLEMA_20 exists in this library but must be distinct.
    if lib.has_component("CON_KLEMA_20"):
        in_range = "CON_KLEMA_20" in EXPECTED_KLEMA
        print(f"  Note: CON_KLEMA_20 also exists and is correctly "
              f"{'INCLUDED' if in_range else 'excluded'} from the range.")

    passed = found_count == len(EXPECTED_KLEMA)
    print()
    print("RESULT: " + ("PASS - all 11 CON_KLEMA components found."
                        if passed else "FAIL - missing components (see above)."))
    print()
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", help="Path to a .SchLib file "
                        "(default: the single file in ./input).")
    parser.add_argument("--limit", type=int, default=25,
                        help="Max components to list (0 = all). Default 25.")
    parser.add_argument("--no-list", action="store_true",
                        help="Skip the component listing, only run verification.")
    args = parser.parse_args(argv)

    path = args.path or find_default_schlib()
    if not os.path.isfile(path):
        raise SystemExit(f"File not found: {path}")

    with SchLib(path) as lib:
        print_library_info(lib)
        if not args.no_list:
            list_components(lib, None if args.limit == 0 else args.limit)
        passed = verify_klema(lib)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

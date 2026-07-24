#!/usr/bin/env python3
"""Batch-add a parameter to every component in a .SchLib that lacks it.

Adds a parameter (name + default value) to all components that don't already
have one with that name, leaving existing ones untouched. Intended for
production parameters such as "Mount" that downstream tooling (pick-and-place)
needs on every component.

By default it writes a NEW file into ./output and never touches the input.
Saving requires pywin32 (Windows).

Examples::

    # Preview what would change, write nothing:
    python batch_add_parameter.py --name Mount --value "Surface Mount" --dry-run

    # Add to all missing, writing output/<same-name>.SchLib:
    python batch_add_parameter.py --name Mount --value "Surface Mount"

    # Choose an explicit output, or edit the source in place:
    python batch_add_parameter.py --name Mount --value "Surface Mount" -o out.SchLib
    python batch_add_parameter.py --name Mount --value "Surface Mount" --in-place

Visibility defaults to hidden (as the archetype SMD_TRAN_ULN2003ADR's "Mount"
parameter is); use --visible or --hidden to force it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import SchLib  # noqa: E402
from cli_common import find_default_schlib, resolve_output  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", nargs="?",
                        help="Path to a .SchLib (default: the single file in ./input).")
    parser.add_argument("--name", required=True, help="Parameter name, e.g. Mount.")
    parser.add_argument("--value", required=True, help="Default value to set.")
    parser.add_argument("-o", "--output", metavar="PATH",
                        help="Output .SchLib path (default: output/<input-name>).")
    parser.add_argument("--in-place", action="store_true",
                        help="Overwrite the input file (atomic). Overrides --output.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing anything.")
    parser.add_argument("--json", action="store_true",
                        help="Emit the summary as JSON on stdout.")
    vis = parser.add_mutually_exclusive_group()
    vis.add_argument("--hidden", dest="hidden", action="store_true", default=None,
                     help="Add as a hidden parameter (default).")
    vis.add_argument("--visible", dest="hidden", action="store_false",
                     help="Add as a visible parameter shown on the symbol.")
    args = parser.parse_args(argv)

    hidden = True if args.hidden is None else args.hidden

    path = args.path or find_default_schlib()
    if not os.path.isfile(path):
        raise SystemExit(f"File not found: {path}")

    with SchLib(path) as lib:
        try:
            summary = lib.add_parameter_to_all(args.name, args.value, hidden=hidden)
        except ValueError as exc:
            raise SystemExit(f"Cannot add parameter: {exc}")

        out_path = None
        if not args.dry_run:
            out_path = resolve_output(path, args.output, args.in_place)
            try:
                lib.save(out_path)
            except ImportError as exc:  # pragma: no cover - platform dependent
                raise SystemExit(
                    f"Saving requires pywin32 (Windows Structured Storage): {exc}"
                )

    result = {
        "name": args.name,
        "value": args.value,
        "hidden": hidden,
        "dry_run": args.dry_run,
        "output": out_path,
        "added_count": summary["added_count"],
        "skipped_count": summary["skipped_count"],
        "total": summary["total"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    verb = "Would add" if args.dry_run else "Added"
    print(f"{verb} parameter {args.name!r} = {args.value!r} "
          f"({'hidden' if hidden else 'visible'}).")
    print(f"  {verb.lower()} to : {summary['added_count']} component(s)")
    print(f"  skipped      : {summary['skipped_count']} "
          f"(already had {args.name!r})")
    print(f"  total        : {summary['total']}")
    if args.dry_run:
        print("  (dry run — nothing written)")
    else:
        print(f"  wrote        : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Batch-set a parameter's value on components matching a query.

Selects components with a composable query (name / parameter / designator / pin
filters — the same selectors ``list_components.py`` accepts for searching) and
sets a target parameter's text on the matches. Components that match but don't
yet have the target parameter are skipped and reported (use --create-missing to
add it instead). Existing values on matches are overwritten.

Selector values can be negated: ``NAME!=VALUE`` for parameter selectors, or a
leading ``!`` elsewhere (escape a literal '!' as '\\!'). E.g. components that
have a Mount parameter with some other value than the two standard ones::

    python batch_set_parameter.py --set Mount="Surface Mount" \\
        --param-exists Mount --param "Mount!=Surface Mount" \\
        --param "Mount!=Through Hole" --dry-run

By default it writes a NEW file into ./output and never touches the input.
Saving requires pywin32 (Windows). Use --dry-run to preview matches first.

More examples::

    # Through-hole parts (name has TH_ but not ETH_):
    python batch_set_parameter.py --set Mount="Through Hole" \\
        --name-contains TH_ --name-contains '!ETH_' --dry-run

    # SOIC parts by package parameter:
    python batch_set_parameter.py --set Mount="Surface Mount" \\
        --param "Case/Package=SOIC"

Selectors are ANDed by default (use --match-any to OR them). Provide at least
one selector, or --all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import SchLib  # noqa: E402
from cli_common import (  # noqa: E402
    add_selector_arguments,
    build_predicate,
    find_default_schlib,
    resolve_output,
    split_pair,
)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?",
                   help="Path to a .SchLib (default: the single file in ./input).")
    p.add_argument("--set", dest="set_spec", required=True, metavar="TARGET=VALUE",
                   help="Parameter to set and the value, e.g. Mount=\"Through Hole\".")
    add_selector_arguments(p)
    p.add_argument("--create-missing", action="store_true",
                   help="Add the target parameter (hidden) to a match that lacks it.")
    p.add_argument("--dry-run", action="store_true",
                   help="List matches and write nothing.")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="Cap the dry-run match listing to N (0 = all).")
    p.add_argument("-o", "--output", metavar="PATH",
                   help="Output path (default: output/<input-name>).")
    p.add_argument("--in-place", action="store_true",
                   help="Overwrite the input file (atomic). Overrides --output.")
    p.add_argument("--json", action="store_true", help="Emit the summary as JSON.")
    args = p.parse_args(argv)

    target, value, negated = split_pair(args.set_spec, "--set")
    if negated:
        raise SystemExit("--set expects TARGET=VALUE ('!=' makes no sense here).")
    predicate = build_predicate(args)
    if predicate is None:
        raise SystemExit(
            "Refusing to match all components: pass a selector "
            "(e.g. --name-contains / --param) or --all."
        )

    path = args.path or find_default_schlib()
    if not os.path.isfile(path):
        raise SystemExit(f"File not found: {path}")

    with SchLib(path) as lib:
        try:
            summary = lib.set_parameter_where(
                target, value, predicate, create_missing=args.create_missing)
        except ValueError as exc:
            raise SystemExit(f"Cannot set parameter: {exc}")

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
        "target": target,
        "value": value,
        "dry_run": args.dry_run,
        "create_missing": args.create_missing,
        "output": out_path,
        "matched_count": summary["matched_count"],
        "updated_count": summary["updated_count"],
        "unchanged_count": summary["unchanged_count"],
        "created_count": summary["created_count"],
        "skipped_missing_count": summary["skipped_missing_count"],
        "total": summary["total"],
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"Query matched {summary['matched_count']} of {summary['total']} "
          f"component(s); target parameter {target!r} = {value!r}.")
    print(f"  updated   : {summary['updated_count']}")
    print(f"  unchanged : {summary['unchanged_count']} (already had the value)")
    if args.create_missing:
        print(f"  created   : {summary['created_count']} (added missing parameter)")
    else:
        print(f"  skipped   : {summary['skipped_missing_count']} "
              f"(matched but missing {target!r}; use --create-missing)")
    if args.dry_run:
        names = summary["matched"]
        shown = names if args.limit == 0 else names[:args.limit]
        print(f"\n  matched components ({len(names)}):")
        for n in shown:
            print(f"    {n}")
        if len(shown) < len(names):
            print(f"    ... and {len(names) - len(shown)} more")
        if summary["skipped_missing"]:
            print(f"\n  matched but missing {target!r} "
                  f"({summary['skipped_missing_count']}):")
            for n in summary["skipped_missing"][:args.limit or None]:
                print(f"    {n}")
        print("\n  (dry run — nothing written)")
    else:
        print(f"  wrote     : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

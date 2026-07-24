#!/usr/bin/env python3
"""Batch-set a parameter's value on components matching a query.

Selects components with a composable query (name / parameter / designator / pin
filters) and sets a target parameter's text on the matches. Components that
match but don't yet have the target parameter are skipped and reported (use
--create-missing to add it instead). Existing values on matches are overwritten.

By default it writes a NEW file into ./output and never touches the input.
Saving requires pywin32 (Windows). Use --dry-run to preview matches first.

Examples::

    # Through-hole parts (name has TH_ but not ETH_):
    python batch_set_parameter.py --set Mount="Through Hole" \\
        --name-contains TH_ --name-excludes ETH_ --dry-run

    # SOIC parts by package parameter:
    python batch_set_parameter.py --set Mount="Surface Mount" \\
        --param "Case/Package=SOIC"

Selectors are ANDed by default (use --match-any to OR them). Repeatable name/
designator flags OR within themselves. Provide at least one selector, or --all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import SchLib, query as q  # noqa: E402
from list_components import find_default_schlib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _resolve_output(input_path: str, args) -> str:
    if args.in_place:
        return input_path
    if args.output:
        if _same_path(args.output, input_path):
            raise SystemExit(
                "Refusing to overwrite the input file; use --in-place to do that."
            )
        return args.output
    out_dir = os.path.join(HERE, "output")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, os.path.basename(input_path))


def _split_pair(spec: str, flag: str):
    """Parse a NAME=VALUE selector (split on the first '=')."""
    name, sep, value = spec.partition("=")
    if not sep or not name:
        raise SystemExit(f"{flag} expects NAME=VALUE, got {spec!r}")
    return name, value


def _build_predicate(args):
    """Assemble the query predicate from CLI args, or None if no selector."""
    ic = args.ignore_case
    conditions = []
    if args.name_contains:
        conditions.append(q.name_contains(*args.name_contains, ignore_case=ic))
    if args.name_excludes:
        conditions.append(q.name_excludes(*args.name_excludes, ignore_case=ic))
    if args.name_regex:
        conditions.append(q.name_regex(args.name_regex, ignore_case=ic))
    for spec in args.param or []:
        name, value = _split_pair(spec, "--param")
        conditions.append(q.param_equals(name, value, ignore_case=ic))
    for spec in args.param_contains or []:
        name, value = _split_pair(spec, "--param-contains")
        conditions.append(q.param_contains(name, value, ignore_case=ic))
    for spec in args.param_regex or []:
        name, value = _split_pair(spec, "--param-regex")
        conditions.append(q.param_regex(name, value, ignore_case=ic))
    for name in args.param_exists or []:
        conditions.append(q.param_exists(name))
    for name in args.param_missing or []:
        conditions.append(q.param_missing(name))
    if args.designator_prefix:
        conditions.append(q.any_of(*[
            q.designator_prefix(p, ignore_case=ic) for p in args.designator_prefix
        ]))
    if args.pins_min is not None or args.pins_max is not None:
        conditions.append(q.pins(args.pins_min, args.pins_max))

    if not conditions:
        return q.always() if args.all else None
    return (q.any_of(*conditions) if args.match_any else q.all_of(*conditions))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?",
                   help="Path to a .SchLib (default: the single file in ./input).")
    p.add_argument("--set", dest="set_spec", required=True, metavar="TARGET=VALUE",
                   help="Parameter to set and the value, e.g. Mount=\"Through Hole\".")

    sel = p.add_argument_group("selectors (ANDed unless --match-any)")
    sel.add_argument("--name-contains", action="append", metavar="SUBSTR",
                     help="Name contains SUBSTR (repeatable → any-of).")
    sel.add_argument("--name-excludes", action="append", metavar="SUBSTR",
                     help="Name contains none of these (repeatable).")
    sel.add_argument("--name-regex", metavar="REGEX", help="Name matches REGEX.")
    sel.add_argument("--param", action="append", metavar="NAME=VALUE",
                     help="Parameter NAME text equals VALUE (repeatable).")
    sel.add_argument("--param-contains", action="append", metavar="NAME=SUBSTR",
                     help="Parameter NAME text contains SUBSTR (repeatable).")
    sel.add_argument("--param-regex", action="append", metavar="NAME=REGEX",
                     help="Parameter NAME text matches REGEX (repeatable).")
    sel.add_argument("--param-exists", action="append", metavar="NAME",
                     help="Component has parameter NAME (repeatable).")
    sel.add_argument("--param-missing", action="append", metavar="NAME",
                     help="Component lacks parameter NAME (repeatable).")
    sel.add_argument("--designator-prefix", action="append", metavar="P",
                     help="Designator starts with P (repeatable → any-of).")
    sel.add_argument("--pins-min", type=int, metavar="N", help="At least N pins.")
    sel.add_argument("--pins-max", type=int, metavar="N", help="At most N pins.")
    sel.add_argument("--all", action="store_true",
                     help="Match every component (required to match all).")

    p.add_argument("--match-any", action="store_true",
                   help="OR the selectors instead of ANDing them.")
    p.add_argument("--ignore-case", action="store_true",
                   help="Case-insensitive text matching.")
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

    target, value = _split_pair(args.set_spec, "--set")
    predicate = _build_predicate(args)
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
            out_path = _resolve_output(path, args)
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

#!/usr/bin/env python3
"""Inspect and batch-change the fonts of a .SchLib library.

A schematic library carries one global font table (in its FileHeader): each
entry is a typeface + size + style/rotation, and every text object references
an entry by ID. Typefaces therefore vary with where a component came from
(self-made, distributor downloads, ...). This tool shows the variance and lets
you consolidate it.

Without options it prints statistics only — the font table, how often each
entry is referenced and by what record types, and a per-typeface rollup. No
component names are listed.

--rename-to NAME changes the *typeface* of table entries (all of them, or a
subset via --only/--ids). Sizes, styles, and rotations are preserved, and no
component data is touched — every FontID reference simply renders in the new
typeface, including text inside binary records the parser treats as opaque.

Examples::

    python font_tool.py                          # statistics
    python font_tool.py --json                   # statistics as JSON
    python font_tool.py --rename-to Tahoma --dry-run
    python font_tool.py --rename-to Tahoma       # writes output/<input-name>
    python font_tool.py --rename-to Tahoma --only "Courier New" --only Tahoma
    python font_tool.py --rename-to Tahoma --ids 1,3,20

The font name is written as given — check the statistics first and pick a font
that is actually installed, or Altium will substitute at render time. By
default a NEW file is written into ./output; the input is never touched unless
--in-place. Saving requires pywin32 (Windows).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import SchLib  # noqa: E402
from cli_common import find_default_schlib, resolve_output  # noqa: E402

# Human-readable names for the record types that commonly carry text.
_RECORD_LABELS = {
    4: "label", 32: "sheet-name", 33: "sheet-filename", 34: "designator",
    41: "parameter", 44: "impl-list", 45: "implementation",
}


def _style_str(entry: dict) -> str:
    bits = list(entry["styles"])
    if entry["rotation"]:
        bits.append(f"Rot{entry['rotation']}")
    return ", ".join(bits)


def _by_record_str(by_record: dict) -> str:
    parts = []
    for rid, n in sorted(by_record.items(), key=lambda kv: -kv[1]):
        label = _RECORD_LABELS.get(rid, f"rec{rid}")
        parts.append(f"{label}×{n}")
    return ", ".join(parts)


def print_stats(lib: SchLib, table: dict, usage: dict) -> None:
    print("=" * 78)
    print(f"Font table ({len(table)} entries, FileHeader FontIdCount="
          f"{lib.header.get('FontIdCount')})")
    print("=" * 78)
    print(f"{'ID':>3}  {'Typeface':<32} {'Size':>4}  {'Style':<16} "
          f"{'Uses':>6} {'Comps':>6}  Used by")
    for k in sorted(table):
        e = table[k]
        u = usage.get(k, {"uses": 0, "components": 0, "by_record": {}})
        print(f"{k:>3}  {e['name']:<32} {e['size'] or '?':>4}  "
              f"{_style_str(e):<16} {u['uses']:>6} {u['components']:>6}  "
              f"{_by_record_str(u['by_record'])}")
    unused = [k for k in table if k not in usage]
    if unused:
        print(f"\nEntries with no text-record references: "
              f"{', '.join(map(str, unused))}")
        print("  (binary pin records are not scanned; such entries may still "
              "be referenced there)")

    # Per-typeface rollup: the consolidation view.
    rollup = defaultdict(lambda: {"entries": 0, "uses": 0})
    for k, e in table.items():
        rollup[e["name"]]["entries"] += 1
        rollup[e["name"]]["uses"] += usage.get(k, {}).get("uses", 0)
    print()
    print(f"{'Typeface':<32} {'Entries':>8} {'Uses':>8}")
    print("-" * 50)
    for name, r in sorted(rollup.items(), key=lambda kv: -kv[1]["uses"]):
        print(f"{name:<32} {r['entries']:>8} {r['uses']:>8}")
    print()


def stats_to_dict(lib: SchLib, table: dict, usage: dict) -> dict:
    fonts = []
    for k in sorted(table):
        e = table[k]
        u = usage.get(k, {"uses": 0, "components": 0, "by_record": {}})
        fonts.append({
            "id": k, "name": e["name"], "size": e["size"],
            "styles": e["styles"], "rotation": e["rotation"],
            "uses": u["uses"], "components": u["components"],
            "by_record": {str(rid): n for rid, n in u["by_record"].items()},
        })
    rollup = defaultdict(lambda: {"entries": 0, "uses": 0})
    for f in fonts:
        rollup[f["name"]]["entries"] += 1
        rollup[f["name"]]["uses"] += f["uses"]
    return {"font_count": len(table), "fonts": fonts,
            "by_typeface": dict(rollup)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?",
                   help="Path to a .SchLib (default: the single file in ./input).")
    p.add_argument("--rename-to", metavar="NAME",
                   help="Change the typeface of font-table entries to NAME "
                        "(sizes/styles/rotations are kept).")
    p.add_argument("--only", action="append", metavar="TYPEFACE",
                   help="Rename only entries whose current typeface is "
                        "TYPEFACE (case-insensitive; repeatable).")
    p.add_argument("--ids", metavar="N,N,...",
                   help="Rename only these font-table entry ids.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing anything.")
    p.add_argument("-o", "--output", metavar="PATH",
                   help="Output path (default: output/<input-name>).")
    p.add_argument("--in-place", action="store_true",
                   help="Overwrite the input file (atomic). Overrides --output.")
    p.add_argument("--json", action="store_true",
                   help="Emit statistics / rename summary as JSON.")
    args = p.parse_args(argv)

    if (args.only or args.ids or args.dry_run) and not args.rename_to:
        raise SystemExit("--only/--ids/--dry-run require --rename-to.")

    only_ids = None
    if args.ids:
        try:
            only_ids = [int(x) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            raise SystemExit(f"--ids expects comma-separated integers, "
                             f"got {args.ids!r}")

    path = args.path or find_default_schlib()
    if not os.path.isfile(path):
        raise SystemExit(f"File not found: {path}")

    with SchLib(path) as lib:
        table = lib.fonts()
        usage = lib.font_usage()

        # -- statistics mode -------------------------------------------------
        if not args.rename_to:
            if args.json:
                print(json.dumps(stats_to_dict(lib, table, usage), indent=2))
            else:
                print_stats(lib, table, usage)
            return 0

        # -- rename mode ------------------------------------------------------
        try:
            summary = lib.rename_fonts(args.rename_to, only_names=args.only,
                                       only_ids=only_ids)
        except ValueError as exc:
            raise SystemExit(f"Cannot rename fonts: {exc}")

        out_path = None
        if not args.dry_run:
            out_path = resolve_output(path, args.output, args.in_place)
            try:
                lib.save(out_path)
            except ImportError as exc:  # pragma: no cover - platform dependent
                raise SystemExit(
                    f"Saving requires pywin32 (Windows Structured Storage): {exc}"
                )

    affected_uses = sum(usage.get(k, {}).get("uses", 0)
                        for k in summary["changed"])
    result = {
        "new_name": summary["new_name"],
        "dry_run": args.dry_run,
        "output": out_path,
        "changed_ids": summary["changed"],
        "changed_count": summary["changed_count"],
        "table_size": summary["table_size"],
        "affected_text_record_uses": affected_uses,
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    verb = "Would rename" if args.dry_run else "Renamed"
    print(f"{verb} {summary['changed_count']} of {summary['table_size']} "
          f"font entries to {args.rename_to!r}"
          f" (affecting {affected_uses} text-record references"
          f"{' plus any binary-record text' if summary['changed'] else ''}).")
    if summary["changed"]:
        print(f"  entry ids : {', '.join(map(str, summary['changed']))}")
    if args.dry_run:
        print("  (dry run — nothing written)")
    elif out_path:
        print(f"  wrote     : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

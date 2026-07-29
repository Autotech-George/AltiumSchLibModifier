#!/usr/bin/env python3
"""Repoint Altium projects from stale (missing) component libraries to a new one.

Scans a folder tree for Altium projects and reports which component libraries
their documents reference. Any reference whose library file cannot be found is
**stale** — typically a library that was renamed or moved — and can be repointed
to a replacement library in one pass.

The library filename is cached in three places, all of which are updated:

  .SchDoc   component records      SourceLibraryName
  .PcbDoc   Components6/Data       SOURCECOMPONENTLIBRARY,
                                   SOURCECOMPLIBRARYIDENTIFIER
                                   (SOURCEFOOTPRINTLIBRARY for a .PcbLib)
  .PrjPcb   cached component list  ComponentLibraryIdentifier<N>

**Reports by default and writes nothing.** Pass --apply to modify files; each
modified file is backed up alongside as <file>.bak (a pre-existing .bak from an
earlier run is kept, never overwritten). Writing requires pywin32 (Windows).

Examples::

    # What would change?
    python relink_libraries.py C:\\Projects path\\to\\NewLibrary.SchLib

    # Do it:
    python relink_libraries.py C:\\Projects path\\to\\NewLibrary.SchLib --apply

    # Repoint one specific old library, whether or not it still exists:
    python relink_libraries.py C:\\Projects NewLib.SchLib \\
        --from "mySCHLib 10_10_2017.SchLib" --apply

    # Footprint libraries work the same way:
    python relink_libraries.py C:\\Projects NewFootprints.PcbLib --apply

A library counts as existing if a file of that name is found next to the new
library, anywhere under the scanned root, or under a --search-path folder.
Generated/archive folders (History, __Previews, Project Outputs/Logs) are
skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import liblinks as ll  # noqa: E402

DOC_TYPES = ("SchDoc", "PcbDoc", "PrjPcb")


def _natural_key(text: str):
    """Sort key so R2 comes before R10."""
    parts = re.split(r"(\d+)", text)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _group_details(details):
    """Collapse per-field rows into one row per component.

    A component often carries the same library in two fields (the PCB records
    both ``SOURCECOMPONENTLIBRARY`` and ``SOURCECOMPLIBRARYIDENTIFIER``), so
    list it once with the fields it appears in.
    """
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for d in details:
        key = (d.designator, d.component, d.context)
        g = groups.setdefault(key, {"designator": d.designator,
                                    "component": d.component,
                                    "context": d.context,
                                    "fields": [], "refs": 0})
        if d.field not in g["fields"]:
            g["fields"].append(d.field)
        g["refs"] += 1
    return sorted(groups.values(),
                  key=lambda g: (_natural_key(g["context"]),
                                 _natural_key(g["designator"] or g["component"])))


def print_reference_detail(affected, replace, root: str) -> None:
    """List every individual reference that would be / was repointed."""
    total = sum(len(fr.details_for(replace)) for fr in affected)
    print(f"Affected references ({total}):")
    print()
    for fr in affected:
        details = fr.details_for(replace)
        if not details:
            continue
        groups = _group_details(details)
        rel = os.path.relpath(fr.path, root)
        if fr.doctype == "PrjPcb":
            noun = "entry" if len(groups) == 1 else "entries"
        else:
            noun = "component" if len(groups) == 1 else "components"
        print(f"  {rel}  —  {fr.doctype}, {len(details)} refs in "
              f"{len(groups)} {noun}")
        # Column 1 is the designator for documents, the cached entry for a
        # project file (which records no designator).
        for g in groups:
            first = g["designator"] or g["context"] or "-"
            ctx = "" if (fr.doctype == "PrjPcb" or not g["context"]) \
                else f"  {g['context']}"
            print(f"    {first:<9} {g['component']:<36}{ctx}"
                  f"  [{', '.join(g['fields'])}]")
        print()


def _project_of(path: str, project_dirs: list[str]) -> str:
    """The project folder owning ``path`` (longest matching prefix)."""
    best = ""
    ap = os.path.abspath(path)
    for d in project_dirs:
        if ap.startswith(d + os.sep) and len(d) > len(best):
            best = d
    return best


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root", help="Top folder to scan for Altium projects.")
    p.add_argument("new_library",
                   help="Path to the replacement library (.SchLib or .PcbLib).")
    p.add_argument("--apply", action="store_true",
                   help="Write the changes (default: report only).")
    p.add_argument("--from", dest="from_names", action="append", metavar="NAME",
                   help="Replace only this library name, regardless of whether "
                        "it exists (repeatable).")
    p.add_argument("--all-others", action="store_true",
                   help="Replace every reference that is not already the new "
                        "library, including libraries that do exist.")
    p.add_argument("--search-path", action="append", metavar="DIR", default=None,
                   help="Extra folder to search when deciding whether a "
                        "referenced library exists (repeatable).")
    p.add_argument("--no-backup", action="store_true",
                   help="Do not create <file>.bak copies when applying.")
    p.add_argument("--skip-schdoc", action="store_true",
                   help="Leave .SchDoc files untouched.")
    p.add_argument("--skip-pcbdoc", action="store_true",
                   help="Leave .PcbDoc files untouched.")
    p.add_argument("--skip-prjpcb", action="store_true",
                   help="Leave .PrjPcb files untouched.")
    p.add_argument("--show-refs", action="store_true",
                   help="List every affected reference individually "
                        "(designator, component, field) instead of just counts. "
                        "Not capped by --limit; combine with --json for tooling.")
    p.add_argument("--limit", type=int, default=20, metavar="N",
                   help="Cap the per-file summary listing (0 = no cap). "
                        "Default 20.")
    p.add_argument("--json", action="store_true",
                   help="Emit the report/summary as JSON.")
    args = p.parse_args(argv)

    if not os.path.isdir(args.root):
        raise SystemExit(f"Not a folder: {args.root}")
    try:
        kind = ll.kind_for_library(args.new_library)
    except ValueError as exc:
        raise SystemExit(str(exc))
    target = os.path.basename(args.new_library)
    if not target.isascii():
        raise SystemExit("The new library name must be ASCII.")
    if not os.path.isfile(args.new_library):
        print(f"Warning: {args.new_library} does not exist — its NAME will "
              f"still be written into the documents.", file=sys.stderr)

    doc_types = tuple(d for d in DOC_TYPES
                      if not getattr(args, f"skip_{d.lower()}"))
    if not doc_types:
        raise SystemExit("All document types are skipped; nothing to do.")

    # -- scan ---------------------------------------------------------------
    scans = ll.scan_tree(args.root, kind, doc_types=doc_types)
    totals = ll.referenced_libraries(scans)
    search_dirs = [os.path.dirname(os.path.abspath(args.new_library)),
                   args.root] + list(args.search_path or [])
    status = ll.classify(totals, target, ll.index_libraries(search_dirs))

    if args.from_names:
        wanted = {n.lower() for n in args.from_names}
        replace = {n for n in totals if n.lower() in wanted}
        unknown = wanted - {n.lower() for n in totals}
        for u in sorted(unknown):
            print(f"Warning: --from {u!r} is not referenced anywhere.",
                  file=sys.stderr)
    elif args.all_others:
        replace = {n for n in totals if status[n] != "target"}
    else:
        replace = {n for n in totals if status[n] == "stale"}

    mapping = {old: target for old in replace}
    affected = [fr for fr in scans if set(fr.refs) & replace]
    planned = sum(sum(c for n, c in fr.refs.items() if n in replace)
                  for fr in affected)

    project_dirs = sorted({os.path.dirname(os.path.abspath(p))
                           for p in ll.find_projects(args.root)})

    # -- apply --------------------------------------------------------------
    changed_files = 0
    changed_refs = 0
    residual: list[tuple[str, int]] = []
    if args.apply and mapping:
        for fr in affected:
            try:
                n = ll.rewrite_file(fr, mapping, kind, backup=not args.no_backup)
            except Exception as exc:
                print(f"ERROR rewriting {fr.path}: {exc}", file=sys.stderr)
                continue
            if n:
                changed_files += 1
                changed_refs += n
                left = ll.residual_occurrences(fr.path, replace)
                if left:
                    residual.append((fr.path, left))

    # -- report -------------------------------------------------------------
    per_type = Counter()
    for fr in affected:
        per_type[fr.doctype] += sum(c for n, c in fr.refs.items() if n in replace)

    result = {
        "root": os.path.abspath(args.root),
        "new_library": target,
        "kind": kind,
        "applied": bool(args.apply),
        "projects": len(project_dirs),
        "libraries": [
            {"name": n, "status": status[n], "references": totals[n],
             "will_replace": n in replace}
            for n, _ in totals.most_common()
        ],
        "files_affected": len(affected),
        "references_planned": planned,
        "by_document_type": dict(per_type),
        "files_changed": changed_files,
        "references_changed": changed_refs,
        "residual_files": [{"path": p, "occurrences": n} for p, n in residual],
    }
    if args.show_refs:
        result["affected_references"] = [
            {"path": fr.path, "doctype": fr.doctype,
             "references": [d.as_dict() for d in fr.details_for(replace)]}
            for fr in affected if fr.details_for(replace)
        ]

    if args.json:
        print(json.dumps(result, indent=2))
        return 1 if residual else 0

    print("=" * 78)
    print(f"Altium library relink — {'APPLY' if args.apply else 'REPORT (no changes written)'}")
    print("=" * 78)
    print(f"Scan root   : {result['root']}")
    print(f"New library : {target}  ({kind} library)")
    print(f"Projects    : {len(project_dirs)}")
    print()
    print("Referenced libraries:")
    for n, c in totals.most_common():
        mark = {"target": "already target", "found": "exists — left alone",
                "stale": "MISSING"}[status[n]]
        act = "  -> will repoint" if n in replace else ""
        if args.apply and n in replace:
            act = "  -> repointed"
        print(f"  {c:>5} refs  [{mark:<19}] {n}{act}")
    print()

    if not mapping:
        print("Nothing to do: no stale library references found.")
        if any(s == "found" for s in status.values()):
            print("  (use --all-others to also repoint libraries that do exist,")
            print("   or --from NAME to force a specific one)")
        return 0

    verb = "Changed" if args.apply else "Would change"
    print(f"{verb} {changed_refs if args.apply else planned} reference(s) "
          f"in {changed_files if args.apply else len(affected)} file(s):")
    for dt in DOC_TYPES:
        if per_type.get(dt):
            print(f"  {dt:<8} {per_type[dt]:>5} refs")
    print()

    shown = affected if args.limit == 0 else affected[:args.limit]
    for fr in shown:
        n = sum(c for name, c in fr.refs.items() if name in replace)
        rel = os.path.relpath(fr.path, args.root)
        print(f"  {n:>4}  {fr.doctype:<7} {rel}")
    if len(shown) < len(affected):
        print(f"  ... and {len(affected) - len(shown)} more file(s) "
              f"(use --limit 0 to list all)")
    print()

    if args.show_refs:
        print_reference_detail(affected, replace, args.root)
    else:
        print("Add --show-refs to list the affected components individually.")
        print()

    if residual:
        print("WARNING: the old name still appears in these files — it is also "
              "stored somewhere this tool does not handle:")
        for path, n in residual:
            print(f"  {n:>4}  {os.path.relpath(path, args.root)}")
        print()

    if args.apply:
        if not args.no_backup:
            print("Backups written alongside each modified file as <file>.bak")
        print("Done. Open a project in Altium to confirm the components resolve.")
    else:
        print("Re-run with --apply to write these changes.")
    return 1 if residual else 0


if __name__ == "__main__":
    raise SystemExit(main())

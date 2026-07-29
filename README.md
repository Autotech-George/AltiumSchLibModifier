# AltiumSchLibModifier

A Python tool to **read and edit Altium schematic library (`.SchLib`) files** —
the binary libraries that hold schematic symbol/component definitions.

It parses the container and the per-component record streams, lets you inspect
and modify component data in memory, and writes a valid `.SchLib` back out
losslessly (only the bytes you actually changed change).

## Status

- ✅ Parse the compound-file container and enumerate all components
- ✅ Byte-exact round-trip of every component's record stream (verified across a
  full production library, including records that contain empty `||` fields)
- ✅ Resolve component names ↔ OLE storage names (handles truncation & sanitization)
- ✅ Edit component header fields / parameters in memory
- ✅ **Batch-add a parameter** to every component that lacks it (e.g. `Mount`
  for pick-and-place) — see `batch_add_parameter.py`
- ✅ **Batch-set a parameter by query** — set a parameter's value on components
  matching a composable query (name / parameter / designator / pins) — see
  `batch_set_parameter.py`
- ✅ **Font statistics & batch typeface change** — see `font_tool.py`
- ✅ **Repoint whole projects off a stale/missing library** onto a new one
  (`.SchDoc` + `.PcbDoc` + `.PrjPcb`) — see `relink_libraries.py`
- ✅ **Atomic** save to a new `.SchLib` (temp file + rename) preserving structure
  and Altium's CLSID; in-place `save(same_path)` supported

### Limitations

- **No add / remove / rename of components yet.** Those require rewriting the
  library-level `FileHeader` and `SectionKeys` (and the OLE storage name), which
  this tool copies verbatim. Editing a component's *identity* fields
  (`LibReference`, `DesignItemId`) is therefore refused with a clear error;
  editing `ComponentDescription`, parameters, graphics, etc. is fine.
- **Non-ASCII component names authored in a non-Latin ANSI code page** (e.g. a
  Greek/Cyrillic Windows) may not resolve by their declared name: the
  `FileHeader` stores raw ANSI bytes (read as latin-1) while OLE storage names
  are Unicode, so the two can disagree. Such components are still reachable via
  their exact storage name (`lib.storage_names`), and saving preserves them
  untouched. Fully-ASCII libraries (the common case) are unaffected.

## Requirements

- Python 3.10+
- [`olefile`](https://pypi.org/project/olefile/) — reads the compound file
- [`pywin32`](https://pypi.org/project/pywin32/) — **Windows only**, used by the
  writer to create the compound file via native Structured Storage. Reading and
  parsing work cross-platform; saving currently requires Windows.

```bash
pip install -r requirements.txt
```

## Quick start

Drop a `.SchLib` into `input/` and run the acceptance program:

```bash
python list_components.py
```

It prints the library metadata, lists components, and runs a name-agnostic
self-check: every declared component resolves to a distinct OLE storage and
every `Data` stream round-trips byte-for-byte through the parser. Exit code is
`0` on success.

Useful flags:

```bash
python list_components.py --limit 0        # list all components
python list_components.py --no-list        # self-check only
python list_components.py path/to/lib.SchLib
```

### Search for components

Not sure of the exact name? `--match` lists every component whose name contains
a substring (case-insensitive), then pick one to inspect with `--show`:

```bash
python list_components.py --match CONN         # e.g. list connector symbols
python list_components.py --show <PART_NAME>   # then inspect the one you want
```

`--match` exits non-zero if nothing matches, and honours `--json` (emitting
`{"pattern", "count", "matches": [...]}`).

For anything beyond a name substring, the full **[query selectors](#query-selectors)**
(the same ones `batch_set_parameter.py` uses) work here too — the listing shows
only the components the query matches:

```bash
# Components that HAVE a Mount parameter but with a non-standard value:
python list_components.py --param-exists Mount \
    --param "Mount!=Surface Mount" --param "Mount!=Through Hole"

# Through-hole family without the Ethernet parts:
python list_components.py --name-contains TH_ --name-contains '!ETH_'
```

### Inspect one component

Query a single component by name (its `LibReference` or raw storage name) to see
its header fields, parameters, and record breakdown:

```bash
python list_components.py --show <PART_NAME>
```

An unknown name exits non-zero and prints close matches (case-insensitive
substring), so a partial name points you at the full one.

### JSON output

Add `--json` for machine-readable output (skips the banner/self-check):

```bash
python list_components.py --show <PART_NAME> --json   # one component
python list_components.py --json                      # whole library
python list_components.py --json | jq '.components | length'
```

With `--show`, the object has `name`, `storage_name`, `design_item_id`,
`description`, `part_count`, `pin_count`, `header` (all RECORD=1 fields),
`parameters` (`[{name, text}]`), and `record_breakdown`. A missing name emits
`{"found": false, "query": ..., "suggestions": [...]}` and exits non-zero.
Without `--show`, it emits the library summary plus a `components` array.

## Batch-add a parameter (`batch_add_parameter.py`)

Add a parameter (name + default value) to **every component that doesn't
already have it** — for example a `Mount` parameter that pick-and-place tooling
reads. Components that already carry the parameter are left untouched (their
values are never overwritten).

```bash
# Preview only — writes nothing:
python batch_add_parameter.py --name Mount --value "Surface Mount" --dry-run

# Add to all missing, writing a NEW file into ./output (input untouched):
python batch_add_parameter.py --name Mount --value "Surface Mount"

# Explicit output, or edit the source in place (atomic):
python batch_add_parameter.py --name Mount --value "Surface Mount" -o out.SchLib
python batch_add_parameter.py --name Mount --value "Surface Mount" --in-place
```

- **Safe by default**: writes `output/<input-name>.SchLib` and refuses to
  overwrite the input unless `--in-place` is given.
- **Visibility**: the parameter is added hidden by default (matching a normal
  machine parameter); use `--visible` / `--hidden` to force it.
- `--json` prints a summary (`added_count`, `skipped_count`, `total`, `output`).
- Only the modified components are touched; the *record content* of every other
  component (and all root streams) is left unchanged, and the operation is
  idempotent at the data level. Note the saved file is **not byte-identical**
  between runs — see [Output determinism](#output-determinism).
- Values must currently be ASCII (Altium's `%UTF8%` dual-encoding for non-ASCII
  parameter text is not yet emitted). Saving requires `pywin32` (Windows).

Programmatically:

```python
with SchLib("input/library.SchLib") as lib:
    summary = lib.add_parameter_to_all("Mount", "Surface Mount")  # hidden=True
    print(summary["added_count"], "added,", summary["skipped_count"], "skipped")
    lib.save("output/library.SchLib")
```

`Component` also exposes `add_parameter(name, value, hidden=True)`,
`ensure_parameter(name, value)` (add only if absent → bool), and
`has_parameter(name)`.

## Batch-set a parameter by query (`batch_set_parameter.py`)

Set a target parameter's value on the components a **query** selects. Always
preview with `--dry-run` first — it lists exactly which components match.

```bash
# Through-hole parts: name contains TH_ but not ETH_  (preview first)
python batch_set_parameter.py --set Mount="Through Hole" \
    --name-contains TH_ --name-excludes ETH_ --dry-run
python batch_set_parameter.py --set Mount="Through Hole" \
    --name-contains TH_ --name-excludes ETH_

# SOIC parts, selected by a parameter value:
python batch_set_parameter.py --set Mount="Surface Mount" --param "Case/Package=SOIC"
```

It accepts the shared **[query selectors](#query-selectors)** below.

Behaviour:

- **Preview**: `--dry-run` lists matches and writes nothing (`--limit N` caps the
  list). At least one selector (or `--all`) is required — it refuses an implicit
  match-all.
- **Missing target**: a matched component that doesn't yet have the target
  parameter is **skipped and reported** by default; pass `--create-missing` to
  add it (hidden) instead.
- **Safe & surgical**: writes `output/<input-name>.SchLib` (never clobbers the
  input unless `--in-place`); only the matched components' record content
  changes, and it is idempotent at the data level (the saved file is not
  byte-identical between runs — see [Output determinism](#output-determinism)).
  `--json` emits the summary. Values are ASCII-only (same limitation as add);
  saving needs `pywin32`.

Programmatically, build a predicate with `altium_schlib.query` and apply it:

```python
from altium_schlib import SchLib, query as q

with SchLib("input/library.SchLib") as lib:
    pred = q.all_of(q.name_contains("TH_"), q.name_excludes("ETH_"))
    summary = lib.set_parameter_where("Mount", "Through Hole", pred)
    # summary: matched_count, updated_count, unchanged_count, created_count,
    #          skipped_missing_count, ...
    lib.save("output/library.SchLib")
```

`query` provides `name_contains/name_excludes/name_regex`,
`param_equals/param_contains/param_regex/param_exists/param_missing`,
`designator_prefix`, `pins`, and the combinators `all_of/any_of/negate`.

## Query selectors

Shared by `list_components.py` (search/list the matches) and
`batch_set_parameter.py` (change the matches). Selectors are **ANDed** by
default; `--match-any` ORs them; `--ignore-case` makes text matches
case-insensitive.

| Flag | Selects components where… |
|---|---|
| `--name-contains [!]SUB` | name contains SUB (repeatable → any-of) |
| `--name-excludes SUB` | name contains none of these (≡ `--name-contains '!SUB'`) |
| `--name-regex [!]RE` | name matches regex RE |
| `--param NAME[!]=VALUE` | parameter NAME text equals (or `!=` differs from) VALUE |
| `--param-contains NAME[!]=SUB` | parameter NAME text contains SUB |
| `--param-regex NAME[!]=RE` | parameter NAME text matches RE |
| `--param-exists [!]NAME` / `--param-missing [!]NAME` | has / lacks parameter NAME |
| `--designator-prefix [!]P` | designator starts with P (e.g. `R`, `U`) |
| `--pins-min N` / `--pins-max N` | pin count in range |
| `--all` | every component (batch-set requires this to match all) |

**Negation.** Every text selector can be inverted: parameter selectors take the
`NAME!=VALUE` operator; the others take a leading `!` on the value. A negated
selector is the pure logical NOT of its positive form — in particular
`Mount!=X` also matches components with **no** `Mount` parameter at all, so
combine it with `--param-exists Mount` when you mean "has Mount, but not X":

```bash
# Has a Mount parameter, but it's neither of the standard values:
python list_components.py --param-exists Mount \
    --param "Mount!=Surface Mount" --param "Mount!=Through Hole"
```

To match a value that literally starts with `!`, escape it as `\!`. (In bash,
single-quote arguments containing `!` to dodge history expansion; PowerShell
and cmd need no special quoting.)

## Fonts (`font_tool.py`)

A `.SchLib` has one **global font table** in its `FileHeader`: each entry is a
typeface + size + style/rotation (`FontIdCount=N`, `FontName<k>`, `Size<k>`,
optional `Bold/Italic/Underline/Strikeout<k>=T`, `Rotation<k>`), and every text
object references an entry by `FontID`. Typeface variance creeps in with the
component source (self-made vs. distributor downloads); this tool shows it and
consolidates it.

```bash
python font_tool.py            # statistics: table, per-entry usage, typeface rollup
python font_tool.py --json
```

Statistics are aggregate only (no component names): per entry the typeface,
size, style, how many text records reference it and of which types, plus a
per-typeface rollup — so you can see at a glance which fonts dominate and pick
the consolidation target.

```bash
python font_tool.py --rename-to Tahoma --dry-run   # preview
python font_tool.py --rename-to Tahoma             # writes output/<input-name>
python font_tool.py --rename-to Tahoma --only "Courier New"   # one typeface only
python font_tool.py --rename-to Tahoma --ids 1,3,20           # specific entries
```

`--rename-to` changes only the **typeface name** of the selected table entries —
sizes, styles, and rotations are preserved, entries already using the target
name are skipped, and **no component stream is touched** (only the `FileHeader`
changes). Because references go through the table, this restyles every text
object — including any referenced from binary records the parser treats as
opaque. The name is written as given, so pick one that is actually installed
(check the statistics first) or Altium will substitute at render time. Usual
safety: writes to `output/` by default, `--in-place` to overwrite, `--json` for
scripting.

Programmatically: `lib.fonts()`, `lib.font_usage()`,
`lib.rename_fonts(name, only_names=..., only_ids=...)`.

## Repoint stale library links (`relink_libraries.py`)

When a component library is renamed or moved, every project that used it keeps
pointing at the old filename and its components stop resolving in Altium. This
tool scans a folder tree of projects, reports which libraries they reference and
which of those no longer exist, and repoints the stale ones at a replacement.

The filename is cached in **three** places, all of which are updated together —
fixing only the schematic leaves the PCB and project file stale:

| File | Where | Field |
|---|---|---|
| `.SchDoc` | component records (`RECORD=1`) | `SourceLibraryName` |
| `.PcbDoc` | `Components6/Data` records | `SOURCECOMPONENTLIBRARY`, `SOURCECOMPLIBRARYIDENTIFIER` (or `SOURCEFOOTPRINTLIBRARY` for a `.PcbLib`) |
| `.PrjPcb` | cached component list | `ComponentLibraryIdentifier<N>` |

```bash
# What would change? (default: reports, writes nothing)
python relink_libraries.py C:\Projects path\to\NewLibrary.SchLib

# Do it:
python relink_libraries.py C:\Projects path\to\NewLibrary.SchLib --apply

# Repoint one specific old library, whether or not it still exists:
python relink_libraries.py C:\Projects NewLib.SchLib \
    --from "mySCHLib 10_10_2017.SchLib" --apply

# Footprint libraries work the same way:
python relink_libraries.py C:\Projects NewFootprints.PcbLib --apply
```

- **Reports by default.** Nothing is written without `--apply`, and each
  modified file is backed up as `<file>.bak` (`--no-backup` to skip). A `.bak`
  from an earlier run is never overwritten, so the pristine original survives
  repeated runs.
- **"Stale" = not found.** A referenced library counts as existing if a file of
  that name is found beside the new library, anywhere under the scanned root, or
  under a `--search-path` folder; only unresolvable references are repointed.
  `--from NAME` targets a specific library regardless, `--all-others` repoints
  every reference that isn't already the target.
- **Surgical:** in each binary document only the one record stream changes
  (verified), the container's other streams and its CLSID are copied verbatim,
  and the original field-name casing is preserved (Altium emits both
  `SourceLibraryName` and `SOURCELIBRARYNAME`). `.PrjPcb` is patched at byte
  level, so its BOM and CRLF endings survive.
- **Residual check:** after writing, each file is re-scanned at byte level for
  the old name; anything left is reported as a warning (and sets a non-zero exit
  code) rather than passing silently.
- Generated/archive folders (`History`, `__Previews`, `Project Outputs/Logs`)
  are skipped. `--json` emits the full report. Writing needs `pywin32`.

Programmatically, the engine is `altium_schlib.liblinks`: `scan_tree`,
`referenced_libraries`, `classify`, `rewrite_file`, `residual_occurrences`.

## Output determinism

Running the same command on the same input **twice produces two files that are
functionally identical but not byte-identical** — a raw compare such as
`fc /b out1.SchLib out2.SchLib` (Windows) or `cmp` will report differences.
This is expected, **not** corruption; both files open identically in Altium.
Two things vary between saves:

1. **Parameter `UniqueID`s.** Altium gives every object an 8-character unique
   ID. When a *new* parameter is created (batch-add, or batch-set with
   `--create-missing`), the tool generates a fresh random ID for it — mirroring
   Altium — so each run assigns different IDs to the newly-created parameters.
   (Editing an existing parameter's value does not change its ID.)
2. **Compound-file timestamps.** A `.SchLib` is an OLE compound file whose
   directory entries carry timestamps; the OS Structured Storage writer stamps
   them at save time, so even re-saving with no edits changes those bytes.

Consequently, "unchanged / byte-identical / idempotent" in this README refers to
**stream (record) content**, not the raw file. To check whether two libraries
are *really* the same, compare their per-component record content (e.g. via the
parser / `--json` output), not raw bytes. Deterministic, byte-reproducible
output (fixed IDs + normalized timestamps) is not currently implemented — it can
be added on request.

## Library usage

```python
from altium_schlib import SchLib

with SchLib("input/library.SchLib") as lib:
    print(len(lib), "components")
    for name in lib.component_names:
        print(name)

    comp = lib.get_component(lib.component_names[0])   # or a name you know
    print(comp.name, comp.pin_count, comp.description)

    # Edit a header field and a parameter, then save a copy.
    comp.set_header_field("ComponentDescription", "updated description")
    comp.set_parameter("Value", "10k")        # returns False if no such param
    lib.save("output/edited.SchLib")
```

### API surface

- `SchLib(path)` — open a library.
  - `.component_names` — authoritative names (from `FileHeader`), in order.
  - `.components` — list of `Component`, lazily loaded.
  - `.get_component(name)` / `.has_component(name)` — look up by `LibReference`
    or storage name.
  - `.storage_name_for(name)` — resolve a name to its OLE storage.
  - `.header` — `LibraryHeader` (metadata + component list).
  - `.declared_count` — the raw `CompCount` field (usually `== len(lib)`).
  - `.save(path)` — write a new `.SchLib` (atomic; in-place allowed).
- `Component`
  - `.name`, `.description`, `.pin_count`, `.design_item_id`
  - `.part_count` — true number of parts (Altium stores `parts + 1`);
    `.raw_part_count` for the stored field
  - `.records` — all parsed `Record`s; `.records_of_type(id)`, `.parameters`
  - `.get_parameter(name)` / `.set_parameter(name, text)`
  - `.set_header_field(key, value)`
  - `.to_data_stream()` — serialize records back to bytes
- `Record` — one record block; `.get/.set/.remove`, `.record_id`, `.is_text`,
  `.is_binary`, `.dirty`.

## File format notes

A `.SchLib` is an **OLE2 / Compound File Binary Format** container
(magic `D0 CF 11 E0`). Structure:

```
/ (root, CLSID = 49A4C073-... Altium schematic library)
├── FileHeader     library metadata + component list (CompCount, LibRef0..N)
├── SectionKeys    maps long LibRef names -> storage names (disambiguation)
├── Storage        embedded binary payload (preserved verbatim)
├── <Component A>/
│   ├── Data        the component's records
│   └── PinTextData
├── <Component B>/
│   └── ...
└── ...
```

**Record framing** inside a `Data` stream — a flat sequence of blocks, each:

```
uint32 header (little-endian)
    length = header & 0x00FF_FFFF     # payload length
    flag   = header >> 24             # 0 = text record, 1 = binary pin record
payload[length]
```

- **Text records** are `|KEY=VALUE|KEY=VALUE|...` ASCII, terminated by one
  `\x00` (counted in `length`). The first record (`RECORD=1`) is the component
  header; `RECORD=41` records are parameters; `RECORD=2` pins are stored as
  binary (`flag = 1`) and preserved opaquely.

**Name resolution.** OLE storage names can't exceed 31 chars or contain
`/ \ : !`. Altium sanitizes the `LibReference` into a storage name and, when
that would truncate or collide, records the mapping in `SectionKeys` (which
takes precedence). This tool reproduces that logic so every declared component
resolves to exactly one storage.

## Tests

```bash
pytest -q
```

The suite covers record-framing unit round-trips, byte-exact round-trip of every
component stream, bijective name resolution, lossless save, and isolated-edit
save. It hard-codes no component names — sample-dependent tests discover their
subjects from whatever library is in `input/`, and skip if none is present; save
tests skip without `pywin32`.

## Project layout

```
altium_schlib/          the library package
    records.py          record framing + text-record parse/serialize
    schlib.py           SchLib / Component / LibraryHeader
    writer.py           compound-file writer (native Structured Storage)
    query.py            composable component predicates for queries
    liblinks.py         scan/repair library references in projects
cli_common.py           shared CLI plumbing (query selectors, output safety)
list_components.py      acceptance program (list / search / show / self-check)
batch_add_parameter.py  batch add a parameter to all components missing it
batch_set_parameter.py  batch set a parameter's value on components by query
font_tool.py            font statistics + batch typeface change
relink_libraries.py     repoint projects off stale/missing libraries
tests/                  pytest suite (test_schlib.py, test_liblinks.py)
input/                  source .SchLib libraries
output/                 written libraries (git-ignored)
```

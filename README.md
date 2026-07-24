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
list_components.py      acceptance program (list / search / show / self-check)
tests/test_schlib.py    pytest suite
input/                  source .SchLib libraries
output/                 written libraries (git-ignored)
```

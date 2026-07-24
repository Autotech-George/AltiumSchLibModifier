# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python toolset that reads and **losslessly edits** Altium schematic libraries
(`.SchLib`) — binary OLE2 compound files holding component definitions. The
core package is `altium_schlib/`; the root-level `*.py` scripts are CLI tools
built on it.

## Commands

```bash
python -m pytest -q                          # full suite (~30s against the sample library)
python -m pytest -q -k test_name             # single test
python -m pytest -q tests/test_schlib.py::test_lossless_save

python list_components.py                    # list + self-check (also --show/--match/selectors/--json)
python batch_add_parameter.py --name Mount --value "Unknown" --dry-run
python batch_set_parameter.py --set Mount="Through Hole" --name-contains TH_ --dry-run
python font_tool.py                          # font statistics; --rename-to NAME to change
```

Dependencies: `olefile` (read), `pywin32` (write — **Windows only**; reading/
parsing is cross-platform), `pytest`. Install via `pip install -r requirements.txt`.

## Architecture

Layering, bottom-up:

- **`altium_schlib/records.py`** — record framing. A component `Data` stream is
  a sequence of blocks: `uint32 LE` header where `length = header & 0xFFFFFF`,
  `flag = header >> 24` (0 = pipe-delimited text `|KEY=VALUE|...\x00`, 1 =
  opaque binary pin record). Text payloads are modeled as raw `split("|")`
  *chunks* so serialization is an exact inverse — empty fields (`||`), bare
  tokens, trailing pipes all survive. `Record` only rebuilds bytes when
  `dirty`; untouched records re-emit their original bytes.
- **`altium_schlib/schlib.py`** — `SchLib` (container), `Component`,
  `LibraryHeader`. Root streams: `FileHeader` (library metadata, component
  list `LibRef0..N`, and the **font table** `FontIdCount`/`FontName<k>`/...),
  `SectionKeys` (maps long names → ≤31-char storage names), `Storage`
  (verbatim). One OLE storage per component with `Data` + `PinTextData`.
  The FileHeader itself is parsed as records so library-level fields (fonts)
  are editable.
- **`altium_schlib/writer.py`** — writes a new compound file via native
  Structured Storage (pywin32). Atomic: temp file + `os.replace`; in-place
  save supported. Preserves Altium's root CLSID.
- **`altium_schlib/query.py`** — composable `Component` predicates
  (`name_contains`, `param_equals`, `designator_prefix`, `pins`,
  `all_of/any_of/negate`) used by search and batch-set.
- **`cli_common.py`** — **the home for reusable CLI functionality.** Anything
  shared by two or more tools goes here, not copy-pasted: input discovery
  (`find_default_schlib`), safe output resolution (`resolve_output` — default
  `output/<name>`, refuses to overwrite input without `--in-place`), the
  selector argument group (`add_selector_arguments`), selector→predicate
  building (`build_predicate`), and negation parsing (`NAME!=VALUE`, `!`
  prefix, `\!` escape). When adding a new CLI tool or selector, extend this
  file so `list_components.py` and the batch tools stay in lockstep.

## Invariants — do not break these

- **Byte-exact round-trip**: parsing then serializing an untouched stream must
  be byte-identical. Every edit path funnels through `Record.set`/dirty
  tracking; tests verify only intentionally-changed streams differ after save.
- **`OwnerIndex` is a positional reference.** Records point at their owner by
  index into the component's record list (footprint implementations: RECORD=44
  ← 45 ← 46/48). **Inserting or removing a record requires renumbering every
  `OwnerIndex` ≥ the insertion point** — see `Component.add_parameter`. Getting
  this wrong silently severs PCB footprint links (a real shipped bug, fixed).
- **Name ↔ storage resolution**: OLE storage names are ≤31 chars and forbid
  `/ \ : !`. Resolution order: `SectionKeys` entry (authoritative,
  disambiguates truncation collisions) → sanitized name → truncated name.
- **Value validation**: parameter names/values and font names must be ASCII
  with no `|`/NUL (`Component._validate_param_field`); `Record.set` rejects
  `|`/NUL outright. A raw `|` in a value corrupts the pipe format. Non-ASCII
  needs Altium's `%UTF8%` dual-encoding — not implemented; reject, don't guess.
- **Identity fields** (`LibReference`, `DesignItemId`) are mirrored in
  `FileHeader`/`SectionKeys`/the storage name; `set_header_field` refuses to
  change them. Component rename/add/remove is unimplemented — it requires
  rewriting those library-level structures together.
- **UniqueID**: 8 chars A–Z, unique within a component; generate via
  `schlib._new_unique_id` against all records' IDs (rng injectable for tests).
- **`PartCount` is stored as parts + 1** (Protel legacy): `part_count` returns
  the true count, `raw_part_count` the stored field.

## Testing conventions

Tests (`tests/test_schlib.py`) are **library-agnostic**: they discover their
subjects at runtime from whatever single `.SchLib` sits in `input/` (helpers
like `_first_name`, `_name_with_double_pipe`) and skip cleanly when none is
present; save/edit tests additionally skip without pywin32. Never hard-code
component names, counts, or other proprietary specifics — this repo is public.
Mutation tests must assert the **isolated-diff** property via `_stream_map`:
exactly the intended streams changed, everything else byte-identical.

## Gotchas

- **`.SchLib` files are proprietary and must never be committed** — gitignored
  at any depth under `input/` and `output/`. Check `git status` before
  committing; history has been scrubbed once already.
- **Output is not byte-deterministic**: newly created records get random
  UniqueIDs and the OLE writer stamps directory timestamps, so `fc /b` on two
  runs differs even though content is identical. Compare stream/record content,
  never raw bytes (see README "Output determinism").
- Saving over a file that is still open elsewhere fails on Windows;
  `SchLib.save` handles the in-place case by closing/reopening its own handle.
- `Component.get_parameter` returns `None` both for "absent" and
  "present-but-empty Text" — use `has_parameter` for existence checks.

"""Scan and repair library references inside Altium projects.

A component remembers which library it came from, and that filename is cached
in three different places. When the library is renamed or moved, every one of
them goes stale:

* ``.SchDoc`` -- ``FileHeader`` stream, component records (``RECORD=1``):
  ``SourceLibraryName``. **Field-name case varies** between Altium versions
  (``SOURCELIBRARYNAME`` also occurs), so fields are matched case-insensitively.
* ``.PcbDoc`` -- ``Components6/Data`` stream records:
  ``SOURCECOMPONENTLIBRARY`` and ``SOURCECOMPLIBRARYIDENTIFIER`` (schematic
  library) and ``SOURCEFOOTPRINTLIBRARY`` (footprint library).
* ``.PrjPcb`` -- INI text: ``ComponentLibraryIdentifier<N>=`` in the project's
  cached component list.

Both binary document types use the same length-prefixed, pipe-delimited record
framing as ``.SchLib`` (see :mod:`altium_schlib.records`), so edits go through
the same dirty-tracking machinery and the container is rewritten with every
other stream copied verbatim. ``.PrjPcb`` is patched with a byte-level regex so
its BOM and CRLF line endings survive untouched.
"""

from __future__ import annotations

import os
import re
import shutil
from collections import Counter, OrderedDict
from typing import Dict, Iterable, List, Optional, Tuple

import olefile

from .records import parse_records, serialize_records
from .writer import write_compound_file

# -- what counts as a library reference ---------------------------------------
SCHEMATIC_EXT = ".schlib"
FOOTPRINT_EXT = ".pcblib"

#: Kind -> {document type -> set of field names (lower-case)}.
FIELDS: Dict[str, Dict[str, frozenset]] = {
    "schematic": {
        ".schdoc": frozenset({"sourcelibraryname"}),
        ".pcbdoc": frozenset({"sourcecomponentlibrary",
                              "sourcecomplibraryidentifier"}),
    },
    "footprint": {
        ".schdoc": frozenset(),
        ".pcbdoc": frozenset({"sourcefootprintlibrary"}),
    },
}

#: Kind -> the ``.PrjPcb`` INI key holding the reference (lower-case, no index).
PRJ_KEYS: Dict[str, Optional[str]] = {
    "schematic": "componentlibraryidentifier",
    "footprint": None,  # projects do not cache a footprint-library list
}

#: Streams searched for records, per document type.
RECORD_STREAMS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    ".schdoc": (("FileHeader",),),
    ".pcbdoc": (("Components6", "Data"),),
}

DOC_EXTS = (".schdoc", ".pcbdoc")
PROJECT_EXT = ".prjpcb"

#: Generated / archived folders skipped while scanning.
SKIP_DIRS = ("history", "__previews")
SKIP_DIR_PREFIXES = ("project logs", "project outputs")


def kind_for_library(path: str) -> str:
    """``"schematic"`` or ``"footprint"``, from a library file's extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext == SCHEMATIC_EXT:
        return "schematic"
    if ext == FOOTPRINT_EXT:
        return "footprint"
    raise ValueError(
        f"unsupported library type {ext!r}: expected .SchLib or .PcbLib"
    )


def _is_skipped_dir(name: str) -> bool:
    low = name.lower()
    return low in SKIP_DIRS or low.startswith(SKIP_DIR_PREFIXES)


def walk_design_files(root: str) -> List[str]:
    """Design documents and project files under ``root`` (generated dirs skipped)."""
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]
        for f in filenames:
            if os.path.splitext(f)[1].lower() in DOC_EXTS + (PROJECT_EXT,):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def find_projects(root: str) -> List[str]:
    """``.PrjPcb`` files under ``root``."""
    return [p for p in walk_design_files(root)
            if p.lower().endswith(PROJECT_EXT)]


def index_libraries(dirs: Iterable[str]) -> Dict[str, str]:
    """Map ``basename.lower() -> path`` for every library file under ``dirs``."""
    index: Dict[str, str] = {}
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if not _is_skipped_dir(x)]
            for f in filenames:
                if os.path.splitext(f)[1].lower() in (SCHEMATIC_EXT, FOOTPRINT_EXT):
                    index.setdefault(f.lower(), os.path.join(dirpath, f))
    return index


# -- scanning -----------------------------------------------------------------
class RefDetail:
    """One individual library reference, with enough context to identify it."""

    __slots__ = ("library", "field", "designator", "component", "context")

    def __init__(self, library: str, field: str, designator: str = "",
                 component: str = "", context: str = ""):
        self.library = library          # the referenced library filename
        self.field = field              # field/key holding it (original casing)
        self.designator = designator    # e.g. 'U3' ('' where not recorded)
        self.component = component      # library reference / design item id
        self.context = context          # sheet path, or the .PrjPcb key

    def as_dict(self) -> dict:
        return {"library": self.library, "field": self.field,
                "designator": self.designator, "component": self.component,
                "context": self.context}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<RefDetail {self.designator or '?'} {self.component!r} "
                f"{self.field}={self.library!r}>")


class FileRefs:
    """Library references of one kind found in a single file."""

    __slots__ = ("path", "doctype", "refs", "details")

    def __init__(self, path: str, doctype: str, refs: Counter,
                 details: Optional[List[RefDetail]] = None):
        self.path = path
        self.doctype = doctype          # 'SchDoc' | 'PcbDoc' | 'PrjPcb'
        self.refs = refs                # library name -> occurrences
        self.details = details or []    # one RefDetail per occurrence

    def details_for(self, libraries) -> List[RefDetail]:
        """The individual references pointing at any of ``libraries``."""
        wanted = set(libraries)
        return [d for d in self.details if d.library in wanted]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FileRefs {self.doctype} {self.path!r} {dict(self.refs)}>"


def _iter_record_streams(ole, ext: str):
    """Yield ``(stream_path, data)`` for the streams that hold records."""
    for parts in RECORD_STREAMS.get(ext, ()):
        if ole.exists(list(parts)):
            yield parts, ole.openstream(list(parts)).read()


def _field_matches(record, names: frozenset):
    """Yield ``(actual_key, value)`` for fields whose name is in ``names``."""
    for key, value in record.fields:
        if key.lower() in names:
            yield key, value


def _designators_by_component(records) -> Dict[int, str]:
    """Map each ``RECORD=1`` index to its designator (``RECORD=34``) text.

    A designator record follows the component it belongs to, so the most recent
    component wins. (Cross-checked against the records' own ``OwnerIndex``
    back-references on real projects: both agree for every component.)
    """
    out: Dict[int, str] = {}
    current: Optional[int] = None
    for i, r in enumerate(records):
        if not r.is_text:
            continue
        if r.record_id == 1:
            current = i
        elif r.record_id == 34 and current is not None and current not in out:
            out[current] = r.get("Text") or ""
    return out


def _get_ci(record, name: str) -> str:
    """Case-insensitive field lookup (Altium varies the spelling)."""
    low = name.lower()
    for key, value in record.fields:
        if key.lower() == low:
            return value
    return ""


def scan_document(path: str, kind: str) -> Optional[FileRefs]:
    """Collect library references of ``kind`` from a ``.SchDoc``/``.PcbDoc``."""
    ext = os.path.splitext(path)[1].lower()
    names = FIELDS[kind].get(ext, frozenset())
    if not names:
        return None
    is_sch = ext == ".schdoc"
    refs: Counter = Counter()
    details: List[RefDetail] = []
    try:
        ole = olefile.OleFileIO(path)
    except Exception:
        return None
    try:
        for _parts, data in _iter_record_streams(ole, ext):
            try:
                records = parse_records(data)
            except ValueError:
                continue
            designators = _designators_by_component(records) if is_sch else {}
            for i, r in enumerate(records):
                if not r.is_text:
                    continue
                matches = list(_field_matches(r, names))
                if not matches:
                    continue
                if is_sch:
                    designator = designators.get(i, "")
                    component = (_get_ci(r, "LibReference")
                                 or _get_ci(r, "DesignItemId"))
                    context = ""
                else:
                    designator = _get_ci(r, "SOURCEDESIGNATOR")
                    component = (_get_ci(r, "SOURCELIBREFERENCE")
                                 or _get_ci(r, "PATTERN"))
                    context = _get_ci(r, "SOURCEHIERARCHICALPATH")
                for key, value in matches:
                    if value:
                        refs[value] += 1
                        details.append(RefDetail(value, key, designator,
                                                 component, context))
    finally:
        ole.close()
    return FileRefs(path, "SchDoc" if is_sch else "PcbDoc", refs, details)


def _prj_pattern(key: str, old: Optional[str] = None) -> re.Pattern:
    value = re.escape(old.encode("utf-8")) if old is not None else rb"[^\r\n]*"
    return re.compile(
        rb"(?mi)^(" + key.encode() + rb"\d*=)(" + value + rb")(?=\r|\n|$)")


def _prj_component_fields(raw: bytes) -> Dict[str, Dict[str, str]]:
    """Parse ``Component<Field><N>=value`` lines into ``{N: {field: value}}``."""
    out: Dict[str, Dict[str, str]] = {}
    pattern = re.compile(rb"(?mi)^Component([A-Za-z]+)(\d+)=([^\r\n]*)")
    for m in pattern.finditer(raw):
        field = m.group(1).decode()
        index = m.group(2).decode()
        value = m.group(3).decode("utf-8", "replace").strip()
        out.setdefault(index, {})[field.lower()] = value
    return out


def scan_project(path: str, kind: str) -> Optional[FileRefs]:
    """Collect library references of ``kind`` from a ``.PrjPcb``."""
    key = PRJ_KEYS.get(kind)
    if not key:
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    refs: Counter = Counter()
    details: List[RefDetail] = []
    entries = _prj_component_fields(raw)
    # Strip the trailing index off the key to look the entry's siblings up.
    field_stem = key
    for m in _prj_pattern(key).finditer(raw):
        value = m.group(2).decode("utf-8", "replace").strip()
        if not value:
            continue
        full_key = m.group(1).decode().rstrip("=")
        index = full_key[len(field_stem):]
        entry = entries.get(index, {})
        refs[value] += 1
        details.append(RefDetail(
            value, full_key, designator="",
            component=(entry.get("designitemid")
                       or entry.get("symbolreference", "")),
            context=f"entry {index}"))
    return FileRefs(path, "PrjPcb", refs, details)


def scan_tree(root: str, kind: str, *, doc_types=("SchDoc", "PcbDoc", "PrjPcb")
              ) -> List[FileRefs]:
    """Scan ``root`` for library references of ``kind``."""
    results: List[FileRefs] = []
    for path in walk_design_files(root):
        ext = os.path.splitext(path)[1].lower()
        if ext == PROJECT_EXT:
            fr = scan_project(path, kind) if "PrjPcb" in doc_types else None
        else:
            want = "SchDoc" if ext == ".schdoc" else "PcbDoc"
            fr = scan_document(path, kind) if want in doc_types else None
        if fr is not None and fr.refs:
            results.append(fr)
    return results


def referenced_libraries(scans: Iterable[FileRefs]) -> Counter:
    """Total occurrences per library name across ``scans``."""
    total: Counter = Counter()
    for fr in scans:
        total.update(fr.refs)
    return total


def classify(names: Iterable[str], target_basename: str,
             library_index: Dict[str, str]) -> Dict[str, str]:
    """Label each referenced name ``"target"``, ``"found"`` or ``"stale"``.

    ``"found"`` means a file of that name exists in one of the searched
    locations, so the reference is live and left alone by default.
    """
    out: Dict[str, str] = {}
    for name in names:
        if name.lower() == target_basename.lower():
            out[name] = "target"
        elif os.path.basename(name).lower() in library_index:
            out[name] = "found"
        else:
            out[name] = "stale"
    return out


# -- rewriting ----------------------------------------------------------------
def _read_stream_tree(ole) -> Tuple[Dict[Tuple[str, ...], bytes], object]:
    streams: Dict[Tuple[str, ...], bytes] = OrderedDict()
    for entry in ole.listdir(streams=True, storages=False):
        streams[tuple(entry)] = ole.openstream(entry).read()
    return streams, ole.root.clsid


def _nest(streams: Dict[Tuple[str, ...], bytes]) -> OrderedDict:
    tree: OrderedDict = OrderedDict()
    for parts, data in streams.items():
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, OrderedDict())
        node[parts[-1]] = data
    return tree


def make_backup(path: str) -> Optional[str]:
    """Copy ``path`` to ``path + ".bak"`` unless that already exists.

    A pre-existing backup is the pristine original from an earlier run and is
    never overwritten. Returns the backup path, or ``None`` if one was already
    there.
    """
    bak = path + ".bak"
    if os.path.exists(bak):
        return None
    shutil.copy2(path, bak)
    return bak


def residual_occurrences(path: str, names: Iterable[str]) -> int:
    """Raw-byte count of ``names`` still present in ``path``.

    Safety net: a non-zero result after rewriting means the name also lives
    somewhere this module does not handle.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:  # pragma: no cover - defensive
        return 0
    return sum(raw.count(n.encode("utf-8")) for n in names)


def rewrite_document(path: str, mapping: Dict[str, str], kind: str,
                     *, backup: bool = True) -> int:
    """Replace library references in a ``.SchDoc``/``.PcbDoc``. Returns the
    number of fields changed (0 leaves the file untouched)."""
    ext = os.path.splitext(path)[1].lower()
    names = FIELDS[kind].get(ext, frozenset())
    if not names or not mapping:
        return 0
    lower_map = {k.lower(): v for k, v in mapping.items()}

    ole = olefile.OleFileIO(path)
    try:
        streams, clsid = _read_stream_tree(ole)
        edited: Dict[Tuple[str, ...], bytes] = {}
        changed = 0
        for parts in RECORD_STREAMS.get(ext, ()):
            data = streams.get(parts)
            if data is None:
                continue
            try:
                records = parse_records(data)
            except ValueError:
                continue
            hits = 0
            for r in records:
                if not r.is_text:
                    continue
                for key, value in list(_field_matches(r, names)):
                    new = lower_map.get(value.lower())
                    if new is not None and new != value:
                        r.set(key, new)   # keeps the original field-name casing
                        hits += 1
            if hits:
                rebuilt = serialize_records(records)
                # Integrity: the rebuilt stream must still parse to the same
                # number of records before we commit it.
                if len(parse_records(rebuilt)) != len(records):
                    raise ValueError(
                        f"{path}: rebuilt {'/'.join(parts)} does not re-parse "
                        f"to the same record count; refusing to write")
                edited[parts] = rebuilt
                changed += hits
    finally:
        ole.close()

    if not changed:
        return 0
    if backup:
        make_backup(path)
    streams.update(edited)
    write_compound_file(path, _nest(streams), root_clsid=clsid)
    return changed


def rewrite_project(path: str, mapping: Dict[str, str], kind: str,
                    *, backup: bool = True) -> int:
    """Replace ``ComponentLibraryIdentifier`` values in a ``.PrjPcb``.

    Byte-level substitution: the BOM, CRLF endings and every other line are
    preserved exactly.
    """
    key = PRJ_KEYS.get(kind)
    if not key or not mapping:
        return 0
    with open(path, "rb") as fh:
        raw = fh.read()
    out = raw
    changed = 0
    for old, new in mapping.items():
        if old == new:
            continue
        pattern = _prj_pattern(key, old)
        out, n = pattern.subn(lambda m, nv=new.encode("utf-8"): m.group(1) + nv,
                              out)
        changed += n
    if not changed:
        return 0
    if backup:
        make_backup(path)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(out)
    os.replace(tmp, path)
    return changed


def rewrite_file(fileref: FileRefs, mapping: Dict[str, str], kind: str,
                 *, backup: bool = True) -> int:
    """Dispatch to the right rewriter for ``fileref``."""
    if fileref.doctype == "PrjPcb":
        return rewrite_project(fileref.path, mapping, kind, backup=backup)
    return rewrite_document(fileref.path, mapping, kind, backup=backup)

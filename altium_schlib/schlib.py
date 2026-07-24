"""High-level reader/editor for Altium ``.SchLib`` schematic libraries.

A SchLib file is an OLE2 / Compound File Binary Format (CFBF) container:

* Root streams:
    - ``FileHeader``  -- library metadata plus the authoritative component list
      (``CompCount`` and ``LibRef0..N``).
    - ``SectionKeys`` -- maps long ``LibRef`` names to their (<=31 char,
      sanitized) storage names, used when a component name is too long or
      contains characters invalid in an OLE storage name.
    - ``Storage``     -- embedded binary payload (models/images); preserved
      verbatim.
* One storage per component, named after the (sanitized) ``LibReference``,
  each containing a ``Data`` stream (the record list) and a ``PinTextData``
  stream.

Reading is lazy; editing mutates in-memory records; :meth:`SchLib.save` writes
a new compound file with every original stream preserved and edited streams
substituted (see :mod:`altium_schlib.writer`).
"""

from __future__ import annotations

import random
import string
import struct
from typing import Dict, List, Optional, Tuple

import olefile

from .records import ENCODING, Record, parse_records, serialize_records


def _parse_pipe_fields(data: bytes) -> List[Tuple[str, str]]:
    """Parse a length-prefixed pipe-delimited stream (FileHeader/SectionKeys)."""
    if len(data) < 4:
        return []
    length = struct.unpack_from("<I", data, 0)[0] & 0x00FF_FFFF
    body = data[4 : 4 + length]
    if body.endswith(b"\x00"):
        body = body[:-1]
    fields: List[Tuple[str, str]] = []
    for chunk in body.decode(ENCODING).split("|"):
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        fields.append((key, value) if sep else (chunk, ""))
    return fields


_UID_ALPHABET = string.ascii_uppercase  # Altium UniqueIDs are 8 chars, A-Z only
_UID_LEN = 8


def _new_unique_id(existing, rng=None) -> str:
    """A fresh 8-char A-Z UniqueID not present in ``existing``."""
    source = rng or random
    for _ in range(10000):
        uid = "".join(source.choice(_UID_ALPHABET) for _ in range(_UID_LEN))
        if uid not in existing:
            return uid
    raise RuntimeError("exhausted attempts generating a unique UniqueID")  # pragma: no cover


class LibraryHeader:
    """Parsed ``FileHeader`` stream: library metadata and the component list."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self._fields = _parse_pipe_fields(raw)
        self._map = dict(self._fields)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._map.get(key, default)

    @property
    def header_string(self) -> str:
        return self._map.get("HEADER", "")

    @property
    def component_count(self) -> int:
        try:
            return int(self._map.get("CompCount", "0"))
        except ValueError:
            return 0

    @property
    def component_names(self) -> List[str]:
        """The declared ``LibRef`` names.

        For a well-formed library these are dense (``LibRef0..CompCount-1``) and
        returned in index order. Should a header be malformed (a gap in the
        indices, or entries beyond ``CompCount``), present names are still
        returned -- gaps are skipped and out-of-range entries appended -- so the
        result may not line up with ``CompCount``.
        """
        names: List[Optional[str]] = [None] * self.component_count
        extra: List[str] = []
        for key, value in self._fields:
            if key.startswith("LibRef"):
                idx_str = key[len("LibRef") :]
                if idx_str.isdigit():
                    idx = int(idx_str)
                    if 0 <= idx < len(names):
                        names[idx] = value
                        continue
                extra.append(value)
        ordered = [n for n in names if n is not None]
        ordered.extend(extra)
        return ordered


class Component:
    """A single component (one OLE storage) and its parsed records."""

    def __init__(
        self,
        storage_name: str,
        data_stream: bytes,
        pin_text_data: Optional[bytes] = None,
        extra_streams: Optional[Dict[str, bytes]] = None,
    ):
        self.storage_name = storage_name
        self.records: List[Record] = parse_records(data_stream)
        # None means the storage has no PinTextData stream at all; b"" means a
        # present-but-empty stream. The distinction is preserved on save.
        self.pin_text_data: Optional[bytes] = pin_text_data
        # Any streams inside the component storage beyond Data/PinTextData,
        # preserved verbatim so save() reproduces them.
        self.extra_streams: Dict[str, bytes] = dict(extra_streams or {})
        self._original_data = data_stream

    # -- the component header (RECORD=1) ------------------------------------
    @property
    def header(self) -> Optional[Record]:
        for r in self.records:
            if r.is_text and r.record_id == 1:
                return r
        return self.records[0] if self.records else None

    def _header_get(self, key: str, default: str = "") -> str:
        h = self.header
        return (h.get(key, default) if h else default) or default

    @property
    def name(self) -> str:
        """The component's ``LibReference`` -- its catalog name in Altium."""
        return self._header_get("LibReference") or self._header_get(
            "DesignItemId"
        ) or self.storage_name

    @property
    def design_item_id(self) -> str:
        return self._header_get("DesignItemId")

    @property
    def description(self) -> str:
        return self._header_get("ComponentDescription")

    @property
    def raw_part_count(self) -> int:
        """The ``PartCount`` field exactly as stored (parts + 1, Protel-style)."""
        try:
            return int(self._header_get("PartCount", "0") or "0")
        except ValueError:
            return 0

    @property
    def part_count(self) -> int:
        """Number of parts (gates) in the component.

        Altium/Protel stores ``PartCount`` as *parts + 1* (a single-part symbol
        stores ``2``), so the true count is ``PartCount - 1``.
        """
        raw = self.raw_part_count
        return raw - 1 if raw >= 1 else raw

    @property
    def pin_count(self) -> int:
        try:
            return int(self._header_get("AllPinCount", "0") or "0")
        except ValueError:
            return 0

    # -- record queries ------------------------------------------------------
    def records_of_type(self, record_id: int) -> List[Record]:
        return [r for r in self.records if r.is_text and r.record_id == record_id]

    @property
    def parameters(self) -> List[Record]:
        """Parameter records (RECORD=41)."""
        return self.records_of_type(41)

    def get_parameter(self, name: str) -> Optional[str]:
        for r in self.parameters:
            if r.get("Name") == name:
                return r.get("Text")
        return None

    def set_parameter(self, name: str, text: str) -> bool:
        """Update an existing parameter's ``Text``. Returns True if found."""
        for r in self.parameters:
            if r.get("Name") == name:
                r.set("Text", text)
                return True
        return False

    def has_parameter(self, name: str) -> bool:
        """True if a parameter with this ``Name`` exists (value or not)."""
        return any(r.get("Name") == name for r in self.parameters)

    @staticmethod
    def _validate_param_field(kind: str, value: str, *, allow_equals: bool) -> None:
        if "|" in value or "\x00" in value:
            raise ValueError(f"parameter {kind} may not contain '|' or NUL")
        if not allow_equals and "=" in value:
            raise ValueError(f"parameter {kind} may not contain '='")
        if not value.isascii():
            raise ValueError(
                f"parameter {kind} must be ASCII; non-ASCII text needs Altium's "
                f"%UTF8% dual-encoding, which is not yet supported"
            )

    def add_parameter(self, name: str, value: str, *, hidden: bool = True,
                      rng=None) -> Record:
        """Create a new RECORD=41 parameter and insert it into the component.

        Geometry (Location/FontID/Color) is cloned from an existing parameter so
        the new record looks native; a fresh component-unique UniqueID is
        generated. Does not check for an existing parameter of the same name --
        use :meth:`ensure_parameter` for the idempotent add.
        """
        name = str(name)
        value = str(value)
        self._validate_param_field("name", name, allow_equals=False)
        self._validate_param_field("value", value, allow_equals=True)

        params = self.parameters
        # Prefer cloning a hidden data parameter (not the special "Comment").
        src = next((r for r in params
                    if r.get("IsHidden") == "T" and r.get("Name") != "Comment"),
                   None)
        if src is None and params:
            src = params[0]  # e.g. components whose only param is "Comment"
        if src is not None:
            loc_x = src.get("Location.X", "-5")
            loc_y = src.get("Location.Y", "13")
            font_id = src.get("FontID", "1")
            color = src.get("Color", "8388608")
            owner = src.get("OwnerPartId", "-1")
        else:  # component with no parameters at all
            loc_x, loc_y, font_id, color, owner = "-5", "13", "1", "8388608", "-1"

        # IndexInSheet is a component-wide ordinal; take one past the current max.
        indices = []
        for r in self.records:
            v = r.get("IndexInSheet") if r.is_text else None
            if v is not None:
                try:
                    indices.append(int(v))
                except ValueError:
                    pass
        index_in_sheet = (max(indices) + 1) if indices else 0

        existing_uids = {r.get("UniqueID") for r in self.records
                         if r.get("UniqueID") is not None}
        unique_id = _new_unique_id(existing_uids, rng)

        pairs = [
            ("RECORD", "41"),
            ("IndexInSheet", str(index_in_sheet)),
            ("OwnerPartId", owner),
            ("Location.X", loc_x),
            ("Location.Y", loc_y),
            ("Color", color),
            ("FontID", font_id),
        ]
        if hidden:
            pairs.append(("IsHidden", "T"))
        pairs.append(("Text", value))
        pairs.append(("Name", name))
        pairs.append(("UniqueID", unique_id))
        rec = Record.from_fields(pairs)

        # Insert at the end of the contiguous RECORD=41 cluster that follows the
        # component header (before the graphics records); for components with no
        # leading cluster this lands right after the header.
        header = self.header
        start = 0
        if header is not None:
            for i, r in enumerate(self.records):
                if r is header:
                    start = i + 1
                    break
        i = start
        while (i < len(self.records) and self.records[i].is_text
               and self.records[i].record_id == 41):
            i += 1
        self.records.insert(i, rec)
        return rec

    def ensure_parameter(self, name: str, value: str, *, hidden: bool = True,
                         rng=None) -> bool:
        """Add the parameter only if absent. Returns True if it was added.

        Never overwrites an existing parameter of the same name (any value).
        """
        if self.has_parameter(name):
            return False
        self.add_parameter(name, value, hidden=hidden, rng=rng)
        return True

    # -- editing -------------------------------------------------------------
    # Fields that also appear in the library-level FileHeader/SectionKeys and in
    # the OLE storage name. Editing them only in the component's Data stream
    # would leave the library internally inconsistent (the component would be
    # unfindable by its new name), so renaming is refused until it is supported
    # end-to-end (FileHeader + SectionKeys + storage rename).
    _IDENTITY_FIELDS = ("LibReference", "DesignItemId")

    def set_header_field(self, key: str, value: str) -> None:
        h = self.header
        if h is None:
            raise ValueError(f"{self.storage_name}: no header record")
        if key in self._IDENTITY_FIELDS and value != h.get(key):
            raise ValueError(
                f"refusing to change {key!r}: it is mirrored in the library "
                f"FileHeader/SectionKeys and the OLE storage name. Renaming a "
                f"component is not yet supported (it would corrupt the library)."
            )
        h.set(key, value)

    @property
    def dirty(self) -> bool:
        return any(r.dirty for r in self.records)

    def to_data_stream(self) -> bytes:
        return serialize_records(self.records)

    def __repr__(self) -> str:
        return (
            f"<Component {self.name!r} storage={self.storage_name!r} "
            f"records={len(self.records)} pins={self.pin_count}>"
        )


class SchLib:
    """Read and edit an Altium ``.SchLib`` file.

    Usage::

        lib = SchLib("input/lib.SchLib")
        print(lib.component_names)
        comp = lib.get_component(lib.component_names[0])
        comp.set_header_field("ComponentDescription", "updated description")
        lib.save("output/lib.SchLib")
        lib.close()

    Or as a context manager::

        with SchLib("input/lib.SchLib") as lib:
            ...
    """

    ROOT_STREAMS = ("FileHeader", "SectionKeys", "Storage")

    def __init__(self, path: str):
        self.path = path
        self._ole = olefile.OleFileIO(path)
        # Anything past this point may raise (e.g. not a SchLib); make sure the
        # OLE file handle is closed rather than leaked if construction fails.
        try:
            header_bytes = self._read_root("FileHeader")
            if header_bytes is None:
                raise ValueError(f"{path}: no FileHeader stream; not a SchLib?")
            self.header = LibraryHeader(header_bytes)
            self._section_keys = self._parse_section_keys()
            self._storages = self._list_storages()
            self._components: Dict[str, Component] = {}  # storage_name -> Component
        except BaseException:
            self._ole.close()
            self._ole = None
            raise

    # -- context manager -----------------------------------------------------
    def __enter__(self) -> "SchLib":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._ole is not None:
            self._ole.close()
            self._ole = None

    # -- low-level stream access --------------------------------------------
    def _read_root(self, name: str) -> Optional[bytes]:
        if self._ole.exists(name):
            with self._ole.openstream(name) as s:
                return s.read()
        return None

    def _read_stream(self, path_parts: List[str]) -> bytes:
        with self._ole.openstream(path_parts) as s:
            return s.read()

    def _list_storages(self) -> List[str]:
        names = []
        for entry in self._ole.listdir(streams=False, storages=True):
            if len(entry) == 1:
                names.append(entry[0])
        return names

    def _parse_section_keys(self) -> Dict[str, str]:
        """Return ``{LibRef: SectionKey}`` from the SectionKeys stream."""
        raw = self._read_root("SectionKeys")
        if not raw:
            return {}
        fields = _parse_pipe_fields(raw)
        librefs: Dict[int, str] = {}
        keys: Dict[int, str] = {}
        for key, value in fields:
            if key.startswith("LibRef") and key[len("LibRef") :].isdigit():
                librefs[int(key[len("LibRef") :])] = value
            elif key.startswith("SectionKey") and key[len("SectionKey") :].isdigit():
                keys[int(key[len("SectionKey") :])] = value
        return {librefs[i]: keys[i] for i in librefs if i in keys}

    # -- name resolution -----------------------------------------------------
    # OLE / CFBF storage names may not contain these reserved characters, nor
    # exceed 31 UTF-16 code units. Altium sanitizes a LibReference into a
    # storage name by replacing each reserved character and truncating.
    _RESERVED_CHARS = "/\\:!"
    _MAX_STORAGE_NAME = 31

    @classmethod
    def _sanitize(cls, name: str) -> str:
        for ch in cls._RESERVED_CHARS:
            name = name.replace(ch, "_")
        return name[: cls._MAX_STORAGE_NAME]

    def storage_name_for(self, lib_reference: str) -> Optional[str]:
        """Resolve a component's ``LibReference`` to its OLE storage name.

        ``SectionKeys`` entries are authoritative and take precedence: Altium
        records them precisely to disambiguate names that would otherwise
        collide after truncation (e.g. two parts sharing a 31-char prefix, one
        of which Altium renamed). Only when no SectionKeys entry applies do we
        fall back to sanitizing/truncating the LibReference itself.
        """
        candidates: List[str] = []
        section_key = self._section_keys.get(lib_reference)
        if section_key is not None:
            # SectionKey value may still contain reserved chars (stored raw).
            candidates += [self._sanitize(section_key), section_key,
                           section_key[: self._MAX_STORAGE_NAME]]
        candidates += [lib_reference, self._sanitize(lib_reference),
                       lib_reference[: self._MAX_STORAGE_NAME]]
        for cand in candidates:
            if cand in self._storages:
                return cand
        return None

    # -- component access ----------------------------------------------------
    @property
    def component_names(self) -> List[str]:
        """Authoritative component names (LibReference) from the FileHeader."""
        return self.header.component_names

    @property
    def declared_count(self) -> int:
        """The ``CompCount`` field from the FileHeader, exactly as stored."""
        return self.header.component_count

    @property
    def storage_names(self) -> List[str]:
        return list(self._storages)

    def _load_component(self, storage_name: str) -> Component:
        if storage_name in self._components:
            return self._components[storage_name]
        data = self._read_stream([storage_name, "Data"])
        pin_text: Optional[bytes] = None  # None = stream absent (vs. empty)
        if self._ole.exists([storage_name, "PinTextData"]):
            pin_text = self._read_stream([storage_name, "PinTextData"])
        extra: Dict[str, bytes] = {}
        for entry in self._ole.listdir(streams=True, storages=False):
            if len(entry) == 2 and entry[0] == storage_name and entry[1] not in (
                "Data",
                "PinTextData",
            ):
                extra[entry[1]] = self._read_stream(list(entry))
        comp = Component(storage_name, data, pin_text, extra)
        self._components[storage_name] = comp
        return comp

    def get_component(self, name: str) -> Component:
        """Look up a component by LibReference (preferred) or storage name."""
        storage = self.storage_name_for(name)
        if storage is None and name in self._storages:
            storage = name
        if storage is None:
            raise KeyError(f"component not found: {name!r}")
        return self._load_component(storage)

    def has_component(self, name: str) -> bool:
        try:
            return self.storage_name_for(name) is not None or name in self._storages
        except Exception:  # pragma: no cover - defensive
            return False

    @property
    def components(self) -> List[Component]:
        """All components, loaded in FileHeader (declared) order."""
        result = []
        for name in self.component_names:
            storage = self.storage_name_for(name)
            if storage is not None:
                result.append(self._load_component(storage))
        return result

    def __len__(self) -> int:
        # Number of names actually enumerated, so len(lib) == len(list(names)).
        # For well-formed libraries this equals declared_count (CompCount).
        return len(self.component_names)

    def __iter__(self):
        return iter(self.components)

    # -- batch editing -------------------------------------------------------
    def add_parameter_to_all(self, name: str, value: str, *, hidden: bool = True,
                             rng=None) -> dict:
        """Add parameter ``name``=``value`` to every component that lacks it.

        Idempotent: components that already have the parameter are left
        untouched. Does not save -- the caller writes the result with
        :meth:`save`. Returns a summary dict with ``added``/``skipped`` name
        lists and their counts plus ``total``.
        """
        added: List[str] = []
        skipped: List[str] = []
        for comp in self.components:
            if comp.ensure_parameter(name, value, hidden=hidden, rng=rng):
                added.append(comp.name)
            else:
                skipped.append(comp.name)
        return {
            "added": added,
            "skipped": skipped,
            "added_count": len(added),
            "skipped_count": len(skipped),
            "total": len(added) + len(skipped),
        }

    # -- saving --------------------------------------------------------------
    def _build_stream_tree(self) -> dict:
        """Capture every stream from the source, substituting edited ones."""
        from collections import OrderedDict

        tree: "OrderedDict[str, object]" = OrderedDict()

        # Root streams first, in canonical order.
        for name in self.ROOT_STREAMS:
            raw = self._read_root(name)
            if raw is not None:
                tree[name] = raw

        # Any other root-level streams we did not name explicitly.
        for entry in self._ole.listdir(streams=True, storages=False):
            if len(entry) == 1 and entry[0] not in tree:
                tree[entry[0]] = self._read_stream(list(entry))

        # Component storages.
        for storage_name in self._storages:
            comp = self._components.get(storage_name)
            storage_tree: "OrderedDict[str, bytes]" = OrderedDict()
            if comp is not None:
                storage_tree["Data"] = comp.to_data_stream()
                if comp.pin_text_data is not None:  # preserve empty streams too
                    storage_tree["PinTextData"] = comp.pin_text_data
                for sname, sbytes in comp.extra_streams.items():
                    storage_tree[sname] = sbytes
            else:
                # Untouched component: copy its streams verbatim.
                for entry in self._ole.listdir(streams=True, storages=False):
                    if len(entry) == 2 and entry[0] == storage_name:
                        storage_tree[entry[1]] = self._read_stream(list(entry))
            tree[storage_name] = storage_tree

        return tree

    def save(self, path: str) -> None:
        """Write the (possibly edited) library to ``path`` as a new SchLib.

        The write is atomic (temp file + :func:`os.replace`), so an existing
        file is only replaced once the new one is fully committed. Saving in
        place (``path == self.path``) is supported: the source handle is
        released for the swap and reopened on the freshly-written file.
        """
        import os

        from .writer import write_compound_file

        if self._ole is None:
            raise ValueError("library is closed")

        # Materialize every stream (reads through the open handle) BEFORE any
        # handle juggling, so the in-place case has no lingering dependency.
        tree = self._build_stream_tree()
        root_clsid = getattr(self._ole.root, "clsid", None)

        in_place = (
            os.path.normcase(os.path.abspath(path))
            == os.path.normcase(os.path.abspath(self.path))
        )
        if in_place:
            self._ole.close()
            self._ole = None
        try:
            write_compound_file(path, tree, root_clsid=root_clsid)
        finally:
            if in_place:
                # Reopen so the object stays usable. On success this points at
                # the new file; on failure the original is intact (atomic write).
                self._ole = olefile.OleFileIO(self.path)

    def __repr__(self) -> str:
        return (
            f"<SchLib {self.path!r} components={self.header.component_count} "
            f"storages={len(self._storages)}>"
        )

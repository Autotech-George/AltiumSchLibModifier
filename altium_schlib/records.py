"""Low-level Altium record framing and text-record (parameter) parsing.

An Altium schematic ``Data`` stream is a flat sequence of *record blocks*.
Each block is framed by a 4-byte little-endian header::

    header = uint32
    length = header & 0x00FF_FFFF   # lower 24 bits: payload length in bytes
    flag   = header >> 24           # top byte: block kind

Two block kinds are observed in SchLib files:

* ``flag == 0`` -- a *text* record: a pipe-delimited ``|KEY=VALUE|...`` ASCII
  string, terminated by a single ``\\x00`` byte (the null is counted in
  ``length``). The leading ``|`` produces an empty first field when split.
* ``flag == 1`` -- a *binary pin* record (schematic ``RECORD=2`` pins are
  stored in a packed binary layout). We treat these as opaque blobs and
  preserve them byte-for-byte.

This module never guesses: unmodified records are re-emitted from their
original bytes, so parsing then serializing an untouched stream is a byte-exact
round-trip. Only records whose fields were edited are rebuilt.
"""

from __future__ import annotations

import struct
from typing import Iterator, List, Optional, Tuple

# Byte encoding. Altium writes text in the machine's ANSI code page. latin-1 is
# a total, lossless byte<->str mapping (every byte 0x00-0xFF round-trips), so it
# never raises and never changes bytes -- ideal for exact round-tripping.
ENCODING = "latin-1"

FLAG_TEXT = 0
FLAG_PIN = 1

_LENGTH_MASK = 0x00FF_FFFF
_MAX_LENGTH = _LENGTH_MASK


def iter_record_blocks(data: bytes) -> Iterator[Tuple[int, bytes]]:
    """Yield ``(flag, payload)`` for each record block in ``data``.

    Raises ``ValueError`` if the framing does not consume the stream exactly,
    which would indicate a malformed stream or a wrong parsing assumption.
    """
    off = 0
    n = len(data)
    while off < n:
        if off + 4 > n:
            raise ValueError(
                f"truncated record header at offset {off} (stream len {n})"
            )
        header = struct.unpack_from("<I", data, off)[0]
        length = header & _LENGTH_MASK
        flag = header >> 24
        start = off + 4
        end = start + length
        if end > n:
            raise ValueError(
                f"record at offset {off} claims length {length} but only "
                f"{n - start} bytes remain"
            )
        yield flag, data[start:end]
        off = end
    if off != n:  # pragma: no cover - defensive; loop condition guarantees this
        raise ValueError(f"framing overran: consumed {off}, stream len {n}")


def serialize_record_block(flag: int, payload: bytes) -> bytes:
    """Inverse of one :func:`iter_record_blocks` step: frame ``payload``."""
    length = len(payload)
    if length > _MAX_LENGTH:
        raise ValueError(
            f"payload length {length} exceeds 24-bit maximum {_MAX_LENGTH}"
        )
    if not (0 <= flag <= 0xFF):
        raise ValueError(f"flag {flag} out of byte range")
    header = (flag << 24) | length
    return struct.pack("<I", header) + payload


def parse_text_payload(payload: bytes) -> Tuple[List[Tuple[str, str]], bool]:
    """Parse a text-record payload into ``([(key, value), ...], had_null)``.

    Field order and duplicate keys are preserved by returning a list of pairs.
    ``had_null`` records whether the payload ended with the ``\\x00``
    terminator so serialization can reproduce it exactly.
    """
    had_null = payload.endswith(b"\x00")
    body = payload[:-1] if had_null else payload
    text = body.decode(ENCODING)
    fields: List[Tuple[str, str]] = []
    # A leading '|' yields an empty first element; skip empties rather than
    # emitting phantom ('', '') pairs.
    for chunk in text.split("|"):
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        fields.append((key, value) if sep else (chunk, ""))
    return fields, had_null


def serialize_text_payload(fields: List[Tuple[str, str]], had_null: bool) -> bytes:
    """Inverse of :func:`parse_text_payload`.

    Produces ``|k=v|k=v|...`` (leading pipe reproduced) plus the trailing null
    when ``had_null`` is set.
    """
    text = "".join(f"|{key}={value}" for key, value in fields)
    payload = text.encode(ENCODING)
    if had_null:
        payload += b"\x00"
    return payload


class Record:
    """A single record block within a Data stream.

    Text records expose their parameters as ordered ``(key, value)`` pairs and
    can be edited; binary (pin) records are preserved as opaque bytes. A record
    only rebuilds its bytes when it has actually been modified, so untouched
    records round-trip exactly.
    """

    __slots__ = ("flag", "_raw", "_fields", "_had_null", "_dirty")

    def __init__(self, flag: int, payload: bytes):
        self.flag = flag
        self._raw = payload
        self._dirty = False
        self._fields: Optional[List[Tuple[str, str]]] = None
        self._had_null = False
        if flag == FLAG_TEXT:
            self._fields, self._had_null = parse_text_payload(payload)

    # -- introspection -------------------------------------------------------
    @property
    def is_text(self) -> bool:
        return self.flag == FLAG_TEXT

    @property
    def is_binary(self) -> bool:
        return self.flag != FLAG_TEXT

    @property
    def record_id(self) -> Optional[int]:
        """The numeric ``RECORD=`` type of a text record, else ``None``."""
        if self._fields is None:
            return None
        raw = self.get("RECORD")
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    @property
    def fields(self) -> List[Tuple[str, str]]:
        """The ordered ``(key, value)`` pairs (text records only)."""
        if self._fields is None:
            raise TypeError("binary record has no text fields")
        return self._fields

    # -- field access --------------------------------------------------------
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if self._fields is None:
            return default
        for k, v in self._fields:
            if k == key:
                return v
        return default

    def set(self, key: str, value: str) -> None:
        """Set (or append) a field's value, marking the record dirty."""
        if self._fields is None:
            raise TypeError("cannot set fields on a binary record")
        value = str(value)
        for i, (k, _) in enumerate(self._fields):
            if k == key:
                if self._fields[i][1] != value:
                    self._fields[i] = (k, value)
                    self._dirty = True
                return
        self._fields.append((key, value))
        self._dirty = True

    def remove(self, key: str) -> bool:
        if self._fields is None:
            raise TypeError("cannot remove fields on a binary record")
        before = len(self._fields)
        self._fields = [(k, v) for k, v in self._fields if k != key]
        if len(self._fields) != before:
            self._dirty = True
            return True
        return False

    def as_dict(self) -> dict:
        """Fields as a plain dict (last value wins on duplicate keys)."""
        if self._fields is None:
            return {}
        return dict(self._fields)

    # -- serialization -------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def payload(self) -> bytes:
        if self._dirty and self._fields is not None:
            return serialize_text_payload(self._fields, self._had_null)
        return self._raw

    def to_bytes(self) -> bytes:
        return serialize_record_block(self.flag, self.payload)

    def __repr__(self) -> str:
        if self.is_text:
            rid = self.record_id
            return f"<Record text RECORD={rid} fields={len(self._fields or [])}>"
        return f"<Record binary flag={self.flag} bytes={len(self._raw)}>"


def parse_records(data: bytes) -> List[Record]:
    """Parse a Data stream into a list of :class:`Record`."""
    return [Record(flag, payload) for flag, payload in iter_record_blocks(data)]


def serialize_records(records: List[Record]) -> bytes:
    """Serialize a list of records back into a Data-stream byte string."""
    return b"".join(r.to_bytes() for r in records)

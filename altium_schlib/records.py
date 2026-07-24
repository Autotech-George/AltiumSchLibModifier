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

Round-trip fidelity is the central guarantee. A text payload is modelled as the
raw list of ``|``-separated *chunks* (``payload.split(b"|")``), because
``"|".join(chunks)`` is an exact inverse of the split for *any* input --
including consecutive pipes (``||``), a trailing ``|``, a lone ``|``, and bare
tokens that contain no ``=``. Editing a single field rewrites only that chunk;
every other byte (empty fields, unusual encodings, field order) is preserved.
Unmodified records are re-emitted from their original bytes, so parsing then
serializing an untouched stream is byte-identical.
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


def parse_text_payload(payload: bytes) -> Tuple[List[str], bool]:
    """Parse a text-record payload into ``(chunks, had_null)``.

    ``chunks`` is ``payload.split("|")`` (minus the trailing null), preserving
    every field exactly -- including empty ones. ``had_null`` records whether
    the payload ended with the ``\\x00`` terminator so serialization can
    reproduce it.
    """
    had_null = payload.endswith(b"\x00")
    body = payload[:-1] if had_null else payload
    chunks = body.decode(ENCODING).split("|")
    return chunks, had_null


def serialize_text_payload(chunks: List[str], had_null: bool) -> bytes:
    """Inverse of :func:`parse_text_payload`: ``"|".join(chunks)`` (+ null)."""
    payload = "|".join(chunks).encode(ENCODING)
    if had_null:
        payload += b"\x00"
    return payload


def _split_chunk(chunk: str) -> Tuple[str, Optional[str]]:
    """Split a chunk into ``(key, value)``; ``value`` is ``None`` if no ``=``.

    Distinguishing "no ``=``" (value ``None``) from "empty value" (value ``""``)
    matters for exact round-tripping: a bare token must not gain a spurious
    ``=`` when its record is re-serialized.
    """
    key, sep, value = chunk.partition("=")
    return (key, value) if sep else (chunk, None)


class Record:
    """A single record block within a Data stream.

    Text records expose their parameters as ``key=value`` fields and can be
    edited; binary (pin) records are preserved as opaque bytes. A record only
    rebuilds its bytes when it has actually been modified, so untouched records
    round-trip exactly.
    """

    __slots__ = ("flag", "_raw", "_chunks", "_had_null", "_dirty")

    def __init__(self, flag: int, payload: bytes):
        self.flag = flag
        self._raw = payload
        self._dirty = False
        self._chunks: Optional[List[str]] = None
        self._had_null = False
        if flag == FLAG_TEXT:
            self._chunks, self._had_null = parse_text_payload(payload)

    @classmethod
    def from_fields(cls, pairs, *, had_null: bool = True) -> "Record":
        """Synthesize a new (dirty) text record from ordered ``(key, value)`` pairs.

        For records that did not exist in the source (e.g. a new parameter). The
        leading ``""`` chunk reproduces the leading ``|``; the record is marked
        dirty so :attr:`payload` serializes it via :func:`serialize_text_payload`
        to ``|k=v|k=v|...\\x00`` -- byte-identical to an equivalent parsed record.
        """
        rec = object.__new__(cls)
        rec.flag = FLAG_TEXT
        rec._raw = b""
        rec._chunks = [""] + [f"{key}={value}" for key, value in pairs]
        rec._had_null = had_null
        rec._dirty = True
        return rec

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
        raw = self.get("RECORD")
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    @property
    def fields(self) -> Tuple[Tuple[str, str], ...]:
        """An immutable snapshot of the ``key=value`` fields (text records).

        Returns a tuple so accidental in-place mutation fails loudly instead of
        being silently discarded -- edits must go through :meth:`set` /
        :meth:`remove`. Bare tokens and empty fields are not included here but
        are still preserved on serialization.
        """
        if self._chunks is None:
            raise TypeError("binary record has no text fields")
        out: List[Tuple[str, str]] = []
        for chunk in self._chunks:
            key, value = _split_chunk(chunk)
            if value is not None:
                out.append((key, value))
        return tuple(out)

    # -- field access --------------------------------------------------------
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if self._chunks is None:
            return default
        for chunk in self._chunks:
            k, v = _split_chunk(chunk)
            if v is not None and k == key:
                return v
        return default

    def set(self, key: str, value: str) -> None:
        """Set (or append) a field's value, marking the record dirty.

        Rejects ``|`` and NUL in the key or value: both are structural
        delimiters of the pipe-format, so allowing them would silently corrupt
        the record on serialization (the value would split into extra fields).
        """
        if self._chunks is None:
            raise TypeError("cannot set fields on a binary record")
        value = str(value)
        for part, label in ((key, "key"), (value, "value")):
            if "|" in part or "\x00" in part:
                raise ValueError(f"record field {label} may not contain '|' or NUL")
        if "=" in key:
            raise ValueError("record field key may not contain '='")
        new_chunk = f"{key}={value}"
        for i, chunk in enumerate(self._chunks):
            k, v = _split_chunk(chunk)
            if v is not None and k == key:
                if chunk != new_chunk:
                    self._chunks[i] = new_chunk
                    self._dirty = True
                return
        self._chunks.append(new_chunk)
        self._dirty = True

    def remove(self, key: str) -> bool:
        if self._chunks is None:
            raise TypeError("cannot remove fields on a binary record")
        kept = []
        removed = False
        for chunk in self._chunks:
            k, v = _split_chunk(chunk)
            if v is not None and k == key:
                removed = True
                continue
            kept.append(chunk)
        if removed:
            self._chunks = kept
            self._dirty = True
        return removed

    def as_dict(self) -> dict:
        """Fields as a plain dict (last value wins on duplicate keys)."""
        return dict(self.fields)

    # -- serialization -------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def payload(self) -> bytes:
        if self._dirty and self._chunks is not None:
            return serialize_text_payload(self._chunks, self._had_null)
        return self._raw

    def to_bytes(self) -> bytes:
        return serialize_record_block(self.flag, self.payload)

    def __repr__(self) -> str:
        if self.is_text:
            n = len(self._chunks) if self._chunks is not None else 0
            return f"<Record text RECORD={self.record_id} chunks={n}>"
        return f"<Record binary flag={self.flag} bytes={len(self._raw)}>"


def parse_records(data: bytes) -> List[Record]:
    """Parse a Data stream into a list of :class:`Record`."""
    return [Record(flag, payload) for flag, payload in iter_record_blocks(data)]


def serialize_records(records: List[Record]) -> bytes:
    """Serialize a list of records back into a Data-stream byte string."""
    return b"".join(r.to_bytes() for r in records)

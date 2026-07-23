"""Test suite for the altium_schlib parser/editor.

The heavyweight tests exercise the real sample library in ./input; they are
skipped automatically if it is missing. Save/edit tests additionally require
pywin32 (Windows) and are skipped elsewhere.

Run:  pytest -q
"""

from __future__ import annotations

import glob
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from altium_schlib import SchLib  # noqa: E402
from altium_schlib.records import (  # noqa: E402
    Record,
    parse_records,
    serialize_record_block,
    serialize_records,
)

EXPECTED_KLEMA = [f"CON_KLEMA_{n}" for n in range(2, 13)]  # the 11 required


def _sample_path():
    matches = sorted(glob.glob(os.path.join(ROOT, "input", "*.SchLib")))
    return matches[0] if matches else None


def _has_pywin32() -> bool:
    try:
        import pythoncom  # noqa: F401
        import win32com.storagecon  # noqa: F401

        return True
    except Exception:
        return False


SAMPLE = _sample_path()
needs_sample = pytest.mark.skipif(SAMPLE is None, reason="no sample .SchLib in ./input")
needs_pywin32 = pytest.mark.skipif(
    not _has_pywin32(), reason="pywin32 (Windows Structured Storage) not available"
)


@pytest.fixture(scope="module")
def lib():
    with SchLib(SAMPLE) as l:
        yield l


# --------------------------------------------------------------------------
# Unit tests: record framing (no sample needed)
# --------------------------------------------------------------------------
def test_text_record_roundtrip_unit():
    payload = b"|RECORD=1|LibReference=FOO|PartCount=2\x00"
    block = serialize_record_block(0, payload)
    recs = parse_records(block)
    assert len(recs) == 1
    r = recs[0]
    assert r.is_text and r.record_id == 1
    assert r.get("LibReference") == "FOO"
    # Untouched -> byte-exact
    assert serialize_records(recs) == block


def test_binary_record_preserved_unit():
    payload = bytes([0x02, 0x00, 0x01, 0xFF, 0x10, 0x20])
    block = serialize_record_block(1, payload)
    recs = parse_records(block)
    assert recs[0].is_binary
    assert serialize_records(recs) == block


def test_edit_marks_dirty_and_reserializes():
    payload = b"|RECORD=1|LibReference=FOO\x00"
    r = parse_records(serialize_record_block(0, payload))[0]
    assert not r.dirty
    r.set("LibReference", "BAR")
    assert r.dirty
    assert r.get("LibReference") == "BAR"
    assert b"LibReference=BAR" in r.to_bytes()
    # trailing null preserved
    assert r.payload.endswith(b"\x00")


def test_length_flag_packing():
    # flag in top byte, length in low 24 bits
    block = serialize_record_block(1, b"abc")
    assert block[:4] == bytes([3, 0, 0, 1])  # len=3 (LE), flag=1


# -- regression: empty-field / bare-token round-trip (the HIGH bug) ---------
@pytest.mark.parametrize("payload", [
    b"|A=1|||B=2\x00",          # consecutive pipes (empty fields)
    b"|A=1|\x00",               # trailing pipe
    b"|\x00",                    # lone pipe
    b"|RECORD=1|FLAG|X=1\x00",  # bare token, no '='
    b"|K=has=equals|Y=2\x00",   # value containing '='
    b"|K=v\xc2\xb0C|Z=3\x00",   # non-ASCII bytes (latin-1)
])
def test_empty_and_odd_fields_survive_edit(payload):
    from altium_schlib.records import parse_text_payload, serialize_text_payload

    block = serialize_record_block(0, payload)
    # Pure parse->serialize is byte-exact for any payload.
    chunks, had_null = parse_text_payload(payload)
    assert serialize_text_payload(chunks, had_null) == payload
    # And after editing an unrelated field, nothing else is disturbed: only the
    # edited field's bytes change; empty fields / bare tokens are preserved.
    r = parse_records(block)[0]
    if r.get("Y") is not None:
        r.set("Y", "99")
    else:
        r.set("NEW", "1")
    out = r.to_bytes()[4:]  # strip 4-byte header
    # Reconstruct expectation by editing the raw string the same way.
    assert b"||" in out if b"||" in payload else True
    assert (b"FLAG|" in out) if (b"|FLAG|" in payload) else True
    assert b"FLAG=" not in out  # no spurious '=' introduced


def test_no_equals_field_not_given_spurious_equals():
    block = serialize_record_block(0, b"|RECORD=1|FLAG|X=1\x00")
    r = parse_records(block)[0]
    r.set("X", "2")
    assert r.to_bytes()[4:] == b"|RECORD=1|FLAG|X=2\x00"


def test_fields_property_is_immutable_snapshot():
    block = serialize_record_block(0, b"|RECORD=1|A=1\x00")
    r = parse_records(block)[0]
    fields = r.fields
    assert isinstance(fields, tuple)
    with pytest.raises((AttributeError, TypeError)):
        fields.append(("B", "2"))  # type: ignore[attr-defined]
    # Editing must go through set(); the snapshot did not silently swallow it.
    r.set("B", "2")
    assert r.get("B") == "2"


# --------------------------------------------------------------------------
# Integration tests against the real sample library
# --------------------------------------------------------------------------
@needs_sample
def test_library_opens_with_expected_count(lib):
    assert len(lib) == 714
    assert len(lib.component_names) == 714
    assert "Schematic Library" in lib.header.header_string


@needs_sample
def test_klema_family_present(lib):
    """The acceptance criterion: CON_KLEMA_2 .. CON_KLEMA_12 (11 components)."""
    for name in EXPECTED_KLEMA:
        assert lib.has_component(name), f"missing {name}"
        assert lib.get_component(name).name == name
    assert len(EXPECTED_KLEMA) == 11


@needs_sample
def test_klema20_is_distinct(lib):
    """CON_KLEMA_20 must not be mistaken for a member of the 2..12 range."""
    assert lib.has_component("CON_KLEMA_20")
    assert "CON_KLEMA_20" not in EXPECTED_KLEMA
    assert lib.get_component("CON_KLEMA_2").name == "CON_KLEMA_2"
    assert lib.get_component("CON_KLEMA_20").name == "CON_KLEMA_20"


@needs_sample
def test_all_data_streams_roundtrip_byte_exact(lib):
    """Parsing then serializing every Data stream reproduces it exactly."""
    failures = []
    for storage in lib.storage_names:
        raw = lib._read_stream([storage, "Data"])
        if serialize_records(parse_records(raw)) != raw:
            failures.append(storage)
    assert not failures, f"{len(failures)} non-round-tripping streams: {failures[:5]}"


@needs_sample
def test_name_resolution_is_bijective(lib):
    """All 714 declared names resolve to 714 distinct storages."""
    used = {}
    for name in lib.component_names:
        storage = lib.storage_name_for(name)
        assert storage is not None, f"unresolved: {name}"
        used.setdefault(storage, []).append(name)
    collisions = {k: v for k, v in used.items() if len(v) > 1}
    assert not collisions, f"storage collisions: {collisions}"
    assert len(used) == 714


@needs_sample
def test_slash_in_name_resolves(lib):
    """Names with OLE-reserved '/' map to sanitized '_' storages."""
    c = lib.get_component("SMD_VREG_LIN_MCP1825ST-3302E/DB")
    assert c.storage_name == "SMD_VREG_LIN_MCP1825ST-3302E_DB"
    assert c.name == "SMD_VREG_LIN_MCP1825ST-3302E/DB"


# --------------------------------------------------------------------------
# Save / edit round-trip (needs pywin32)
# --------------------------------------------------------------------------
@needs_sample
@needs_pywin32
def test_lossless_save(tmp_path):
    import olefile

    out = str(tmp_path / "roundtrip.SchLib")
    with SchLib(SAMPLE) as lib:
        lib.save(out)

    def streams(path):
        o = olefile.OleFileIO(path)
        d = {tuple(e): o.openstream(e).read()
             for e in o.listdir(streams=True, storages=False)}
        clsid = o.root.clsid
        o.close()
        return d, clsid

    a, ca = streams(SAMPLE)
    b, cb = streams(out)
    assert set(a) == set(b)
    assert ca == cb  # Altium CLSID preserved
    mismatched = [k for k in a if a[k] != b[k]]
    assert not mismatched, f"{len(mismatched)} streams differ"


@needs_sample
@needs_pywin32
def test_save_after_loading_all_is_lossless(tmp_path):
    """Loading every component (the serialize path) still saves byte-identically.

    This exercises Component.to_data_stream() for all 714 components -- including
    the ~284 that have no PinTextData stream -- not just the verbatim copy path.
    """
    import olefile

    out = str(tmp_path / "allloaded.SchLib")
    with SchLib(SAMPLE) as lib:
        _ = lib.components  # force-load all components
        lib.save(out)

    def streams(path):
        o = olefile.OleFileIO(path)
        d = {tuple(e): o.openstream(e).read()
             for e in o.listdir(streams=True, storages=False)}
        o.close()
        return d

    before, after = streams(SAMPLE), streams(out)
    assert set(before) == set(after)
    mismatched = [k for k in before if before[k] != after[k]]
    assert not mismatched, f"{len(mismatched)} streams differ"


@needs_sample
@needs_pywin32
def test_edit_roundtrip_is_isolated(tmp_path):
    import olefile

    out = str(tmp_path / "edited.SchLib")
    marker = "Edited by altium_schlib test"
    with SchLib(SAMPLE) as lib:
        lib.get_component("CON_KLEMA_2").set_header_field(
            "ComponentDescription", marker
        )
        lib.save(out)

    with SchLib(out) as lib2:
        assert lib2.get_component("CON_KLEMA_2").description == marker
        assert len(lib2) == 714
        for name in EXPECTED_KLEMA:
            assert lib2.has_component(name)

    def streams(path):
        o = olefile.OleFileIO(path)
        d = {tuple(e): o.openstream(e).read()
             for e in o.listdir(streams=True, storages=False)}
        o.close()
        return d

    before, after = streams(SAMPLE), streams(out)
    changed = [k for k in before if before[k] != after.get(k)]
    assert changed == [("CON_KLEMA_2", "Data")], changed


@needs_sample
@needs_pywin32
def test_edit_record_with_empty_fields_preserves_them(tmp_path):
    """Regression: editing a parameter in a record containing '||' must not
    drop the empty fields (the HIGH-severity data-loss bug)."""
    import olefile

    out = str(tmp_path / "empty_fields.SchLib")
    target = "CON_B2B_FEM_53748-0504"  # has a RECORD=41 with '|||'
    with SchLib(SAMPLE) as lib:
        comp = lib.get_component(target)
        # sanity: this component really does contain a '||' text record
        assert any(b"||" in r.payload for r in comp.records if r.is_text)
        assert comp.set_parameter("Min Operating Temperature", "-45")
        lib.save(out)

    def data(path, storage):
        o = olefile.OleFileIO(path)
        b = o.openstream([storage, "Data"]).read()
        o.close()
        return b

    after = data(out, target)
    assert b"||" in after           # empty fields survived
    assert b"Text=-45" in after     # our edit landed


@needs_sample
def test_len_matches_component_names(lib):
    assert len(lib) == len(lib.component_names)
    assert lib.declared_count == 714


@needs_sample
def test_set_header_field_refuses_rename(lib):
    comp = lib.get_component("CON_KLEMA_3")
    with pytest.raises(ValueError):
        comp.set_header_field("LibReference", "SOMETHING_ELSE")
    # non-identity fields are still editable
    comp.set_header_field("ComponentDescription", "ok")
    assert comp.description == "ok"


@needs_pywin32
def test_init_closes_handle_on_bad_file(tmp_path):
    """A valid OLE2 file lacking FileHeader must raise AND not leak the handle."""
    import olefile

    from altium_schlib.writer import write_compound_file

    bad = str(tmp_path / "not_a_schlib.bin")
    write_compound_file(bad, {"SomethingElse": b"x"})
    with pytest.raises(ValueError):
        SchLib(bad)
    # If the handle leaked, this reopen+read would fail on Windows.
    o = olefile.OleFileIO(bad)
    o.close()
    os.remove(bad)  # would raise PermissionError if a handle were still open


@needs_pywin32
def test_empty_pintextdata_stream_preserved(tmp_path):
    """A present-but-empty PinTextData stream survives get_component()+save()."""
    import olefile

    from altium_schlib.writer import write_compound_file

    src = str(tmp_path / "empty_pin.SchLib")
    header = b"|HEADER=X|CompCount=1|LibRef0=TESTCOMP\x00"
    header = (len(header)).to_bytes(4, "little") + header
    data = b"|RECORD=1|LibReference=TESTCOMP\x00"
    data = (len(data)).to_bytes(4, "little") + data
    write_compound_file(src, {
        "FileHeader": header,
        "TESTCOMP": {"Data": data, "PinTextData": b""},
    })

    out = str(tmp_path / "empty_pin_out.SchLib")
    with SchLib(src) as lib:
        _ = lib.get_component("TESTCOMP")  # load -> exercises the tree builder
        lib.save(out)

    o = olefile.OleFileIO(out)
    assert o.exists(["TESTCOMP", "PinTextData"])  # empty stream not dropped
    assert o.get_size(["TESTCOMP", "PinTextData"]) == 0
    o.close()


@needs_sample
@needs_pywin32
def test_in_place_save_is_atomic(tmp_path):
    """save(self.path) works, stays usable, and preserves untouched streams."""
    import olefile
    import shutil

    work = str(tmp_path / "inplace.SchLib")
    shutil.copyfile(SAMPLE, work)

    lib = SchLib(work)
    lib.get_component("CON_KLEMA_4").set_header_field(
        "ComponentDescription", "in-place edit"
    )
    lib.save(work)  # same path
    # Object remains usable after the in-place swap.
    assert lib.get_component("CON_KLEMA_4").description == "in-place edit"
    assert len(lib) == 714
    lib.close()

    with SchLib(work) as reopened:
        assert reopened.get_component("CON_KLEMA_4").description == "in-place edit"

    def streams(path):
        o = olefile.OleFileIO(path)
        d = {tuple(e): o.openstream(e).read()
             for e in o.listdir(streams=True, storages=False)}
        o.close()
        return d

    before, after = streams(SAMPLE), streams(work)
    changed = [k for k in before if before[k] != after.get(k)]
    assert changed == [("CON_KLEMA_4", "Data")], changed

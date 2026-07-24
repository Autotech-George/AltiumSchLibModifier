"""Test suite for the altium_schlib parser/editor.

The heavyweight tests exercise whatever ``.SchLib`` is present in ./input; they
are skipped automatically if there is none. No specific component names are
hard-coded -- test subjects are discovered from the library at runtime -- so the
suite works with any library and reveals nothing about a particular one. Save/
edit tests additionally require pywin32 (Windows) and are skipped elsewhere.

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


# -- helpers: discover test subjects from the library, name-agnostic --------
def _first_name(lib: SchLib) -> str:
    return lib.component_names[0]


def _name_with_pins(lib: SchLib) -> str:
    for n in lib.component_names:
        if lib.get_component(n).pin_count > 0:
            return n
    return _first_name(lib)


def _mixed_case_name(lib: SchLib):
    """A name that differs from its own lowercasing (for case-insensitive tests)."""
    return next((n for n in lib.component_names if n != n.lower()), None)


def _name_with_slash(lib: SchLib):
    return next((n for n in lib.component_names if "/" in n), None)


def _name_with_double_pipe(lib: SchLib):
    """A component whose Data has a text record containing '||' (empty fields)."""
    for n in lib.component_names:
        comp = lib.get_component(n)
        if any(r.is_text and b"||" in r.payload for r in comp.records):
            return n
    return None


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
# Integration tests against whatever library is in ./input
# --------------------------------------------------------------------------
@needs_sample
def test_library_opens(lib):
    assert len(lib) > 0
    assert len(lib) == len(lib.component_names)
    assert "Schematic Library" in lib.header.header_string


@needs_sample
def test_components_resolve_by_name(lib):
    """Every declared component resolves and reports its own declared name."""
    for name in lib.component_names[:25]:
        assert lib.has_component(name), f"missing {name}"
        assert lib.get_component(name).name == name


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
    """Every declared name resolves to a distinct storage (no collisions)."""
    used = {}
    for name in lib.component_names:
        storage = lib.storage_name_for(name)
        assert storage is not None, f"unresolved: {name}"
        used.setdefault(storage, []).append(name)
    collisions = {k: v for k, v in used.items() if len(v) > 1}
    assert not collisions, f"storage collisions: {collisions}"
    assert len(used) == len(lib.component_names)


@needs_sample
def test_reserved_char_in_name_resolves(lib):
    """A name with an OLE-reserved '/' maps to a sanitized '_' storage."""
    name = _name_with_slash(lib)
    if name is None:
        pytest.skip("no component name contains '/'")
    comp = lib.get_component(name)
    assert "/" not in comp.storage_name
    assert comp.storage_name == name[:31].replace("/", "_")
    assert comp.name == name  # original name preserved in the record


@needs_sample
def test_len_matches_component_names(lib):
    assert len(lib) == len(lib.component_names)
    assert lib.declared_count == len(lib)


# --------------------------------------------------------------------------
# Save / edit round-trip (needs pywin32)
# --------------------------------------------------------------------------
def _stream_map(path):
    import olefile

    o = olefile.OleFileIO(path)
    try:
        return ({tuple(e): o.openstream(e).read()
                 for e in o.listdir(streams=True, storages=False)},
                o.root.clsid)
    finally:
        o.close()


@needs_sample
@needs_pywin32
def test_lossless_save(tmp_path):
    out = str(tmp_path / "roundtrip.SchLib")
    with SchLib(SAMPLE) as lib:
        lib.save(out)

    a, ca = _stream_map(SAMPLE)
    b, cb = _stream_map(out)
    assert set(a) == set(b)
    assert ca == cb  # Altium CLSID preserved
    mismatched = [k for k in a if a[k] != b[k]]
    assert not mismatched, f"{len(mismatched)} streams differ"


@needs_sample
@needs_pywin32
def test_save_after_loading_all_is_lossless(tmp_path):
    """Loading every component (the serialize path) still saves byte-identically.

    Exercises Component.to_data_stream() for all components -- including any
    without a PinTextData stream -- not just the verbatim copy path.
    """
    out = str(tmp_path / "allloaded.SchLib")
    with SchLib(SAMPLE) as lib:
        _ = lib.components  # force-load all components
        lib.save(out)

    before, _ = _stream_map(SAMPLE)
    after, _ = _stream_map(out)
    assert set(before) == set(after)
    mismatched = [k for k in before if before[k] != after[k]]
    assert not mismatched, f"{len(mismatched)} streams differ"


@needs_sample
@needs_pywin32
def test_edit_roundtrip_is_isolated(tmp_path):
    out = str(tmp_path / "edited.SchLib")
    marker = "Edited by altium_schlib test"
    with SchLib(SAMPLE) as lib:
        target = _first_name(lib)
        storage = lib.storage_name_for(target)
        count = len(lib)
        lib.get_component(target).set_header_field("ComponentDescription", marker)
        lib.save(out)

    with SchLib(out) as lib2:
        assert lib2.get_component(target).description == marker
        assert len(lib2) == count

    before, _ = _stream_map(SAMPLE)
    after, _ = _stream_map(out)
    changed = [k for k in before if before[k] != after.get(k)]
    assert changed == [(storage, "Data")], changed


@needs_sample
@needs_pywin32
def test_edit_record_with_empty_fields_preserves_them(tmp_path):
    """Regression: editing a record that contains '||' must not drop the empty
    fields (the HIGH-severity data-loss bug)."""
    out = str(tmp_path / "empty_fields.SchLib")
    with SchLib(SAMPLE) as lib:
        target = _name_with_double_pipe(lib)
        if target is None:
            pytest.skip("no component has a text record containing '||'")
        storage = lib.storage_name_for(target)
        comp = lib.get_component(target)
        # Edit the header (always present) so we exercise re-serialization of a
        # component that also owns a separate '||'-bearing record.
        comp.set_header_field("ComponentDescription", "edit-marker-xyz")
        lib.save(out)

    import olefile
    o = olefile.OleFileIO(out)
    after = o.openstream([storage, "Data"]).read()
    o.close()
    assert b"||" in after                 # empty fields survived the edit
    assert b"edit-marker-xyz" in after    # our edit landed


# --------------------------------------------------------------------------
# CLI (list_components.py) -- subjects discovered at runtime
# --------------------------------------------------------------------------
@needs_sample
def test_cli_show_component(lib, capsys):
    import list_components as cli

    target = _name_with_pins(lib)
    assert cli.main(["--show", target]) == 0
    out = capsys.readouterr().out
    assert f"Component: {target}" in out
    assert f"LibReference = {target}" in out
    assert "Parameters (RECORD=41)" in out
    assert "pin (binary)" in out  # chosen component has pins


@needs_sample
def test_cli_show_missing_component_suggests(lib, capsys):
    import list_components as cli

    name = _mixed_case_name(lib)
    if name is None:
        pytest.skip("no mixed-case component name to build a non-exact query")
    query = name.lower()  # case-insensitive substring, but not an exact name
    assert cli.main(["--show", query]) == 1
    out = capsys.readouterr().out
    assert "not found" in out
    assert name in out  # the real name is suggested


@needs_sample
def test_cli_match_lists(lib, capsys):
    import list_components as cli

    target = _first_name(lib)
    assert cli.main(["--match", target]) == 0
    out = capsys.readouterr().out
    assert target in out
    assert "--show" in out  # points the user to the next step


@needs_sample
def test_cli_match_case_insensitive(lib):
    import list_components as cli

    name = _mixed_case_name(lib)
    if name is None:
        pytest.skip("no mixed-case component name")
    assert name in cli.match_components(lib, name.lower())


@needs_sample
def test_cli_match_no_results_exits_nonzero(capsys):
    import list_components as cli

    assert cli.main(["--match", "zz-no-such-component-zz"]) == 1
    assert "no matches" in capsys.readouterr().out


@needs_sample
def test_cli_match_json(lib, capsys):
    import json

    import list_components as cli

    target = _first_name(lib)
    assert cli.main(["--match", target, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["pattern"] == target
    assert doc["count"] >= 1
    assert target in {m["name"] for m in doc["matches"]}


@needs_sample
def test_match_components_helper(lib):
    import list_components as cli

    target = _first_name(lib)
    assert target in cli.match_components(lib, target)
    assert cli.match_components(lib, "zz-no-such-component-zz") == []


@needs_sample
def test_cli_json_single_component(lib, capsys):
    import json

    import list_components as cli

    target = _name_with_pins(lib)
    expected_pins = lib.get_component(target).pin_count
    assert cli.main(["--show", target, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["name"] == target
    assert doc["pin_count"] == expected_pins
    assert doc["header"]["LibReference"] == target
    assert isinstance(doc["parameters"], list)


@needs_sample
def test_cli_json_library(lib, capsys):
    import json

    import list_components as cli

    assert cli.main(["--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["declared_count"] == len(lib)
    assert len(doc["components"]) == len(lib.component_names)
    assert {c["name"] for c in doc["components"]} == set(lib.component_names)


@needs_sample
def test_cli_json_not_found(capsys):
    import json

    import list_components as cli

    assert cli.main(["--show", "does-not-exist", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["found"] is False


@needs_sample
def test_set_header_field_refuses_rename(lib):
    comp = lib.get_component(_first_name(lib))
    with pytest.raises(ValueError):
        comp.set_header_field("LibReference", "SOMETHING_ELSE")
    # non-identity fields are still editable
    comp.set_header_field("ComponentDescription", "ok")
    assert comp.description == "ok"


# --------------------------------------------------------------------------
# Writer / robustness (synthetic inputs -- no library needed)
# --------------------------------------------------------------------------
@needs_pywin32
def test_init_closes_handle_on_bad_file(tmp_path):
    """A valid OLE2 file lacking FileHeader must raise AND not leak the handle."""
    import olefile

    from altium_schlib.writer import write_compound_file

    bad = str(tmp_path / "not_a_schlib.bin")
    write_compound_file(bad, {"SomethingElse": b"x"})
    with pytest.raises(ValueError):
        SchLib(bad)
    # If the handle leaked, this reopen/remove would fail on Windows.
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
    import shutil

    work = str(tmp_path / "inplace.SchLib")
    shutil.copyfile(SAMPLE, work)

    lib = SchLib(work)
    target = _first_name(lib)
    storage = lib.storage_name_for(target)
    count = len(lib)
    lib.get_component(target).set_header_field("ComponentDescription", "in-place edit")
    lib.save(work)  # same path
    # Object remains usable after the in-place swap.
    assert lib.get_component(target).description == "in-place edit"
    assert len(lib) == count
    lib.close()

    with SchLib(work) as reopened:
        assert reopened.get_component(target).description == "in-place edit"

    before, _ = _stream_map(SAMPLE)
    after, _ = _stream_map(work)
    changed = [k for k in before if before[k] != after.get(k)]
    assert changed == [(storage, "Data")], changed


# --------------------------------------------------------------------------
# Batch add-parameter feature
# --------------------------------------------------------------------------
import random  # noqa: E402
import string  # noqa: E402

from altium_schlib.schlib import Component, _new_unique_id  # noqa: E402

# A parameter name no real library component is expected to carry.
TESTPARAM = "ZZ_TESTPARAM"


def _make_component(payloads, storage="X"):
    data = b"".join(serialize_record_block(0, p) for p in payloads)
    return Component(storage, data)


# -- unit: record factory + UniqueID (no sample) ----------------------------
def test_record_from_fields_bytes_and_dirty():
    r = Record.from_fields([("RECORD", "41"), ("Name", "Mount"), ("Text", "X")])
    assert r.is_text and r.dirty and r.record_id == 41
    assert r.get("Name") == "Mount"
    assert r.to_bytes()[4:] == b"|RECORD=41|Name=Mount|Text=X\x00"
    # A synthesized record is byte-identical to the equivalent parsed one.
    assert parse_records(r.to_bytes())[0].to_bytes() == r.to_bytes()


def test_new_unique_id_format_and_retry():
    uid = _new_unique_id(set())
    assert len(uid) == 8 and uid.isalpha() and uid.isupper()
    # Force a collision: the first candidate from seed 1 is already taken, so
    # the generator must retry and return a different id.
    rng = random.Random(1)
    first = "".join(rng.choice(string.ascii_uppercase) for _ in range(8))
    out = _new_unique_id({first}, random.Random(1))
    assert out != first and len(out) == 8


# -- unit: add_parameter behavior (no sample) -------------------------------
def test_add_parameter_clones_and_inserts_in_cluster():
    comp = _make_component([
        b"|RECORD=1|LibReference=X|AllPinCount=0\x00",
        b"|RECORD=41|IndexInSheet=1|OwnerPartId=-1|Location.X=-5|Location.Y=13"
        b"|Color=8388608|FontID=11|IsHidden=T|Text=A|Name=P1|UniqueID=AAAAAAAA\x00",
        b"|RECORD=6|X=1\x00",  # a non-param record after the cluster
    ])
    rec = comp.add_parameter("Mount", "Surface Mount", rng=random.Random(0))
    f = dict(rec.fields)
    assert f["Name"] == "Mount" and f["Text"] == "Surface Mount"
    assert f["IsHidden"] == "T"
    assert f["Location.X"] == "-5" and f["FontID"] == "11"  # cloned from sibling
    assert len(f["UniqueID"]) == 8 and f["UniqueID"] != "AAAAAAAA"
    # Inserted at the end of the leading RECORD=41 cluster (before RECORD=6).
    assert [r.record_id for r in comp.records] == [1, 41, 41, 6]


def test_add_parameter_visible_omits_ishidden():
    comp = _make_component([b"|RECORD=1|LibReference=X\x00"])
    rec = comp.add_parameter("V", "x", hidden=False)
    assert "IsHidden" not in dict(rec.fields)


def test_add_parameter_empty_cluster_inserts_after_header():
    # Only parameter is a late "Comment"; leading cluster is empty.
    comp = _make_component([
        b"|RECORD=1|LibReference=X\x00",
        b"|RECORD=34|Name=Designator|Text=U?|UniqueID=BBBBBBBB\x00",
        b"|RECORD=41|IndexInSheet=-1|OwnerPartId=-1|Location.X=0|Location.Y=5"
        b"|Color=8388608|FontID=14|Text=c|Name=Comment|UniqueID=CCCCCCCC\x00",
    ])
    rec = comp.add_parameter("Mount", "SM")
    assert [r.record_id for r in comp.records] == [1, 41, 34, 41]
    f = dict(rec.fields)
    assert f["IsHidden"] == "T"          # hidden even though Comment isn't
    assert f["Color"] == "8388608"        # geometry cloned from the Comment param


def test_add_parameter_rejects_bad_input():
    comp = _make_component([b"|RECORD=1|LibReference=X\x00"])
    for bad in ["a|b", "a\x00b"]:
        with pytest.raises(ValueError):
            comp.add_parameter(bad, "v")
        with pytest.raises(ValueError):
            comp.add_parameter("N", bad)
    with pytest.raises(ValueError):
        comp.add_parameter("has=eq", "v")     # '=' illegal in a name
    with pytest.raises(ValueError):
        comp.add_parameter("N", "café")  # non-ASCII value
    # '=' inside a value is allowed
    rec = comp.add_parameter("N", "a=b")
    assert rec.get("Text") == "a=b"


def test_ensure_parameter_skips_existing():
    comp = _make_component([
        b"|RECORD=1|LibReference=X\x00",
        b"|RECORD=41|OwnerPartId=-1|Location.X=0|Location.Y=0|Color=1|FontID=1"
        b"|IsHidden=T|Text=old|Name=Mount|UniqueID=DDDDDDDD\x00",
    ])
    before = len(comp.records)
    assert comp.ensure_parameter("Mount", "new") is False
    assert len(comp.records) == before
    assert comp.get_parameter("Mount") == "old"  # not overwritten


# -- integration against the sample -----------------------------------------
@needs_sample
def test_add_parameter_to_all_covers_everything(lib):
    summary = lib.add_parameter_to_all(TESTPARAM, "V", rng=random.Random(0))
    assert summary["added_count"] + summary["skipped_count"] == len(lib)
    assert summary["total"] == len(lib)
    for name in lib.component_names:
        assert lib.get_component(name).has_parameter(TESTPARAM)


@needs_sample
@needs_pywin32
def test_batch_add_roundtrip_isolated_and_idempotent(tmp_path):
    out1 = str(tmp_path / "p1.SchLib")
    with SchLib(SAMPLE) as lib:
        added = set()  # storages that will be modified (lack the param)
        for name in lib.component_names:
            comp = lib.get_component(name)
            if not comp.has_parameter(TESTPARAM):
                added.add(comp.storage_name)
        summary = lib.add_parameter_to_all(TESTPARAM, "V", rng=random.Random(0))
        lib.save(out1)

    # every component now has it, and no component was added/removed
    with SchLib(out1) as lib1:
        assert len(lib1) == summary["total"]
        for name in lib1.component_names:
            assert lib1.get_component(name).get_parameter(TESTPARAM) == "V"

    # isolated diff: only added components' Data changed; root + PinTextData intact
    before, cb = _stream_map(SAMPLE)
    after, ca = _stream_map(out1)
    assert ca == cb
    changed = {k for k in before if before[k] != after.get(k)}
    assert changed == {(s, "Data") for s in added}
    assert not (set(after) - set(before))  # no new/removed streams

    # idempotent: re-running adds nothing and yields a byte-identical file
    out2 = str(tmp_path / "p2.SchLib")
    with SchLib(out1) as lib2:
        s2 = lib2.add_parameter_to_all(TESTPARAM, "V", rng=random.Random(0))
        assert s2["added_count"] == 0
        lib2.save(out2)
    a1, _ = _stream_map(out1)
    a2, _ = _stream_map(out2)
    assert a1 == a2


# -- CLI --------------------------------------------------------------------
@needs_sample
def test_cli_batch_dry_run_writes_nothing(tmp_path, capsys):
    import batch_add_parameter as cli

    out = tmp_path / "should_not_exist.SchLib"
    rc = cli.main(["--name", TESTPARAM, "--value", "V", "--dry-run",
                   "-o", str(out), "--json"])
    assert rc == 0
    import json
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True and doc["output"] is None
    assert doc["total"] == doc["added_count"] + doc["skipped_count"]
    assert not out.exists()


@needs_sample
@needs_pywin32
def test_cli_batch_writes_and_adds(tmp_path):
    import batch_add_parameter as cli

    out = tmp_path / "cli_out.SchLib"
    rc = cli.main(["--name", TESTPARAM, "--value", "Surface Mount", "-o", str(out)])
    assert rc == 0 and out.exists()
    with SchLib(str(out)) as lib:
        for name in lib.component_names[:20]:
            assert lib.get_component(name).get_parameter(TESTPARAM) == "Surface Mount"


@needs_sample
def test_cli_batch_refuses_overwriting_input():
    import batch_add_parameter as cli

    with pytest.raises(SystemExit):
        cli.main(["--name", TESTPARAM, "--value", "V", "-o", SAMPLE])

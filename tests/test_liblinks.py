"""Tests for the library-relink engine and CLI.

Fixtures are synthesized from scratch (a minimal .SchDoc, .PcbDoc and .PrjPcb),
so these tests need no real Altium projects and reveal nothing proprietary.
Building the binary fixtures needs pywin32, so those tests skip elsewhere.
"""

from __future__ import annotations

import os
import sys
from collections import Counter

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from altium_schlib import liblinks as ll  # noqa: E402
from altium_schlib.records import serialize_record_block  # noqa: E402

OLD = "OldLibrary 01_01_2011.SchLib"
NEW = "NewLibrary.SchLib"
LIVE = "LiveLibrary.SchLib"
OLD_FP = "OldFootprints.PcbLib"


def _has_pywin32() -> bool:
    try:
        import pythoncom  # noqa: F401
        import win32com.storagecon  # noqa: F401

        return True
    except Exception:
        return False


needs_pywin32 = pytest.mark.skipif(
    not _has_pywin32(), reason="pywin32 (Windows Structured Storage) not available"
)


# -- fixture builders --------------------------------------------------------
def _blocks(*payloads: bytes) -> bytes:
    return b"".join(serialize_record_block(0, p) for p in payloads)


def make_schdoc(path: str, libs=(OLD, LIVE)) -> None:
    """A .SchDoc whose component records carry SourceLibraryName.

    The second component deliberately uses the upper-case field spelling that
    some Altium versions emit, to pin down case-insensitive matching.
    """
    from altium_schlib.writer import write_compound_file

    payloads = []
    for i, lib in enumerate(libs):
        key = "SourceLibraryName" if i % 2 == 0 else "SOURCELIBRARYNAME"
        payloads.append(
            f"|RECORD=1|LibReference=PART{i}|LibraryPath=*"
            f"|{key}={lib}|UniqueID=AAAAAAA{i}\x00".encode()
        )
        # The designator record that follows the component it belongs to.
        payloads.append(
            f"|RECORD=34|OwnerIndex={i}|Text=D{i}|Name=Designator\x00".encode()
        )
    # A sheet-filename record: looks library-ish but must never be touched.
    payloads.append(b"|RECORD=33|Text=[01]_SHEET.SchDoc|Name=SheetName\x00")
    write_compound_file(path, {"FileHeader": _blocks(*payloads),
                              "Additional": b"", "Storage": b""})


def make_pcbdoc(path: str, libs=(OLD, LIVE)) -> None:
    """A .PcbDoc with Components6/Data records (upper-case field names)."""
    from altium_schlib.writer import write_compound_file

    payloads = []
    for i, lib in enumerate(libs):
        payloads.append(
            f"|SELECTION=FALSE|PATTERN=PAT{i}|SOURCEDESIGNATOR=R{i}"
            f"|SOURCEFOOTPRINTLIBRARY={OLD_FP}"
            f"|SOURCECOMPONENTLIBRARY={lib}"
            f"|SOURCECOMPLIBIDENTIFIERKIND=2"
            f"|SOURCECOMPLIBRARYIDENTIFIER={lib}"
            f"|UNIQUEID=BBBBBBB{i}\x00".encode()
        )
    write_compound_file(path, {
        "FileHeader": b"",
        "Components6": {"Header": b"x", "Data": _blocks(*payloads)},
        # A binary-ish stream that must be copied verbatim, never parsed/edited.
        "Tracks6": {"Data": bytes(range(256))},
    })


def make_prjpcb(path: str, libs=(OLD, LIVE)) -> None:
    """A .PrjPcb with a BOM, CRLF endings and a cached component list."""
    lines = ["[Design]", "Version=1.0", "", "[DatabaseUpdateOptions]"]
    for i, lib in enumerate(libs):
        lines += [
            f"ComponentLibIdentifierKind{i}=Library Name And Type",
            f"ComponentLibraryIdentifier{i}={lib}",
            f"ComponentDesignItemID{i}=PART{i}",
            f"ComponentUpdate{i}=1",
        ]
    body = "\r\n".join(lines) + "\r\n"
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + body.encode("utf-8"))


@pytest.fixture
def project(tmp_path):
    """A project folder with all three document types, plus ignored dirs."""
    d = tmp_path / "Proj" / "Design_v1"
    d.mkdir(parents=True)
    make_prjpcb(str(d / "Proj_v1.PrjPcb"))
    # An unrelated file that merely mentions the library must never be touched.
    (d / "notes.txt").write_text(f"we used to use {OLD}\n", encoding="utf-8")
    if _has_pywin32():
        make_schdoc(str(d / "Sheet.SchDoc"))
        make_pcbdoc(str(d / "Board.PcbDoc"))
    # Generated/archive folders that must be skipped.
    hist = d / "History"
    hist.mkdir()
    make_prjpcb(str(hist / "Archived.PrjPcb"), libs=(OLD,))
    prev = d / "Project Outputs for Proj"
    prev.mkdir()
    make_prjpcb(str(prev / "Generated.PrjPcb"), libs=(OLD,))
    return d


# -- unit: helpers -----------------------------------------------------------
def test_kind_for_library():
    assert ll.kind_for_library("a/b/My.SchLib") == "schematic"
    assert ll.kind_for_library("My.PCBLIB") == "footprint"
    for bad in ("x.IntLib", "x.txt", "x"):
        with pytest.raises(ValueError):
            ll.kind_for_library(bad)


def test_walk_skips_generated_dirs_and_other_files(project):
    files = ll.walk_design_files(str(project))
    assert any(f.endswith("Proj_v1.PrjPcb") for f in files)
    assert not any("History" in f for f in files)
    assert not any("Project Outputs" in f for f in files)
    assert not any(f.endswith(".txt") for f in files)


def test_classify_target_found_stale(tmp_path):
    (tmp_path / LIVE).write_bytes(b"x")
    index = ll.index_libraries([str(tmp_path)])
    status = ll.classify([NEW, LIVE, OLD], NEW, index)
    assert status == {NEW: "target", LIVE: "found", OLD: "stale"}


def test_classify_is_case_insensitive(tmp_path):
    (tmp_path / "MiXeD.SchLib").write_bytes(b"x")
    index = ll.index_libraries([str(tmp_path)])
    assert ll.classify(["mixed.schlib"], NEW, index)["mixed.schlib"] == "found"


# -- .PrjPcb (no pywin32 needed) --------------------------------------------
def test_scan_and_rewrite_prjpcb(tmp_path):
    p = str(tmp_path / "P.PrjPcb")
    make_prjpcb(p)
    fr = ll.scan_project(p, "schematic")
    assert fr.doctype == "PrjPcb"
    assert fr.refs == {OLD: 1, LIVE: 1}
    # footprint kind: projects cache no footprint list
    assert ll.scan_project(p, "footprint") is None

    before = open(p, "rb").read()
    assert ll.rewrite_project(p, {OLD: NEW}, "schematic", backup=True) == 1
    after = open(p, "rb").read()

    assert after.startswith(b"\xef\xbb\xbf")                  # BOM kept
    assert after.count(b"\r\n") == before.count(b"\r\n")      # CRLF kept
    assert after.count(b"\n") == before.count(b"\n")          # line count kept
    assert OLD.encode() not in after
    assert f"ComponentLibraryIdentifier0={NEW}".encode() in after
    assert f"ComponentLibraryIdentifier1={LIVE}".encode() in after  # untouched
    assert b"ComponentDesignItemID0=PART0" in after           # neighbours intact
    assert os.path.exists(p + ".bak")
    assert open(p + ".bak", "rb").read() == before


def test_backup_not_overwritten_on_second_run(tmp_path):
    p = str(tmp_path / "P.PrjPcb")
    make_prjpcb(p)
    original = open(p, "rb").read()
    ll.rewrite_project(p, {OLD: NEW}, "schematic", backup=True)
    # second pass repoints the other library; the .bak must stay pristine
    ll.rewrite_project(p, {LIVE: NEW}, "schematic", backup=True)
    assert open(p + ".bak", "rb").read() == original


def test_rewrite_project_no_match_leaves_file_alone(tmp_path):
    p = str(tmp_path / "P.PrjPcb")
    make_prjpcb(p)
    before = open(p, "rb").read()
    assert ll.rewrite_project(p, {"Absent.SchLib": NEW}, "schematic") == 0
    assert open(p, "rb").read() == before
    assert not os.path.exists(p + ".bak")


# -- binary documents --------------------------------------------------------
def _streams(path):
    import olefile

    o = olefile.OleFileIO(path)
    try:
        return ({tuple(e): o.openstream(e).read()
                 for e in o.listdir(streams=True, storages=False)}, o.root.clsid)
    finally:
        o.close()


@needs_pywin32
def test_scan_schdoc_mixed_field_case(tmp_path):
    p = str(tmp_path / "S.SchDoc")
    make_schdoc(p)
    fr = ll.scan_document(p, "schematic")
    # Both spellings of the field are found.
    assert fr.refs == {OLD: 1, LIVE: 1}
    # The RECORD=33 sheet filename is not mistaken for a library.
    assert not any(v.endswith(".SchDoc") for v in fr.refs)


@needs_pywin32
def test_rewrite_schdoc_isolated_and_preserves_field_case(tmp_path):
    p = str(tmp_path / "S.SchDoc")
    make_schdoc(p, libs=(OLD, OLD))       # record 0 lower-case, record 1 UPPER
    before, clsid_before = _streams(p)

    assert ll.rewrite_document(p, {OLD: NEW}, "schematic") == 2
    after, clsid_after = _streams(p)

    assert clsid_before == clsid_after
    assert set(before) == set(after)
    assert [k for k in before if before[k] != after[k]] == [("FileHeader",)]
    fh = after[("FileHeader",)]
    assert f"SourceLibraryName={NEW}".encode() in fh      # original casing kept
    assert f"SOURCELIBRARYNAME={NEW}".encode() in fh
    assert OLD.encode() not in fh
    assert ll.residual_occurrences(p, [OLD]) == 0


@needs_pywin32
def test_rewrite_pcbdoc_schematic_vs_footprint_fields(tmp_path):
    p = str(tmp_path / "B.PcbDoc")
    make_pcbdoc(p, libs=(OLD, OLD))
    before, _ = _streams(p)

    # schematic kind touches the two component-library fields, not the footprint
    assert ll.rewrite_document(p, {OLD: NEW}, "schematic") == 4
    after, _ = _streams(p)
    assert [k for k in before if before[k] != after[k]] == [("Components6", "Data")]
    data = after[("Components6", "Data")]
    assert f"SOURCECOMPONENTLIBRARY={NEW}".encode() in data
    assert f"SOURCECOMPLIBRARYIDENTIFIER={NEW}".encode() in data
    assert f"SOURCEFOOTPRINTLIBRARY={OLD_FP}".encode() in data   # untouched
    # the opaque binary stream was copied verbatim
    assert after[("Tracks6", "Data")] == bytes(range(256))

    # footprint kind touches only the footprint field
    assert ll.rewrite_document(p, {OLD_FP: "New.PcbLib"}, "footprint") == 2
    data = _streams(p)[0][("Components6", "Data")]
    assert b"SOURCEFOOTPRINTLIBRARY=New.PcbLib" in data
    assert OLD_FP.encode() not in data


@needs_pywin32
def test_scan_tree_groups_all_document_types(project):
    scans = ll.scan_tree(str(project), "schematic")
    types = {fr.doctype for fr in scans}
    assert types == {"SchDoc", "PcbDoc", "PrjPcb"}
    totals = ll.referenced_libraries(scans)
    assert totals[OLD] == 1 + 2 + 1          # SchDoc + PcbDoc(2 fields) + PrjPcb
    # nothing from History / Project Outputs leaked in
    assert not any("History" in fr.path for fr in scans)


@needs_pywin32
def test_scan_tree_doc_type_filter(project):
    scans = ll.scan_tree(str(project), "schematic", doc_types=("SchDoc",))
    assert {fr.doctype for fr in scans} == {"SchDoc"}


# -- CLI ---------------------------------------------------------------------
@needs_pywin32
def test_cli_reports_without_writing(project, tmp_path, capsys):
    import json

    import relink_libraries as cli

    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    snapshot = {p: open(p, "rb").read()
                for p in ll.walk_design_files(str(project))}

    rc = cli.main([str(project), str(newlib), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["applied"] is False
    assert doc["references_changed"] == 0
    assert doc["references_planned"] > 0
    assert doc["projects"] == 1
    # nothing on disk moved
    for p, data in snapshot.items():
        assert open(p, "rb").read() == data
    assert not any(f.endswith(".bak") for f in os.listdir(project))


@needs_pywin32
def test_cli_apply_repoints_only_stale(project, tmp_path, capsys):
    import json

    import relink_libraries as cli

    libdir = tmp_path / "libs"
    libdir.mkdir()
    (libdir / NEW).write_bytes(b"x")
    (libdir / LIVE).write_bytes(b"x")       # makes LIVE resolvable -> left alone

    rc = cli.main([str(project), str(libdir / NEW), "--apply", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["applied"] is True
    assert doc["references_changed"] == doc["references_planned"] > 0
    assert doc["residual_files"] == []
    status = {l["name"]: l["status"] for l in doc["libraries"]}
    assert status[OLD] == "stale" and status[LIVE] == "found"

    # re-scan: the stale name is gone, the live one survived
    after = ll.referenced_libraries(ll.scan_tree(str(project), "schematic"))
    assert OLD not in after
    assert after[LIVE] > 0 and after[NEW] > 0


@needs_pywin32
def test_cli_from_flag_forces_specific_library(project, tmp_path, capsys):
    import json

    import relink_libraries as cli

    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    (tmp_path / LIVE).write_bytes(b"x")     # LIVE exists, so not stale...
    rc = cli.main([str(project), str(newlib), "--from", LIVE, "--apply", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    # ...but --from repoints it anyway, and leaves the stale one alone
    after = ll.referenced_libraries(ll.scan_tree(str(project), "schematic"))
    assert LIVE not in after
    assert after[OLD] > 0


@needs_pywin32
def test_cli_all_others_repoints_everything(project, tmp_path, capsys):
    import relink_libraries as cli

    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    (tmp_path / LIVE).write_bytes(b"x")
    assert cli.main([str(project), str(newlib), "--all-others", "--apply",
                     "--json"]) == 0
    capsys.readouterr()
    after = ll.referenced_libraries(ll.scan_tree(str(project), "schematic"))
    assert set(after) == {NEW}


@needs_pywin32
def test_cli_no_backup_flag(project, tmp_path, capsys):
    import relink_libraries as cli

    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    cli.main([str(project), str(newlib), "--apply", "--no-backup", "--json"])
    capsys.readouterr()
    assert not any(f.endswith(".bak") for f in os.listdir(project))


@needs_pywin32
def test_details_identify_each_reference(project):
    scans = ll.scan_tree(str(project), "schematic")
    for fr in scans:
        # one detail per counted occurrence, and they agree with the counter
        assert len(fr.details) == sum(fr.refs.values())
        assert Counter(d.library for d in fr.details) == fr.refs
        for d in fr.details:
            assert d.field                      # the real field/key name
            assert d.component                  # part name or design item id
        if fr.doctype == "SchDoc":
            # designator recovered from the RECORD=34 that follows the component
            assert {d.designator for d in fr.details} == {"D0", "D1"}
        if fr.doctype == "PcbDoc":
            assert {d.designator for d in fr.details} == {"R0", "R1"}
            assert all(d.context == "" for d in fr.details)
        if fr.doctype == "PrjPcb":
            assert {d.component for d in fr.details} == {"PART0", "PART1"}
            assert all(d.context.startswith("entry ") for d in fr.details)


@needs_pywin32
def test_details_for_filters_by_library(project):
    scans = ll.scan_tree(str(project), "schematic")
    fr = next(f for f in scans if f.doctype == "PrjPcb")
    picked = fr.details_for([OLD])
    assert picked and all(d.library == OLD for d in picked)
    assert len(picked) == fr.refs[OLD]


@needs_pywin32
def test_cli_show_refs_lists_components(project, tmp_path, capsys):
    import relink_libraries as cli

    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    assert cli.main([str(project), str(newlib), "--show-refs"]) == 0
    out = capsys.readouterr().out
    assert "Affected references" in out
    assert "PART0" in out                     # component identified by name
    assert "ComponentLibraryIdentifier0" in out
    assert "SOURCECOMPLIBRARYIDENTIFIER" in out
    # without the switch the detail block is replaced by a hint
    assert cli.main([str(project), str(newlib)]) == 0
    plain = capsys.readouterr().out
    assert "Affected references" not in plain
    assert "--show-refs" in plain


@needs_pywin32
def test_cli_show_refs_json(project, tmp_path, capsys):
    import json

    import relink_libraries as cli

    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    (tmp_path / LIVE).write_bytes(b"x")   # resolvable, so only OLD is stale
    cli.main([str(project), str(newlib), "--show-refs", "--json"])
    doc = json.loads(capsys.readouterr().out)
    entries = doc["affected_references"]
    assert entries
    flat = [r for e in entries for r in e["references"]]
    assert len(flat) == doc["references_planned"]
    # only the stale library's references are listed
    assert all(r["library"] == OLD for r in flat)
    assert {"library", "field", "designator", "component", "context"} == set(flat[0])
    # omitted unless asked for
    cli.main([str(project), str(newlib), "--json"])
    assert "affected_references" not in json.loads(capsys.readouterr().out)


def test_cli_guards(project, tmp_path):
    import relink_libraries as cli

    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    with pytest.raises(SystemExit):      # unsupported library type
        cli.main([str(project), str(tmp_path / "x.IntLib")])
    with pytest.raises(SystemExit):      # root is not a folder
        cli.main([str(newlib), str(newlib)])
    with pytest.raises(SystemExit):      # every document type skipped
        cli.main([str(project), str(newlib), "--skip-schdoc", "--skip-pcbdoc",
                  "--skip-prjpcb"])


def test_cli_nothing_to_do_is_success(tmp_path, capsys):
    import relink_libraries as cli

    empty = tmp_path / "empty"
    empty.mkdir()
    newlib = tmp_path / NEW
    newlib.write_bytes(b"x")
    assert cli.main([str(empty), str(newlib)]) == 0
    assert "Nothing to do" in capsys.readouterr().out

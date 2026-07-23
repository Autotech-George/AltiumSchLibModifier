"""Write an OLE2 / Compound File Binary Format container.

Editing a SchLib can change stream sizes and add or remove components, which
``olefile`` cannot do (it only overwrites same-size streams in place). We build
a fresh compound file instead, using the native Windows Structured Storage API
exposed through ``pywin32`` -- the same implementation the OS (and Altium) use,
so the result is a well-formed docfile with 512-byte sectors.

The public entry point is :func:`write_compound_file`, which serializes a
nested ``{name: bytes | {name: ...}}`` tree of storages and streams.
"""

from __future__ import annotations

import os
import tempfile
from typing import Dict, Optional, Union

import pythoncom
import pywintypes
import win32com.storagecon as sc

# A stream is ``bytes``; a storage is a dict mapping names to further entries.
StreamTree = Dict[str, Union[bytes, "StreamTree"]]

_NULL_CLSID = "00000000-0000-0000-0000-000000000000"

_ROOT_MODE = sc.STGM_CREATE | sc.STGM_READWRITE | sc.STGM_SHARE_EXCLUSIVE
_STORAGE_MODE = sc.STGM_CREATE | sc.STGM_READWRITE | sc.STGM_SHARE_EXCLUSIVE
_STREAM_MODE = sc.STGM_CREATE | sc.STGM_WRITE | sc.STGM_SHARE_EXCLUSIVE

# OLE storage/stream names are limited to 31 UTF-16 characters.
_MAX_NAME_LEN = 31


def _write_stream(storage, name: str, data: bytes) -> None:
    stream = storage.CreateStream(name, _STREAM_MODE, 0, 0)
    if data:
        stream.Write(data)
    # Release promptly so the parent Commit sees a settled child.
    stream = None


def _write_tree(storage, tree: StreamTree) -> None:
    for name, value in tree.items():
        if len(name) > _MAX_NAME_LEN:
            raise ValueError(
                f"entry name {name!r} exceeds {_MAX_NAME_LEN} chars; "
                "Altium/OLE storage names must be sanitized/truncated first"
            )
        if isinstance(value, (bytes, bytearray)):
            _write_stream(storage, name, bytes(value))
        elif isinstance(value, dict):
            substorage = storage.CreateStorage(name, _STORAGE_MODE, 0, 0)
            _write_tree(substorage, value)
            substorage.Commit(sc.STGC_DEFAULT)
            substorage = None
        else:
            raise TypeError(
                f"entry {name!r} must be bytes or dict, got {type(value).__name__}"
            )


def _write_docfile(path: str, tree: StreamTree, root_clsid: Optional[str]) -> None:
    root = pythoncom.StgCreateDocfile(path, _ROOT_MODE)
    try:
        if root_clsid and root_clsid != _NULL_CLSID:
            iid = pywintypes.IID("{" + root_clsid.strip("{}") + "}")
            root.SetClass(iid)
        _write_tree(root, tree)
        root.Commit(sc.STGC_DEFAULT)
    finally:
        root = None


def write_compound_file(
    path: str,
    tree: StreamTree,
    root_clsid: Optional[str] = None,
) -> None:
    """Write ``tree`` to ``path`` as a compound file, atomically.

    ``tree`` maps entry names to either ``bytes`` (a stream) or a nested dict
    (a storage). ``root_clsid`` (e.g. Altium's schematic-library CLSID) is
    stamped on the root storage when provided and non-null, so the target
    application recognizes the file type.

    The file is built in a temporary file in the same directory and then moved
    into place with :func:`os.replace`, so an existing file at ``path`` is only
    ever replaced by a fully-written, committed docfile. A failure mid-write
    leaves the original ``path`` untouched. NOTE: writing over a file that is
    still open elsewhere (e.g. the source library) may fail on Windows -- close
    such handles first; :meth:`SchLib.save` handles this for in-place saves.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(suffix=".schlib.tmp", dir=directory)
    os.close(fd)  # StgCreateDocfile reopens/truncates the path itself
    try:
        _write_docfile(tmp, tree, root_clsid)
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
        raise

"""altium_schlib -- read and edit Altium ``.SchLib`` schematic libraries.

Quick start::

    from altium_schlib import SchLib

    with SchLib("input/lib.SchLib") as lib:
        for name in lib.component_names:
            print(name)
        comp = lib.get_component("CON_KLEMA_2")
        comp.set_header_field("ComponentDescription", "2-pin terminal block")
        lib.save("output/lib.SchLib")
"""

from .records import (
    FLAG_PIN,
    FLAG_TEXT,
    Record,
    parse_records,
    serialize_records,
)
from .schlib import Component, LibraryHeader, SchLib

__all__ = [
    "SchLib",
    "Component",
    "LibraryHeader",
    "Record",
    "parse_records",
    "serialize_records",
    "FLAG_TEXT",
    "FLAG_PIN",
]

__version__ = "0.1.0"

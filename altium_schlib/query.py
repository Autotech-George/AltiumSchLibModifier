"""Composable component predicates for querying a :class:`~altium_schlib.SchLib`.

Each builder returns a ``Predicate`` -- a ``Callable[[Component], bool]``. Build
a compound query by combining predicates with :func:`all_of` / :func:`any_of` /
:func:`negate`, then pass it to :meth:`SchLib.set_parameter_where` (or use it to
filter ``lib.components`` directly).

Example (name contains ``TH_`` but not ``ETH_``)::

    from altium_schlib import query as q
    pred = q.all_of(q.name_contains("TH_"), q.name_excludes("ETH_"))
    lib.set_parameter_where("Mount", "Through Hole", pred)

Example (parameter ``Case/Package`` equals ``SOIC``)::

    pred = q.param_equals("Case/Package", "SOIC")
"""

from __future__ import annotations

import re
from typing import Callable, Optional

# A predicate takes a Component (duck-typed) and returns True to select it.
Predicate = Callable[[object], bool]


def _fold(s: str, ignore_case: bool) -> str:
    return s.casefold() if ignore_case else s


# -- combinators ------------------------------------------------------------
def all_of(*predicates: Predicate) -> Predicate:
    """Match when every predicate matches (AND). No predicates → always True."""
    preds = list(predicates)
    return lambda c: all(p(c) for p in preds)


def any_of(*predicates: Predicate) -> Predicate:
    """Match when any predicate matches (OR). No predicates → always False."""
    preds = list(predicates)
    return lambda c: any(p(c) for p in preds)


def negate(predicate: Predicate) -> Predicate:
    """Match when ``predicate`` does not."""
    return lambda c: not predicate(c)


def always() -> Predicate:
    """Match every component."""
    return lambda c: True


# -- name predicates --------------------------------------------------------
def name_contains(*subs: str, ignore_case: bool = False) -> Predicate:
    """Match if the component name contains ANY of ``subs``."""
    needles = [_fold(s, ignore_case) for s in subs]
    return lambda c: any(n in _fold(c.name, ignore_case) for n in needles)


def name_excludes(*subs: str, ignore_case: bool = False) -> Predicate:
    """Match if the component name contains NONE of ``subs``."""
    needles = [_fold(s, ignore_case) for s in subs]
    return lambda c: not any(n in _fold(c.name, ignore_case) for n in needles)


def name_regex(pattern: str, ignore_case: bool = False) -> Predicate:
    """Match if the component name matches ``pattern`` (``re.search``)."""
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    return lambda c: rx.search(c.name) is not None


# -- parameter predicates ---------------------------------------------------
def param_equals(name: str, value: str, ignore_case: bool = False) -> Predicate:
    """Match if parameter ``name`` exists and its text equals ``value``."""
    target = _fold(value, ignore_case)

    def _pred(c) -> bool:
        if not c.has_parameter(name):
            return False
        text = c.get_parameter(name)
        return text is not None and _fold(text, ignore_case) == target

    return _pred


def param_contains(name: str, sub: str, ignore_case: bool = False) -> Predicate:
    """Match if parameter ``name`` exists and its text contains ``sub``."""
    needle = _fold(sub, ignore_case)

    def _pred(c) -> bool:
        text = c.get_parameter(name)
        return text is not None and needle in _fold(text, ignore_case)

    return _pred


def param_regex(name: str, pattern: str, ignore_case: bool = False) -> Predicate:
    """Match if parameter ``name`` exists and its text matches ``pattern``."""
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)

    def _pred(c) -> bool:
        text = c.get_parameter(name)
        return text is not None and rx.search(text) is not None

    return _pred


def param_exists(name: str) -> Predicate:
    """Match if the component has a parameter named ``name`` (value or not)."""
    return lambda c: c.has_parameter(name)


def param_missing(name: str) -> Predicate:
    """Match if the component has no parameter named ``name``."""
    return lambda c: not c.has_parameter(name)


# -- other predicates -------------------------------------------------------
def designator_prefix(prefix: str, ignore_case: bool = False) -> Predicate:
    """Match if the schematic designator (RECORD=34) starts with ``prefix``."""
    needle = _fold(prefix, ignore_case)
    return lambda c: _fold(c.designator, ignore_case).startswith(needle)


def pins(minimum: Optional[int] = None, maximum: Optional[int] = None) -> Predicate:
    """Match if the pin count is within ``[minimum, maximum]`` (either optional)."""
    def _pred(c) -> bool:
        n = c.pin_count
        if minimum is not None and n < minimum:
            return False
        if maximum is not None and n > maximum:
            return False
        return True

    return _pred

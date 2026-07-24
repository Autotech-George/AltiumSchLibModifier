#!/usr/bin/env python3
"""Shared CLI plumbing for the AltiumSchLibModifier tools.

Holds what the command-line tools have in common so they behave consistently:

* input discovery (the single ``.SchLib`` in ``./input``),
* safe output-path resolution (never clobber the input unless ``--in-place``),
* the component **query selector** flags and their translation into an
  ``altium_schlib.query`` predicate -- used by ``list_components.py`` (to
  search/list) and ``batch_set_parameter.py`` (to select what to change).

Negation
--------
Every text selector value can be negated:

* parameter selectors take a ``NAME!=VALUE`` operator, e.g.
  ``--param "Mount!=Surface Mount"``;
* the other selectors take a leading ``!``, e.g. ``--name-contains '!ETH_'``
  or ``--param-exists '!Mount'`` (same as ``--param-missing Mount``).

A negated selector is the pure logical NOT of its positive form. In particular
``Mount!=X`` also matches components that have no ``Mount`` parameter at all;
add ``--param-exists Mount`` to require presence. To match a value that
literally starts with ``!``, escape it as ``\\!``. (In bash, quote ``!`` with
single quotes to dodge history expansion; PowerShell/cmd need no escaping.)
"""

from __future__ import annotations

import glob
import os
import re
import sys
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from altium_schlib import query as q  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# -- input / output paths ----------------------------------------------------
def find_default_schlib() -> str:
    """Locate a single .SchLib under ./input, or raise a helpful error."""
    matches = sorted(glob.glob(os.path.join(SCRIPT_DIR, "input", "*.SchLib")))
    if not matches:
        raise SystemExit(
            "No .SchLib found in ./input. Pass the file path explicitly:\n"
            "    python <tool>.py path/to/library.SchLib"
        )
    if len(matches) > 1:
        names = "\n  ".join(os.path.basename(m) for m in matches)
        raise SystemExit(
            f"Multiple .SchLib files in ./input; specify one:\n  {names}"
        )
    return matches[0]


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def resolve_output(input_path: str, output: Optional[str], in_place: bool) -> str:
    """Decide where a mutating tool writes, never clobbering the input
    unless ``--in-place`` was given."""
    if in_place:
        return input_path
    if output:
        if same_path(output, input_path):
            raise SystemExit(
                "Refusing to overwrite the input file; use --in-place to do that."
            )
        return output
    out_dir = os.path.join(SCRIPT_DIR, "output")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, os.path.basename(input_path))


# -- negation parsing ---------------------------------------------------------
def split_negation(value: str) -> Tuple[str, bool]:
    """Strip a leading ``!`` negation marker: ``"!x" -> ("x", True)``.

    A leading ``\\!`` escapes a literal ``!``: ``"\\!x" -> ("!x", False)``.
    """
    if value.startswith("\\!"):
        return value[1:], False
    if value.startswith("!"):
        return value[1:], True
    return value, False


def split_pair(spec: str, flag: str) -> Tuple[str, str, bool]:
    """Parse ``NAME=VALUE`` / ``NAME!=VALUE`` -> ``(name, value, negated)``.

    Splits on the FIRST operator, so the value may itself contain ``=`` or
    ``!=`` (e.g. ``T=a!=b`` -> name ``T``, value ``a!=b``).
    """
    ne = spec.find("!=")
    eq = spec.find("=")
    if ne != -1 and eq == ne + 1:  # the first '=' belongs to '!='
        name, value, negated = spec[:ne], spec[ne + 2:], True
    elif eq != -1:
        name, value, negated = spec[:eq], spec[eq + 1:], False
    else:
        raise SystemExit(f"{flag} expects NAME=VALUE or NAME!=VALUE, got {spec!r}")
    if not name:
        raise SystemExit(f"{flag} expects NAME=VALUE or NAME!=VALUE, got {spec!r}")
    return name, value, negated


def _regex(builder, *args, **kwargs):
    """Build a regex predicate, turning re.error into a clean CLI error."""
    try:
        return builder(*args, **kwargs)
    except re.error as exc:
        raise SystemExit(f"invalid regular expression: {exc}")


# -- selector arguments -> predicate ------------------------------------------
def add_selector_arguments(parser) -> None:
    """Add the shared component-selector flags to an argparse parser."""
    sel = parser.add_argument_group(
        "selectors (ANDed unless --match-any; prefix a value with '!' to "
        "negate, or use NAME!=VALUE; escape a literal leading '!' as '\\!')")
    sel.add_argument("--name-contains", action="append", metavar="[!]SUBSTR",
                     help="Name contains SUBSTR (repeatable; positives are "
                          "any-of, '!'-negated ones must all be absent).")
    sel.add_argument("--name-excludes", action="append", metavar="SUBSTR",
                     help="Name contains none of these (same as "
                          "--name-contains '!SUBSTR').")
    sel.add_argument("--name-regex", metavar="[!]REGEX",
                     help="Name matches REGEX.")
    sel.add_argument("--param", action="append", metavar="NAME[!]=VALUE",
                     help="Parameter NAME text equals (or != differs from) "
                          "VALUE (repeatable).")
    sel.add_argument("--param-contains", action="append", metavar="NAME[!]=SUBSTR",
                     help="Parameter NAME text contains SUBSTR (repeatable).")
    sel.add_argument("--param-regex", action="append", metavar="NAME[!]=REGEX",
                     help="Parameter NAME text matches REGEX (repeatable).")
    sel.add_argument("--param-exists", action="append", metavar="[!]NAME",
                     help="Component has parameter NAME (repeatable).")
    sel.add_argument("--param-missing", action="append", metavar="[!]NAME",
                     help="Component lacks parameter NAME (repeatable).")
    sel.add_argument("--designator-prefix", action="append", metavar="[!]P",
                     help="Designator starts with P (repeatable; positives "
                          "are any-of).")
    sel.add_argument("--pins-min", type=int, metavar="N", help="At least N pins.")
    sel.add_argument("--pins-max", type=int, metavar="N", help="At most N pins.")
    sel.add_argument("--all", action="store_true",
                     help="Match every component explicitly.")
    parser.add_argument("--match-any", action="store_true",
                        help="OR the selectors instead of ANDing them.")
    parser.add_argument("--ignore-case", action="store_true",
                        help="Case-insensitive text matching.")


def build_predicate(args):
    """Assemble the query predicate from parsed selector args.

    Returns ``None`` when no selector was given (and ``--all`` wasn't passed) --
    callers decide whether that means "no search" (list) or an error (set).
    """
    ic = args.ignore_case
    conditions = []

    include, exclude = [], []
    for v in args.name_contains or []:
        s, negated = split_negation(v)
        (exclude if negated else include).append(s)
    exclude.extend(args.name_excludes or [])
    if include:
        conditions.append(q.name_contains(*include, ignore_case=ic))
    if exclude:
        conditions.append(q.name_excludes(*exclude, ignore_case=ic))

    if args.name_regex:
        s, negated = split_negation(args.name_regex)
        pred = _regex(q.name_regex, s, ignore_case=ic)
        conditions.append(q.negate(pred) if negated else pred)

    for spec in args.param or []:
        name, value, negated = split_pair(spec, "--param")
        pred = q.param_equals(name, value, ignore_case=ic)
        conditions.append(q.negate(pred) if negated else pred)
    for spec in args.param_contains or []:
        name, value, negated = split_pair(spec, "--param-contains")
        pred = q.param_contains(name, value, ignore_case=ic)
        conditions.append(q.negate(pred) if negated else pred)
    for spec in args.param_regex or []:
        name, value, negated = split_pair(spec, "--param-regex")
        pred = _regex(q.param_regex, name, value, ignore_case=ic)
        conditions.append(q.negate(pred) if negated else pred)
    for v in args.param_exists or []:
        name, negated = split_negation(v)
        conditions.append(q.param_missing(name) if negated else q.param_exists(name))
    for v in args.param_missing or []:
        name, negated = split_negation(v)
        conditions.append(q.param_exists(name) if negated else q.param_missing(name))

    dpos, dneg = [], []
    for v in args.designator_prefix or []:
        s, negated = split_negation(v)
        (dneg if negated else dpos).append(s)
    if dpos:
        conditions.append(q.any_of(*[
            q.designator_prefix(p, ignore_case=ic) for p in dpos]))
    for p in dneg:
        conditions.append(q.negate(q.designator_prefix(p, ignore_case=ic)))

    if args.pins_min is not None or args.pins_max is not None:
        conditions.append(q.pins(args.pins_min, args.pins_max))

    if not conditions:
        return q.always() if args.all else None
    return q.any_of(*conditions) if args.match_any else q.all_of(*conditions)


def select_components(lib, predicate):
    """Names of the components matching ``predicate``, in FileHeader order."""
    return [c.name for c in lib.components if predicate(c)]

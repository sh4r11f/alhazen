"""Every string a live monitor reader sees, written in a journal figure's conventions.

:mod:`alhazen.live_monitor.panels` computes what each panel plots and names
things in the record's terms (``rt_ms``, ``FIX_BREAK``), because that is what
its statistics and its tests are about. This module decides how those names,
numbers and sentences *read*: a column name as words with its unit
(:func:`axis_label`, :func:`split_unit`), a number with as many decimals as it
deserves (:func:`format_number`), and :func:`present`, the one pass every
payload goes through before it leaves Python.

It lives apart from the panel computations so a reader of those can skip it:
nothing here counts, bins or averages anything, and nothing in the panels
depends on how a label is cased.

:func:`format_number` has a twin, ``fmt`` in ``assets/live_monitor.js``, for the
numbers the page formats itself (axis ticks, hover read-outs);
``tests/js/helpers.test.mjs`` checks the page's side of the same rule. Change one,
change both.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Column names in this framework carry their unit as a suffix — ``rt_ms``,
# ``endpoint_x_dva``. Reading it off the name is what lets every axis be
# labelled with a unit without every task having to say so.
_UNIT_SUFFIXES = {
    "ms": "ms",
    "s": "s",
    "us": "µs",
    "hz": "Hz",
    # Degrees of visual angle are written as the degree sign, the convention
    # of vision-science figures, not as the column suffix that means it.
    "dva": "°",
    "deg": "°",
    "px": "px",
    "mm": "mm",
    "cm": "cm",
    "ul": "µL",
    "ml": "mL",
    "pct": "%",
    "v": "V",
}


def format_number(value: float) -> str:
    """A number with as many decimals as it deserves and no more."""
    if not math.isfinite(value):
        return "—"
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 100:
        return f"{value:.0f}"
    if magnitude >= 10:
        return f"{value:.1f}"
    if magnitude >= 1:
        return f"{value:.2f}"
    if magnitude == 0:
        return "0"
    return f"{value:.3g}"


# Abbreviations a figure writes in their conventional case, not as words. A
# column named ``saccade_rt_ms`` is "saccade RT", never "saccade rt".
_ABBREVIATIONS = {
    "rt": "RT",
    "iqr": "IQR",
    "ci": "CI",
    "sd": "SD",
    "sem": "s.e.m.",
    "id": "ID",
    "isi": "ISI",
    "iti": "ITI",
    "soa": "SOA",
    "rf": "RF",
    "eeg": "EEG",
    "lfp": "LFP",
    "roi": "ROI",
    "fov": "FOV",
}


def _field_words(tokens: list[str]) -> list[str]:
    """Lowercase words from a column's underscore-separated parts, with
    abbreviations in their conventional case and a leading ``n`` spelled out:
    ``n_inducers`` is "number of inducers"."""
    words = [_ABBREVIATIONS.get(token.lower(), token.lower()) for token in tokens if token]
    if len(words) > 1 and words[0] == "n":
        words = ["number", "of", *words[1:]]
    return words


def split_unit(field: str) -> tuple[str, str | None]:
    """``'rt_ms'`` -> ``('RT', 'ms')``; ``'response_key'`` -> ``('response key', None)``.

    The name comes back as the words a sentence would use mid-way, so it can
    sit inside a message; :func:`display_name` gives it its capital.
    """
    parts = field.split("_")
    unit = None
    if len(parts) > 1 and parts[-1].lower() in _UNIT_SUFFIXES:
        unit = _UNIT_SUFFIXES[parts[-1].lower()]
        parts = parts[:-1]
    return " ".join(_field_words(parts)), unit


def sentence_start(text: str) -> str:
    """Capitalise a label's first letter, and nothing else.

    Journal figures write labels in sentence case. A first word that is
    lowercase on purpose is left alone: the sample size ``n``, and ``x`` or
    ``y`` naming a coordinate by itself.
    """
    if not text or not text[0].islower():
        return text
    first = re.split(r"[\s(]", text, maxsplit=1)[0]
    if first in {"n", "x", "y"}:
        return text
    return text[0].upper() + text[1:]


def display_name(field: str) -> str:
    """A record column's name as a figure writes it, without its unit.

    ``'saccade_latency_ms'`` -> ``'Saccade latency'``; ``'rt_ms'`` -> ``'RT'``.
    """
    return sentence_start(split_unit(field)[0])


def axis_label(field: str | None, unit: str | None = None) -> str:
    """The text under (or beside) an axis, unit included when one is known."""
    if not field:
        return ""
    name, detected = split_unit(field)
    shown = unit or detected
    name = sentence_start(name)
    return f"{name} ({shown})" if shown else name


# A record-style name leaking into prose: FIX_BREAK, cue_report_correct.
_SNAKE_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
# A hyphen standing in for a minus sign in front of a number.
_HYPHEN_MINUS = re.compile(r"(?<![\w.])-(?=\d)")
# The space a unit suffix leaves between a number and a degree sign.
_SPACED_DEGREE = re.compile(r"(\d)\s+°")
# The keys of a payload that are prose a reader sees.
_PROSE_KEYS = (
    "x_label",
    "y_label",
    "value_label",
    "error_label",
    "note",
    "message",
    "origin_label",
)


def display_value(value: Any) -> str:
    """A level, outcome or response as a figure writes it.

    Record constants (``LANDED_ON_FIGURE``) and lowercase levels (``aligned``)
    become sentence case, with abbreviations kept in theirs. Numbers keep
    their digits and gain a true minus sign. Anything written in mixed case on
    purpose (``Kanizsa``, ``95% CI``) is left as written, and so is a short
    all-caps abbreviation (``RT``, ``ESC``).
    """
    text = str(value).strip()
    if not text:
        return text
    try:
        float(text)
    except ValueError:
        pass
    else:
        return _HYPHEN_MINUS.sub("\u2212", text)
    letters = [ch for ch in text if ch.isalpha()]
    shouting = bool(letters) and all(ch.isupper() for ch in letters)
    quiet = bool(letters) and all(ch.islower() for ch in letters)
    if "_" in text or (shouting and len(letters) > 3) or quiet:
        words = " ".join(_field_words(re.split(r"[_\s]+", text)))
        return sentence_start(words)
    return text


def _prose(text: str) -> str:
    """A label or sentence on a figure: record-style names spelled out as
    words, a true minus sign, the degree sign against its number, and a
    capital first letter."""
    text = _SNAKE_TOKEN.sub(lambda match: split_unit(match.group(0))[0], text)
    text = _HYPHEN_MINUS.sub("\u2212", text)
    text = _SPACED_DEGREE.sub(r"\1°", text)
    return sentence_start(text)


def _stat_value(text: str) -> str:
    """A number in the stats strip, or a one-word verdict (``FAILED``)."""
    if any(ch.isdigit() for ch in text):
        return _SPACED_DEGREE.sub(r"\1°", _HYPHEN_MINUS.sub("\u2212", text))
    return display_value(text)


def present(payload: dict[str, Any]) -> dict[str, Any]:
    """Write every string in a payload the way a journal figure does.

    The panel builders name things in the record's terms, because that is
    what their tests and their logic are about. What the reader sees is
    decided here, once, for every panel: labels in sentence case, column and
    outcome names as words, abbreviations in their usual case, degrees as the
    degree sign, and a true minus sign. Prose (axis titles, notes, stat
    labels) is rewritten in place; data values gain a ``display_*`` twin and
    are otherwise left alone. Numbers the page draws are never touched.
    Applying it twice changes nothing.
    """
    for key in _PROSE_KEYS:
        if isinstance(payload.get(key), str):
            payload[key] = _prose(payload[key])
    if isinstance(payload.get("color_label"), str) and payload["color_label"]:
        payload["display_color_label"] = display_name(payload["color_label"])
    if isinstance(payload.get("label"), str):
        payload["label"] = _prose(payload["label"])
    if isinstance(payload.get("secondary"), str):
        payload["secondary"] = _stat_value(payload["secondary"])
    for stat in payload.get("stats") or []:
        if isinstance(stat.get("label"), str):
            stat["label"] = _prose(stat["label"])
        if isinstance(stat.get("value"), str):
            stat["value"] = _stat_value(stat["value"])
    # Data values keep their record form: a level, an outcome, a response
    # key, the column a factor lives in. Code that maps a panel back to its
    # trials compares them with the record, and a sentence-cased "Occluder"
    # would silently stop matching "occluder". What the reader sees travels
    # beside each one as a ``display_*`` field, and the page draws that.
    for item in payload.get("items") or []:
        if isinstance(item.get("label"), str):
            item["display_label"] = display_value(item["label"])
    for series in payload.get("series") or []:
        if isinstance(series.get("name"), str) and series["name"]:
            series["display_name"] = display_value(series["name"])
    band = payload.get("band")
    if isinstance(band, dict) and isinstance(band.get("name"), str):
        band["display_name"] = display_value(band["name"])
    for group in payload.get("groups") or []:
        if isinstance(group.get("label"), str):
            group["display_label"] = display_value(group["label"])
        if isinstance(group.get("series"), str) and group["series"]:
            group["display_series"] = display_name(group["series"])
    for one_map in payload.get("maps") or []:
        if isinstance(one_map.get("name"), str) and one_map["name"]:
            one_map["display_name"] = display_value(one_map["name"])
    return payload

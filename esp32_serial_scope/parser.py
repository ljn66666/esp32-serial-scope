"""Line parsers for esp32-serial-scope.

One line of serial text becomes an ordered mapping of ``name -> float``.

Supported shapes
----------------
``key:value``
    ``temp:23.5,hum:60``   or   ``temp:23.5 hum:60``
``key=value``
    ``temp=23.5,hum=60``
plain CSV
    ``23.5,60,1.2``  paired with ``--labels temp,hum,vbus``.
    A non-numeric first line such as ``temp,hum,vbus`` is treated as a
    header and learned automatically.

Design notes
------------
* Keys must start with a letter or underscore, so a timestamp such as
  ``12:30`` is ignored instead of being read as data.
* A value is read as the first number found after the separator, so units
  and suffixes are fine: ``rssi:-62dBm`` becomes ``-62.0``.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import List, Optional

_NUMBER = r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?"
_KEY = r"[A-Za-z_][A-Za-z0-9_.\-]*"

_KV_PAIR_RE = re.compile(r"(" + _KEY + r")\s*[:=]\s*(" + _NUMBER + r")")
_KV_HINT_RE = re.compile(r"(" + _KEY + r")\s*[:=]")
_NUMBER_RE = re.compile(_NUMBER)
_NUMBER_ONLY_RE = re.compile(r"^\s*" + _NUMBER + r"\s*$")

MODE_AUTO = "auto"
MODE_KV = "kv"
MODE_CSV = "csv"


def extract_first_number(text):
    """Return the first number in *text* as a ``float``, otherwise ``None``."""
    if text is None:
        return None
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:  # pragma: no cover - defensive
        return None


def looks_like_kv(line):
    """Return ``True`` when *line* contains at least one ``key``/separator pair."""
    if not line:
        return False
    return _KV_HINT_RE.search(line) is not None


def parse_kv(line):
    """Parse ``key:value`` / ``key=value`` pairs into an ``OrderedDict``.

    Returns ``None`` when the line holds no usable pairs.
    """
    record = OrderedDict()
    for key, value in _KV_PAIR_RE.findall(line or ""):
        try:
            record[key] = float(value)
        except ValueError:  # pragma: no cover - regex already restricts values
            continue
    return record or None


def split_tokens(line):
    """Split a CSV-ish line on commas, semicolons or tabs."""
    if ";" in line and "," not in line:
        parts = line.split(";")
    elif "\t" in line and "," not in line:
        parts = line.split("\t")
    else:
        parts = line.split(",")
    return [part.strip() for part in parts]


def parse_csv(line, labels=None):
    """Parse a plain numeric CSV line into an ``OrderedDict``.

    Columns are named from *labels* when given, otherwise ``ch0``, ``ch1``...
    Returns ``None`` when any column is not numeric.
    """
    tokens = [token for token in split_tokens(line or "") if token != ""]
    if not tokens:
        return None

    values = []
    for token in tokens:
        if _NUMBER_ONLY_RE.match(token):
            values.append(float(token))
            continue
        number = extract_first_number(token)
        if number is None:
            return None
        values.append(number)

    record = OrderedDict()
    for index, value in enumerate(values):
        if labels and index < len(labels):
            name = labels[index]
        else:
            name = "ch%d" % index
        record[name] = value
    return record or None


class LineParser:
    """Stateful parser that auto-detects ``kv`` vs ``csv`` input.

    In ``csv`` mode the first all-text line is captured as the column header
    unless explicit ``labels`` were supplied.
    """

    def __init__(self, labels=None, mode=MODE_AUTO):
        self.mode = mode if mode in (MODE_AUTO, MODE_KV, MODE_CSV) else MODE_AUTO
        self.labels: Optional[List[str]] = list(labels) if labels else None
        self.lines_seen = 0
        self.records_seen = 0

    def parse(self, line):
        """Parse one line into an ``OrderedDict`` or return ``None``."""
        if line is None:
            return None
        text = line.strip()
        if not text:
            return None
        self.lines_seen += 1

        if self.mode in (MODE_AUTO, MODE_KV):
            record = parse_kv(text)
            if record:
                self.records_seen += 1
                return record
            if self.mode == MODE_KV:
                return None

        if self.mode in (MODE_AUTO, MODE_CSV):
            # In auto mode a line containing ":" is treated as kv-only, so a
            # timestamp such as "12:30 boot ok" is ignored instead of being
            # misread as the single CSV column "12.0". Explicit --mode csv
            # always parses.
            if self.mode == MODE_AUTO and ":" in text:
                return None
            if self._maybe_learn_header(text):
                return None
            record = parse_csv(text, self.labels)
            if record:
                self.records_seen += 1
            return record
        return None

    def _maybe_learn_header(self, text):
        if self.labels is not None:
            return False
        tokens = [token for token in split_tokens(text) if token]
        if len(tokens) < 2:
            return False
        for token in tokens:
            if extract_first_number(token) is not None:
                return False
        self.labels = tokens
        return True

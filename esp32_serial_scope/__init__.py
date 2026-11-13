"""esp32-serial-scope: live line charts for ESP32 serial data.

Only cheap, dependency-light helpers are exported here, so that
``import esp32_serial_scope`` never pulls in matplotlib. The plotting code
loads lazily, when the command line tool actually needs it.
"""
from __future__ import annotations

from .parser import (
    LineParser,
    extract_first_number,
    looks_like_kv,
    parse_csv,
    parse_kv,
)

__version__ = "0.1.0"
__author__ = "esp32-serial-scope contributors"

__all__ = [
    "LineParser",
    "extract_first_number",
    "looks_like_kv",
    "parse_csv",
    "parse_kv",
    "__version__",
]

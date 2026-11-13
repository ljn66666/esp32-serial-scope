"""Unit tests for esp32_serial_scope.parser. Run with: pytest -q"""
from esp32_serial_scope.parser import (
    LineParser,
    extract_first_number,
    parse_csv,
    parse_kv,
)


def test_kv_colon_comma():
    assert parse_kv("temp:23.5,hum:60") == {"temp": 23.5, "hum": 60.0}


def test_kv_equals_spaces():
    assert dict(parse_kv("temp=23.5 hum=60")) == {"temp": 23.5, "hum": 60.0}


def test_kv_units_and_negative_values():
    record = parse_kv("rssi:-62dBm,adc:2048,cpu:80MHz")
    assert dict(record) == {"rssi": -62.0, "adc": 2048.0, "cpu": 80.0}


def test_kv_rejects_plain_text():
    assert parse_kv("boot ok") is None
    assert parse_kv("=== ready ===") is None


def test_csv_with_labels():
    record = parse_csv("23.5,60,5.02", ["temp", "hum", "vbus"])
    assert dict(record) == {"temp": 23.5, "hum": 60.0, "vbus": 5.02}


def test_csv_without_labels_falls_back_to_ch_names():
    assert dict(parse_csv("1,2,3")) == {"ch0": 1.0, "ch1": 2.0, "ch2": 3.0}


def test_csv_rejects_text():
    assert parse_csv("hello,world") is None


def test_extract_first_number():
    assert extract_first_number("v=1.5e3x") == 1500.0
    assert extract_first_number("no digits here") is None
    assert extract_first_number("") is None


def test_auto_mode_ignores_timestamps():
    parser = LineParser()
    assert parser.parse("12:30 boot ok") is None
    assert parser.parse("2026-10-08 12:30:00 ready") is None


def test_auto_mode_learns_csv_header():
    parser = LineParser()
    assert parser.parse("temp,hum,vbus") is None
    assert dict(parser.parse("23.5,60,5.02")) == {"temp": 23.5, "hum": 60.0, "vbus": 5.02}


def test_auto_mode_prefers_kv():
    parser = LineParser()
    assert dict(parser.parse("temp:1,hum:2")) == {"temp": 1.0, "hum": 2.0}


def test_kv_only_mode_skips_csv():
    parser = LineParser(mode="kv")
    assert parser.parse("1,2,3") is None


def test_counters_track_progress():
    parser = LineParser()
    parser.parse("boot ok")
    parser.parse("temp:1,hum:2")
    assert parser.lines_seen == 2
    assert parser.records_seen == 1

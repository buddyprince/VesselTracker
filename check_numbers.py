# -*- coding: utf-8 -*-
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
sys.path.insert(0, r"F:\OneDrive\中粮\船表")

import json
from pathlib import Path
import pandas as pd

cache_path = Path(r"F:\OneDrive\中粮\船表\VesselProcessAPI\.json\.match_tickets\.matched_tickets_with_port_calls.json")

# Save original
if cache_path.is_file():
    original = cache_path.read_text(encoding="utf-8")
else:
    original = None

mock_brazil = [
    {"query_vessel_name": "Ship1", "arrival_time": "2026-09-01", "quantity": "5000000", "status": "unique"},
    {"query_vessel_name": "Ship2", "arrival_time": "2026-09-15", "quantity": "4831935", "status": "unique"},
]
mock_gulf = [
    {"query_vessel_name": "Ship3", "arrival_time": "2026-09-10", "quantity": "1000000", "status": "unique"},
]
mock_west = [
    {"query_vessel_name": "Ship4", "arrival_time": "2026-09-20", "quantity": "2000000", "status": "unique"},
]
mock_arg = [
    {"query_vessel_name": "Ship5", "arrival_time": "2026-09-05", "quantity": "500000", "status": "unique"},
]

from VesselProcessAPI.Presentation import Present
present = Present()

# Test 1: Old cache with named keys only (no "0")
cache1 = {"updated_at": "2026-09-21", "items": {
    "巴西": mock_brazil, "美湾": mock_gulf, "美西": mock_west, "阿根廷": mock_arg,
}}
cache_path.write_text(json.dumps(cache1, ensure_ascii=False), encoding="utf-8")
r1 = present.combine_initial_vessel_sheet_with_query_result("1900-01-01", "2100-01-01", None, sheet_name=None)
print("Test1 (no key '0'): combine(None) rows =", len(r1), "  expected=5 (merged)")

# Test 2: Cache with key "0" + old named keys
cache2 = {"updated_at": "2026-09-21", "items": {
    "0": mock_brazil, "巴西": mock_brazil, "美湾": mock_gulf, "美西": mock_west, "阿根廷": mock_arg,
}}
cache_path.write_text(json.dumps(cache2, ensure_ascii=False), encoding="utf-8")
r2 = present.combine_initial_vessel_sheet_with_query_result("1900-01-01", "2100-01-01", None, sheet_name=None)
print("Test2 (key '0' + old): combine(None) rows =", len(r2), "  expected=2 (only key '0')")

# Test 3: Cache with ONLY key "0" (after precompute deletes old)
cache3 = {"updated_at": "2026-09-21", "items": {
    "0": mock_brazil,
}}
cache_path.write_text(json.dumps(cache3, ensure_ascii=False), encoding="utf-8")
r3 = present.combine_initial_vessel_sheet_with_query_result("1900-01-01", "2100-01-01", None, sheet_name=None)
print("Test3 (only key '0'): combine(None) rows =", len(r3), "  expected=2 (only key '0')")

# Restore
if original:
    cache_path.write_text(original, encoding="utf-8")
    print("\nOriginal cache restored.")
else:
    cache_path.unlink(missing_ok=True)
    print("\nOriginal cache deleted (was already missing).")

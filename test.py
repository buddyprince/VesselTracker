from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from VesselProcessAPI import Present, Store  # noqa: E402

NAME = "TONG SHUN"

info = Present().get_vessel_info_df
hits = info[info["query_vessel_name"].astype(str).str.strip() == NAME]
print("=== vessel_info ===")
cols = [c for c in ["query_vessel_name", "status", "mmsi", "vesselNameEn"] if c in hits.columns]
print(hits[cols].to_string(index=False) if len(hits) else "未找到")

if len(hits) and hits["mmsi"].notna().any():
    store = Store()
    hist = store.load("history_ports")
    for mmsi in hits["mmsi"].dropna().astype(str).unique():
        records = hist.get(mmsi) or []
        print(f"\n=== history_ports mmsi={mmsi} ({len(records)} 条) ===")
        if records:
            df = pd.DataFrame(records)
            print(df.to_string(index=False))
        else:
            print("无历史靠泊数据")

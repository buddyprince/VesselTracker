"""MatchTicket: 归一化原始数据，实现配票逻辑。"""

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from .JsonOperation import JsonOperation
from .Store import Store
from .Tool import Tool


class MatchTicket:
    """读取原始 JSON 和船表，归一化挂靠事件，完成配票。

    Attributes:
        ACTUAL_FIELDS: 实际到港时间回退链。
        ETA_FIELDS: 预计到港时间回退链。
    """

    ACTUAL_FIELDS = (
        "atBerthA", "atBerthArrival", "ata",
        "atAnchorA", "atAnchorArrival", "atPortArrival",
    )
    ETA_FIELDS = ("etbStd", "etb", "etaStd", "eta")

    def __init__(self, save_returns: Optional[str] = None):
        """初始化。

        Args:
            save_returns: 存档目录路径，默认包内 .json/.saved_returns。
        """
        self._store = Store(save_returns) if save_returns else Store()
        self._io = JsonOperation(Path(__file__).with_name(".json") / ".match_tickets")

    # ── 缓存读写 ────────────────────────────

    def _read_cache(self, name: str, key: Optional[str] = None) -> Optional[pd.DataFrame]:
        """读缓存; 命中返回 DataFrame, 未命中/过期返回 None。

        Args:
            name: 缓存文件名。
            key: 多国分键缓存的国家码; None 时 items 直接是行列表。
        """
        cache = self._io.read(name)
        # 时间戳不一致 => Store 已更新, 缓存过期
        if not JsonOperation.cache_fresh(cache, self._store.updated_at):
            return None
        items = cache.get("items")
        if key is not None:
            # 分键结构 {country: rows}; 缺该国 => 未命中
            if not isinstance(items, dict) or key not in items:
                return None
            items = items[key]
        # 空 items 也按命中处理, 由 _ensure_vessel_info_cols 补列
        if not isinstance(items, list):
            return None
        df = pd.DataFrame(items)
        if df.empty and key is None:
            return df
        # JSON 还原类型: mmsi 统一 string, 时间列转 datetime
        if "mmsi" in df.columns:
            df["mmsi"] = df["mmsi"].astype("string")
        for col in ("arrival_time", "departure_time"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        return df

    def _write_cache(self, name: str, df: pd.DataFrame, key: Optional[str] = None) -> None:
        """按 Store 时间戳写入缓存。

        Args:
            name: 缓存文件名。
            df: 待写入的 DataFrame。
            key: 多国分键缓存的国家码; None 时 items 直接是行列表。
        """
        # DataFrame -> JSON records; 日期转 ISO 字符串, 中文不转义
        rows = json.loads(
            df.to_json(orient="records", force_ascii=False, date_format="iso")
        )
        if key is None:
            # 平铺结构: items 直接是行列表, 整体覆盖
            items = rows
        else:
            # 分键结构: 保留同快照内其他国家, 只覆盖当前 key
            cache = self._io.read(name)
            if JsonOperation.cache_fresh(cache, self._store.updated_at):
                items = dict(cache.get("items") or {})
            else:
                # 旧缓存过期/损坏时清空重建, 避免混入过期国家数据
                items = {}
            items[key] = rows
        # updated_at 记为当前 Store 时间戳, 与 _read_cache 的新鲜度判断对齐
        self._io.write(name, {
            "updated_at": self._store.updated_at,
            "items": items,
        })

    # ── info_df ────────────────────────

    VESSEL_INFO_BASE_COLS = (
        "query_vessel_name", "status", "mmsi", "shipType", "flagName",
    )

    def get_vessel_info_df(self) -> pd.DataFrame:
        """带缓存的 vessel_info DataFrame: 同一 Store 快照内不重复构建。"""
        df = self._read_cache(".info_df.json")
        if df is not None:
            return self._ensure_vessel_info_cols(df)
        df = self._ensure_vessel_info_cols(self._build_vessel_info_df())
        self._write_cache(".info_df.json", df)
        return df

    @classmethod
    def _ensure_vessel_info_cols(cls, df: pd.DataFrame) -> pd.DataFrame:
        """补齐 vessel_info 基础列, 避免下游 merge/drop_duplicates KeyError。"""
        return Tool.ensure_columns(df, cls.VESSEL_INFO_BASE_COLS)

    # ── port_calls_by_country ───────────────────────

    def get_port_calls_by_country(self, country: str = "CN") -> pd.DataFrame:
        """带缓存的 port_calls: 同一 Store 快照内不重复构建。"""
        code = country.strip().upper()
        df = self._read_cache(".port_calls_by_country.json", code)
        if df is not None:
            return df
        df = self._build_port_calls_by_country(code)
        self._write_cache(".port_calls_by_country.json", df, code)
        return df

    # ── 时间解析 ──────────────────────────────────────────────

    @staticmethod
    def _first_parseable(rec: dict, fields: tuple):
        """按回退链取第一个可解析的时间字段。

        Args:
            rec: 原始记录字典。
            fields: 候选时间字段名元组。

        Returns:
            (pd.Timestamp, field_name) 或 (pd.NaT, None)。
        """
        for field in fields:
            value = rec.get(field)
            if value:
                ts = pd.to_datetime(value, errors="coerce")
                if pd.notna(ts):
                    return ts, field
        return pd.NaT, None

    # ── vessel_info 扁平化 ────────────────────────────────────

    def _build_vessel_info_df(self) -> pd.DataFrame:
        """从 Store 加载 vessel_info dict，扁平化为 DataFrame。

        Returns:
            列: query_vessel_name, status, mmsi, vesselNameEn 等。
            mmsi 统一为字符串类型。
        """
        info_dict = self._store.load("vessel_info")
        rows = []
        for name, entry in info_dict.items():
            status = entry["status"]
            if status == "not_found":
                rows.append({"query_vessel_name": name, "status": status})
            elif status == "unique":
                rows.append({
                    "query_vessel_name": name, "status": status,
                    **(entry["info"] or {}),
                })
            else:  # multiple
                for info in entry["info"]:
                    rows.append({
                        "query_vessel_name": name, "status": status,
                        **(info or {}),
                    })

        df = pd.DataFrame(rows)
        df = Tool.ensure_columns(df, self.VESSEL_INFO_BASE_COLS)
        if "mmsi" in df.columns:
            df["mmsi"] = df["mmsi"].apply(
                lambda x: str(int(x))
                if pd.notna(x) and isinstance(x, (int, float)) else x
            )
            df["mmsi"] = df["mmsi"].astype("string")
        return df

    # ── port_calls 归一化 ────────────────────────────────────

    def _build_port_calls_by_country(
        self,
        country: str = "CN",
    ) -> pd.DataFrame:
        """归一化全部 mmsi 的挂靠事件。

        history_ports + current_port -> actual 挂靠；ais -> ETA。
        不做跨文件去重，arrival >= departure 已保证不跨票重复计数。

        Args:
            country: 目标国家码，只保留该国挂靠。

        Returns:
            列: mmsi, port_code, port_name_en, port_name_cn,
            country_code, arrival_time, departure_time, call_type。
        """
        hist = self._store.load("history_ports")
        cur_map = self._store.load("current_port")
        ais_map = self._store.load("ais")
        code = country.strip().upper()
        rows = []

        for mmsi in set(hist) | set(cur_map) | set(ais_map):
            records:list[dict] = list(hist.get(mmsi) or [])
            cur = cur_map.get(mmsi)
            if isinstance(cur, list):
                records += cur
            elif isinstance(cur, dict):
                records.append(cur)

            for rec in records:
                if str(rec.get("countryCode") or "").strip().upper() != code:
                    continue
                arrival, source = self._first_parseable(rec, self.ACTUAL_FIELDS)
                if pd.isna(arrival):
                    continue
                rows.append({
                    "mmsi": str(mmsi),
                    "port_code": rec.get("arrivalPortCodeStd")
                                 or rec.get("portCodeStd"),
                    "port_name_en": rec.get("arrivalPortName")
                                    or rec.get("portEnName"),
                    "port_name_cn": rec.get("arrivalPortCName")
                                    or rec.get("portCnName"),
                    "country_code": code,
                    "arrival_time": arrival,
                    "departure_time": pd.to_datetime(
                        rec.get("atd"), errors="coerce"
                    ),
                    "call_type": "actual",
                    "arrival_source": source,
                })

            ais = ais_map.get(mmsi) or {}
            eta, source = self._first_parseable(ais, self.ETA_FIELDS)
            if pd.notna(eta):
                dest_code = str(
                    ais.get("destcode") or ""
                ).strip().upper()
                if dest_code[:2] == code:
                    dest_text = str(
                        ais.get("destStd") or ais.get("dest") or ""
                    )
                    rows.append({
                        "mmsi": str(mmsi),
                        "port_code": dest_code[:4] or None,
                        "port_name_en": dest_text or None,
                        "port_name_cn": None,
                        "country_code": code,
                        "arrival_time": eta,
                        "departure_time": pd.NaT,
                        "call_type": "eta",
                        "arrival_source": source,
                    })

        cols = [
            "mmsi", "port_code", "port_name_en", "port_name_cn",
            "country_code", "arrival_time", "departure_time", "call_type",
            "arrival_source",
        ]
        df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
        df["arrival_time"] = pd.to_datetime(df["arrival_time"], errors="coerce")
        df["departure_time"] = pd.to_datetime(df["departure_time"], errors="coerce")
        return df

    # ── 配票 ──────────────────────────────────────────────────

    def _greedy_assign_tickets_with_port_calls(
        self,
        tickets: pd.DataFrame,
        port_calls: pd.DataFrame,
        max_voyage_days: "int | None" = None,
    ) -> pd.DataFrame:
        """贪心配票（actual + ETA + estimated 兜底）。

        同 mmsi 的票按 departure 升序，挂靠按 arrival 升序，每条 actual
        挂靠只消耗一次。actual 优先 -> ETA 回退 -> estimated 兜底。
        estimated 行的 arrival_time 暂填 NaT，由调用方在 combine 中补算。

        航程约束: arrival < departure 的挂靠对后续票也过早, 跳过并消费;
        arrival - departure > max_voyage_days 只对当前票不合法, 不消费
        该挂靠 (留给 departure 更晚、航程更短的票), 当前票走 ETA/estimated。

        Args:
            tickets: 含 mmsi, departure_from_origin_port 的 DataFrame。
            port_calls: 归一化挂靠事件。
            max_voyage_days: 航程上界(天), None 不限制。

        Returns:
            添加了挂靠信息的 assigned DataFrame。
        """
        assigned = tickets.copy()
        assigned["call_type"] = None

        if assigned.empty or port_calls.empty:
            return assigned

        max_days = int(max_voyage_days) if max_voyage_days else None

        for mmsi, grp in assigned.groupby("mmsi"):
            calls = port_calls[
                port_calls.mmsi == mmsi
            ].sort_values("arrival_time")
            actuals = calls[
                calls.call_type == "actual"
            ].to_dict("records")
            eta_row = (
                calls[calls.call_type == "eta"]
                .sort_values("arrival_time")
                .iloc[0]
                .to_dict()
                if not calls[calls.call_type == "eta"].empty
                else None
            )

            sorted_idx = grp.index[
                grp.loc[grp.index, "departure_from_origin_port"]
                .argsort()
            ]
            ptr = 0
            for idx in sorted_idx:
                dep = grp.loc[idx, "departure_from_origin_port"]
                while (ptr < len(actuals)
                       and actuals[ptr]["arrival_time"] < dep):
                    ptr += 1

                matched = None
                if ptr < len(actuals):
                    voyage = (actuals[ptr]["arrival_time"] - dep).days
                    if max_days is None or voyage <= max_days:
                        matched = actuals[ptr]
                        ptr += 1
                    # voyage > max: 不消费 ptr, 留给 dep 更晚的票

                if (matched is None and eta_row is not None
                        and eta_row["arrival_time"] >= dep):
                    eta_voyage = (eta_row["arrival_time"] - dep).days
                    if max_days is None or eta_voyage <= max_days:
                        matched = eta_row

                if matched:
                    for k, v in matched.items():
                        assigned.loc[idx, k] = v
                else:
                    assigned.loc[idx, "call_type"] = "estimated"
                    assigned.loc[idx, "arrival_source"] = "estimated"

        return assigned

    def generate_matched_tickets_with_port_calls(
        self,
        initial_vessel_sheet,
        datetime_col: str,
        quantity_col: str = 'quantity',
        country: str = "CN",
        sheet_name=0,
        write_cache=True,
        max_voyage_days: "int | None" = 90,
    ) -> pd.DataFrame:
        """读取原始船表，匹配全部挂靠，返回配票结果并缓存到 JSON。

        结果包含全部票（actual / ETA / estimated），不做窗口过滤。
        缓存路径: .json/.match_tickets/.matched_tickets_with_port_calls.json (按 sheet_name 分键)

        Args:
            initial_vessel_sheet: 原始船表路径或 DataFrame。
            datetime_col: 船票日期列名。
            quantity_col: 装运量列名，非 None 时去除逗号并转为 float。
            country: 目标国家码。
            sheet_name: Excel sheet 名或序号。
            max_voyage_days: 配票航程上界(天), None 不限制; 默认与查询超窗一致。

        Returns:
            原始船表列 + port_code, port_name_en, port_name_cn,
            arrival_time, departure_time, call_type。
        """
        # 读原始船表，merge mmsi
        initial_vessel_df = Tool.read_initial_vessel_sheet(
            initial_vessel_sheet, sheet_name=sheet_name, quantity_col=quantity_col
        )
        info_df = self.get_vessel_info_df()
        initial_vessel_df_JOIN_info_df = initial_vessel_df.merge(
            info_df[["query_vessel_name", "mmsi", "status"]],
            how="left", on="query_vessel_name",
        )
        initial_vessel_df_JOIN_info_df[datetime_col] = pd.to_datetime(
            initial_vessel_df_JOIN_info_df[datetime_col], errors="coerce"
        )

        # 构建 tickets + port_calls
        port_calls = self.get_port_calls_by_country(country)
        tickets = initial_vessel_df_JOIN_info_df.rename(
            columns={datetime_col: "departure_from_origin_port"}
        ).copy()
        tickets["mmsi"] = tickets["mmsi"].astype("string")

        # 贪心配票（actual + ETA + estimated 兜底）
        result = self._greedy_assign_tickets_with_port_calls(
            tickets, port_calls, max_voyage_days=max_voyage_days,
        )

        # 保留原始船表列 + 港口信息列
        keep_cols = list(tickets.columns) + [
            "port_code", "port_name_en", "port_name_cn",
            "arrival_time", "departure_time", "call_type", "arrival_source",
        ]
        result = result[keep_cols]

        # 缓存到 JSON (按 sheet_name 分键)
        if write_cache:
            self._write_cache(
                ".matched_tickets_with_port_calls.json", result, key=str(sheet_name)
            )

        return result

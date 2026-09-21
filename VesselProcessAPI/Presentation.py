from typing import TYPE_CHECKING
import json
import logging
from pathlib import Path

import pandas as pd

if TYPE_CHECKING:  # 仅类型检查时导入, 运行时不执行, 切断循环
    import folium
    from .Freightower import FreightowerAPI
from .MatchTicket import MatchTicket
from .Store import Store

log = logging.getLogger(__name__)


class Present:
    """
    将JSON转换为DataFrame展示
    """
    def __init__(
        self,
        api: "FreightowerAPI | None" = None,
        save_returns: "str | None" = None,
        matched_tickets_with_port_calls: "str | None" = None,
    ):
        """
        将JSON转换为DataFrame展示
        Args:
            api(FreightowerAPI): 直接接收类, 按其本批 mmsi 现场拼装展示视图
            save_returns: 存档目录 (.saved_returns) 路径, 默认包内目录;
                API 属性缺失或为空时回退读取该分片存档
            matched_tickets_with_port_calls: 配票缓存 JSON 文件路径,
                默认 .json/.match_tickets/.matched_tickets_with_port_calls.json
        """
        if matched_tickets_with_port_calls is not None:
            self._matched_tickets_path = Path(matched_tickets_with_port_calls)
        else:
            self._matched_tickets_path = (
                Path(__file__).with_name(".json") / ".match_tickets" / ".matched_tickets_with_port_calls.json"
            )
        if api is not None:
            # 船名查询视图直接取 info 块; 宽表按本批 mmsi 从 5 个分块现场拼装
            self._returns_by_vessel_name = dict(api._items["info"])
            mmsi_list = list(api._mmsi_for_search)
            self._returns_by_mmsi = {m: {"mmsi": m} for m in mmsi_list}
            for key in Store.MMSI_KEYED:
                section = api._items[key]
                for m in mmsi_list:
                    if m in section:
                        self._returns_by_mmsi[m][key] = section[m]
        else:
            self._returns_by_mmsi = {}
            self._returns_by_vessel_name = {}
        if not self._returns_by_mmsi or not self._returns_by_vessel_name:
            # API 缺失/为空 (或无参调用) 时回退到分片存档
            try:
                store = Store(save_returns) if save_returns else Store()
                snapshot = store.load_snapshot()
                print(f'读取存储，时间戳：{snapshot.get("saved_at")}')
            except OSError as e:
                log.warning(f"读取分片存档失败: {e}")
                snapshot = {}
            if not self._returns_by_mmsi:
                self._returns_by_mmsi = snapshot.get("returns_by_mmsi") or {}
            if not self._returns_by_vessel_name:
                self._returns_by_vessel_name = snapshot.get("returns_by_vessel_name") or {}

    @property
    def returns_by_vessel_name(self) -> dict:
        """只读船名查询视图 {船名: {status, info}}。"""
        return self._returns_by_vessel_name

    @property
    def returns_by_mmsi(self) -> dict:
        """只读宽表视图 {mmsi: {mmsi, ais, current_port, ...}}。"""
        return self._returns_by_mmsi

    @property
    def get_info_by_vessel_name(self) -> pd.DataFrame:
        """委托 MatchTicket.get_info_df 生成船名查询表（带缓存）。"""
        return MatchTicket().get_info_df()

    def _read_from_search_returns(self, key: str) -> pd.DataFrame:
        """从 search 结果取每个船的键展平成行。

        dict (如 ais) 取一条; list (如 current_port/history_route) 逐条展开;
        无数据的船补一行仅含 mmsi 的空记录。
        """
        result: dict[str, dict] = self._returns_by_mmsi
        rows = []
        for mmsi, entry in result.items():
            val = entry.get(key)
            if isinstance(val, list):
                if val:
                    rows.extend({"mmsi": mmsi, **item} for item in val)
                else:
                    rows.append({"mmsi": mmsi})
            else:
                rows.append(val or {"mmsi": mmsi})
        return pd.DataFrame(rows)

    @property
    def current_situation(self) -> pd.DataFrame:
        """合并每艘船的 ais 与 current_port 全部列, 按 mmsi 左连接。"""
        ais = self._read_from_search_returns('ais')
        current_port = self._read_from_search_returns('current_port')
        ais["mmsi"] = ais["mmsi"].astype(str)
        current_port["mmsi"] = current_port["mmsi"].astype(str)
        return ais.merge(current_port, on="mmsi", how="left", suffixes=("", "_port"))

    # ── 主入口 ────────────────────────────────────────────────

    def combine_initial_vessel_sheet_with_query_result(
            self,
            start: str,
            end: str,
            expected_shipping_days: "int | None" = None,
            sheet_name=None,
        ):
        """读取缓存的配票结果，补算 estimated arrival_time，返回窗口内命中的业务行。

        流程: 读取缓存 -> 补算 estimated arrival_time -> 窗口过滤。
        缓存格式: ``{"updated_at": ISO时间戳, "items": {... 按 sheet_name 分键 ...}}``。

        Args:
            start: 统计窗口起始日期。
            end: 统计窗口截止日期。
            expected_shipping_days: 估算航程天数，None 时不估算。
            sheet_name: 工作表名，用于从缓存中读取对应 sheet 的配票。None 时向兼容平铺格式。

        Returns:
            窗口内命中的业务行。
        """
        start_ts = pd.Timestamp(start).normalize()
        end_ts = pd.Timestamp(end).normalize() + pd.Timedelta(days=1)

        cache = json.loads(self._matched_tickets_path.read_text(encoding="utf-8"))
        raw_items = cache["items"]
        if isinstance(raw_items, dict):
            if sheet_name is not None and str(sheet_name) in raw_items:
                result = pd.DataFrame(raw_items[str(sheet_name)])
            else:
                merged = []
                for sheet_rows in raw_items.values():
                    merged.extend(sheet_rows)
                result = pd.DataFrame(merged)
        else:
            result = pd.DataFrame(raw_items)
        for col in ("arrival_time", "departure_time", "departure_from_origin_port"):
            if col in result.columns:
                result[col] = pd.to_datetime(result[col], errors="coerce")

        # estimated 补算 arrival_time
        if expected_shipping_days is not None:
            estimated = result.call_type == "estimated"
            result.loc[estimated, "arrival_time"] = (
                result.loc[estimated, "departure_from_origin_port"]
                + pd.Timedelta(days=expected_shipping_days)
            )

        # 窗口过滤
        in_window = (
            result["arrival_time"].ge(start_ts)
            & result["arrival_time"].lt(end_ts)
        )
        return result[in_window].reset_index(drop=True)

    @staticmethod
    def _week_num_sat_fri(dates: pd.Series) -> pd.Series:
        """计算周数，每周定义为周六至周五，从1开始。"""
        result = pd.Series(0, index=dates.index, dtype=int)
        for year in dates.dt.year.unique():
            mask = dates.dt.year == year
            jan1 = pd.Timestamp(year, 1, 1)
            first_sat = jan1 + pd.Timedelta(days=(5 - jan1.weekday()) % 7)
            in_range = mask & (dates >= first_sat)
            result[in_range] = ((dates[in_range] - first_sat).dt.days // 7 + 1).astype(int)
        return result

    def get_quantity_statistics(
            self,
            start: str = None,
            end: str = None,
            years: "int | list[int]" = None,
            expected_shipping_days: "int | None" = None,
            sheet_name=None,
            quantity_col: str = "quantity",
            arrival_source_choices: "str | list[str]" = "all",
            status_choices: "str | list[str]" = "all",
        ):
        """统计各年/月/周的装运量。

        Args:
            start: 起始日期，需与 end 同时使用。
            end: 截止日期，需与 start 同时使用。
            years: 统计年份，int 或 list[int]。与 start/end 二选一，start/end 优先。
            expected_shipping_days: 估算航程天数，None 时不估算。
            sheet_name: 工作表名。
            quantity_col: 装运量列名，默认 "quantity"。
            arrival_source_choices: 到港来源筛选，"all" 不过滤，否则按指定值过滤。
            status_choices: 状态筛选，"all" 不过滤，否则按指定值过滤。

        Returns:
            df: 带有 arrival_month 和 arrival_week 列的业务行。
            monthly: {year: DataFrame} 按月 groupby sum 的 quantity，无数据月份补0。
            weekly: {year: DataFrame} 按周 groupby sum 的 quantity，无数据周补0。
        """
        if start and end:
            pass
        elif not start and not end:
            if years is None:
                raise ValueError("years 和 start/end 不能同时为空")
        else:
            raise ValueError("start 和 end 必须同时提供或同时为空")

        if start and end:
            df = self.combine_initial_vessel_sheet_with_query_result(
                start, end, expected_shipping_days, sheet_name
            )
            years = sorted(df["arrival_time"].dt.year.dropna().unique().astype(int))
        else:
            if isinstance(years, int):
                years = [years]
            start = f"{min(years)}-01-01"
            end = f"{max(years)}-12-31"
            df = self.combine_initial_vessel_sheet_with_query_result(
                start, end, expected_shipping_days, sheet_name
            )
        if arrival_source_choices != "all":
            if isinstance(arrival_source_choices, str):
                arrival_source_choices = [arrival_source_choices]
            df = df[df["arrival_source"].isin(arrival_source_choices)]
        if status_choices != "all":
            if isinstance(status_choices, str):
                status_choices = [status_choices]
            df = df[df["status"].isin(status_choices)]
        df = df.copy()
        df["arrival_month"] = df["arrival_time"].dt.month
        df["arrival_week"] = self._week_num_sat_fri(df["arrival_time"])

        def _sum(series):
            return pd.to_numeric(
                series.astype(str).str.replace(",", ""), errors="coerce"
            ).sum()

        monthly = {}
        weekly = {}
        for year in years:
            mask = df["arrival_time"].dt.year == year
            df_year = df.loc[mask]

            m = df_year.groupby("arrival_month").agg(
                quantity_sum=(quantity_col, _sum)
            ).reindex(range(1, 13), fill_value=0).reset_index()
            monthly[year] = m

            max_week = int(df_year["arrival_week"].max()) if not df_year.empty else 0
            w = df_year.groupby("arrival_week").agg(
                quantity_sum=(quantity_col, _sum)
            ).reindex(range(1, max_week + 1), fill_value=0).reset_index()
            weekly[year] = w

        return df, monthly, weekly

    @property
    def route(self) -> "folium.Map":
        """把各船轨迹画在 folium 地图上 (实现已拆到 :class:`~VesselProcessAPI.Map.Map`)。

        传入 Present 初始化得到的 returns_by_mmsi; 调用方式保持不变,
        如 ``Present().route.save("map.html")``。
        """
        from .Map import Map  # 局部导入避免包初始化循环
        return Map(self._returns_by_mmsi).route

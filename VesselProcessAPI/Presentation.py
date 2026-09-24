from typing import TYPE_CHECKING
import json
from pathlib import Path

import pandas as pd

if TYPE_CHECKING:  # 仅类型检查时导入, 运行时不执行, 切断循环
    import folium
from .MatchTicket import MatchTicket
from .Store import Store
from .Tool import Tool


class Present:
    """
    将JSON转换为DataFrame展示
    """
    def __init__(
        self,
        matched_tickets_with_port_calls: "str | None" = None,
    ):
        """
        将JSON转换为DataFrame展示
        Args:
            matched_tickets_with_port_calls: 配票缓存 JSON 文件路径,
                默认 .json/.match_tickets/.matched_tickets_with_port_calls.json
        """
        if matched_tickets_with_port_calls is not None:
            self._matched_tickets_path = Path(matched_tickets_with_port_calls)
        else:
            self._matched_tickets_path = (
                Path(__file__).with_name(".json") / ".match_tickets" / ".matched_tickets_with_port_calls.json"
            )

    @property
    def get_vessel_info_df(self) -> pd.DataFrame:
        """委托 MatchTicket.get_vessel_info_df 生成船名查询表（带缓存）。"""
        return MatchTicket().get_vessel_info_df()

    @property
    def current_situation(self) -> pd.DataFrame:
        """合并每艘船的 ais 与 current_port 全部列, 按 mmsi 左连接。"""
        store = Store()

        def flat(data: dict) -> pd.DataFrame:
            rows = []
            for mmsi, val in data.items():
                if isinstance(val, list):
                    if val:
                        rows.extend({"mmsi": mmsi, **item} for item in val)
                    else:
                        rows.append({"mmsi": mmsi})
                else:
                    rows.append(val or {"mmsi": mmsi})
            if not rows:
                return pd.DataFrame(columns=["mmsi"])
            return pd.DataFrame(rows)

        ais = Tool.ensure_columns(flat(store.load("ais")), ["mmsi"])
        current_port = Tool.ensure_columns(
            flat(store.load("current_port")), ["mmsi"])
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
            outdated_eliminate_window: "int | None" = 90,
        ):
        """读取缓存的配票结果，补算 estimated arrival_time，返回窗口内命中的业务行。

        流程: 读取缓存 -> 补算 estimated arrival_time -> 航程超窗过滤 -> 窗口过滤。
        缓存格式: ``{"updated_at": ISO时间戳, "items": {... 按 sheet_name 分键 ...}}``。

        Args:
            start: 统计窗口起始日期。
            end: 统计窗口截止日期。
            expected_shipping_days: 估算航程天数，None 时不估算。
            sheet_name: 工作表名，用于从缓存中读取对应 sheet 的配票。None 时向兼容平铺格式。
            outdated_eliminate_window: 原始离港到到港超过该天数则剔除；None 或
                <=0 不过滤。默认 90 天。

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

        # 航程超窗剔除: arrival_time - departure_from_origin_port > N 天
        if outdated_eliminate_window and outdated_eliminate_window > 0:
            if (
                "arrival_time" in result.columns
                and "departure_from_origin_port" in result.columns
            ):
                voyage_days = (
                    result["arrival_time"] - result["departure_from_origin_port"]
                ).dt.days
                not_outdated = voyage_days.le(int(outdated_eliminate_window))
                # 只剔除明确超窗的, NaT 保留
                not_outdated = not_outdated | voyage_days.isna()
                result = result[not_outdated]

        # 窗口过滤
        in_window = (
            result["arrival_time"].ge(start_ts)
            & result["arrival_time"].lt(end_ts)
        )
        return result[in_window].reset_index(drop=True)


    ARRIVAL_PHASE_ORDER = ["靠泊", "锚泊", "在途 ETA", "估算到港"]
    ARRIVAL_SOURCE_TO_PHASE = {
        "atBerthA": "靠泊", "atBerthArrival": "靠泊",
        "atAnchorA": "锚泊", "atAnchorArrival": "锚泊",
        "etbStd": "在途 ETA", "etb": "在途 ETA",
        "etaStd": "在途 ETA", "eta": "在途 ETA",
        "estimated": "估算到港",
    }

    def get_quantity_statistics(
            self,
            start: str = None,
            end: str = None,
            years: "int | list[int]" = None,
            expected_shipping_days: "int | None" = None,
            sheet_name=None,
            quantity_col: str = "quantity",
            arrival_source_filter: "str | list[str] | None" = None,
            status_filter: "str | list[str] | None" = None,
            outdated_eliminate_window: "int | None" = 90,
        ):
        """统计各年/月/周的装运量。

        Args:
            start: 起始日期，需与 end 同时使用。
            end: 截止日期，需与 start 同时使用。
            years: 统计年份，int 或 list[int]。与 start/end 二选一，start/end 优先。
            expected_shipping_days: 估算航程天数，None 时不估算。
            sheet_name: 工作表名。
            quantity_col: 装运量列名，默认 "quantity"。
            arrival_source_filter: 到港来源筛选，None 不过滤，否则按指定值过滤。
            status_filter: 状态筛选，None 不过滤，否则按指定值过滤。
            outdated_eliminate_window: 航程超窗剔除天数，见
                combine_initial_vessel_sheet_with_query_result；None 或 <=0 不过滤。

        Returns:
            df: 业务行，仅含 query_vessel_name / mmsi /
                departure_from_origin_port / port / quantity / status /
                port_name_en / port_name_cn / arrival_time / departure_time /
                arrival_source / phase / cal_year / cal_month /
                week_year / week_num（有则保留）。
                汇总请对返回值再调 sum_by_phase，例如
                sum_by_phase(df, "cal_year", "cal_month")。
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
                start, end, expected_shipping_days, sheet_name,
                outdated_eliminate_window=outdated_eliminate_window,
            )
        else:
            if isinstance(years, int):
                years = [years]
            start = f"{min(years)}-01-01"
            end = f"{max(years)}-12-31"
            df = self.combine_initial_vessel_sheet_with_query_result(
                start, end, expected_shipping_days, sheet_name,
                outdated_eliminate_window=outdated_eliminate_window,
            )
        if arrival_source_filter is not None:
            if isinstance(arrival_source_filter, str):
                arrival_source_filter = [arrival_source_filter]
            if "arrival_source" in df.columns:
                df = df[df["arrival_source"].isin(arrival_source_filter)]
        if status_filter is not None:
            if isinstance(status_filter, str):
                status_filter = [status_filter]
            if "status" in df.columns:
                df = df[df["status"].isin(status_filter)]

        df = df.copy()
        if quantity_col in df.columns:
            df[quantity_col] = pd.to_numeric(
                df[quantity_col].astype(str).str.replace(",", "", regex=False).str.strip(),
                errors="coerce",
            ).fillna(0.0)
        df = Tool.ensure_columns(df, {
            "arrival_time": "datetime64[ns]",
            "arrival_source": "object",
        })

        df["cal_year"] = df["arrival_time"].dt.year
        df["cal_month"] = df["arrival_time"].dt.month

        labels = Tool.calculate_week_number(df["arrival_time"])
        df["week_year"] = labels["week_year"]
        df["week_num"] = labels["week_num"]
        df["week_start"] = labels["week_start"]
        df["week_end"] = labels["week_end"]

        df["phase"] = df["arrival_source"].map(self.ARRIVAL_SOURCE_TO_PHASE)

        keep = [
            "query_vessel_name", "mmsi", "departure_from_origin_port", "port",
            "quantity", "status", "port_name_en", "port_name_cn",
            "arrival_time", "departure_time", "arrival_source", "phase",
            "cal_year", "cal_month", "week_year", "week_num",
        ]
        return df[[c for c in keep if c in df.columns]]

    @staticmethod
    def sum_by_phase(
        df: pd.DataFrame,
        *by: str,
        quantity_col: str = "quantity",
    ) -> pd.DataFrame:
        """按 by + phase 汇总 quantity，返回 [by..., phase, quantity_sum]。

        不补缺失格子；画图时 pivot/reindex(fill_value=0) 自行补齐。
        """
        cols = [*by, "phase", "quantity_sum"]
        if quantity_col not in df.columns or "phase" not in df.columns:
            return pd.DataFrame(columns=cols)
        return (
            df.groupby([*by, "phase"], dropna=False)[quantity_col]
            .sum()
            .reset_index(name="quantity_sum")
        )

    @property
    def route(self) -> "folium.Map":
        """把各船轨迹画在 folium 地图上 (实现已拆到 :class:`~VesselProcessAPI.Map.Map`)。

        Map 自读 Store 全量分片; 调用方式保持不变,
        如 ``Present().route.save("map.html")``。
        """
        from .Map import Map  # 局部导入避免包初始化循环
        return Map().route

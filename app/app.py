import contextlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from VesselProcessAPI import FreightowerAPI, MatchTicket, Present, Store, Tool

APP_DATA_DIR = Path(__file__).resolve().parent / "app_data"
INITIAL_XLSX = APP_DATA_DIR / "initial_vessel_sheet.xlsx"


def current_workbook_path():
    raw_path = st.session_state.get("workbook_path") or str(INITIAL_XLSX)
    return Path(raw_path)


MANAGED_COLS = ["status", "mmsi", "shipType", "flagName"]
MAPPING_VIEW_COLS = ["query_vessel_name", *MANAGED_COLS]

st.set_page_config(
    page_title="船表跟踪流程",
    page_icon=":material/directions_boat:",
    layout="wide",
)


class _LogCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(self.format(record))


def snapshot_saved_at():
    try:
        return Store().updated_at
    except Exception:
        return None


def normalize_mmsi(value):
    if pd.isna(value):
        return pd.NA
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return pd.NA
    try:
        return str(int(float(text)))
    except ValueError:
        return text


def load_initial_workbook(path=None):
    workbook_path = Path(path) if path is not None else current_workbook_path()
    return pd.read_excel(workbook_path, sheet_name=None)


def save_workbook(path, sheets):
    tmp = path.with_suffix(".tmp.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=str(name)[:31], index=False)
    os.replace(tmp, path)


@st.cache_data(show_spinner=False)
def mapping_view(sheet_name, snapshot_stamp, workbook_path):
    """当前初始工作表的船名花名册, 左连接 Store 里的查询结果。

    Store 中没有的船名状态/mmsi 留空; unique/not_found 一行, multiple 两候选两行。
    """
    initial = load_initial_workbook(workbook_path)[sheet_name]
    roster = pd.DataFrame({
        "query_vessel_name":
            initial["query_vessel_name"].dropna().astype(str).drop_duplicates()
    })
    info = Present().get_info_by_vessel_name
    info = info[[c for c in MAPPING_VIEW_COLS if c in info.columns]]
    if "query_vessel_name" in info.columns:
        info = info.drop_duplicates(
            subset=["query_vessel_name", "mmsi"], keep="first"
        )
    return roster.merge(info, on="query_vessel_name", how="left")


@st.cache_data(show_spinner=False)
def mmsis_for_names(names, snapshot_stamp):
    """给定船名集合, 返回其在 Store 中涉及的全部 mmsi (multiple 含两个候选)。"""
    info = Present().get_info_by_vessel_name
    if "mmsi" not in info.columns:
        return []
    hit = info[info["query_vessel_name"].astype(str).isin(set(names))]
    return sorted({
        str(m) for m in hit["mmsi"].dropna().astype(str)
        if str(m).strip() and str(m) != "<NA>"
    })


@st.cache_data(show_spinner=False)
def sheet_mmsis(sheet_name, snapshot_stamp, workbook_path):
    """当前工作表花名册船名在 Store 中涉及的全部 mmsi (multiple 含两个候选)。"""
    initial = load_initial_workbook(workbook_path)[sheet_name]
    names = tuple(sorted(
        initial["query_vessel_name"].dropna().astype(str).unique()
    ))
    return mmsis_for_names(names, snapshot_stamp)


def info_entries_from_editor(df):
    """把映射编辑器的行还原为 Store 的 info 信封 {船名: {status, info}}。

    仅保存有明确状态或 mmsi 的船名, 未查询的空行不写入 (避免污染 Store)。
    """
    result = {}
    if "query_vessel_name" not in df.columns:
        return result
    for name, grp in df.groupby("query_vessel_name", sort=False):
        if pd.isna(name):
            continue
        statuses = grp.get("status", pd.Series(dtype=object)).dropna().astype(str)
        status = statuses.iloc[0] if len(statuses) else None
        infos, seen = [], set()
        for _, row in grp.iterrows():
            mmsi = normalize_mmsi(row.get("mmsi"))
            if pd.isna(mmsi):
                continue
            mmsi = str(mmsi)
            if mmsi in seen:
                continue
            seen.add(mmsi)
            info = {"mmsi": mmsi}
            for col in ("shipType", "flagName"):
                if col in df.columns and pd.notna(row.get(col)):
                    info[col] = row.get(col)
            infos.append(info)
        if status == "not_found":
            result[str(name)] = {"status": "not_found", "info": None}
        elif len(infos) > 1 and status == "multiple":
            result[str(name)] = {"status": "multiple", "info": infos[:2]}
        elif infos:
            result[str(name)] = {"status": "unique", "info": infos[0]}
    return result


def precompute_matched_tickets(
    initial_df, country, datetime_col, snapshot_stamp,
):
    """预计算配票并写入共享 JSON 缓存文件。

    注意：此函数有副作用（写文件），不应被 st.cache_data 缓存。
    缓存命中会导致文件不更新，下游读到旧数据。
    """
    # 清空旧缓存，防止 combine(sheet_name=None) 合并多 sheet 旧数据导致重复计数
    cache_path = Path(sys.modules[MatchTicket.__module__].__file__).with_name(".json") / ".match_tickets" / ".matched_tickets_with_port_calls.json"
    if cache_path.is_file():
        cache_path.unlink()
    mt = MatchTicket()
    mt.generate_matched_tickets_with_port_calls(initial_df, datetime_col, country)
    return json.loads(cache_path.read_text(encoding="utf-8")).get("updated_at")


@st.cache_data(show_spinner=False)
def build_tracking_outputs(
    initial_df,
    start,
    end,
    country,
    snapshot_stamp,
    datetime_col=None,
    expected_shipping_days=None,
    sheet_name=None,
):
    precompute_matched_tickets(initial_df, country, datetime_col, snapshot_stamp)
    present = Present()
    combined = present.combine_initial_vessel_sheet_with_query_result(
        str(start), str(end), expected_shipping_days, sheet_name=sheet_name,
    )
    roster = Tool().read_initial_vessel_sheet(initial_df)
    roster_names = set(roster["query_vessel_name"].dropna().astype(str))
    
    # 过滤combined，只保留当前sheet中的船
    if "query_vessel_name" in combined.columns:
        combined = combined[
            combined["query_vessel_name"].astype(str).isin(roster_names)
        ].reset_index(drop=True)
    
    info = present.get_info_by_vessel_name
    mmsis = set(
        info.loc[
            info["query_vessel_name"].isin(roster_names), "mmsi"
        ].dropna().astype(str)
    )
    current = present.current_situation
    if "mmsi" in current.columns:
        current = current[
            current["mmsi"].astype(str).isin(mmsis)
        ].reset_index(drop=True)
    return combined, current, present.returns_by_mmsi


def custom_saturday_weeks(arrival_time: pd.Series) -> pd.DataFrame:
    week_start = (
        arrival_time.dt.normalize()
        - pd.to_timedelta((arrival_time.dt.weekday + 2) % 7, unit="D")
    )
    years = range(int(week_start.dt.year.min()) - 1,
                  int(week_start.dt.year.max()) + 2)
    year_starts = {}
    for year in years:
        jan_first = pd.Timestamp(year=year, month=1, day=1)
        year_starts[year] = jan_first - pd.Timedelta(
            days=(jan_first.weekday() + 2) % 7
        )

    labels = []
    for ws in week_start:
        year = ws.year
        if ws < year_starts[year]:
            year -= 1
        elif ws >= year_starts[year + 1]:
            year += 1
        year_start = year_starts[year]
        week = (ws - year_start).days // 7 + 1
        labels.append((year, int(week), ws, ws + pd.Timedelta(days=6)))
    return pd.DataFrame(
        labels, columns=["year", "week", "week_start", "week_end"]
    )


ARRIVAL_PHASE_ORDER = ["靠泊", "锚泊", "港界待靠", "在途 ETA", "估算到港"]
ARRIVAL_PHASE_COLORS = [
    "#54a24b", "#4c78a8", "#f58518", "#b279a2", "#e45756",
]


ARRIVAL_SOURCE_TO_PHASE = {
    "atBerthA": "靠泊", "atBerthArrival": "靠泊",
    "atAnchorA": "锚泊", "atAnchorArrival": "锚泊",
    "ata": "港界待靠", "atPortArrival": "港界待靠",
    "etbStd": "在途 ETA", "etb": "在途 ETA",
    "etaStd": "在途 ETA", "eta": "在途 ETA",
    "estimated": "估算到港",
}


def _collect_arrival_rows(
    initial_df, country,
    datetime_col=None, expected_shipping_days=None,
    sheet_configs=None, extra_cols=None,
    skip_precompute=False,
    start_date="1900-01-01", end_date="2100-01-01",
):
    """收集 unique 到港行, 按船名去重, 打月/周标签。

    与查询页同口径: 先 combine(start, end) 取窗口内票, 再按船名
    groupby 汇总 quantity (一票多行只计一次), 并用 departure_time
    判定到港阶段。
    """
    extra_cols = list(extra_cols or [])
    
    # 安全检查：确保datetime_col有效
    if datetime_col is None or datetime_col not in initial_df.columns:
        # 尝试使用默认列
        available_cols = list(initial_df.columns)
        if "departure" in available_cols:
            datetime_col = "departure"
        elif "arrival" in available_cols:
            datetime_col = "arrival"
        else:
            # 没有任何可用列，返回空DataFrame
            out_cols = [
                "query_vessel_name", "arrival_time", "arrival_source",
                "departure_time", "quantity", "phase",
                "year", "week", "week_start", "week_end",
                "cal_year", "cal_month", *extra_cols,
            ]
            return pd.DataFrame(columns=out_cols)
    
    present = Present()

    def _combine(df, dt_col, days, sheet_name=None):
        return present.combine_initial_vessel_sheet_with_query_result(
            start_date, end_date, days, sheet_name=sheet_name,
        )

    if sheet_configs is not None:
        if not skip_precompute:
            import json as _json
            _sheet_items = {}
            for item in sheet_configs:
                df, dt_col, days = item[0], item[1], item[2]
                _sn = item[3] if len(item) > 3 else None
                mt = MatchTicket()
                result = mt.generate_matched_tickets_with_port_calls(df, dt_col, country, write_cache=False)
                _sheet_items[str(_sn)] = _json.loads(
                    result.to_json(orient="records", force_ascii=False, date_format="iso")
                )
            _cache_path = Path(sys.modules[MatchTicket.__module__].__file__).with_name(".json") / ".match_tickets" / ".matched_tickets_with_port_calls.json"
            _cache_data = {
                "updated_at": snapshot_saved_at() or datetime.now().isoformat(),
                "items": _sheet_items,
            }
            _cache_path.write_text(_json.dumps(_cache_data, ensure_ascii=False), encoding="utf-8")
        parts = [_combine(item[0], item[1], item[2], sheet_name=item[3] if len(item) > 3 else None) for item in sheet_configs]
        combined = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    else:
        if not skip_precompute:
            precompute_matched_tickets(
                initial_df, country, datetime_col, snapshot_saved_at(),
            )
        combined = present.combine_initial_vessel_sheet_with_query_result(
            start_date, end_date, expected_shipping_days,
        )

    read_cols = [
        "query_vessel_name", "arrival_time", "arrival_source",
        "departure_time", "quantity",
        *extra_cols,
    ]
    out_cols = [
        "query_vessel_name", "arrival_time", "arrival_source",
        "departure_time", "quantity", "phase",
        "year", "week", "week_start", "week_end",
        "cal_year", "cal_month", *extra_cols,
    ]
    if combined.empty or "arrival_time" not in combined.columns:
        return pd.DataFrame(columns=out_cols)
    arr = combined[combined["arrival_time"].notna()]
    if "status" in arr.columns:
        arr = arr[arr["status"] == "unique"]
    if arr.empty:
        return pd.DataFrame(columns=out_cols)

    arrived = arr[[c for c in read_cols if c in arr.columns]].copy()
    arrived["arrival_time"] = pd.to_datetime(arrived["arrival_time"])
    arrived["quantity"] = pd.to_numeric(
        arrived["quantity"].astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce",
    ).fillna(0.0)

    # 按船名去重: 同一条船多张票只计一次 quantity, 与查询页一致
    agg_dict = {
        "arrival_time": "first",
        "arrival_source": "first",
        "quantity": "sum",
    }
    if "departure_time" in arrived.columns:
        arrived["departure_time"] = pd.to_datetime(arrived["departure_time"], errors="coerce")
        agg_dict["departure_time"] = "first"
    for ec in extra_cols:
        if ec in arrived.columns:
            agg_dict[ec] = "first"
    arrived = (
        arrived.groupby("query_vessel_name", as_index=False).agg(agg_dict).reset_index(drop=True)
    )

    # 阶段判定: 与查询页逻辑一致 (检查 departure_time)
    eta_fields = {"etbStd", "etb", "etaStd", "eta"}
    anchor_fields = {"atAnchorA", "atAnchorArrival"}
    def _assign_phase(row):
        src = row.get("arrival_source")
        dep = row.get("departure_time")
        if src == "estimated":
            return "估算到港"
        if src in eta_fields:
            return "在途 ETA"
        if pd.notna(dep) or src in ("atBerthA", "atBerthArrival"):
            return "靠泊"
        if src in anchor_fields:
            return "锚泊"
        return "港界待靠"
    arrived["phase"] = arrived.apply(_assign_phase, axis=1)

    arrived["cal_year"] = arrived["arrival_time"].dt.year
    arrived["cal_month"] = arrived["arrival_time"].dt.month
    week_labels = custom_saturday_weeks(arrived["arrival_time"])
    week_labels.index = arrived.index
    arrived[["year", "week", "week_start", "week_end"]] = week_labels
    return arrived[out_cols].sort_values(["cal_year", "cal_month"]).reset_index(drop=True)


def weekly_arrival_tonnage(initial_df, country, snapshot_stamp, datetime_col=None, expected_shipping_days=None, sheet_configs=None, skip_precompute=False, start_date="1900-01-01", end_date="2100-01-01"):
    return _collect_arrival_rows(
        initial_df, country,
        datetime_col, expected_shipping_days, sheet_configs,
        skip_precompute=skip_precompute,
        start_date=start_date, end_date=end_date,
    )


def arrival_by_port_granular(initial_df, country, snapshot_stamp, datetime_col=None, expected_shipping_days=None, sheet_configs=None, skip_precompute=False, start_date="1900-01-01", end_date="2100-01-01"):
    port_cols = ["port_name_cn", "year", "week", "month", "quantity"]
    rows = _collect_arrival_rows(
        initial_df, country,
        datetime_col, expected_shipping_days, sheet_configs,
        extra_cols=["port_name_cn"],
        skip_precompute=skip_precompute,
        start_date=start_date, end_date=end_date,
    )
    if rows.empty:
        return pd.DataFrame(columns=port_cols)
    # 分区域 tab 的年/月沿用到港自然年/月 (不用自定义周年)
    return pd.DataFrame({
        "port_name_cn": rows["port_name_cn"],
        "year": rows["cal_year"],
        "week": rows["week"],
        "month": rows["cal_month"],
        "quantity": rows["quantity"],
    })


def clear_tracking_caches():
    build_tracking_outputs.clear()


@st.cache_data(show_spinner=False)
def render_route_map(by_mmsi_subset, snapshot_stamp):
    from VesselProcessAPI import Map

    return Map(by_mmsi_subset).route.get_root().render()


def run_login(force, box):
    buffer = io.StringIO()
    holder = {}

    def worker():
        try:
            with contextlib.redirect_stdout(buffer):
                holder["api"] = FreightowerAPI(force=force)
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    shown = False
    deadline = time.time() + 300
    while thread.is_alive() and time.time() < deadline:
        time.sleep(0.5)
        if not shown:
            match = re.search(r"https?://\S+", buffer.getvalue())
            if match:
                login_url = match.group(0)
                box.image(login_url, width=240, caption="请使用微信扫码登录")
                box.markdown(f"无法显示二维码时可手动打开：[登录链接]({login_url})")
                shown = True
    thread.join(2)
    if thread.is_alive():
        return None, TimeoutError("扫码登录超时，请重试")
    return holder.get("api"), holder.get("error")


def attach_log_collector():
    collector = _LogCollector()
    collector.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger = logging.getLogger("VesselProcessAPI")
    logger.addHandler(collector)
    logger.setLevel(logging.WARNING)
    return logger, collector


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")


if "api" not in st.session_state:
    st.session_state.api = None
if "workbook_path" not in st.session_state:
    st.session_state.workbook_path = str(INITIAL_XLSX)
if "wb_edited" not in st.session_state:
    st.session_state.wb_edited = None
if "wb_edited_path" not in st.session_state:
    st.session_state.wb_edited_path = None


def reset_workbook_ui_state():
    st.session_state.wb_edited = None
    st.session_state.wb_edited_path = None
    st.session_state.pop("track_sheet", None)
    st.session_state.pop("chart_sheet", None)
    for key in list(st.session_state):
        if key.startswith(("mapping_editor_", "wb_editor_")):
            st.session_state.pop(key, None)


def on_workbook_path_change():
    if not st.session_state.workbook_path.strip():
        st.session_state.workbook_path = str(INITIAL_XLSX)
    reset_workbook_ui_state()


with st.sidebar:
    st.header("API")
    login_box = st.container()
    if st.session_state.api is not None:
        login_box.success("API 已就绪")
    col_a, col_b = st.columns(2)
    if col_a.button("初始化 API", width="stretch"):
        with login_box:
            with st.spinner("正在读取本地 token 初始化…"):
                api, error = run_login(force=False, box=login_box)
        if error is not None:
            login_box.error(f"初始化失败：{error}")
        else:
            st.session_state.api = api
            st.rerun()
    if col_b.button("微信扫码重登", width="stretch"):
        with login_box:
            with st.spinner("等待微信扫码…"):
                api, error = run_login(force=True, box=login_box)
        if error is not None:
            login_box.error(f"登录失败：{error}")
        else:
            st.session_state.api = api
            st.rerun()
    saved_at = snapshot_saved_at()
    if saved_at:
        st.caption(f"本地存档更新时间：{saved_at}")

    # 配票计算状态
    _mt_cache_path = Path(sys.modules[MatchTicket.__module__].__file__).with_name(".json") / ".match_tickets" / ".matched_tickets_with_port_calls.json"
    _mt_status_box = st.container(border=True)
    with _mt_status_box:
        st.caption("配票计算")
        _mt_status_placeholder = st.empty()
        if _mt_cache_path.is_file():
            try:
                _mt_cache = json.loads(_mt_cache_path.read_text(encoding="utf-8"))
                _mt_updated = _mt_cache.get("updated_at", "")
                if _mt_updated:
                    _dt = datetime.fromisoformat(_mt_updated)
                    _mt_status_placeholder.markdown(f"**{_dt.strftime('%Y-%m-%d %H:%M')}**")
            except Exception:
                pass
        else:
            _mt_status_placeholder.caption("尚未执行配票计算")

tab_track, tab_chart, tab_manage = st.tabs(
    ["查询", "图表可视化", "数据管理"]
)
with tab_manage:
    sub_data, sub_query = st.tabs(["原始船表", "MMSI 查询"])

with sub_query:
    workbook_xlsx = current_workbook_path()
    st.caption(
        f"映射关系保存在本地存档（Store），按 {workbook_xlsx.name} 各工作表的"
        "船名花名册展示；可直接手工编辑状态/MMSI 后保存，或展开下方在线拉取。"
    )
    if not workbook_xlsx.is_file():
        st.error(f"初始船表路径无效或不是文件：{workbook_xlsx}")
    else:
        initial_sheets = load_initial_workbook(workbook_xlsx)
        sheet_name = st.selectbox("工作表", options=list(initial_sheets.keys()))

        with st.expander("在线拉取 MMSI（需要 API）", icon=":material/cloud_download:"):
            pull_columns = list(initial_sheets[sheet_name].columns)
            pc1, pc2, pc3, pc4 = st.columns(4)
            pull_default_col = (
                "departure" if "departure" in pull_columns else pull_columns[0]
            )
            pull_datetime_col = pc1.selectbox(
                "时间列",
                options=pull_columns,
                index=pull_columns.index(pull_default_col),
            )
            pull_start = pc2.date_input(
                "起始日期",
                value=date.today() - timedelta(days=90),
                format="YYYY-MM-DD",
            )
            pull_end = pc3.date_input(
                "截止日期",
                value=date.today() + timedelta(days=60),
                format="YYYY-MM-DD",
            )
            loading_text = pc4.text_input(
                "装港国家码（两位码，逗号分隔，用于重名决胜）",
                value="BR",
            )
            window_df = Tool().read_initial_vessel_sheet(
                initial_sheets[sheet_name],
                pull_datetime_col,
                str(pull_start),
                str(pull_end),
            ).reset_index()
            window_names = tuple(sorted(
                window_df["query_vessel_name"].dropna().astype(str).unique()
            )) if "query_vessel_name" in window_df.columns else ()
            pull_mmsi = mmsis_for_names(window_names, snapshot_saved_at())
            with st.container(border=True):
                sc1, sc2 = st.columns(2)
                sc1.caption("时间窗内船名")
                sc1.markdown(f"**{len(window_names)} 个船名**")
                sc2.caption("对应 MMSI")
                sc2.markdown(f"**{len(pull_mmsi)} 个 MMSI**")
            if st.session_state.api is None:
                st.warning("API 尚未初始化，请在侧边栏登录。")
            pull_clicked = st.button(
                "拉取 MMSI 并写入存档",
                type="primary",
                width="stretch",
                disabled=st.session_state.api is None or not pull_mmsi,
            )
            pull_progress = st.container()

        edit_df = st.data_editor(
            mapping_view(sheet_name, snapshot_saved_at(), str(workbook_xlsx)),
            key=f"mapping_editor_{sheet_name}",
            num_rows="dynamic",
            column_config={
                "status": st.column_config.SelectboxColumn(
                    "status",
                    options=["unique", "multiple", "not_found"],
                ),
                "mmsi": st.column_config.TextColumn("mmsi"),
            },
        )
        save_col, reset_col = st.columns(2)
        if save_col.button("保存本表修改", type="primary", width="stretch"):
            entries = info_entries_from_editor(edit_df)
            try:
                Store().save_items("info", entries)
            except OSError as exc:
                st.error(f"保存失败（存档文件可能被占用）：{exc}")
            except Exception as exc:
                st.error(f"保存失败：{exc}")
            else:
                mapping_view.clear()
                clear_tracking_caches()
                st.success(f"已保存 {len(entries)} 个船名到本地存档")
                st.rerun()
        if reset_col.button("放弃修改，重新加载", width="stretch"):
            st.session_state.pop(f"mapping_editor_{sheet_name}", None)
            st.rerun()

        if pull_clicked:
            with pull_progress:
                collector = None
                started = time.perf_counter()
                try:
                    pull_sheet = Tool().read_initial_vessel_sheet(
                        str(workbook_xlsx),
                        pull_datetime_col,
                        str(pull_start),
                        str(pull_end),
                        sheet_name=sheet_name,
                    ).reset_index()
                    if "query_vessel_name" not in pull_sheet.columns:
                        raise ValueError("初始船表缺少 query_vessel_name 列")
                    names = list(
                        dict.fromkeys(
                            pull_sheet["query_vessel_name"].dropna().astype(str).tolist()
                        )
                    )
                    if not names:
                        raise ValueError(
                            f"{pull_start} ~ {pull_end} 时间窗口内没有可查询的"
                            "query_vessel_name"
                        )
                    codes = [
                        c.strip().upper()
                        for c in loading_text.split(",")
                        if c.strip()
                    ]
                    logger, collector = attach_log_collector()
                    api = st.session_state.api
                    holder = {}
                    def _worker():
                        try:
                            api.get_info_by_vessel_name(
                                names, loading_country=codes or None
                            )
                            holder["ok"] = True
                        except Exception as e:
                            holder["error"] = e
                    t = threading.Thread(target=_worker, daemon=True)
                    t.start()
                    with st.status(f"正在查询 {len(names)} 个船名…", expanded=True) as status:
                        timer = st.empty()
                        while t.is_alive():
                            elapsed = time.perf_counter() - started
                            timer.markdown(f"已运行 **{elapsed:.1f}** 秒")
                            t.join(0.5)
                        elapsed = time.perf_counter() - started
                        timer.markdown(f"已运行 **{elapsed:.1f}** 秒")
                        logger.removeHandler(collector)
                        if holder.get("error"):
                            raise holder["error"]
                except Exception as exc:
                    elapsed = time.perf_counter() - started
                    st.error(f"查询失败，运行 {elapsed:.1f} 秒：{exc}")
                else:
                    mapping_view.clear()
                    clear_tracking_caches()
                    st.session_state.pop(f"mapping_editor_{sheet_name}", None)
                    st.success(
                        f"已写入存档（{len(names)} 个船名），运行 {elapsed:.1f} 秒"
                    )
                    st.rerun()
                if collector is not None and collector.messages:
                    st.caption(f"匹配日志（{len(collector.messages)} 条）")
                    st.code("\n".join(collector.messages))

with tab_track:
    workbook_xlsx = current_workbook_path()
    if not workbook_xlsx.is_file():
        st.info(f"初始船表路径无效或不是文件：{workbook_xlsx}")
    else:
        initial_sheets = load_initial_workbook(workbook_xlsx)
        tc1, tc2, tc3, tc4 = st.columns(4)
        track_name = tc1.selectbox(
            "工作表", options=list(initial_sheets.keys()), key="track_sheet"
        )
        initial_df = initial_sheets[track_name]
        start_date = tc2.date_input(
            "到港统计起始日期",
            value=date.today() - timedelta(days=90),
            format="YYYY-MM-DD",
            key="track_start_date",
        )
        end_date = tc3.date_input(
            "到港统计截止日期",
            value=date.today() + timedelta(days=60),
            format="YYYY-MM-DD",
            key="track_end_date",
        )
        country = tc4.text_input("到达统计国家码", value="CN", key="track_country")

        _origin_days = {"美湾": 60, "美西": 25, "巴西": 48, "阿根廷": 60}
        _default_days = _origin_days.get(track_name, 60)
        if st.session_state.get("_prev_track_sheet") != track_name:
            st.session_state["track_exp_days"] = _default_days
            st.session_state["_prev_track_sheet"] = track_name
            # sheet改变时清除cache，确保重新生成
            build_tracking_outputs.clear()
        with st.expander("航程天数设置"):
            _oc1, _oc2 = st.columns(2)
            datetime_col = _oc1.selectbox(
                "日期列名", options=list(initial_df.columns),
                index=list(initial_df.columns).index("departure") if "departure" in initial_df.columns else 0,
                key="track_datetime_col",
                help="用该列日期 + 航程天数作为估算到港日期"
            )
            expected_shipping_days = _oc2.number_input(
                "航程天数", min_value=0, key="track_exp_days",
                help="在日期列基础上加的天数"
            )
        valid_mmsi = sheet_mmsis(
            track_name, snapshot_saved_at(), str(workbook_xlsx)
        )
        if not valid_mmsi:
            st.warning("该工作表在存档中没有有效 MMSI，请先在 MMSI 查询页在线拉取或手工填写并保存。")
        with st.container(border=True):
            sc1, sc2 = st.columns(2)
            sc1.caption("本地存档更新时间")
            sc1.markdown(f"**{snapshot_saved_at() or '无'}**")
            sc2.caption("存档 MMSI 数")
            sc2.markdown(f"**{len(valid_mmsi)} 个 MMSI**")

        with st.expander("在线拉取最新数据（需要 API）", icon=":material/cloud_download:"):
            data_columns = list(initial_df.columns)
            dc1, dc2, dc3 = st.columns(3)
            data_default_col = (
                "departure" if "departure" in data_columns else data_columns[0]
            )
            data_datetime_col = dc1.selectbox(
                "时间列（只拉窗口内船名）",
                options=data_columns,
                index=data_columns.index(data_default_col),
                key="pull_data_col",
            )
            data_start = dc2.date_input(
                "起始日期",
                value=date.today() - timedelta(days=90),
                format="YYYY-MM-DD",
                key="pull_data_start",
            )
            data_end = dc3.date_input(
                "截止日期",
                value=date.today() + timedelta(days=60),
                format="YYYY-MM-DD",
                key="pull_data_end",
            )
            window_df = Tool().read_initial_vessel_sheet(
                initial_df, data_datetime_col, str(data_start), str(data_end)
            ).reset_index()
            window_names = tuple(sorted(
                window_df["query_vessel_name"].dropna().astype(str).unique()
            )) if "query_vessel_name" in window_df.columns else ()
            pull_mmsi = mmsis_for_names(window_names, snapshot_saved_at())
            with st.container(border=True):
                sc1, sc2, sc3 = st.columns(3)
                sc1.caption("时间窗内船名")
                sc1.markdown(f"**{len(window_names)} 个船名**")
                sc2.caption("对应 MMSI")
                sc2.markdown(f"**{len(pull_mmsi)} 个 MMSI**")
                sc3.caption("已查询存档")
                from VesselProcessAPI.Store import Store
                store_mmsis = set(Store()._registry["items"].get("mmsi", {}).get("data", []))
                archived_in_window = store_mmsis & set(pull_mmsi)
                sc3.markdown(f"**{len(archived_in_window)} 个 MMSI**")

            rc1, rc2 = st.columns(2)
            route_days = rc1.number_input(
                "历史轨迹回溯天数", min_value=1, max_value=180, value=7
            )
            port_days = rc2.number_input(
                "历史挂靠回溯天数", min_value=1, max_value=365, value=90
            )
            pc1, pc2, pc3, pc4, pc5 = st.columns(5)
            batch_ais = pc1.checkbox("批量取 AIS", value=True)
            fetch_current = pc2.checkbox("取当前挂靠港", value=True)
            fetch_history_port = pc3.checkbox("取历史挂靠港", value=True)
            fetch_history_route = pc4.checkbox("取历史航线", value=True)
            fetch_future_route = pc5.checkbox("取预测航线", value=True)
            pull_clicked = st.button(
                "拉取最新数据并刷新结果",
                type="primary",
                width="stretch",
                disabled=not pull_mmsi,
            )
            track_progress = st.container()

        if pull_clicked:
            with track_progress:
                if st.session_state.api is None:
                    st.warning("API 尚未初始化，请先在侧边栏点击“初始化 API”。")
                elif not pull_mmsi:
                    st.warning(
                        "时间窗内没有有效 MMSI，请调整时间窗，或先在“MMSI 查询”页"
                        "在线拉取或手工填写并保存。"
                    )
                else:
                    api = st.session_state.api
                    started = time.perf_counter()
                    try:
                        with st.status(
                            f"正在拉取 {len(pull_mmsi)} 艘船的数据…",
                            expanded=True,
                        ) as status:
                            timer = st.empty()
                            def _show(step):
                                elapsed = time.perf_counter() - started
                                timer.markdown(
                                    f"{step} — 已运行 **{elapsed:.1f}** 秒"
                                )
                            _show("set_mmsi + get_ais")
                            api.set_mmsi(pull_mmsi).get_ais(
                                get_ais_by_multiple=batch_ais
                            )
                            if fetch_current:
                                _show("get_current_port")
                                api.get_current_port()
                            if fetch_history_port:
                                _show(f"get_history_port({port_days})")
                                api.get_history_port(int(port_days))
                            if fetch_history_route:
                                _show(f"get_history_route({route_days})")
                                api.get_history_route(int(route_days))
                            if fetch_future_route:
                                _show("get_future_route")
                                api.get_future_route()
                            elapsed = time.perf_counter() - started
                            status.update(
                                label=f"拉取完成，运行 {elapsed:.1f} 秒",
                                state="complete",
                                expanded=False,
                            )
                    except Exception as exc:
                        elapsed = time.perf_counter() - started
                        st.error(f"拉取失败，运行 {elapsed:.1f} 秒：{exc}")
                    else:
                        clear_tracking_caches()
                        st.success(
                            f"拉取完成，共 {len(pull_mmsi)} 艘船，运行 {elapsed:.1f} 秒"
                        )
                        st.rerun()

        code = country.strip().upper() or "CN"
        with st.spinner("正在读取本地存档生成结果…"):
            try:
                combined, current, by_mmsi = build_tracking_outputs(
                    initial_df,
                    str(start_date),
                    str(end_date),
                    code,
                    snapshot_saved_at(),
                    datetime_col,
                    expected_shipping_days,
                    sheet_name=track_name,
                )
            except Exception as exc:
                st.exception(exc)
            else:
                sub_combine, sub_current = st.tabs(
                    [
                        "到港记录",
                        "当前状态",
                    ]
                )
                with sub_combine:
                    # 所有指标按船名去重, 一船一状态; 多行业务行/多候选不重复计数
                    if "status" in combined.columns:
                        name_status = (
                            combined.drop_duplicates("query_vessel_name")
                            .set_index("query_vessel_name")["status"]
                        )
                    else:
                        name_status = pd.Series(
                            pd.NA,
                            index=pd.Index(
                                combined["query_vessel_name"]
                                .dropna().astype(str).drop_duplicates()
                            ),
                        )
                    # combined 已在 Presentation 层过滤:
                    # 窗口内首次到港命中的 unique/multiple + 全部 not_found
                    all_names = set(name_status.index)
                    unique_names = set(name_status[name_status == "unique"].index)
                    multiple_names = set(name_status[name_status == "multiple"].index)
                    unmatched_names = all_names - unique_names - multiple_names
                    cohort_names = all_names

                    eta_fields = {"etbStd", "etb", "etaStd", "eta"}
                    anchor_fields = {"atAnchorA", "atAnchorArrival"}
                    if "arrival_time" not in combined.columns:
                        arrived = pd.DataFrame(
                            columns=["arrival_source", "departure_time"]
                        )
                    else:
                        arrived = combined[
                            combined["arrival_time"].notna()
                        ].drop_duplicates("query_vessel_name").set_index(
                            "query_vessel_name"
                        )
                    # 阶段分桶只针对 unique; multiple 候选不进事实桶
                    # 歧义船候选的到港仅作提示, 不计入事实分桶
                    multi_arrived = sorted(multiple_names)

                    phase = {}
                    for n in unique_names:
                        row = arrived.loc[n]
                        source, departure = row["arrival_source"], row["departure_time"]
                        if source == "estimated":
                            phase[n] = "估算到港"
                        elif source in eta_fields:
                            phase[n] = "在途 ETA"
                        elif pd.notna(departure) or source in ("atBerthA", "atBerthArrival"):
                            phase[n] = "靠泊"
                        elif source in anchor_fields:
                            phase[n] = "锚泊"
                        else:
                            phase[n] = "港界待靠"
                    counts = pd.Series(phase).value_counts()
                    n_phase = lambda k: int(counts.get(k, 0))

                    a1, a2, a3, a4 = st.columns(4)
                    a1.metric(
                        "窗口内查询船数", len(cohort_names),
                        help=(f"首次靠泊 {code} 的时间落在 "
                              f"{start_date} ~ {end_date} 内的 unique/multiple 船，"
                              f"另含全部 not_found 船名"),
                    )
                    a2.metric(
                        "唯一匹配 unique", len(unique_names),
                        help="窗口队列中船名查询唯一命中 MMSI 的船，下排到港阶段仅统计这些船",
                    )
                    a3.metric(
                        "重名歧义 multiple", len(multiple_names),
                        delta=(f"{len(multi_arrived)} 个候选窗口内有到港"
                               if multi_arrived else None),
                        help=("两个候选 MMSI 未消歧，候选到港不计入下方统计；"
                              "请在 MMSI 查询页核实并改为 unique。"
                              + (f"涉及：{', '.join(multi_arrived)}"
                                 if multi_arrived else "")),
                    )
                    a4.metric(
                        "未匹配", len(unmatched_names),
                        help="not_found 船名：无 MMSI，始终保留在结果中",
                    )

                    st.markdown(f"**窗口内唯一匹配（unique）：共 {len(unique_names)} 艘**")
                    b1, b2, b3, b4, b5 = st.columns(5)
                    b1.metric(
                        "靠泊",
                        n_phase("靠泊"),
                        help="首次挂靠已靠泊 (atBerth) 的船，含已有离港时间和尚在卸货的船",
                    )
                    b2.metric(
                        "锚泊", n_phase("锚泊"),
                        help="首次挂靠仅到锚地 (atAnchor*) 尚未靠泊",
                    )
                    b3.metric(
                        "港界待靠", n_phase("港界待靠"),
                        help="首次挂靠仅到港界 (ata/atPortArrival) 尚未锚泊/靠泊",
                    )
                    b4.metric(
                        "在途 ETA", n_phase("在途 ETA"),
                        help="回溯数据内无实际挂靠，按 AIS ETB/ETA 预计窗口内到达",
                    )
                    b5.metric(
                        "估算到港", n_phase("估算到港"),
                        help="ETA 过滤为 low 时，按初始船表日期 + 航程天数估算",
                    )

                    # 到货量: 初始表 quantity 列可能是带千分位的文本, 按船名汇总
                    # 多票货业务行后再归入该船唯一的到港阶段; multiple/未匹配不计
                    if "quantity" in combined.columns:
                        combined_unique = combined[combined["status"] == "unique"] if "status" in combined.columns else combined
                        qty_num = pd.to_numeric(
                            combined_unique["quantity"].astype(str)
                            .str.replace(",", "", regex=False)
                            .str.strip(),
                            errors="coerce",
                        ).fillna(0.0)
                        qty_by_name = (
                            qty_num.groupby(combined_unique["query_vessel_name"].astype(str))
                            .sum()
                        )
                        phase_order = [
                            "靠泊", "锚泊",
                            "港界待靠", "在途 ETA", "估算到港",
                        ]
                        tonnage = {
                            k: float(sum(
                                qty_by_name.get(n, 0.0)
                                for n, ph in phase.items() if ph == k
                            ))
                            for k in phase_order
                        }
                        total_tonnage = sum(tonnage.values())
                        st.markdown("**到货量统计：**")
                        st.metric("总计", f"{total_tonnage:,.0f} 吨")
                        d1, d2, d3, d4, d5 = st.columns(5)
                        for col, key in zip(
                            (d1, d2, d3, d4, d5), phase_order
                        ):
                            col.metric(
                                key, f"{tonnage[key]:,.0f} 吨",
                                help=f"{key}船对应初始船表 quantity 之和",
                            )
                    with st.expander("到港明细（点击展开）"):
                        show_cols = [
                            c for c in [
                                "query_vessel_name", "departure", "departure_from_origin_port", "port_name",
                                "quantity", "status", "mmsi", "shipType",
                                "port_name_cn", "port_en", "arrival_time",
                                "departure_time", "arrival_source",
                            ] if c in combined.columns
                        ]
                        display_df = combined[show_cols]
                        st.dataframe(display_df, width="stretch")
                        st.download_button(
                            "下载到港记录 CSV",
                            csv_bytes(display_df),
                            file_name="combined_vessel_sheet.csv",
                            mime="text/csv",
                        )
                with sub_current:
                    name_by_mmsi = {}
                    for m, entry in by_mmsi.items():
                        ais = entry.get("ais") or {}
                        name_by_mmsi[str(m)] = (
                            ais.get("nameEn") or ais.get("aisName") or str(m)
                        )
                    option_mmsis = sorted(name_by_mmsi)
                    selection_key = "route_selection"
                    st.session_state[selection_key] = [
                        m
                        for m in st.session_state.get(selection_key, [])
                        if m in option_mmsis
                    ]

                    def _select_all_route():
                        st.session_state[selection_key] = option_mmsis

                    def _clear_route():
                        st.session_state[selection_key] = []

                    st.multiselect(
                        "搜索并选择要在地图上显示的船（默认不显示）",
                        options=option_mmsis,
                        format_func=lambda m: f"{name_by_mmsi.get(m, m)} ({m})",
                        key=selection_key,
                    )
                    sel1, sel2 = st.columns(2)
                    sel1.button(
                        "全选", width="stretch", on_click=_select_all_route
                    )
                    sel2.button(
                        "清空", width="stretch", on_click=_clear_route
                    )
                    selected = st.session_state[selection_key]
                    if not selected:
                        st.info("未选择任何船舶，请在上方搜索框中选择后查看轨迹。")
                    else:
                        subset = {m: by_mmsi[m] for m in selected}
                        with st.spinner("正在渲染地图…"):
                            map_html = render_route_map(
                                subset, snapshot_saved_at()
                            )
                        st.caption(
                            "实线为历史轨迹，同色虚线为官方预测航线或到目的港直线。"
                        )
                        st.iframe(map_html, height=680)
                        st.download_button(
                            "下载地图 HTML",
                            map_html.encode("utf-8"),
                            file_name="map.html",
                            mime="text/html",
                        )

                    with st.expander(
                        f"当前状态明细（{len(current)} 行，点击展开）"
                    ):
                        st.dataframe(current, width="stretch")
                        st.download_button(
                            "下载当前态势 CSV",
                            csv_bytes(current),
                            file_name="current_situation.csv",
                            mime="text/csv",
                        )

with tab_chart:
    workbook_xlsx = current_workbook_path()
    if not workbook_xlsx.is_file():
        st.info(f"初始船表路径无效或不是文件：{workbook_xlsx}")
    else:
        chart_sheets = load_initial_workbook(workbook_xlsx)
        cc1, cc2 = st.columns(2)
        sheet_options = ["全部"] + list(chart_sheets.keys())
        chart_sheet_name = cc1.selectbox(
            "工作表", options=sheet_options, key="chart_sheet"
        )
        chart_country = cc2.text_input("到达统计国家码", value="CN", key="chart_country")

        _origin_days_c = {"美湾": 60, "美西": 25, "巴西": 48, "阿根廷": 60}
        _chart_default = _origin_days_c.get(chart_sheet_name, 60)
        _is_all_sheets = (chart_sheet_name == "全部")
        if st.session_state.get("_prev_chart_sheet") != chart_sheet_name:
            st.session_state["chart_exp_days"] = _chart_default
            st.session_state["_prev_chart_sheet"] = chart_sheet_name
            # sheet改变时清除图表相关的cache
        with st.expander("航程天数设置"):
            _cc1, _cc2 = st.columns(2)
            chart_datetime_col_options = list(chart_sheets[chart_sheet_name].columns) if not _is_all_sheets else []
            
            # 当选择"全部"时，使用第一个sheet的列作为参考
            if _is_all_sheets and chart_sheets:
                first_sheet_cols = list(list(chart_sheets.values())[0].columns)
                chart_datetime_col = _cc1.selectbox(
                    "日期列名", options=first_sheet_cols,
                    index=first_sheet_cols.index("departure") if "departure" in first_sheet_cols else 0,
                    key="chart_datetime_col",
                    help="用该列日期 + 航程天数作为估算到港日期",
                    disabled=_is_all_sheets,
                )
            else:
                chart_datetime_col = _cc1.selectbox(
                    "日期列名", options=chart_datetime_col_options,
                    index=chart_datetime_col_options.index("departure") if not _is_all_sheets and "departure" in chart_datetime_col_options else 0,
                    key="chart_datetime_col",
                    help="用该列日期 + 航程天数作为估算到港日期",
                    disabled=_is_all_sheets,
                )
            chart_exp_days = _cc2.number_input(
                "航程天数", min_value=0, key="chart_exp_days",
                help="在日期列基础上加的天数",
                disabled=_is_all_sheets,
            )
        if chart_sheet_name == "全部":
            chart_df_input = pd.concat(
                chart_sheets.values(), ignore_index=True
            )
            all_mmsis = set()
            for sn in chart_sheets:
                all_mmsis.update(
                    sheet_mmsis(sn, snapshot_saved_at(), str(workbook_xlsx))
                )
            chart_mmsi = sorted(all_mmsis)
        else:
            chart_df_input = chart_sheets[chart_sheet_name]
            chart_mmsi = sheet_mmsis(
                chart_sheet_name, snapshot_saved_at(), str(workbook_xlsx)
            )
        with st.container(border=True):
            sc1, sc2 = st.columns(2)
            sc1.caption("本地存档更新时间")
            sc1.markdown(f"**{snapshot_saved_at() or '无'}**")
            sc2.caption("存档 MMSI 数")
            sc2.markdown(f"**{len(chart_mmsi)} 个 MMSI**")

        chart_sub_time, chart_sub_port = st.tabs(["按时间", "分区域到货"])

        _sheet_configs = None
        if _is_all_sheets:
            _sheet_configs = tuple(
                (df, "departure", _origin_days_c.get(name, 60), name)
                for name, df in chart_sheets.items()
            )

        # 统一预计算一次配票（同一 snapshot 不重复计算）
        _code = chart_country.strip().upper() or "CN"
        _snap = snapshot_saved_at()
        _xlsx_mtime = workbook_xlsx.stat().st_mtime if workbook_xlsx.is_file() else 0
        _precompute_key = (_code, _snap, _xlsx_mtime)
        _need_precompute = st.session_state.get("_last_precompute_key") != _precompute_key
        if _need_precompute:
            _mt_status_placeholder.caption("正在计算配票…")

        with chart_sub_time:
            weekly = weekly_arrival_tonnage(
                chart_df_input,
                _code,
                _snap,
                chart_datetime_col,
                chart_exp_days,
                _sheet_configs,
                skip_precompute=not _need_precompute,
            )
            if _need_precompute:
                _mt_status_placeholder.caption("配票计算完成")
                st.session_state["_last_precompute_key"] = _precompute_key
            if weekly.empty:
                st.info("暂无可用于周度图的 unique 到港数据。")
            else:
                granularity = st.radio(
                    "时间粒度", options=["周度", "月度"], horizontal=True
                )
                available_years = sorted(
                    set(weekly["year"].unique())
                    | set(weekly["cal_year"].unique()),
                    reverse=True,
                )
                chart_years = st.multiselect(
                    "选择年份",
                    options=available_years,
                    default=available_years,
                )
                if not chart_years:
                    st.warning("请至少选择一个年份。")
                else:
                    import altair as alt

                    today = pd.Timestamp.now().normalize()

                    def _saturday_week_start(year, week):
                        jan1 = pd.Timestamp(year=int(year), month=1, day=1)
                        year_start = jan1 - pd.Timedelta(
                            days=(jan1.weekday() + 2) % 7
                        )
                        return year_start + pd.Timedelta(weeks=int(week) - 1)

                    if granularity == "周度":
                        selected_phases = st.multiselect(
                            "筛选到港阶段",
                            options=ARRIVAL_PHASE_ORDER,
                            default=ARRIVAL_PHASE_ORDER,
                            key="weekly_phase_filter",
                        )
                        if not selected_phases:
                            st.warning("请至少选择一个到港阶段。")
                        period_label = "周次（周六–周五）"
                        x_label = "周次（周六开始，周五结束，每年 52 周）"
                        week_rows = weekly[weekly["year"].isin(chart_years)]
                        if selected_phases:
                            week_rows = week_rows[
                                week_rows["phase"].isin(selected_phases)
                            ]
                        pivot = (
                            week_rows.pivot_table(
                                index="week",
                                columns="year",
                                values="quantity",
                                aggfunc="sum",
                                fill_value=0,
                            )
                            .reindex(range(1, 53), fill_value=0)
                            .fillna(0.0)
                        )
                        pivot.columns = [int(y) for y in pivot.columns]
                        chart_df = pivot.reset_index().rename(
                            columns={"week": period_label}
                        )
                        rows = []
                        for yr in pivot.columns:
                            for wk in pivot.index:
                                ws = _saturday_week_start(yr, wk)
                                we = ws + pd.Timedelta(days=6)
                                if we < today:
                                    cat = "历史"
                                elif ws > today:
                                    cat = "未来"
                                else:
                                    cat = "当前"
                                rows.append({
                                    "period": int(wk),
                                    "year": str(yr),
                                    "quantity": float(pivot.loc[wk, yr]),
                                    "time_category": cat,
                                })
                        long_df = pd.DataFrame(rows)
                    else:
                        period_label = "月份"
                        x_label = "月份"
                        # 月度按 arrival_time 所在自然月汇总, 与查询页所选
                        # 日期窗口的到货量统计同口径 (不用周六周起始日定月)
                        month_rows = weekly[
                            weekly["cal_year"].isin(chart_years)
                        ]
                        pivot = (
                            month_rows.pivot_table(
                                index="cal_month",
                                columns="cal_year",
                                values="quantity",
                                aggfunc="sum",
                                fill_value=0,
                            )
                            .reindex(range(1, 13), fill_value=0)
                            .fillna(0.0)
                        )
                        pivot.columns = [int(y) for y in pivot.columns]
                        chart_df = pivot.reset_index().rename(
                            columns={"cal_month": period_label}
                        )
                        current_year = today.year
                        current_month = today.month
                        rows = []
                        for yr in pivot.columns:
                            for mo in pivot.index:
                                if yr < current_year or (
                                    yr == current_year and mo < current_month
                                ):
                                    cat = "历史"
                                elif yr == current_year and mo == current_month:
                                    cat = "当前"
                                else:
                                    cat = "未来"
                                rows.append({
                                    "period": int(mo),
                                    "year": str(yr),
                                    "quantity": float(pivot.loc[mo, yr]),
                                    "time_category": cat,
                                })
                        long_df = pd.DataFrame(rows)

                    cat_order = ["历史", "当前", "未来"]
                    color_scale = alt.Scale(
                        domain=cat_order,
                        range=["#4c78a8", "#f58518", "#e45756"],
                    )
                    chart = (
                        alt.Chart(long_df)
                        .mark_bar()
                        .encode(
                            x=alt.X(
                                f"period:O", title=x_label,
                                axis=alt.Axis(labelAngle=0),
                            ),
                            y=alt.Y("quantity:Q", title="到货量"),
                            xOffset="year:N",
                            color=alt.Color(
                                "time_category:N",
                                scale=color_scale,
                                title="时间分类",
                                sort=cat_order,
                            ),
                            tooltip=[
                                alt.Tooltip("year:N", title="年份"),
                                alt.Tooltip("period:O", title=period_label),
                                alt.Tooltip(
                                    "quantity:Q", title="到货量", format=",.0f",
                                ),
                                alt.Tooltip(
                                    "time_category:N", title="时间分类",
                                ),
                            ],
                        )
                        .properties(height=480)
                    )
                    if granularity == "月度":
                        bar_labels = (
                            alt.Chart(long_df[long_df["quantity"] > 0])
                            .mark_text(dy=-7, fontSize=11)
                            .encode(
                                x=alt.X("period:O"),
                                y=alt.Y("quantity:Q"),
                                xOffset="year:N",
                                text=alt.Text("quantity:Q", format=",.0f"),
                                color=alt.Color(
                                    "time_category:N",
                                    scale=color_scale,
                                ),
                                tooltip=[
                                    alt.Tooltip("year:N", title="年份"),
                                    alt.Tooltip("period:O", title=period_label),
                                    alt.Tooltip(
                                        "quantity:Q", title="到货量",
                                        format=",.0f",
                                    ),
                                    alt.Tooltip(
                                        "time_category:N", title="时间分类",
                                    ),
                                ],
                            )
                        )
                        chart = chart + bar_labels
                    st.altair_chart(chart, width="stretch")
                    if granularity == "周度":
                        phase_hint = (
                            "、".join(selected_phases)
                            if selected_phases else "（未选择）"
                        )
                        st.caption(
                            f"筛选阶段：{phase_hint}。"
                            "口径与到货量统计一致：仅 unique 船，按业务行汇总 "
                            "quantity；每张船票按航次配唯一到港（实际挂靠优先，"
                            "其次 AIS ETA，low 模式无数据时按 departure+航程天数"
                            "估算）。周度按周六–周五自定义周，均与查询页所选日期"
                            "窗口同口径。"
                        )
                    else:
                        st.caption(
                            "口径与到货量统计一致：仅 unique 船，按业务行汇总 "
                            "quantity；每张船票按航次配唯一到港（实际挂靠优先，"
                            "其次 AIS ETA，low 模式无数据时按 departure+航程天数"
                            "估算）。月度按到港日期所在自然月，均与查询页所选日期"
                            "窗口同口径。"
                        )
                    st.download_button(
                        f"下载{granularity}到货量 CSV",
                        csv_bytes(chart_df),
                        file_name=f"{granularity}_arrival_tonnage.csv",
                        mime="text/csv",
                    )

                    st.divider()
                    # 当前期 (月度=当前自然月, 周度=今天所在周六–周五周) 的
                    # 到货量结构: 单条横向堆叠 bar, 按到港阶段拆五段
                    if granularity == "月度":
                        current_rows = weekly[
                            (weekly["cal_year"] == today.year)
                            & (weekly["cal_month"] == today.month)
                        ]
                        current_label = f"{today.year}年{today.month}月"
                    else:
                        cur_week = custom_saturday_weeks(
                            pd.Series([today])
                        ).iloc[0]
                        current_rows = weekly[
                            (weekly["year"] == int(cur_week["year"]))
                            & (weekly["week"] == int(cur_week["week"]))
                        ]
                        current_label = (
                            f"{int(cur_week['year'])}年第{int(cur_week['week'])}周"
                            f"（{cur_week['week_start']:%m-%d}~"
                            f"{cur_week['week_end']:%m-%d}）"
                        )
                    st.markdown(f"**当前到货量结构 · {current_label}**")
                    if current_rows.empty:
                        st.info("当前期暂无到港数据。")
                    else:
                        phase_df = current_rows.copy()
                        phase_sum = (
                            phase_df.groupby("phase")["quantity"].sum()
                            .reindex(ARRIVAL_PHASE_ORDER, fill_value=0.0)
                            .reset_index()
                        )
                        current_total = float(phase_sum["quantity"].sum())
                        st.metric("当前期到货量合计", f"{current_total:,.0f} 吨")
                        phase_sum["bar"] = current_label
                        bar = (
                            alt.Chart(phase_sum)
                            .mark_bar(height=46)
                            .encode(
                                x=alt.X("quantity:Q", title=None, stack=True),
                                y=alt.Y("bar:N", title=None, axis=None),
                                color=alt.Color(
                                    "phase:N", title="到港阶段",
                                    scale=alt.Scale(
                                        domain=ARRIVAL_PHASE_ORDER,
                                        range=ARRIVAL_PHASE_COLORS,
                                    ),
                                    sort=ARRIVAL_PHASE_ORDER,
                                ),
                                tooltip=[
                                    alt.Tooltip("phase:N", title="到港阶段"),
                                    alt.Tooltip(
                                        "quantity:Q", title="到货量", format=",.0f"
                                    ),
                                ],
                            )
                        )
                        st.altair_chart(
                            bar.properties(height=150),
                            width="stretch",
                        )
                        st.caption(
                            "　".join(
                                f"{p}：{v:,.0f} 吨"
                                for p, v in zip(
                                    phase_sum["phase"], phase_sum["quantity"]
                                )
                            )
                        )

        with chart_sub_port:
            granular_data = arrival_by_port_granular(
                chart_df_input,
                chart_country.strip().upper() or "CN",
                snapshot_saved_at(),
                chart_datetime_col,
                chart_exp_days,
                _sheet_configs,
                skip_precompute=True,
            )
            if granular_data.empty:
                st.info("暂无可用于港口统计的到港数据。")
            else:
                import altair as alt

                port_to_region = {
                    "大连": "东北", "营口": "东北", "锦州": "东北", "丹东": "东北",
                    "唐山": "华北", "天津": "华北", "黄骅": "华北", "秦皇岛": "华北",
                    "青岛": "山东", "烟台": "山东", "日照": "山东", "威海": "山东", "东营": "山东",
                    "上海": "华东", "宁波": "华东", "舟山": "华东", "连云港": "华东",
                    "南通": "华东", "南京": "华东", "镇江": "华东", "泰州": "华东",
                    "苏州": "华东", "嘉兴": "华东", "温州": "华东", "台州": "华东",
                    "盐城": "华东", "大丰": "华东", "崇明": "华东",
                    "福州": "福建", "厦门": "福建", "泉州": "福建", "漳州": "福建",
                    "莆田": "福建", "宁德": "福建", "湄洲湾": "福建", "秀屿": "福建",
                    "广州": "广东", "深圳": "广东", "珠海": "广东", "东莞": "广东",
                    "中山": "广东", "江门": "广东", "佛山": "广东", "肇庆": "广东",
                    "惠州": "广东", "汕头": "广东", "湛江": "广东", "茂名": "广东",
                    "阳江": "广东", "汕尾": "广东", "揭阳": "广东", "潮州": "广东", "梅州": "广东",
                    "防城港": "广西", "钦州港": "广西", "钦州": "广西", "北海": "广西",
                    "铁山港": "广西", "涠洲岛": "广西",
                }

                def _map_region(port_name):
                    if pd.isna(port_name):
                        return "其他"
                    name = str(port_name).strip()
                    for port, region in port_to_region.items():
                        if name.startswith(port):
                            return region
                    return "其他"

                granular_data = granular_data.copy()
                granular_data["region"] = granular_data["port_name_cn"].apply(_map_region)

                port_granularity = st.radio(
                    "时间粒度", options=["周度", "月度", "年度"], horizontal=True, key="port_granularity"
                )

                _today = pd.Timestamp.now().normalize()
                _cur_week_info = custom_saturday_weeks(pd.Series([_today])).iloc[0]
                _cur_year = int(_cur_week_info["year"])
                _cur_week = int(_cur_week_info["week"])
                _cur_month = int(_today.month)

                if port_granularity == "周度":
                    available_years = sorted(granular_data["year"].unique().tolist(), reverse=True)
                    _default_idx_y = available_years.index(_cur_year) if _cur_year in available_years else 0
                    sel_year = st.selectbox("选择年份", options=available_years, index=_default_idx_y, key="port_sel_year_w")
                    year_data = granular_data[granular_data["year"] == sel_year]
                    available_weeks = sorted(year_data["week"].unique().tolist())
                    _default_idx_w = available_weeks.index(_cur_week) if _cur_week in available_weeks else 0
                    sel_week = st.selectbox("选择周次", options=available_weeks, index=_default_idx_w, key="port_sel_week")
                    plot_data = year_data[year_data["week"] == sel_week]
                    period_label = f"{sel_year}年第{sel_week}周"
                elif port_granularity == "月度":
                    available_years = sorted(granular_data["year"].unique().tolist(), reverse=True)
                    _default_idx_y = available_years.index(_cur_year) if _cur_year in available_years else 0
                    sel_year = st.selectbox("选择年份", options=available_years, index=_default_idx_y, key="port_sel_year_m")
                    year_data = granular_data[granular_data["year"] == sel_year]
                    available_months = sorted(year_data["month"].unique().tolist())
                    _default_idx_m = available_months.index(_cur_month) if _cur_month in available_months else 0
                    sel_month = st.selectbox("选择月份", options=available_months, index=_default_idx_m, key="port_sel_month")
                    plot_data = year_data[year_data["month"] == sel_month]
                    period_label = f"{sel_year}年{sel_month}月"
                else:
                    available_years = sorted(granular_data["year"].unique().tolist(), reverse=True)
                    _default_idx_y = available_years.index(_cur_year) if _cur_year in available_years else 0
                    sel_year = st.selectbox("选择年份", options=available_years, index=_default_idx_y, key="port_sel_year_y")
                    plot_data = granular_data[granular_data["year"] == sel_year]
                    period_label = f"{sel_year}年"

                agg_df = (
                    plot_data.groupby("region", as_index=False)["quantity"]
                    .sum()
                    .sort_values("quantity", ascending=False)
                )

                region_order = ["东北", "华北", "山东", "华东", "福建", "广东", "广西", "其他"]
                agg_df["region"] = pd.Categorical(
                    agg_df["region"], categories=region_order, ordered=True
                )

                port_chart = (
                    alt.Chart(agg_df)
                    .mark_bar()
                    .encode(
                        y=alt.Y("region:N", title="区域", sort=region_order),
                        x=alt.X("quantity:Q", title="到货量"),
                        color=alt.Color("region:N", legend=None, scale=alt.Scale(scheme="tableau10")),
                        tooltip=[
                            alt.Tooltip("region:N", title="区域"),
                            alt.Tooltip("quantity:Q", title="到货量"),
                        ],
                    )
                    .properties(height=400)
                )
                st.altair_chart(port_chart, width="stretch")
                st.caption(f"统计期间：{period_label}")

                port_detail = (
                    plot_data.groupby("region")["port_name_cn"]
                    .apply(lambda x: sorted(x.dropna().unique().tolist()))
                    .reindex(region_order)
                    .dropna()
                )
                cols = st.columns(min(len(port_detail), 4))
                for i, (region, ports) in enumerate(port_detail.items()):
                    with cols[i % len(cols)]:
                        with st.container(border=True):
                            st.markdown(f"**{region}**（{len(ports)}个港口）")
                            st.markdown("、".join(ports) if ports else "无数据")

                st.download_button(
                    f"下载{period_label}区域到货量 CSV",
                    csv_bytes(agg_df),
                    file_name=f"arrival_by_region_{period_label}.csv",
                    mime="text/csv",
                )

with sub_data:
    st.text_input(
        "初始船表文件路径",
        value=st.session_state.workbook_path,
        key="workbook_path",
        on_change=on_workbook_path_change,
    )
    st.caption(
        "每个工作表必须包含 `query_vessel_name`；"
        "建议包含 `departure`（默认时间窗列）和 `quantity`（到货量统计/图表）。"
    )
    workbook_xlsx = current_workbook_path()
    workbook_disk = None
    if not workbook_xlsx.is_file():
        st.error(f"初始船表路径无效或不是文件：{workbook_xlsx}")
    else:
        try:
            workbook_disk = load_initial_workbook(workbook_xlsx)
        except Exception as exc:
            st.error(f"读取失败：{exc}")
        else:
            if (
                st.session_state.wb_edited is None
                or st.session_state.wb_edited_path != str(workbook_xlsx)
            ):
                st.session_state.wb_edited = dict(workbook_disk)
                st.session_state.wb_edited_path = str(workbook_xlsx)
            wb_names = list(st.session_state.wb_edited.keys())

    if workbook_disk is not None:
        edit_sheet = st.selectbox("选择要编辑的工作表", options=wb_names)
        edited_df = st.data_editor(
            st.session_state.wb_edited[edit_sheet],
            key=f"wb_editor_{edit_sheet}",
            num_rows="dynamic",
        )
        st.session_state.wb_edited[edit_sheet] = edited_df
        wb_save_col, wb_reset_col = st.columns(2)
        if wb_save_col.button("保存修改", type="primary", width="stretch"):
            if "query_vessel_name" not in edited_df.columns:
                st.error("当前工作表缺少 query_vessel_name 列，无法保存")
            else:
                try:
                    save_workbook(
                        workbook_xlsx, st.session_state.wb_edited
                    )
                except OSError as exc:
                    st.error(f"保存失败（文件可能正被 Excel 占用）：{exc}")
                except Exception as exc:
                    st.error(f"保存失败：{exc}")
                else:
                    st.session_state.wb_edited = None
                    st.session_state.wb_edited_path = None
                    st.success(f"已保存到 {workbook_xlsx.name}")
                    st.rerun()
        if wb_reset_col.button("放弃修改，从磁盘重新加载", width="stretch"):
            st.session_state.wb_edited = None
            st.session_state.wb_edited_path = None
            for key in [k for k in st.session_state if k.startswith("wb_editor_")]:
                st.session_state.pop(key, None)
            st.rerun()

    st.divider()
    st.subheader("删除本地存档（.saved_returns）")

    def delete_store(names):
        # 已登录的 api 同步清内存; 未登录时直接操作 Store
        api = st.session_state.api
        (api.delete(names) if api is not None else Store().delete(names))

    info_entries = Store().load_info()
    if info_entries:
        st.caption(f"存档内共 {len(info_entries)} 个船名查询结果，可多选删除。")
        del_names = st.multiselect(
            "选择要删除的船名（unique/multiple 会连带删除该 mmsi 的全部抓取数据）",
            options=sorted(info_entries),
            format_func=lambda n: f"{n}（{info_entries[n].get('status')}）",
        )
        if st.button("删除选中船名", type="primary", disabled=not del_names):
            delete_store(del_names)
            clear_tracking_caches()
            st.success(f"已删除 {len(del_names)} 个船名及相关船舶数据")
            st.rerun()
    else:
        st.caption("存档中暂无船名查询结果。")

    st.divider()
    confirm_wipe = st.checkbox("我确认清空全部本地存档（不可恢复）")
    if st.button("清空全部存档", disabled=not confirm_wipe):
        delete_store(None)
        clear_tracking_caches()
        st.success("本地存档已清空")
        st.rerun()

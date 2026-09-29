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

ARRIVAL_PHASE_ORDER = Present.ARRIVAL_PHASE_ORDER
calculate_week_number = Tool.calculate_week_number

APP_DATA_DIR = Path(__file__).resolve().parent / "app_data"
INITIAL_XLSX = APP_DATA_DIR / "initial_vessel_sheet.xlsx"


def current_workbook_path():
    raw_path = st.session_state.get("workbook_path") or str(INITIAL_XLSX)
    return Path(raw_path)


MANAGED_COLS = ["status", "mmsi", "shipType", "flagName"]
MAPPING_VIEW_COLS = ["query_vessel_name", *MANAGED_COLS]
ORIGIN_DAYS = {"美湾": 60, "美西": 25, "巴西": 48, "阿根廷": 60}

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


def store_updated_at():
    try:
        return Store().updated_at
    except Exception:
        return None


def _month_end(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)


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


def resolve_departure_col(df):
    """配票用离港时间列: session 选择优先, 否则 departure, 否则第一列。"""
    chosen = st.session_state.get("departure_col")
    if chosen and chosen in df.columns:
        return chosen
    if "departure" in df.columns:
        return "departure"
    return df.columns[0] if len(df.columns) else None


def regenerate_match_cache():
    """按当前工作簿全部 sheet 重算配票缓存; 失败静默忽略。"""
    workbook = current_workbook_path()
    if not workbook.is_file():
        return
    try:
        sheets = load_initial_workbook(workbook)
        window = st.session_state.get("max_voyage_days", 90)
        max_voyage_days = int(window) if window else None
        for name, df in sheets.items():
            dt_col = resolve_departure_col(df)
            if dt_col is None:
                continue
            MatchTicket().generate_matched_tickets_with_port_calls(
                df, dt_col,
                country=st.session_state.arrival_country,
                sheet_name=name, write_cache=True,
                max_voyage_days=max_voyage_days,
            )
    except Exception:
        pass


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
    info = Present().get_vessel_info_df
    info = info[[c for c in MAPPING_VIEW_COLS if c in info.columns]]
    if "query_vessel_name" not in info.columns:
        # Store 空/异常: 只回花名册, 补空管理列便于编辑
        return Tool.ensure_columns(roster, MAPPING_VIEW_COLS)
    subset = [c for c in ("query_vessel_name", "mmsi") if c in info.columns]
    info = info.drop_duplicates(subset=subset, keep="first")
    merged = roster.merge(info, on="query_vessel_name", how="left")
    return Tool.ensure_columns(merged, MAPPING_VIEW_COLS)


@st.cache_data(show_spinner=False)
def archived_mmsis(stamp):
    """存档内有抓取数据的 mmsi 集合; stamp 变化时重算。"""
    return Store().archived_mmsis


@st.cache_data(show_spinner=False)
def mmsis_for_names(names, snapshot_stamp):
    """给定船名集合, 返回其在 Store 中涉及的全部 mmsi (multiple 含两个候选)。"""
    info = Present().get_vessel_info_df
    if "mmsi" not in info.columns or "query_vessel_name" not in info.columns:
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
    """把映射编辑器的行还原为 Store 的 vessel_info 信封 {船名: {status, info}}。

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


ARRIVAL_PHASE_COLORS = [
    "#54a24b", "#4c78a8", "#b279a2", "#e45756",
]


def _gqs_multi(
    start,
    end,
    sheet_days=None,
    expected_shipping_days=None,
    sheet_name=None,
    status_filter=None,
):
    """单表或多表调 get_quantity_statistics，汇总后返回 (df, monthly, weekly)。

    gqs 只返回业务行 df；monthly/weekly 由本函数用 sum_by_phase 统一算。
    sheet_days 非 None 时按各表航程天数分别取 df 再拼接汇总；
    否则单表直调。空结果返回三空表。
    """
    present = Present()
    if sheet_days is not None:
        parts = [
            present.get_quantity_statistics(
                str(start), str(end),
                expected_shipping_days=days,
                sheet_name=sn,
                status_filter=status_filter,
            )
            for sn, days in sheet_days.items()
        ]
        df = (
            pd.concat(parts, ignore_index=True)
            if parts else pd.DataFrame()
        )
    else:
        df = present.get_quantity_statistics(
            str(start), str(end),
            expected_shipping_days=expected_shipping_days,
            sheet_name=sheet_name,
            status_filter=status_filter,
        )
    if df.empty or "quantity" not in df.columns:
        return df, pd.DataFrame(), pd.DataFrame()
    monthly = Present.sum_by_phase(df, "cal_year", "cal_month")
    weekly = Present.sum_by_phase(df, "week_year", "week_num")
    return df, monthly, weekly


def _get_arrival_df_for_chart(
    sheet_configs,
    expected_shipping_days=None, sheet_name=None,
    start_date="1900-01-01", end_date="2100-01-01",
):
    """返回 (明细 df, monthly 长表, weekly 长表)；多 sheet 时先拼明细再统一汇总。"""
    if sheet_configs is not None:
        sheet_days = {
            (item[3] if len(item) > 3 else None): item[2]
            for item in sheet_configs
        }
        combined, monthly, weekly = _gqs_multi(
            start_date, end_date,
            sheet_days=sheet_days,
            status_filter="unique",
        )
    else:
        combined, monthly, weekly = _gqs_multi(
            start_date, end_date,
            expected_shipping_days=expected_shipping_days,
            sheet_name=sheet_name,
            status_filter="unique",
        )
    if combined.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    return combined.reset_index(drop=True), monthly, weekly


def render_route_map(mmsis, snapshot_stamp):
    from VesselProcessAPI import Map

    return Map(mmsis).route.get_root().render()


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
if "arrival_country" not in st.session_state:
    st.session_state.arrival_country = "CN"
if "departure_col" not in st.session_state:
    st.session_state.departure_col = None


def reset_workbook_ui_state():
    st.session_state.wb_edited = None
    st.session_state.wb_edited_path = None
    st.session_state.pop("track_sheet", None)
    st.session_state.pop("chart_sheet", None)
    st.session_state.pop("departure_col", None)
    for key in list(st.session_state):
        if key.startswith(("mapping_editor_", "wb_editor_")):
            st.session_state.pop(key, None)


def on_workbook_path_change():
    if not st.session_state.workbook_path.strip():
        st.session_state.workbook_path = str(INITIAL_XLSX)
    reset_workbook_ui_state()


def on_match_config_change():
    regenerate_match_cache()
    st.rerun()


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
    saved_at = store_updated_at()
    if saved_at:
        st.caption(f"本地存档更新时间：{saved_at}")

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
            _loading_code_default = {
                "美湾": "US", "美西": "US", "巴西": "BR", "阿根廷": "ARG",
            }.get(sheet_name, "BR")
            loading_text = pc4.text_input(
                "装港国家码（两位码，逗号分隔，用于重名决胜）",
                value=_loading_code_default,
                key=f"pull_loading_{sheet_name}",
            )
            window_df = Tool.read_initial_vessel_sheet(
                initial_sheets[sheet_name],
                pull_datetime_col,
                start=str(pull_start),
                end=str(pull_end),
            ).reset_index()
            window_names = tuple(sorted(
                window_df["query_vessel_name"].dropna().astype(str).unique()
            )) if "query_vessel_name" in window_df.columns else ()
            pull_mmsi = mmsis_for_names(window_names, store_updated_at())
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
                disabled=st.session_state.api is None or not window_names,
            )
            pull_progress = st.container()

        edit_df = st.data_editor(
            mapping_view(sheet_name, store_updated_at(), str(workbook_xlsx)),
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
                Store().save("vessel_info", entries)
            except OSError as exc:
                st.error(f"保存失败（存档文件可能被占用）：{exc}")
            except Exception as exc:
                st.error(f"保存失败：{exc}")
            else:
                mapping_view.clear()
                regenerate_match_cache()
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
                    pull_sheet = Tool.read_initial_vessel_sheet(
                        str(workbook_xlsx),
                        pull_datetime_col,
                        start=str(pull_start),
                        end=str(pull_end),
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
                    regenerate_match_cache()
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
        tc1, tc2, tc3 = st.columns(3)
        track_options = ["全部"] + list(initial_sheets.keys())
        track_name = tc1.selectbox(
            "工作表", options=track_options, key="track_sheet"
        )
        _is_all_track = track_name == "全部"
        if _is_all_track:
            initial_df = pd.concat(
                initial_sheets.values(), ignore_index=True, sort=False
            )
        else:
            initial_df = initial_sheets[track_name]
        start_date = tc2.date_input(
            "到港统计起始日期",
            value=date.today().replace(day=1),
            format="YYYY-MM-DD",
            key="track_start_date",
        )
        end_date = tc3.date_input(
            "到港统计截止日期",
            value=_month_end(date.today()),
            format="YYYY-MM-DD",
            key="track_end_date",
        )

        ORIGIN_DEFAULT_DAYS = ORIGIN_DAYS.get(track_name, 60)
        if st.session_state.get("_prev_track_sheet") != track_name:
            st.session_state["track_exp_days"] = ORIGIN_DEFAULT_DAYS
            st.session_state["_prev_track_sheet"] = track_name
        with st.expander("航程天数设置"):
            expected_shipping_days = st.number_input(
                "航程天数", min_value=0, key="track_exp_days",
                help=(
                    "全部时按各表默认航程天数计算（美湾60/美西25/巴西48/阿根廷60）"
                    if _is_all_track else "在日期列基础上加的天数"
                ),
                disabled=_is_all_track,
            )
        if _is_all_track:
            valid_mmsi = sorted({
                m
                for sn in initial_sheets
                for m in sheet_mmsis(sn, store_updated_at(), str(workbook_xlsx))
            })
        else:
            valid_mmsi = sheet_mmsis(
                track_name, store_updated_at(), str(workbook_xlsx)
            )
        if not valid_mmsi:
            st.warning("该工作表在存档中没有有效 MMSI，请先在 MMSI 查询页在线拉取或手工填写并保存。")
        with st.container(border=True):
            sc1, sc2 = st.columns(2)
            sc1.caption("本地存档更新时间")
            sc1.markdown(f"**{store_updated_at() or '无'}**")
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
            window_df = Tool.read_initial_vessel_sheet(
                initial_df, data_datetime_col,
                start=str(data_start), end=str(data_end),
            ).reset_index()
            window_names = tuple(sorted(
                window_df["query_vessel_name"].dropna().astype(str).unique()
            )) if "query_vessel_name" in window_df.columns else ()
            pull_mmsi = mmsis_for_names(window_names, store_updated_at())
            with st.container(border=True):
                sc1, sc2, sc3 = st.columns(3)
                sc1.caption("时间窗内船名")
                sc1.markdown(f"**{len(window_names)} 个船名**")
                sc2.caption("对应 MMSI")
                sc2.markdown(f"**{len(pull_mmsi)} 个 MMSI**")
                sc3.caption("已查询存档")
                store_mmsis = archived_mmsis(store_updated_at())
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
                        regenerate_match_cache()
                        st.success(
                            f"拉取完成，共 {len(pull_mmsi)} 艘船，运行 {elapsed:.1f} 秒"
                        )
                        st.rerun()

        with st.spinner("正在读取本地存档生成结果…"):
            try:
                present = Present()
                if _is_all_track:
                    sheet_days = {sn: ORIGIN_DAYS.get(sn, 60) for sn in initial_sheets}
                    combined = _gqs_multi(
                        str(start_date), str(end_date),
                        sheet_days=sheet_days,
                    )[0]
                else:
                    combined = _gqs_multi(
                        str(start_date), str(end_date),
                        expected_shipping_days=expected_shipping_days,
                        sheet_name=track_name,
                    )[0]
                if combined.empty:
                    current = pd.DataFrame()
                else:
                    mmsis = (
                        set(combined["mmsi"].dropna().astype(str))
                        if "mmsi" in combined.columns else set()
                    )
                    current = present.current_situation
                    if mmsis and "mmsi" in current.columns:
                        current = current[
                            current["mmsi"].astype(str).isin(mmsis)
                        ].reset_index(drop=True)
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
                    # 指标直接聚合 combined: 船数按船名一行, 阶段/吨位只计 unique
                    by_name = combined.drop_duplicates("query_vessel_name")
                    if "status" in by_name.columns:
                        status_vc = by_name["status"].value_counts()
                        multi_names = sorted(
                            by_name.loc[by_name["status"] == "multiple", "query_vessel_name"]
                        )
                        n_unique = int(status_vc.get("unique", 0))
                        n_multiple = int(status_vc.get("multiple", 0))
                    else:
                        multi_names = []
                        n_unique = n_multiple = 0
                    n_names = len(by_name)
                    uniq_names = (
                        by_name[by_name["status"] == "unique"]
                        if "status" in by_name.columns else by_name
                    )

                    a1, a2, a3, a4 = st.columns(4)
                    a1.metric(
                        "窗口内查询船数", n_names,
                        help=(f"首次靠泊 {st.session_state.arrival_country} 的时间落在 "
                              f"{start_date} ~ {end_date} 内的 unique/multiple 船，"
                              f"另含全部 not_found 船名"),
                    )
                    a2.metric(
                        "唯一匹配 unique", n_unique,
                        help="窗口队列中船名查询唯一命中 MMSI 的船，下排到港阶段仅统计这些船",
                    )
                    a3.metric(
                        "重名歧义 multiple", n_multiple,
                        delta=(f"{n_multiple} 个候选窗口内有到港"
                               if multi_names else None),
                        help=("两个候选 MMSI 未消歧，候选到港不计入下方统计；"
                              "请在 MMSI 查询页核实并改为 unique。"
                              + (f"涉及：{', '.join(multi_names)}"
                                 if multi_names else "")),
                    )
                    a4.metric(
                        "未匹配", n_names - n_unique - n_multiple,
                        help="not_found 船名：无 MMSI，始终保留在结果中",
                    )

                    st.markdown(f"**窗口内唯一匹配（unique）：共 {len(uniq_names)} 艘**")
                    phase_vc = (
                        uniq_names["phase"].dropna().value_counts()
                        if "phase" in uniq_names.columns
                        else pd.Series(dtype="int64")
                    )
                    phase_helps = (
                        "首次挂靠命中 atBerth* 的船",
                        "首次挂靠仅到锚地 (atAnchor*) 尚未靠泊",
                        "回溯数据内无实际挂靠，按 AIS ETB/ETA 预计窗口内到达",
                        "ETA 过滤为 low 时，按初始船表日期 + 航程天数估算",
                    )
                    for col, key, tip in zip(
                        st.columns(4), ARRIVAL_PHASE_ORDER, phase_helps
                    ):
                        col.metric(key, int(phase_vc.get(key, 0)), help=tip)

                    # 到货量: unique 业务行按 phase 直接 groupby; multiple/未匹配不计
                    if "quantity" in combined.columns and "phase" in combined.columns:
                        uniq_rows = (
                            combined[combined["status"] == "unique"]
                            if "status" in combined.columns else combined
                        )
                        phase_qty = uniq_rows.groupby("phase")["quantity"].sum()
                        tonnage = {
                            k: float(phase_qty.get(k, 0.0))
                            for k in ARRIVAL_PHASE_ORDER
                        }
                        st.markdown("**到货量统计：**")
                        st.metric("总计", f"{sum(tonnage.values()):,.0f} 吨")
                        for col, key in zip(st.columns(4), ARRIVAL_PHASE_ORDER):
                            col.metric(
                                key, f"{tonnage[key]:,.0f} 吨",
                                help=f"{key}船对应初始船表 quantity 之和",
                            )
                    with st.expander("到港明细（点击展开）"):
                        st.dataframe(combined, width="stretch")
                        st.download_button(
                            "下载到港记录 CSV",
                            csv_bytes(combined),
                            file_name="combined_vessel_sheet.csv",
                            mime="text/csv",
                        )
                with sub_current:
                    ais_items = Store().load("ais")
                    name_by_mmsi = {
                        str(m): (
                            entry.get("nameEn") or entry.get("aisName") or str(m)
                        )
                        if isinstance(entry, dict) else str(m)
                        for m, entry in ais_items.items()
                    }
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
                        with st.spinner("正在渲染地图…"):
                            map_html = render_route_map(
                                selected, store_updated_at()
                            )
                        st.caption(
                            "绿标=起点，红标=历史终点，蓝旗=AIS 当前位置；"
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
        sheet_options = ["全部"] + list(chart_sheets.keys())
        chart_sheet_name = st.selectbox(
            "工作表", options=sheet_options, key="chart_sheet"
        )

        ORIGIN_DEFAULT_DAYS_C = ORIGIN_DAYS.get(chart_sheet_name, 60)
        _is_all_sheets = (chart_sheet_name == "全部")
        if st.session_state.get("_prev_chart_sheet") != chart_sheet_name:
            st.session_state["chart_exp_days"] = ORIGIN_DEFAULT_DAYS_C
            st.session_state["_prev_chart_sheet"] = chart_sheet_name
        with st.expander("航程天数设置"):
            chart_exp_days = st.number_input(
                "航程天数", min_value=0, key="chart_exp_days",
                help="在日期列基础上加的天数",
                disabled=_is_all_sheets,
            )
        if chart_sheet_name == "全部":
            all_mmsis = set()
            for sn in chart_sheets:
                all_mmsis.update(
                    sheet_mmsis(sn, store_updated_at(), str(workbook_xlsx))
                )
            chart_mmsi = sorted(all_mmsis)
        else:
            chart_mmsi = sheet_mmsis(
                chart_sheet_name, store_updated_at(), str(workbook_xlsx)
            )
        with st.container(border=True):
            sc1, sc2 = st.columns(2)
            sc1.caption("本地存档更新时间")
            sc1.markdown(f"**{store_updated_at() or '无'}**")
            sc2.caption("存档 MMSI 数")
            sc2.markdown(f"**{len(chart_mmsi)} 个 MMSI**")

        chart_sub_time, chart_sub_port = st.tabs(["按时间", "分区域到货"])

        _sheet_configs = None
        if _is_all_sheets:
            _sheet_configs = tuple(
                (df, "departure", ORIGIN_DAYS.get(name, 60), name)
                for name, df in chart_sheets.items()
            )

        arrival_df, monthly_agg, weekly_agg = _get_arrival_df_for_chart(
            _sheet_configs,
            chart_exp_days, chart_sheet_name if not _is_all_sheets else None,
        )

        with chart_sub_time:
            if arrival_df.empty:
                st.info("暂无可用于周度图的 unique 到港数据。")
            else:
                granularity = st.radio(
                    "时间粒度", options=["周度", "月度"], horizontal=True
                )
                available_years = sorted(
                    set(weekly_agg["week_year"].unique())
                    | set(monthly_agg["cal_year"].unique())
                    | set(arrival_df["cal_year"].unique()),
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
                        week_rows = weekly_agg[weekly_agg["week_year"].isin(chart_years)]
                        if selected_phases:
                            week_rows = week_rows[
                                week_rows["phase"].isin(selected_phases)
                            ]
                        pivot = (
                            week_rows.pivot_table(
                                index="week_num",
                                columns="week_year",
                                values="quantity_sum",
                                aggfunc="sum",
                                fill_value=0,
                            )
                            .reindex(range(1, 53), fill_value=0)
                            .fillna(0.0)
                        )
                        pivot.columns = [int(y) for y in pivot.columns]
                        chart_df = pivot.reset_index().rename(
                            columns={"week_num": period_label}
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
                        month_rows = monthly_agg[
                            monthly_agg["cal_year"].isin(chart_years)
                        ]
                        pivot = (
                            month_rows.pivot_table(
                                index="cal_month",
                                columns="cal_year",
                                values="quantity_sum",
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
                        current_rows = monthly_agg[
                            (monthly_agg["cal_year"] == today.year)
                            & (monthly_agg["cal_month"] == today.month)
                        ]
                        current_label = f"{today.year}年{today.month}月"
                    else:
                        cur_week = calculate_week_number(
                            pd.Series([today])
                        ).iloc[0]
                        current_rows = weekly_agg[
                            (weekly_agg["week_year"] == int(cur_week["week_year"]))
                            & (weekly_agg["week_num"] == int(cur_week["week_num"]))
                        ]
                        current_label = (
                            f"{int(cur_week['week_year'])}年第{int(cur_week['week_num'])}周"
                            f"（{cur_week['week_start']:%m-%d}~"
                            f"{cur_week['week_end']:%m-%d}）"
                        )
                    st.markdown(f"**当前到货量结构 · {current_label}**")
                    if current_rows.empty:
                        st.info("当前期暂无到港数据。")
                    else:
                        phase_df = current_rows.copy()
                        phase_sum = (
                            phase_df.groupby("phase")["quantity_sum"].sum()
                            .reindex(ARRIVAL_PHASE_ORDER, fill_value=0.0)
                            .reset_index(name="quantity")
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
            if arrival_df.empty or "port_name_cn" not in arrival_df.columns:
                granular_data = pd.DataFrame()
            else:
                granular_data = arrival_df[["port_name_cn", "week_year", "week_num", "cal_month", "quantity"]].rename(
                    columns={"cal_month": "month"}
                ).copy()
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
                _cur_week_info = calculate_week_number(pd.Series([_today])).iloc[0]
                _cur_year = int(_cur_week_info["week_year"])
                _cur_week = int(_cur_week_info["week_num"])
                _cur_month = int(_today.month)

                if port_granularity == "周度":
                    available_years = sorted(granular_data["week_year"].unique().tolist(), reverse=True)
                    _default_idx_y = available_years.index(_cur_year) if _cur_year in available_years else 0
                    sel_year = st.selectbox("选择年份", options=available_years, index=_default_idx_y, key="port_sel_year_w")
                    year_data = granular_data[granular_data["week_year"] == sel_year]
                    available_weeks = sorted(year_data["week_num"].unique().tolist())
                    _default_idx_w = available_weeks.index(_cur_week) if _cur_week in available_weeks else 0
                    sel_week = st.selectbox("选择周次", options=available_weeks, index=_default_idx_w, key="port_sel_week")
                    plot_data = year_data[year_data["week_num"] == sel_week]
                    period_label = f"{sel_year}年第{sel_week}周"
                elif port_granularity == "月度":
                    available_years = sorted(granular_data["week_year"].unique().tolist(), reverse=True)
                    _default_idx_y = available_years.index(_cur_year) if _cur_year in available_years else 0
                    sel_year = st.selectbox("选择年份", options=available_years, index=_default_idx_y, key="port_sel_year_m")
                    year_data = granular_data[granular_data["week_year"] == sel_year]
                    available_months = sorted(year_data["month"].unique().tolist())
                    _default_idx_m = available_months.index(_cur_month) if _cur_month in available_months else 0
                    sel_month = st.selectbox("选择月份", options=available_months, index=_default_idx_m, key="port_sel_month")
                    plot_data = year_data[year_data["month"] == sel_month]
                    period_label = f"{sel_year}年{sel_month}月"
                else:
                    available_years = sorted(granular_data["week_year"].unique().tolist(), reverse=True)
                    _default_idx_y = available_years.index(_cur_year) if _cur_year in available_years else 0
                    sel_year = st.selectbox("选择年份", options=available_years, index=_default_idx_y, key="port_sel_year_y")
                    plot_data = granular_data[granular_data["week_year"] == sel_year]
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
    st.text_input(
        "到达统计国家码",
        value=st.session_state.get("arrival_country", "CN"),
        key="arrival_country",
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
        all_cols = list(dict.fromkeys(
            c for df in workbook_disk.values() for c in df.columns
        ))
        mv_col, dep_col = st.columns(2)
        with mv_col:
            st.number_input(
                "配票航程上界（天）",
                min_value=0, value=90, key="max_voyage_days",
                on_change=on_match_config_change,
                help=(
                    "配票时只接受 0 < 到港−离港 ≤ 该天数的挂靠；"
                    "超窗挂靠不消费，留给离港更晚的票。"
                    "0=不限制。改动后自动重算配票缓存。"
                    "查询/图表页不再单独做航程超窗剔除，数据质量以本设置为准。"
                ),
            )
        with dep_col:
            if all_cols:
                if st.session_state.get("departure_col") not in all_cols:
                    st.session_state.departure_col = (
                        "departure" if "departure" in all_cols else all_cols[0]
                    )
                st.selectbox(
                    "配票离港时间列（departure_from_origin_port）",
                    options=all_cols,
                    key="departure_col",
                    on_change=on_match_config_change,
                    help="写入配票结果的离港时间；改动后自动重算配票缓存。",
                )
        st.caption(
            "每个工作表必须包含 `query_vessel_name`；"
            "建议包含 `quantity`（到货量统计/图表）。"
            "离港时间列与航程上界用于配票缓存，改动后自动重算。"
        )
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

    info_entries = Store().load("vessel_info")
    if info_entries:
        st.caption(f"存档内共 {len(info_entries)} 个船名查询结果，可多选删除。")
        del_names = st.multiselect(
            "选择要删除的船名（unique/multiple 会连带删除该 mmsi 的全部抓取数据）",
            options=sorted(info_entries),
            format_func=lambda n: f"{n}（{info_entries[n].get('status')}）",
        )
        if st.button("删除选中船名", type="primary", disabled=not del_names):
            delete_store(del_names)
            regenerate_match_cache()
            st.success(f"已删除 {len(del_names)} 个船名及相关船舶数据")
            st.rerun()
    else:
        st.caption("存档中暂无船名查询结果。")

    st.divider()
    confirm_wipe = st.checkbox("我确认清空全部本地存档（不可恢复）")
    if st.button("清空全部存档", disabled=not confirm_wipe):
        delete_store(None)
        regenerate_match_cache()
        st.success("本地存档已清空")
        st.rerun()

"""ExcelReader: 定位 Excel 原始表中的表头锚点单元格; Brazil: 巴西船表的解析与合并。"""

import re
from datetime import date, datetime

import numpy as np
import pandas as pd


class ExcelReader:
    """读取 Excel 并定位表头。"""

    @staticmethod
    def _locate(df: pd.DataFrame, keywords: str | list) -> "tuple[int, int]":
        """从左上角起按行优先扫描, 返回第一个严格等于 keyword 的单元格坐标。

        该坐标视为表格左上角锚点: row 为表头行, col 为起始列,
        调用方以 ``df.iloc[row, col:]`` 取表头、``df.iloc[row+1:, col:]`` 取数据。

        Args:
            df: 原始 DataFrame (建议 header=None 读入)。
            keywords: 目标单元格值, 严格 == 匹配

        Returns:
            (row, col) 整数坐标。

        Raises:
            ValueError: 未找到匹配单元格。
        """
        keywords = [keywords] if isinstance(keywords, str) else list(keywords)
        for kw in keywords:
            hits = df.eq(kw).to_numpy(dtype=bool, na_value=False)
            idx = np.flatnonzero(hits)
            if idx.size:
                return divmod(int(idx[0]), hits.shape[1])
        raise ValueError(f"未找到keywords: {keywords!r}")

    @staticmethod
    def _resize(df: pd.DataFrame, row: int, col: int) -> pd.DataFrame:
        """以 (row, col) 为左上角锚点裁剪 df, 并把 row 行设为列名。

        表头取 ``df.iloc[row, col:]``, 数据取 ``df.iloc[row+1:, col:]``,
        左侧 col 列与 row 行之上的前言行全部丢弃;
        数据全为 NaN 的列一并去掉 (0 行数据时保留全部列)。

        Args:
            df: 原始 DataFrame (建议 header=None 读入)。
            row: 表头行号 (0-based, 含)。
            col: 起始列号 (0-based, 含)。

        Returns:
            裁剪后的 DataFrame, index 已重置为 0..n-1。

        Raises:
            IndexError: row/col 越界。
        """
        nrow, ncol = df.shape
        if not (0 <= row < nrow and 0 <= col < ncol):
            raise IndexError(
                f"锚点 ({row}, {col}) 越界, df 形状为 ({nrow}, {ncol})"
            )
        out = df.iloc[row + 1:, col:].copy()
        out.columns = df.iloc[row, col:].tolist()
        out = out.reset_index(drop=True)
        if len(out):
            out = out.dropna(axis=1, how="all")
        return out

    @staticmethod
    def Brazil_soybean(
        excel_path_list: "str | list[str]",
    ) -> pd.DataFrame:
        """读取一个或多个巴西船表, 按货物过滤、去重、合并多航次船, 最后按状态过滤。

        实现全部在同文件的 :class:`Brazil` 里, 这里只委托给 :meth:`Brazil.soybean`。
        处理顺序:

        1. 过滤 ``cargoes == 'SB'`` —— 必须在去重之前: 同船同到港的不同货类行
           key 相同, 留到合并后过滤会在去重时把 SB 行挤掉。
        2. 按 ``(query_vessel_name, arrival_at_original_port, order)`` 去重且保留
           最后一行 —— issued 较新的文件覆盖较旧的同 key 行。
        3. 合并多航次船: 仅当组内航次从 1 连续到最大序号才合并,
           缺号的组整组去除并提示, 无标记的行保持原样。
        4. 合并之后再按 ``status == 'SAILED'`` 过滤 (按合并后的组状态)。

        Args:
            excel_path_list: 单个 Excel 路径 ``str``, 或路径列表 ``list[str]``;
                传 str 时按单元素列表处理。

        Returns:
            六列 original_port / query_vessel_name / arrival_at_original_port /
            departure_from_original_port / quantity / order,
            index 重置为 0..n-1; 空列表时返回空 DataFrame。
        """
        return Brazil.soybean(excel_path_list)


class Brazil:
    """巴西大豆船表: 单表清洗 + 多表合并。

    对外只暴露 :meth:`soybean` (由 :meth:`ExcelReader.Brazil_soybean` 调用),
    其余成员都是内部实现, 不保证稳定。
    """
    # 单表需要保留的源列
    _DATE_COLS = ("ARR / ETA", "ETB", "ETS")
    _NUM_COLS = ("STATUS", "CHARTERER", "DESTINATION/ORIGIN", "BERTH")

    @staticmethod
    def soybean(excel_path_list: "str | list[str]") -> pd.DataFrame:
        """读取一个或多个巴西船表, 返回清洗、合并后的船期表。

        Args:
            excel_path_list: 单个 Excel 路径 ``str``, 或路径列表 ``list[str]``;
                传 str 时按单元素列表处理; 空列表返回空 DataFrame。

        Returns:
            六列 original_port / query_vessel_name / arrival_at_original_port /
            departure_from_original_port / quantity / order, index 重置为 0..n-1,
            两个日期列为 Timestamp, quantity 为数值,
            order 为航次序号 (可空整数, 无航次标记为 <NA>)。
        """
        if isinstance(excel_path_list, str):
            excel_path_list = [excel_path_list]
        parts: list[tuple[pd.Timestamp, pd.DataFrame]] = []
        for path in excel_path_list:
            try:
                parts.append(Brazil._single(path))
            except Exception as e:
                print(f"处理文件失败: {path}, 错误: {e}")

        if not parts:
            return pd.DataFrame()
        parts.sort(key=lambda part: part[0])  # issued 升序, 稳定排序
        # 显式标注元素类型, 否则 pd.concat 会匹配到 "objs: Iterable[None] -> Never" 重载
        frames: list[pd.DataFrame] = [df for _, df in parts]
        df = pd.concat(frames, ignore_index=True)
        # 货物过滤必须在去重之前: 同船同到港的不同货类行 (一船分两票 SB / MZ)
        # key 相同, 留到合并后过滤会在去重时把 SB 行挤掉
        df = df.loc[df["cargoes"] == "SB"]
        # key 带 order 以免同船不同航次 (ARR/ETA 常相同) 互相覆盖;
        # issued 较新的文件覆盖较旧的同 key 行, 只在旧文件出现的行原样保留
        df = df.drop_duplicates(
            subset=["query_vessel_name", "arrival_at_original_port", "order"],
            keep="last",
        ).reset_index(drop=True)
        df = Brazil._merge_voyage(df)
        # 合并之后按组状态过滤, 并丢弃透传用的 status / cargoes 两列
        out = df.loc[df["status"] == "SAILED"]
        return out.drop(columns=["status", "cargoes",'order']).reset_index(drop=True)

    # ---------------------------------------------------------------- 单表解析

    @staticmethod
    def _single(excel_path) -> "tuple[pd.Timestamp, pd.DataFrame]":
        """读取一张巴西船表: 定位表头 -> 清洗 -> 解析航次序号。

        Args:
            excel_path: Excel 文件路径。

        Returns:
            ``(issued, df)``: issued 为 ``Issued:`` 右侧单元格的日期;
            df 为清洗后的全部行 (未按 CARGOES / STATUS 过滤),
            列名已按局部 ``_COLUMNS`` 映射, index 重置为 0..n-1。

        Raises:
            ValueError: 找不到 ``PORT`` / ``Issued:`` 锚点, 或其右侧不是日期。
        """
        _KEEP = (
                "PORT",
                "VESSEL",
                "ARR / ETA",
                "ETB",
                "ETS",
                "STATUS",
                "QTITY",
                "CARGOES",
                "CHARTERER",
                "DESTINATION/ORIGIN",
                "BERTH",
                "AGENT",
            )

        # 源列 -> 对外列名 (dict 顺序即输出列顺序);
        # status / cargoes 只是内部透传 (货物过滤在去重前, 状态过滤在合并后), 最终丢弃
        _COLUMNS = {
            "PORT": "original_port",
            "VESSEL": "query_vessel_name",
            "ARR / ETA": "arrival_at_original_port",
            "ETS": "departure_from_original_port",
            "QTITY": "quantity",
            "order": "order",
            "STATUS": "status",
            "CARGOES": "cargoes",
        }

        df = pd.read_excel(excel_path, header=None)
        row, col = ExcelReader._locate(df, "PORT")
        issued, year = Brazil._issued(df)
        df = ExcelReader._resize(df, row, col)
        # 只保留处理需要的源列 (源表缺列时跳过)
        df = df.loc[:, [c for c in _KEEP if c in df.columns]]
        df = Brazil._dates(df, year)
        # QTITY 转成数值, 同时丢弃无法解析的行
        df["QTITY"] = pd.to_numeric(
            df["QTITY"].astype(str).str.replace(",", "", regex=False).str.strip(),
            errors="coerce",
        )
        df = df[df["QTITY"].notna()]
        df = Brazil._drop_invalid(df)
        df = Brazil._voyages(df)
        # 按 _COLUMNS 取列并重命名为对外列名 (dict 顺序即输出列顺序)
        out = (
            df.loc[:, list(_COLUMNS)]
            .rename(columns=_COLUMNS)
            .reset_index(drop=True)
        )
        return issued, out

    @staticmethod
    def _issued(df: pd.DataFrame) -> "tuple[pd.Timestamp, int]":
        """取 ``Issued:`` 右侧单元格的日期及其年份 (dd/mm 文本补年份用)。"""
        issued_row, issued_col = ExcelReader._locate(df, ["Issued:","ISSUED:"])
        if issued_col + 1 >= df.shape[1]:
            raise ValueError("Issued: 右侧缺少日期单元格")
        value = df.iat[issued_row, issued_col + 1]
        if not isinstance(value, (datetime, date, pd.Timestamp)) or pd.isna(value):
            raise ValueError(f"Issued: 右侧单元格不是日期: {value!r}")
        issued = pd.Timestamp(value)
        return issued, issued.year

    @staticmethod
    def _dates(df: pd.DataFrame, year: int) -> pd.DataFrame:
        """日期列转 Timestamp: dd/mm 文本按 year 补全, 非法值转 NaT。"""

        def fill_year(value):
            """dd/mm 文本按 year 补成 Timestamp, 非法日期转 NaT, 其余原样返回。"""
            _DD_MM = re.compile(r"^(\d{1,2})/(\d{1,2})$")
            if isinstance(value, str):
                m = _DD_MM.match(value.strip())
                if m:
                    day, month = (int(g) for g in m.groups())
                    try:
                        return pd.Timestamp(year=year, month=month, day=day)
                    except ValueError:
                        return pd.NaT
            return value

        for c in Brazil._DATE_COLS:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c].map(fill_year), errors="coerce")
        return df

    @staticmethod
    def _drop_invalid(df: pd.DataFrame) -> pd.DataFrame:
        """丢弃日期列非法、或数字列实为数字的行 (两个条件互相独立, 可一次算完)。"""
        def _is_date(value) -> bool:
            """是否为有效日期 (NaT、非日期值为 False)。"""
            return isinstance(value, (datetime, date, pd.Timestamp)) and not pd.isna(
                value
            )
        def _is_number(value) -> bool:
            """数字: 数值类型, 或可解析为数字的文本 (NaN 不算)。"""
            if value is None or isinstance(value, bool):
                return False
            if isinstance(value, (int, float, np.integer, np.floating)):
                return not (isinstance(value, (float, np.floating)) and np.isnan(value))
            text = str(value).strip().replace(",", "")
            return bool(text) and not pd.isna(pd.to_numeric(text, errors="coerce"))

        def _flags(masker, cols: "tuple[str, ...]") -> "tuple[pd.Series, bool]":
            """逐行求 ``所有存在列都满足 masker``, 并返回该组列是否至少存在一列。"""
            present = [c for c in cols if c in df.columns]
            if not present:
                return pd.Series(True, index=df.index), False
            frame = pd.DataFrame(
                {c: df[c].map(masker) for c in present},
                index=df.index,
            )
            return frame.all(axis=1), True

        date_ok, _ = _flags(_is_date, Brazil._DATE_COLS)
        all_num, has_num = _flags(_is_number, Brazil._NUM_COLS)
        # 一个数字列都没有时不该把整表判成 "全是数字" 而丢弃
        not_num = ~all_num if has_num else pd.Series(True, index=df.index)
        return df[date_ok & not_num]

    @staticmethod
    def _voyages(df: pd.DataFrame) -> pd.DataFrame:
        """解析航次序号到 order 列, 并去掉船名上的航次标记。

        各航次仍为独立行, 是否合并由 :meth:`_merge_voyage` 按 order 判定。
        有航次标记 <=> order 非空 (两者都由内部 ``_voyage_no`` 判定)。
        """
        # 航次标记, 通配常见写法:
        #   "ANNA G. - 01ST CALL" / "KONA EXPLORER (1st STEP)" / "PUNKT - 2ND BERTH" / "X - CALL 2"
        #   序号: 1 / 01ST / 1st / FIRST ...;
        #   关键词: CALL STEP TRIP VOYAGE VISIT LEG PART ROUND LOAD BERTH
        #   序号在前时分隔符可省 ("PUNKT 01ST BERTH"), 关键词在前时必须带 - ( # ("X - CALL 2")
        _NUM = r"(?:\d{1,2}(?:ST|ND|RD|TH)?|FIRST|SECOND|THIRD|FOURTH)"
        _WORD = (
            r"(?:CALLS?|STEPS?|TRIPS?|VOYAGES?|VISITS?"
            r"|LEGS?|PARTS?|ROUNDS?|LOAD(?:ING)?|BERTHS?|BERTHING)"
        )
        _CALL_PAT = re.compile(
            rf"\s*(?:"
            rf"[-(]?\s*{_NUM}\s*#?\s*{_WORD}"
            rf"|[-(#]\s*{_WORD}\s*#?\s*{_NUM}"
            rf")(?:\s*\))?",
            re.I,
        )
        _NUM_PAT = re.compile(r"\d{1,2}|FIRST|SECOND|THIRD|FOURTH", re.I)
        _ORDINALS = {"FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4}
        def _voyage_no(value) -> float:
            """船名标记中的航次序号 ("01ST CALL" -> 1), 无标记返回 NaN。"""
            hit = _CALL_PAT.search(str(value))
            if hit is None:
                return np.nan
            token = _NUM_PAT.search(hit.group(0))
            if token is None:
                return np.nan
            word = token.group(0).upper()
            if word in _ORDINALS:
                return float(_ORDINALS[word])
            return float(int(word))
        df = df.reset_index(drop=True)
        df["order"] = (
            df["VESSEL"].map(_voyage_no).astype("float64").astype("Int64")
        )
        marked = df["order"].notna()
        stripped = (
            df["VESSEL"]
            .astype(str)
            .str.replace(_CALL_PAT, "", regex=True)
            .str.strip()
        )
        df["VESSEL"] = stripped.where(marked, df["VESSEL"])
        return df

    # ---------------------------------------------------------------- 多表合并

    @staticmethod
    def _merge_voyage(df: pd.DataFrame) -> pd.DataFrame:
        """合并多航次船: 航次从 1 连续到最大序号的组压成一行, 缺号的组整组去除。

        合并规则: quantity 求和、departure 取最后航次、arrival 取最早、
        order 取最大航次号、status 取最后航次的状态, 其余列取组内首个非空值;
        航次缺号的组整组去除并提示; 无标记的行 (order 为 <NA>) 原样保留。
        """
        def _is_complete(values: pd.Series) -> bool:
            """航次是否从 1 连续到组内最大序号 (缺号则整组去除)。"""
            if values.isna().any():
                return False
            seq = sorted({int(v) for v in values})
            return bool(seq) and seq[0] == 1 and seq[-1] == len(seq)
        marked = df["order"].notna()
        rest = df.loc[~marked].copy()
        sub = df.loc[marked].copy()
        full = sub.groupby("query_vessel_name")["order"].apply(_is_complete)
        mergeable = sub["query_vessel_name"].map(full).fillna(False).astype(bool)
        for name in sub.loc[~mergeable, "query_vessel_name"].drop_duplicates():
            print(f"{name}船次不全，已去除")
        sub = sub.loc[mergeable]
        # _pos 记录行在拼接结果里的位置, 合并后按它还原原表顺序
        rest["_pos"] = rest.index
        sub["_pos"] = sub.index
        rule = {
            "arrival_at_original_port": "min",
            "departure_from_original_port": "max",
            "quantity": "sum",
            "order": "max",
            "status": "last",
        }
        agg = {
            c: rule.get(c, "first") for c in df.columns if c != "query_vessel_name"
        }
        agg["_pos"] = "min"
        return (
            pd.concat(
                [
                    sub.sort_values("departure_from_original_port")  # 组内按离港升序
                    .groupby("query_vessel_name", sort=False)
                    .agg(agg)
                    .reset_index()[list(df.columns) + ["_pos"]],
                    rest,
                ]
            )
            .sort_values("_pos")
            .drop(columns="_pos")
            .reset_index(drop=True)
        )


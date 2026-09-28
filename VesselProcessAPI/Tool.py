import pandas as pd
import os

_WEEK_START_ALIASES = {
    "mon": 0, "monday": 0, "周一": 0, "一": 0,
    "tue": 1, "tuesday": 1, "周二": 1, "二": 1,
    "wed": 2, "wednesday": 2, "周三": 2, "三": 2,
    "thu": 3, "thursday": 3, "周四": 3, "四": 3,
    "fri": 4, "friday": 4, "周五": 4, "五": 4,
    "sat": 5, "saturday": 5, "周六": 5, "六": 5,
    "sun": 6, "sunday": 6, "周日": 6, "周天": 6, "日": 6, "天": 6,
}


class Tool():
    def __init__(self):
        pass

    @staticmethod
    def ensure_columns(
        df: pd.DataFrame,
        columns,
        default_dtype="object",
    ) -> pd.DataFrame:
        """缺列则补空列, 避免下游 merge/drop_duplicates KeyError。

        Args:
            df: 输入 DataFrame。
            columns: 列名可迭代, 或 ``{列名: dtype}`` 映射 (dtype 覆盖 default_dtype)。
            default_dtype: columns 为纯列名清单时使用的 dtype, 默认 object。

        Returns:
            补齐后的 DataFrame; 无缺失时原样返回 (不拷贝)。
        """
        schema = (dict(columns) if isinstance(columns, dict)
                  else {c: default_dtype for c in columns})
        missing = {c: dt for c, dt in schema.items() if c not in df.columns}
        if not missing:
            return df
        out = df.copy()
        for col, dt in missing.items():
            out[col] = pd.Series(dtype=dt)
        return out

    @staticmethod
    def calculate_week_number(time: pd.Series, week_start: "int | str" = 5) -> pd.DataFrame:
        """按周起始日标注 week_year / week_num / week_start / week_end。

        Args:
            time: 时间序列（可含 NaT）。
            week_start: 周起始日，0=周一 … 6=周日，或
                "mon"/"Saturday"/"周六" 等；默认 5（周六）。

        Returns:
            DataFrame: week_year / week_num / week_start / week_end 四列；
            空输入或全 NaT 时返回同结构空表；NaT 行不出现，索引对齐非空行。
        """
        if isinstance(week_start, str):
            key = week_start.strip().lower()
            if key not in _WEEK_START_ALIASES:
                raise ValueError(f"week_start 无效: {week_start}")
            week_start = _WEEK_START_ALIASES[key]
        week_start = int(week_start)
        if week_start not in range(7):
            raise ValueError(f"week_start 需在 0~6: {week_start}")

        cols = ["week_year", "week_num", "week_start", "week_end"]
        if time is None or len(time) == 0:
            return pd.DataFrame(columns=cols)
        valid = time.dropna()
        if valid.empty:
            return pd.DataFrame(columns=cols)

        # 归到本周 week_start 日
        start = valid.dt.normalize() - pd.to_timedelta(
            (valid.dt.weekday - week_start) % 7, unit="D"
        )

        def _first_start(y):
            # 第 y 年第 1 周起点: ≤1/1 的最近 week_start 日（可能落在上一年12月）
            j = pd.Timestamp(year=y, month=1, day=1)
            return j - pd.Timedelta(days=(j.weekday() - week_start) % 7)

        week_years, week_nums = [], []
        for ws in start:
            y = ws.year
            # 年末的周若已进入下一年第 1 周则 +1 年
            if ws >= _first_start(y + 1):
                y += 1
            week_years.append(int(y))
            week_nums.append(int((ws - _first_start(y)).days // 7 + 1))

        return pd.DataFrame(
            {
                "week_year": week_years,
                "week_num": week_nums,
                "week_start": start.to_numpy(),
                "week_end": (start + pd.Timedelta(days=6)).to_numpy(),
            },
            index=valid.index,
        )

    @staticmethod
    def read_initial_vessel_sheet(source, datetime_col:str=None, start:str=None, end:str=None, quantity_col:str='quantity', sheet_name=0):
        """
        读取原始船表 (csv 或 excel), 也可直接传入 DataFrame
        Args:
            source: 船表路径 (csv/excel), 或已读入的 pd.DataFrame
            datetime_col(str): 用作时间索引的列名; 为 None 时原样返回,
                不做 to_datetime / set_index / 时间窗口过滤
            start(str):起始日期 (含当天)
            end(str):结束日期 (含当天)
            quantity_col(str): 装运量列名, 非 None 时去除逗号并转为 float
            sheet_name: excel 的 sheet 名或序号 (csv/DataFrame 时忽略), 默认第一个 sheet
        """
        if isinstance(source, pd.DataFrame):
            sheet = source
        else:
            path = os.fspath(source)
            ext = os.path.splitext(path)[1].lower()
            if ext == '.csv':
                sheet = pd.read_csv(path)
            elif ext in ('.xls', '.xlsx', '.xlsm', '.xlsb'):
                sheet = pd.read_excel(path, sheet_name=sheet_name)
            else:
                raise ValueError(f'不支持的文件类型: {ext}')
        if quantity_col and quantity_col in sheet.columns:
            sheet[quantity_col] = pd.to_numeric(
                sheet[quantity_col].astype(str).str.replace(',', ''),
                errors='coerce'
            )
        if not datetime_col:
            return sheet
        sheet[datetime_col] = pd.to_datetime(sheet[datetime_col], errors='coerce')
        sheet = sheet.set_index(datetime_col)
        if start and end:
            left = pd.Timestamp(start)
            right = pd.Timestamp(end) + pd.Timedelta(days=1)
            sheet = sheet.loc[(sheet.index >= left) & (sheet.index < right)]
        return sheet
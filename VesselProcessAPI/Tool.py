import pandas as pd
import os

class Tool():
    def __init__(self):
        pass
    def read_initial_vessel_sheet(self, source, datetime_col:str=None, quantity_col:str='quantity', start:str=None, end:str=None, sheet_name=0):
        """
        读取原始船表 (csv 或 excel), 也可直接传入 DataFrame
        Args:
            source: 船表路径 (csv/excel), 或已读入的 pd.DataFrame
            datetime_col(str): 用作时间索引的列名; 为 None 时原样返回,
                不做 to_datetime / set_index / 时间窗口过滤
            quantity_col(str): 装运量列名, 非 None 时去除逗号并转为 float
            start(str):起始日期
            end(str):结束日期
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
            sheet = sheet.loc[(sheet.index > start) & (sheet.index < end)]
        return sheet

    
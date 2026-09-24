import json
import re
from datetime import datetime
from pathlib import Path

from .JsonOperation import JsonOperation


class Store:
    """统一信封的分片 JSON 存档。

    目录 (root 默认包内 ``.json/.saved_returns``) 内 6 个点文件信封完全相同::

        {"updated_at": ..., "items": {键: {"timestamp", "API_response"}}}

    - ``.vessel_info.json`` / ``.ais.json`` / ... : items 以 mmsi 或船名为键,
      API_response 为原始 API 返回 (Store 不解释字段)。

    写时按块拆开、merge 落盘, 读时按船拼装、对外同形; 所有写入均为
    tmp + os.replace 原子替换。全局时间戳取各文件 updated_at 之最大值
    (不设单独注册表); 锁按 root 目录在 JsonOperation 层共享。
    """

    # 6 个数据文件的逻辑名 (即文件名主体)
    FILES = ("vessel_info", "ais", "current_port", "history_ports",
             "history_route", "future_route")
    # 其中以 mmsi 为键的 5 个
    MMSI_KEYED = FILES[1:]
    DATE_FMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self, root: "str | Path | None" = None) -> None:
        self.root = (Path(root) if root
                     else Path(__file__).with_name(".json") / ".saved_returns")
        self._io = JsonOperation(self.root)

    @staticmethod
    def _fresh_envelope() -> dict:
        return {"updated_at": None, "items": {}}

    def _now(self) -> str:
        return datetime.now().strftime(self.DATE_FMT)

    @property
    def archived_mmsis(self) -> set:
        """有抓取数据的 mmsi (任一 mmsi 分片的键之并集)。"""
        out = set()
        for file_name in self.MMSI_KEYED:
            out.update(self.load(file_name))
        return out

    def save(self, file_name: str, items: dict) -> None:
        """把 ``{键: API_response}`` merge 进指定数据文件。

        若旧值和新值都是 list, 则拼接后按 JSON 字符串去重 (保留旧值顺序,
        新值追加在末尾); 否则同名键覆盖 (vessel_info / ais / current_port 等)。

        Args:
            file_name: 数据文件逻辑名, 必须属于 :attr:`FILES`。
            items: 待写入的 ``{键: API_response}`` 映射。

        Raises:
            KeyError: ``file_name`` 不在 :attr:`FILES` 中。
        """
        if file_name not in self.FILES:
            raise KeyError(f"未知数据块: {file_name}")
        with self._io.lock:
            now = self._now()
            doc = self._io.read(f".{file_name}.json")
            if not isinstance(doc, dict) or not isinstance(doc.get("items"), dict):
                doc = self._fresh_envelope()
            stored = doc["items"]
            for k, data in items.items():
                str_k = str(k)
                old = stored.get(str_k)
                old_data = (old.get("API_response")
                            if isinstance(old, dict) else None)
                if (file_name in {"history_ports", "history_route", "future_route"}
                        and isinstance(old_data, list)
                        and isinstance(data, list)):
                    seen = {json.dumps(r, ensure_ascii=False, sort_keys=True)
                            for r in old_data}
                    merged = list(old_data)
                    for r in data:
                        sig = json.dumps(r, ensure_ascii=False, sort_keys=True)
                        if sig not in seen:
                            seen.add(sig)
                            merged.append(r)
                    stored[str_k] = {"timestamp": now, "API_response": merged}
                else:
                    stored[str_k] = {"timestamp": now, "API_response": data}
            doc["updated_at"] = now
            self._io.write(f".{file_name}.json", doc)

    def load(self, file_name: str) -> dict:
        """返回 ``{键: API_response}``, 解包掉 timestamp 外壳。

        文件缺失或损坏时返回空 dict, 不抛异常。

        Args:
            file_name: 数据文件逻辑名, 必须属于 :attr:`FILES`。

        Returns:
            ``{键: API_response}``; 文件缺失/损坏时为空 dict。

        Raises:
            KeyError: ``file_name`` 不在 :attr:`FILES` 中。
        """
        if file_name not in self.FILES:
            raise KeyError(f"未知数据块: {file_name}")
        doc = self._io.read(f".{file_name}.json")
        items = doc.get("items", {}) if isinstance(doc, dict) else {}
        return {k: item["API_response"] for k, item in items.items()
                if isinstance(item, dict) and "API_response" in item}

    @staticmethod
    def _entry_mmsis(entry) -> set:
        """一条船名查询结果涉及的全部 mmsi: unique 取档案 mmsi, multiple 取候选列表。"""
        data = entry.get("info") if isinstance(entry, dict) else None
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        return {str(r["mmsi"]) for r in rows
                if isinstance(r, dict) and r.get("mmsi") is not None}

    def delete(self, names: "str | list[str] | tuple[str, ...] | None" = None) -> None:
        """按船名删除这些船的全部存档; names=None 时清空整个存档。

        可传单名或船名 list/tuple。船名指 .vessel_info.json 的查询键: 沿结果里的
        mmsi (unique 档案 mmsi, multiple 的两个候选 mmsi) 连带删除 5 个 mmsi
        分片里的船舶数据; .vessel_info.json 内引用这些 mmsi 的其他船名
        (别名/重名行) 一并清除。not_found 无 mmsi, 只删船名条目本身。
        """
        with self._io.lock:
            now = self._now()
            if names is None:  # 全部清空: 6 个文件重置为空信封
                for file_name in self.FILES:
                    self._io.write(f".{file_name}.json", self._fresh_envelope())
                return

            wanted = [names] if isinstance(names, str) else [str(n) for n in names]
            wanted = set(wanted)
            info_doc = self._io.read(".vessel_info.json")
            info_items = info_doc.get("items", {}) if isinstance(info_doc, dict) else {}
            if not (wanted & set(info_items)):
                return
            # 汇总所有指定船名涉及的 mmsi
            target_mmsis = set()
            for name in wanted:
                rec = info_items.get(name)
                if isinstance(rec, dict):
                    target_mmsis |= self._entry_mmsis(rec.get("API_response"))

            # vessel_info: 删指定船名, 以及任何引用目标 mmsi 的其他条目 (别名/重名候选)
            for query, rec in list(info_items.items()):
                payload = rec.get("API_response") if isinstance(rec, dict) else None
                if query in wanted or (target_mmsis
                                       and target_mmsis & self._entry_mmsis(payload)):
                    del info_items[query]
            self._io.write(".vessel_info.json",
                        {"updated_at": now, "items": info_items})

            # 5 个 mmsi 分片: 只重写真正含目标船的那几个
            for file_name in self.MMSI_KEYED:
                doc = self._io.read(f".{file_name}.json")
                stored = doc.get("items") if isinstance(doc, dict) else None
                if not isinstance(stored, dict):
                    continue
                hit = [m for m in target_mmsis if m in stored]
                if not hit:
                    continue
                for m in hit:
                    stored.pop(m, None)
                doc["items"], doc["updated_at"] = stored, now
                self._io.write(f".{file_name}.json", doc)

    @property
    def updated_at(self):
        """全局时间戳: 各数据文件 updated_at 之最大值; 全空时 None。

        只读各文件头 200 字节, 不解析整个 JSON。
        """
        stamps = []
        for k in self.FILES:
            try:
                with open(self.root / f".{k}.json", encoding="utf-8") as f:
                    head = f.read(200)
            except OSError:
                continue
            m = re.search(r'"updated_at"\s*:\s*"([^"]+)"', head)
            if m:
                stamps.append(m.group(1))
        return max(stamps) if stamps else None

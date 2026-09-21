import json
from datetime import datetime
from pathlib import Path

from .JsonOperation import JsonOperation


class Store:
    """统一信封的分片 JSON 存档。

    目录 (root 默认包内 ``.json/.saved_returns``) 内 7 个点文件信封完全相同::

        {"updated_at": ..., "items": {键: {"timestamp", "data"}}}

    - 6 个数据文件 ``.info.json`` / ``.ais.json`` / ... : items 以 mmsi
      或船名为键, data 为原始 API 返回 (Store 不解释字段);
    - 注册表 ``.mmsi_registry.json``: items 以逻辑存储名为键——``"mmsi"``
      的 data 是去重保序的 mmsi 清单, 其余键的 data 是 {count} 索引。

    写时按块拆开、merge 落盘, 读时按船拼装、对外同形; 所有写入均为
    tmp + os.replace 原子替换, 进程内用锁把"读-改-写"串行化。
    """

    # 6 个数据文件的逻辑名 (即文件名主体, 也是注册表 items 内的键)
    ITEMS = ("info", "ais", "current_port", "history_ports", "history_route",
             "future_route")
    # 其中以 mmsi 为键的 5 个, 写入时顺带登记 mmsi 清单
    MMSI_KEYED = ITEMS[1:]
    REGISTRY = ".mmsi_registry.json"
    DATE_FMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self, root: "str | Path | None" = None) -> None:
        self.root = (Path(root) if root
                     else Path(__file__).with_name(".json") / ".saved_returns")
        self._io = JsonOperation(self.root)
        registry = self._io._read(self.REGISTRY)
        self._registry = (registry if isinstance(registry, dict)
                          and isinstance(registry.get("items"), dict)
                          else self._fresh_envelope())

    @staticmethod
    def _fresh_envelope() -> dict:
        return {"updated_at": None, "items": {}}

    def _now(self) -> str:
        return datetime.now().strftime(self.DATE_FMT)

    @property
    def _lock(self):
        return self._io._lock

    @property
    def _mmsis_entry(self) -> dict:
        return self._registry["items"].setdefault(
            "mmsi", {"timestamp": self._now(), "data": []})

    def _register_mmsis(self, mmsis) -> None:
        """合入注册表 mmsi 清单: 只增不清、去重保序, 拉另一批船不影响已有船。"""
        entry = self._mmsis_entry
        known = dict.fromkeys(entry["data"])
        for m in mmsis:
            known.setdefault(str(m), None)
        entry["data"] = list(known)

    def register(self, mmsis) -> None:
        """登记本批 mmsi, 不触碰任何数据文件。"""
        with self._lock:
            before = list(self._mmsis_entry["data"])
            self._register_mmsis(mmsis)
            if self._mmsis_entry["data"] != before:
                now = self._now()
                self._registry["updated_at"] = now
                self._io._write(self.REGISTRY, self._registry)

    def save_items(self, key: str, items: dict) -> None:
        """把 {键: data} merge 进指定数据文件。

        若旧值和新值都是 list, 则拼接后按 JSON 字符串去重 (保留旧值顺序,
        新值追加在末尾); 否则同名键覆盖 (info / ais / current_port 等)。
        """
        if key not in self.ITEMS:
            raise KeyError(f"未知数据块: {key}")
        with self._lock:
            now = self._now()
            doc = self._io._read(f".{key}.json")
            if not isinstance(doc, dict) or not isinstance(doc.get("items"), dict):
                doc = self._fresh_envelope()
            stored = doc["items"]
            for k, data in items.items():
                str_k = str(k)
                old = stored.get(str_k)
                old_data = old["data"] if isinstance(old, dict) else None
                if (key in {"history_ports", "history_route", "future_route"}
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
                    stored[str_k] = {"timestamp": now, "data": merged}
                else:
                    stored[str_k] = {"timestamp": now, "data": data}
            doc["updated_at"] = now
            self._io._write(f".{key}.json", doc)

            if key in self.MMSI_KEYED:
                self._register_mmsis(items)
            # 注册表 items 内登记该逻辑存储的规模
            self._registry["items"][key] = {
                "timestamp": now,
                "data": {"count": len(stored)},
            }
            self._registry["updated_at"] = now
            self._io._write(self.REGISTRY, self._registry)

    def load_items(self, key: str) -> dict:
        """返回 {键: data}, 解包掉 timestamp 外壳; 文件缺失/损坏时为空 dict。"""
        if key not in self.ITEMS:
            raise KeyError(f"未知数据块: {key}")
        doc = self._io._read(f".{key}.json")
        items = doc.get("items", {}) if isinstance(doc, dict) else {}
        return {k: item["data"] for k, item in items.items()
                if isinstance(item, dict) and "data" in item}

    def load_info(self) -> dict:
        return self.load_items("info")

    @staticmethod
    def _entry_mmsis(entry) -> set:
        """一条 info 结果涉及的全部 mmsi: unique 取档案 mmsi, multiple 取候选列表。"""
        data = entry.get("info") if isinstance(entry, dict) else None
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        return {str(r["mmsi"]) for r in rows
                if isinstance(r, dict) and r.get("mmsi") is not None}

    def delete(self, names: "str | list[str] | tuple[str, ...] | None" = None) -> None:
        """按船名删除这些船的全部存档; names=None 时清空整个存档。

        可传单名或船名 list/tuple。船名指 .info.json 的查询键: 沿结果里的
        mmsi (unique 档案 mmsi, multiple 的两个候选 mmsi) 连带删除注册表清单
        及 5 个 mmsi 分片里的船舶数据; .info.json 内引用这些 mmsi 的其他船名
        (别名/重名行) 一并清除。not_found 无 mmsi, 只删船名条目本身。
        """
        with self._lock:
            now = self._now()
            if names is None:  # 全部清空: 7 个文件重置为空信封
                self._registry = self._fresh_envelope()
                self._io._write(self.REGISTRY, self._registry)
                for key in self.ITEMS:
                    self._io._write(f".{key}.json", self._fresh_envelope())
                return

            wanted = [names] if isinstance(names, str) else [str(n) for n in names]
            wanted = set(wanted)
            info_doc = self._io._read(".info.json")
            info_items = info_doc.get("items", {}) if isinstance(info_doc, dict) else {}
            if not (wanted & set(info_items)):
                return
            # 汇总所有指定船名涉及的 mmsi
            target_mmsis = set()
            for name in wanted:
                rec = info_items.get(name)
                if isinstance(rec, dict):
                    target_mmsis |= self._entry_mmsis(rec.get("data"))

            # info: 删指定船名, 以及任何引用目标 mmsi 的其他条目 (别名/重名候选)
            for query, rec in list(info_items.items()):
                data = rec.get("data") if isinstance(rec, dict) else None
                if query in wanted or (target_mmsis
                                       and target_mmsis & self._entry_mmsis(data)):
                    del info_items[query]
            self._io._write(".info.json",
                        {"updated_at": now, "items": info_items})

            # 5 个 mmsi 分片: 只重写真正含目标船的那几个
            for key in self.MMSI_KEYED:
                doc = self._io._read(f".{key}.json")
                stored = doc.get("items") if isinstance(doc, dict) else None
                if not isinstance(stored, dict):
                    continue
                hit = [m for m in target_mmsis if m in stored]
                if not hit:
                    continue
                for m in hit:
                    stored.pop(m, None)
                doc["items"], doc["updated_at"] = stored, now
                self._io._write(f".{key}.json", doc)

            # 注册表: 过滤 mmsi 清单, 并按各文件实际内容重建 count 索引
            reg_items = self._registry["items"]
            mmsi_entry = reg_items.setdefault("mmsi", {"timestamp": now, "data": []})
            mmsi_entry["data"] = [
                m for m in mmsi_entry.get("data", []) if m not in target_mmsis]
            for key in self.ITEMS:
                doc = self._io._read(f".{key}.json")
                items = doc.get("items", {}) if isinstance(doc, dict) else {}
                reg_items[key] = {
                    "timestamp": doc.get("updated_at") if isinstance(doc, dict) else None,
                    "data": {"count": len(items)},
                }
            self._registry["updated_at"] = now
            self._io._write(self.REGISTRY, self._registry)

    def load_all(self):
        """反向拼装: 把"按块存"的数据重新按船拼成 (info, {m: {mmsi, ais, ...}})。"""
        by_mmsi = {}
        for key in self.MMSI_KEYED:
            for m, data in self.load_items(key).items():
                by_mmsi.setdefault(m, {"mmsi": m})[key] = data
        for m in self._registry["items"].get("mmsi", {}).get("data", []):
            by_mmsi.setdefault(m, {"mmsi": m})  # 已登记但从没抓过的船补空壳
        return self.load_info(), by_mmsi

    def load_snapshot(self) -> dict:
        """返回 {saved_at, returns_by_mmsi, returns_by_vessel_name} 快照同形结构。"""
        names, by_mmsi = self.load_all()
        return {
            "saved_at": self._registry.get("updated_at"),
            "returns_by_mmsi": by_mmsi,
            "returns_by_vessel_name": names,
        }

    @property
    def updated_at(self):
        return self._registry.get("updated_at")

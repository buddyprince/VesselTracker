"""JsonOperation: JSON 文件读写的公共基座。"""

import json
import os
import threading
from pathlib import Path
from typing import Optional


class JsonOperation:
    """统一信封的 JSON 文件原子读写，供 Store / MatchTicket 复用。"""

    def __init__(self, root: "str | Path") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read(self, name: str) -> Optional[dict]:
        """读 JSON；文件缺失或损坏时返回 None。"""
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, name: str, obj) -> None:
        """先写 .tmp 再 os.replace 原子改名，崩溃时只留旧文件或新文件。"""
        path = self.root / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _cache_fresh(cache_doc: Optional[dict], store_ts: Optional[str]) -> bool:
        """比对缓存的 updated_at 与 Store 当前时间戳。"""
        return (cache_doc is not None
                and isinstance(cache_doc, dict)
                and cache_doc.get("updated_at") == store_ts)

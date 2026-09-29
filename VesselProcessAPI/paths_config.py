"""paths_config: 集中定义本包的数据目录路径。

存档 / 配票缓存 / token / 地图缓存等 JSON 都放在 ``DATA_DIR`` 下。
要迁移数据目录, 只需改 ``DATA_DIR`` 一处, 其余子路径自动跟随。
"""

from pathlib import Path

# 数据根目录。当前在仓库根; 若要迁回包内, 改成:
#   DATA_DIR = Path(__file__).resolve().parent / ".json"
DATA_DIR = Path(__file__).resolve().parent.parent / ".json"

# ── 子目录 ──────────────────────────────────────────────
SAVED_RETURNS_DIR = DATA_DIR / ".saved_returns"
MATCH_TICKETS_DIR = DATA_DIR / ".match_tickets"

# ── 常用文件 ────────────────────────────────────────────
TOKEN_FILE = DATA_DIR / ".freightower_token.json"
WORLD_PORTS_CACHE = DATA_DIR / ".world_ports.json"
WORLD_COUNTRIES_CACHE = DATA_DIR / ".world_countries.json"
MATCHED_TICKETS_FILE = MATCH_TICKETS_DIR / ".matched_tickets_with_port_calls.json"

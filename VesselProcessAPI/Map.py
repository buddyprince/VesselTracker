import html
import json
import logging
import re
from pathlib import Path

import folium
import requests

from .Store import Store
from .paths_config import WORLD_COUNTRIES_CACHE, WORLD_PORTS_CACHE

log = logging.getLogger(__name__)


class Map:
    """把各船轨迹画在 folium 地图上。

    自读 Store 的 mmsi 分片拼宽表, 与 Present 解耦, 可独立实例化。
    """

    def __init__(self, mmsis=None, save_returns: "str | Path | None" = None):
        """Args:
            mmsis: 可选, 只画这些 mmsi (str); None 表示存档内全部船。
            save_returns: 存档目录路径, 默认包内 .json/.saved_returns。
        """
        store = Store(save_returns) if save_returns else Store()
        by = {}
        for file_name in Store.MMSI_KEYED:
            for m, data in store.load(file_name).items():
                by.setdefault(m, {"mmsi": m})[file_name] = data
        if mmsis is not None:
            keep = {str(x) for x in mmsis}
            by = {m: e for m, e in by.items() if str(m) in keep}
        self._mmsi_keyed_dict = by

    @staticmethod
    def _norm_port(name) -> str:
        """标准化港名, 去国家后缀/标点和空白, 用于英文名匹配。"""
        text = str(name or "").split(",", 1)[0]
        return re.sub(r"[^A-Z0-9]+", "", text.upper())

    def _world_ports(self) -> dict[str, dict[str, tuple[float, float]]]:
        """读取 searoute-py 全球港口缓存; 无缓存或旧缓存时下载并缓存。"""
        cache = WORLD_PORTS_CACHE
        version = "searoute-ports-1.6.0"
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if (data.get("version") == version and data.get("by_code")
                    and data.get("by_name")):
                return {
                    "by_code": {k: tuple(v) for k, v in data["by_code"].items()},
                    "by_name": {k: tuple(v) for k, v in data["by_name"].items()},
                }
        except (OSError, ValueError):
            pass
        ports = {"version": version, "by_code": {}, "by_name": {}}
        try:
            geo = requests.get(
                "https://cdn.jsdelivr.net/gh/genthalili/searoute-py@1.6.0/"
                "searoute/data/ports.geojson", timeout=30).json()
            for feature in geo.get("features", []):
                props = feature.get("properties") or {}
                geom = feature.get("geometry") or {}
                if geom.get("type") != "Point":
                    continue
                lon, lat = geom.get("coordinates") or [None, None]
                if lat is None or lon is None:
                    continue
                code = str(props.get("port") or "").strip().upper()
                if code and code not in ports["by_code"]:
                    ports["by_code"][code] = (lat, lon)
                name = self._norm_port(props.get("name"))
                if name and name not in ports["by_name"]:
                    ports["by_name"][name] = (lat, lon)
        except Exception:
            log.warning("港口 GeoJSON 读取/下载失败, 已跳过目的港直线")
            return {"by_code": {}, "by_name": {}}
        try:
            cache.write_text(json.dumps(ports, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return {"by_code": ports["by_code"], "by_name": ports["by_name"]}

    @property
    def route(self) -> "folium.Map":
        """把各船轨迹直接画在 folium 地图上。

        Esri 街道底图 + folium 示例世界国家边界; 历史轨迹按时间实线连接,
        官方预测航线或无预测时到目的港的直线兜底用同色虚线显示; 每船一种颜色,
        起点绿标、终点红标、AIS 当前位置蓝旗标; 无历史轨迹的船只画预测虚线。
        有轨迹时自动缩放到轨迹范围, 无轨迹时以 [35, 100]、zoom 5 打开。
        """
        result: dict[str, dict] = self._mmsi_keyed_dict
        colors = ["red", "blue", "green", "purple", "orange",
                  "darkred", "cadetblue", "darkpurple"]
        m = folium.Map(
            location=[35, 100], zoom_start=5,
            min_lat=-85.06, max_lat=85.06, min_lon=-180, max_lon=180,
            max_bounds=True, tiles=None)
        folium.TileLayer(
            tiles=("https://server.arcgisonline.com/ArcGIS/rest/services/"
                   "World_Street_Map/MapServer/tile/{z}/{y}/{x}"),
            attr="Esri", no_wrap=True).add_to(m)
        m.get_root().html.add_child(folium.Element(
            "<style>"
            ".voyage-legend{position:absolute;z-index:999;top:10px;right:10px;"
            "background:#fff;padding:8px 10px;border-radius:4px;"
            "box-shadow:0 1px 5px rgba(0,0,0,.4);font:12px Arial,sans-serif;}"
            ".voyage-legend div{display:flex;align-items:center;gap:6px;margin:3px 0;}"
            ".voyage-dot{width:10px;height:10px;border-radius:50%;display:inline-block;}"
            ".voyage-dash{width:22px;border-top:2px dashed #555;display:inline-block;}"
            "</style>"
            "<div class='voyage-legend'>"
            "<div><span class='voyage-dot' style='background:red'></span>终点</div>"
            "<div><span class='voyage-dot' style='background:green'></span>起点</div>"
            "<div><span class='voyage-dot' style='background:#1e90ff'></span>当前位置</div>"
            "<div><span class='voyage-dash'></span>预测航线</div>"
            "</div>"))
        # 国境线: 优先读包内缓存, 无缓存或损坏时联网下载并缓存; 失败仅告警不影响出图
        border = None
        cache = WORLD_COUNTRIES_CACHE
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("features"):
                border = cached
        except (OSError, ValueError):
            pass
        if border is None:
            try:
                border = requests.get(
                    "https://cdn.jsdelivr.net/gh/python-visualization/folium@master/"
                    "examples/data/world-countries.json",
                    timeout=30).json()
            except Exception:
                log.warning("国境线 GeoJSON 读取/下载失败, 已跳过")
            else:
                try:
                    cache.write_text(json.dumps(border, ensure_ascii=False),
                                     encoding="utf-8")
                except OSError:
                    pass
        if border is not None:
            folium.GeoJson(
                border, name="countries",
                style_function=lambda x: {
                    "fillColor": "transparent", "color": "black",
                    "weight": 1, "opacity": 0.6}).add_to(m)
        ports = self._world_ports()
        bounds = []
        for i, (mmsi, entry) in enumerate(result.items()):
            color = colors[i % len(colors)]
            ais = entry.get("ais") or {}
            popup_text = (
                "<br>".join(
                    f"{key}: {html.escape(str(ais[key]))}"
                    for key in ("nameEn", "navStatusCn", "updateTime",
                                "draught", "dest", "etaStd")
                    if ais.get(key) is not None)
                if ais else "无 AIS 数据")
            # 历史轨迹: lat/lon 为 1/1e6 度, 实线 + 起终点标记
            pts = [p for p in (entry.get("history_route") or [])
                   if p.get("lat") is not None and p.get("lon") is not None]
            coords = [(p["lat"] / 1e6, p["lon"] / 1e6) for p in pts]
            # AIS 当前位置: lat/lon 为 1/1e6 度, 蓝标 (与轨迹红终点区分)
            current = None
            if ais.get("lat") is not None and ais.get("lon") is not None:
                current = (ais["lat"] / 1e6, ais["lon"] / 1e6)
            if coords:
                folium.PolyLine(coords, color=color, weight=2.5,
                                tooltip=str(mmsi)).add_to(m)
                folium.Marker(
                    coords[0], icon=folium.Icon(color="green"),
                    popup=folium.Popup(f"<b>起点</b><br>{popup_text}",
                                       max_width=300)).add_to(m)
                folium.Marker(
                    coords[-1], icon=folium.Icon(color="red"),
                    popup=folium.Popup(f"<b>终点</b><br>{popup_text}",
                                       max_width=300)).add_to(m)
                bounds += coords
            if current:
                folium.Marker(
                    current, icon=folium.Icon(color="blue", icon="flag"),
                    tooltip=str(mmsi),
                    popup=folium.Popup(f"<b>当前位置</b><br>{popup_text}",
                                       max_width=300)).add_to(m)
                bounds.append(current)
            # 官方预测航线: coordinates 实际为 [经度, 纬度] 的度, 虚线
            future = entry.get("future_route")
            fcoords = []
            if isinstance(future, dict) and future.get("coordinates"):
                fcoords = [(c[1], c[0]) for c in future["coordinates"]
                           if c[0] is not None and c[1] is not None]
            if fcoords:
                folium.PolyLine(
                    fcoords, color=color, weight=2.5, opacity=0.75,
                    dash_array="8,6",
                    tooltip=f"{mmsi} 预测航程 {future.get('distance')} km"
                ).add_to(m)
                bounds += fcoords
            else:  # 无官方预测时, 从当前 AIS 位置(或历史末点)直线到目的港
                if current is None and coords:
                    current = coords[-1]
                dest_name = ais.get("destStd") or ais.get("dest")
                dest_code = str(ais.get("destcode") or "").strip().upper()
                destination = ports["by_code"].get(dest_code)
                if not destination:
                    destination = ports["by_name"].get(self._norm_port(dest_name))
                if current and destination:
                    straight = [current, destination]
                    folium.PolyLine(
                        straight, color=color, weight=2.5, opacity=0.75,
                        dash_array="8,6",
                        tooltip=f"{mmsi} 直线至 {dest_name}"
                    ).add_to(m)
                    bounds += straight
                elif dest_name:
                    log.warning(f"[目的港未匹配] {mmsi} {dest_name}")
        if bounds:
            m.fit_bounds([
                (min(x[0] for x in bounds), min(x[1] for x in bounds)),
                (max(x[0] for x in bounds), max(x[1] for x in bounds))])
        return m

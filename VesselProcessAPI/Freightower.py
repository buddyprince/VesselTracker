import difflib
import functools
import json
import logging
import re
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import requests
from .Presentation import Present
from .Store import Store

log = logging.getLogger(__name__)


class FreightowerAPI:
    """飞驼官方 OpenAPI 客户端: 初始化时登录拿 token, 方法直接返回官方 JSON。"""

    def __init__(self, force: bool = False) -> None:
        """Args:
            force: True 时忽略本地 token, 强制重新微信扫码。
        """
        self._openapi = "https://openapi.freightower.com"
        self._ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": self._ua, "Accept": "application/json"})
        self._http.headers["Authorization"] = "Bearer " + self._login(force=force)
        self._store = Store()
        # 内存与磁盘分片一一对应: 键名即 Store.ITEMS (info/ais/...),
        # 值均为 {键: data}; 展示视图统一由 Present 现拼
        self._items = {key: {} for key in Store.ITEMS}
        self._items["info"] = self._store.load_info()
        self._mmsi_for_search = []
        self._date_format = "%Y-%m-%d %H:%M:%S"

    def delete(self, names: "str | list[str] | tuple[str, ...] | None" = None):
        """删除存档并同步内存; names=None 清空全部。语义见 :meth:`Store.delete`。"""
        if names is None:
            self._store.delete(None)
            self._items = {key: {} for key in Store.ITEMS}
            self._mmsi_for_search = []
            return self
        wanted = {names} if isinstance(names, str) else {str(n) for n in names}
        target_mmsis = set()
        for name in wanted:  # 汇总待删船名涉及的 mmsi (含 multiple 的两个候选)
            rec = self._items["info"].get(name)
            if isinstance(rec, dict):
                target_mmsis |= Store._entry_mmsis(rec)
        self._store.delete(wanted)
        # 内存与磁盘保持一致: 删指定船名 + 引用目标 mmsi 的别名条目
        for query, rec in list(self._items["info"].items()):
            if query in wanted or (target_mmsis
                                   and target_mmsis & Store._entry_mmsis(rec)):
                self._items["info"].pop(query, None)
        for key in Store.MMSI_KEYED:
            for m in target_mmsis:
                self._items[key].pop(m, None)
        self._mmsi_for_search = [
            m for m in self._mmsi_for_search if m not in target_mmsis]
        return self

    def _save_section(section: str):
        """链式方法装饰器: 读完即存——只把本批 mmsi 的一个数据块 merge 落盘。

        Args:
            section: 数据块名 (ais/current_port/history_ports/history_route/future_route)。
        """
        def decorator(method):
            @functools.wraps(method)
            def wrapper(self: "FreightowerAPI", *args, **kwargs):
                result = method(self, *args, **kwargs)
                block = self._items[section]
                vessels = {m: block[m] for m in self._mmsi_for_search if m in block}
                try:
                    self._store.save_items(section, vessels)
                except OSError as e:
                    log.warning(f"结果块 {section} 写入失败: {e}")
                return result
            return wrapper
        return decorator

    def _save_info(method):
        """船名查询方法装饰器: 读完即把船名结果 merge 进 .info.json。"""
        @functools.wraps(method)
        def wrapper(self: "FreightowerAPI", *args, **kwargs):
            result = method(self, *args, **kwargs)
            try:
                self._store.save_items("info", self._items["info"])
            except OSError as e:
                log.warning(f"船名结果写入失败: {e}")
            return result
        return wrapper


    def _token_valid(self, token: str) -> bool:
        headers = {"Authorization": "Bearer " + token}
        try:
            body = self._http.get(
                self._openapi + "/vessel2/ais/getVesselDetail",
                params={"mmsi": "0"},
                headers=headers,
                timeout=15,
            ).json()
        except (requests.RequestException, ValueError):
            return True
        return not str(body.get("statusCode", "")).startswith("401")

    def _get(self, path: str, params: dict | None = None):
        """GET 官方接口直接返回 data; 无数据补空列表; 40100 token 失效时扫码重登后再试一次。"""
        for attempt in range(2):
            body = self._http.get(self._openapi + path,
                                  params=params or {}, timeout=30).json()
            if body.get("statusCode") != 40100 or attempt == 1:
                break
            self._http.headers["Authorization"] = "Bearer " + self._login(force=True)
        return body.get("data") or []

    def _login(self, force: bool = False, timeout: int = 300) -> str:
        """返回可用 token: 优先读本地, 读不到(或 force)就弹微信二维码扫码登录。

        二维码在 Jupyter 输出区直接渲染, 无 IPython 时用默认浏览器打开。

        Args:
            force: True 时忽略本地 token, 强制重新扫码。
            timeout: 等待扫码确认的最长秒数。

        Returns:
            登录后的 access token, 并写入本地 json 复用。

        Raises:
            TimeoutError: 超时仍未确认。
        """
        token_file = Path(__file__).with_name(".json") / ".freightower_token.json"
        if not force:  # 优先读本地 token, 校验失效后继续扫码
            try:
                token = json.loads(token_file.read_text(encoding="utf-8"))["access_token"]
            except (OSError, ValueError, KeyError):
                pass
            else:
                if self._token_valid(token):
                    return token

        self._http.headers.pop("Authorization", None)  # 清掉失效 token, 否则 /auth/* 会 401
        scene, url, deadline = "", "", time.time() + timeout  # 匿名取码, /auth/* 不带 Authorization
        while time.time() < deadline:
            if not scene:  # 首次或过期后取一张新码
                qr = self._get("/auth/wechat/qrcode")
                scene, url = qr["scene"], qr["url"]
                print("微信扫码登录 (也可手动打开):\n" + url)
                try:  # Jupyter 内嵌渲染二维码, 无 IPython 时用浏览器打开
                    from IPython.display import Image, display
                    display(Image(url=url))
                except ImportError:
                    webbrowser.open(url)
            state = self._get("/auth/wechat/qrcode_state", {"scene": scene})["state"]
            if state == "EXPIRED":
                scene = ""  # 下一轮重新取码
            elif state == "SUCCESS":
                d = self._get("/auth/wechat/token", {"sceneId": scene})
                token = d if isinstance(d, str) else d.get("access_token")
                if not token:
                    raise RuntimeError(f"扫码成功但未取到 token: {d}")
                token_file.write_text(json.dumps({"access_token": token}), encoding="utf-8")
                print("登录成功, token 已保存到", token_file)
                return token
            time.sleep(3)
        raise TimeoutError("扫码超时, 请重新运行")

    @staticmethod
    def _norm(name: str) -> str:
        """压缩空白、转大写、去 M.V./MV/MT/SS 等船名前缀, 用于船名匹配。"""
        text = re.sub(r"\s+", " ", str(name).strip()).upper()
        return re.sub(r"^(?:M[./]\s?[VT]|S[./]\s?S|MV|MT|SS)[ .]+", "", text).strip()

    @staticmethod
    def _vessel_score(cand: dict, info: "dict | None") -> int:
        """消歧打分: 面向国际农产品海运——干散货船优先, 油船/集装箱等降权。"""
        # 业务目标: 追踪国际农产品(粮食/大豆/玉米等)海运, 主力是干散货船
        cargo_types = set(range(70, 80))      # 70~79 货船(含干散货/杂货)
        tanker_types = set(range(80, 90))     # 80~89 油船等液散, 与农产品不相关
        small_types = set(range(30, 40)) | set(range(50, 60))  # 渔船/拖轮等小艇
        agri_keywords = ("干散货", "散货", "bulk", "grain")
        non_agri_keywords = ("集装箱", "container", "油", "tanker", "液体", "液化",
                             "气体", "化工", "客", "滚装", "汽车", "冷藏")
        try:
            st = int(cand.get("shiptype"))
        except (TypeError, ValueError):
            st = None
        score = 0
        # 进入打分的候选已保证有档案且 IMO 合法, IMO 不再区分分值

        # 船型: 有档案时按档案船型文本细分, 没有则退化为 AIS 数字码
        type_text = ""
        if info:
            type_text = f"{info.get('shipType') or ''} {info.get('aisVesselType') or ''}".lower()
        if type_text:
            if any(k in type_text for k in non_agri_keywords):
                score -= 60  # 集装箱/油船/气体/客滚等与干散农产品无关, 重罚; "液体散货"也归此类
            elif any(k in type_text for k in agri_keywords):
                score += 60  # 干散货: 粮食/大豆等农产品主力船型, 重奖
        elif st in cargo_types:
            score += 20  # 无档案时: 货船(含干散/杂货)小幅加分
        elif st in tanker_types:
            score -= 20  # 油船等液散小幅减分
        if st in small_types:
            score -= 20  # 渔船/拖轮等小艇降权

        if cand.get("callsign"):
            score += 5
        if info:
            score += 40  # 有船舶资料库档案者优先
        if cand.get("lasttime"):
            age_days = (time.time() - cand["lasttime"]) / 86400
            score += max(0, 30 - int(age_days))  # AIS 越新分越高, 30 天前封顶为 0
        return score
    
    @_save_info
    def get_info_by_vessel_name(
        self,
        names: "str | list[str] | tuple[str, ...]",
        max: int = 10,
        loading_country: "str | list[str] | tuple[str, ...] | None" = None,
    ) -> dict:
        """船名转 MMSI: 搜索 -> 拉档案 -> 打分选出唯一 MMSI。

        三步: 1) getVessels 按船名搜出候选 MMSI; 2) 对每个候选 MMSI 调
        getShipVesselInfo 拿档案, 无档案或 IMO 非法/缺失的候选直接淘汰;
        3) 按商船船型/呼号/AIS 新鲜度打分, 选出唯一 MMSI。

        精确同名落空时先从已请求候选里做相似度兜底 (船名后缀或单字符拼写错,
        阈值 0.85; 无过阈值候选时再用首单词重搜一次); 同合法 IMO 的换旗/旧
        MMSI 折叠为一艘船, 取 AIS 最新者。先用入参 max 搜, 结论非 unique 且
        max<100 时放大到 100 重走一遍, 防止真船排在前 max 条之外被截断。

        Args:
            names: 单个船名/MMSI, 或多个组成的 list/tuple。
            max: 每个船名首轮搜索候选条数, 默认 10, 最大 100。
            loading_country: 并列决胜用, UN/LOCODE 两位国家码 (如 "BR"),
                可传多个; 仅当打分并列时查近 60 天历史挂靠, 候选曾挂靠任一
                国家即加 30 分; 若仍并列, 仅在这些装港国命中候选中, 下一港
                位于东亚/东南亚者再加 20 分。不作过滤, 不传则维持 multiple。

        Returns:
            ``{船名: {"status": ..., "info": ...}}``, 顺序与入参一致, 三种情况:
            唯一命中: status 为 ``"unique"``, info 为 getShipVesselInfo 的档案首条 dict
            (MMSI 在 info 的 ``mmsi`` 字段);
            无法消歧(重名且前两名分差 ≤15): status 为 ``"multiple"``,
            info 为最终得分最高的前两个候选档案 dict 列表 (高分在前, 同分保序);
            无同名候选: status 为 ``"not_found"``, info 为 None。
        """
        name_list = [names] if isinstance(names, str) else list(names)
        country_codes = None
        if loading_country is not None:
            seq = [loading_country] if isinstance(loading_country, str) else list(loading_country)
            country_codes = {str(c).strip().upper()[:2] for c in seq}

        result: dict[str, dict] = {}
        infos: dict[str, "dict | None"] = {}  # MMSI -> 档案, 同 MMSI 只查一次且跨船复用
        recent_ports: dict = {}  # MMSI -> 近60天挂靠, 并列决胜时才查, 跨船/跨遍缓存
        dest_details: dict = {}  # MMSI -> AIS详情, 二级决胜时才查, 跨船/跨遍缓存
        history_begin = (datetime.now() - timedelta(days=60)).strftime(self._date_format)

        for name in name_list:
            target = self._norm(name)
            entry = None
            # 两遍搜索: 先用入参 max; 非 unique 且 max<100 时放大到 100 重走,
            # 避免真船排在前 max 条之外被截断 (infos 跨遍/跨船缓存, 不重复请求)
            for search_max in [max] + ([100] if max < 100 else []):
                # 第一步: 船名 -> 候选 (只保留同名候选, 无同名即视为未找到)
                cands = self._get("/vessel2/ais/getVessels",
                                  {"kw": name, "max": search_max})
                same = [c for c in cands if self._norm(c.get("name", "")) == target]
                if not same:
                    # 精确同名落空后先从已经请求的候选里面挑选可能的
                    # (船名后缀/单字符拼写错, 相似度阈值 0.85); 没有过阈值候选时
                    # 再用首单词放大候选量重搜一次
                    pool = cands
                    fuzzy = [(difflib.SequenceMatcher(
                                  None, target, self._norm(c.get("name", ""))).ratio(),
                              self._norm(c.get("name", "")))
                             for c in pool if c.get("name")]
                    fuzzy = [item for item in fuzzy if item[0] >= 0.85]
                    if not fuzzy and search_max < 100 and " " in name.strip():
                        pool = self._get("/vessel2/ais/getVessels",
                                         {"kw": name.split()[0], "max": 100})
                        fuzzy = [(difflib.SequenceMatcher(
                                      None, target, self._norm(c.get("name", ""))).ratio(),
                                  self._norm(c.get("name", "")))
                                 for c in pool if c.get("name")]
                        fuzzy = [item for item in fuzzy if item[0] >= 0.85]
                    if fuzzy:
                        matched_names = {norm for _, norm in fuzzy}
                        log.warning(f"[模糊匹配] {name} -> "
                                    f"{', '.join(sorted(matched_names))}")
                        same = [c for c in pool
                                if self._norm(c.get("name", "")) in matched_names]

                # 第二步: 只给合法 IMO 候选查档案 (其余必被淘汰),
                # 第二遍对首遍瞬时空返回的合法 IMO 重试一次
                for c in same:
                    mmsi = c["mmsi"]
                    imo = str(c.get("imo") or "").strip()
                    imo_valid = (imo.isdigit() and len(imo) == 7 and
                                 sum(int(d) * (7 - i) for i, d in enumerate(imo[:-1])) % 10
                                 == int(imo[-1]))
                    if not imo_valid:
                        infos.setdefault(mmsi, None)
                        continue
                    if (mmsi not in infos
                            or (search_max == 100 and infos[mmsi] is None)):
                        rows = self._get("/vessel2/getShipVesselInfo", {"mmsi": mmsi})
                        infos[mmsi] = rows[0] if rows else None

                # 第三步: 无档案或 IMO 非法/缺失的候选直接淘汰; 同合法 IMO
                # (换旗/旧 MMSI) 视为同一艘船, 只保留 AIS 最新者 (并列保序)
                folds, order = {}, []
                for c in same:
                    imo = str(c.get("imo") or "").strip()
                    imo_valid = (imo.isdigit() and len(imo) == 7 and
                                 sum(int(d) * (7 - i) for i, d in enumerate(imo[:-1])) % 10
                                 == int(imo[-1]))
                    if infos[c["mmsi"]] is None or not imo_valid:
                        continue
                    if imo not in folds:
                        folds[imo], order = c, order + [imo]
                    elif (folds[imo].get("lasttime") or 0) < (c.get("lasttime") or 0):
                        folds[imo] = c
                same = [folds[k] for k in order]
                scores = {c["mmsi"]: self._vessel_score(c, infos[c["mmsi"]]) for c in same}
                top2 = sorted(scores.values(), reverse=True)[:2]
                # 并列决胜: 先查近60天历史挂靠, 曾到过任一装港国的候选加 30 分;
                # 若仍并列, 仅在装港国命中候选中查 AIS 下一港, 东亚/东南亚再加 20 分;
                # 都不满足或仍并列时维持 multiple
                if (country_codes is not None and len(top2) > 1
                        and top2[0] - top2[1] <= 15):
                    loading_hits = set()
                    for c in same:
                        mmsi = c["mmsi"]
                        if mmsi not in recent_ports:
                            recent_ports[mmsi] = self._get(
                                "/vessel2/getPortCallContainerByMmsi",
                                {"mmsi": mmsi, "begin": history_begin})
                        ports = recent_ports[mmsi]
                        if isinstance(ports, list) and any(
                                str(p.get("countryCode") or "").strip().upper()
                                in country_codes for p in ports):
                            scores[mmsi] += 30
                            loading_hits.add(mmsi)
                    top2 = sorted(scores.values(), reverse=True)[:2]
                    if len(top2) > 1 and top2[0] - top2[1] <= 15:
                        asia_codes = {
                            "CN", "HK", "TW", "JP", "KP", "KR", "MO",
                            "SG", "MY", "TH", "VN", "ID", "PH", "KH", "MM", "BN",
                        }
                        for mmsi in loading_hits:
                            if mmsi not in dest_details:
                                detail = self._get(
                                    "/vessel2/ais/getVesselDetail", {"mmsi": mmsi})
                                dest_details[mmsi] = detail if isinstance(detail, dict) else {}
                            ais = dest_details[mmsi]
                            dest_code = str(ais.get("destcode") or "").strip().upper()[:2]
                            dest_text = str(ais.get("destStd") or ais.get("dest") or "")
                            dest_suffix = dest_text.rsplit(",", 1)[-1].strip().upper()
                            if dest_code in asia_codes or dest_suffix in asia_codes:
                                scores[mmsi] += 20
                        top2 = sorted(scores.values(), reverse=True)[:2]

                # 一个候选都没有: not_found; 重名且前两名分差 ≤15: multiple
                # (info 只给得分最高的前两个候选档案 dict, 高分在前、同分保序)
                if not same:
                    entry = {"status": "not_found", "info": None}
                elif len(top2) > 1 and top2[0] - top2[1] <= 15:
                    log.warning(f"[歧义] {name}")
                    ranked = sorted(same, key=lambda c: scores[c["mmsi"]], reverse=True)
                    entry = {
                        "status": "multiple",
                        "info": [infos[c["mmsi"]] or c for c in ranked[:2]]}
                else:
                    picked = sorted(same, key=lambda c: scores[c["mmsi"]])[-1]
                    entry = {"status": "unique", "info": infos[picked["mmsi"]]}
                if entry["status"] == "unique":
                    break
            result[name] = entry

        self._items["info"].update(result)
        return result

    def set_mmsi(
        self,
        mmsis: ("str | int | list[str | int] | tuple[str | int, ...] "
                "| pd.Series | pd.DataFrame")
    ):
        """
        设定mmsi，用于后续提取数据
        Args:
            mmsis: 单个 MMSI (str/int)、list/tuple、pd.Series,
                或单列 pd.DataFrame; 空值自动跳过, 数字转不带 .0 的字符串。
        """
        if isinstance(mmsis, pd.DataFrame):  # 单列 DataFrame -> 该列 Series
            if mmsis.shape[1] != 1:
                raise ValueError("mmsis 只支持单列 DataFrame")
            mmsis = mmsis.iloc[:, 0]
        if isinstance(mmsis, pd.Series):
            mmsi_list = [str(int(float(m))) if pd.api.types.is_number(m) else str(m)
                            for m in mmsis if pd.notna(m)]
        elif isinstance(mmsis, (str, int, float)):
            mmsi_list = [str(int(float(mmsis)))
                         if pd.api.types.is_number(mmsis) else str(mmsis)]
        else:
            mmsi_list = [str(int(float(m))) if pd.api.types.is_number(m) else str(m)
                         for m in mmsis if pd.notna(m)]
        self._mmsi_for_search = mmsi_list
        # 只登记不清空: 逐块从分片把旧数据播种进内存, 本会话已抓的新数据优先,
        # 其他船的存档不受影响 (拉另一张工作表不再冲掉既有结果)
        self._store.register(mmsi_list)
        for key in Store.MMSI_KEYED:
            block = self._items[key]
            saved = self._store.load_items(key)
            for m in mmsi_list:
                if m not in block and m in saved:
                    block[m] = saved[m]
        return self

    def _check_mmsi(self):
        if not self._mmsi_for_search:
            raise ValueError('未设定用于搜索的mmsi，请先调用set_mmsi')

    @_save_section("ais")
    def get_ais(self, get_ais_by_multiple: bool = True):
        """
        取多船 AIS 定位 + ETA getManyVesselDetail (批量一次) 或者 getVesselDetail (串行多次)。
        Args:
            get_ais_by_multiple: 在ais为True时，是否通过调并发多船接口getManyVesselDetail，若触发限流改用单船接口串行调用
        """
        self._check_mmsi()
        if get_ais_by_multiple:
            ais_map = {}
            for i in range(0, len(self._mmsi_for_search), 300):
                rows = self._get(
                    "/vessel2/ais/getManyVesselDetail",
                    {"mmsis": ",".join(self._mmsi_for_search[i:i + 300])})
                ais_map.update({str(r["mmsi"]): r for r in rows})
            for m in self._mmsi_for_search:
                self._items["ais"][m] = ais_map.get(m)
        else: # 串行请求, 不并发以免限流; 单船接口无数据时 _get 返回 []
            for m in self._mmsi_for_search:
                rec = self._get("/vessel2/ais/getVesselDetail", {"mmsi": m})
                self._items["ais"][m] = rec if isinstance(rec, dict) else None
        return self

    @_save_section("current_port")
    def get_current_port(self):
        """
        取当前挂靠港 getCurrentVoyageByMmsi
        """
        self._check_mmsi()
        for m in self._mmsi_for_search:
            self._items["current_port"][m] = self._get(
                "/vessel2/getCurrentVoyageByMmsi", {"mmsi": m})
        return self

    @_save_section("history_ports")
    def get_history_port(self, port_call_days: int = 90):
        """
        取历史挂靠港 getPortCallContainerByMmsi。
        Args:
            port_call_days: 历史挂靠回溯天数 (最多 365), 默认 90。
        """
        self._check_mmsi()
        now = datetime.now()
        begin = (now - timedelta(days=port_call_days)).strftime(self._date_format)
        for m in self._mmsi_for_search:
            self._items["history_ports"][m] = self._get(
                "/vessel2/getPortCallContainerByMmsi",
                {"mmsi": m, "begin": begin})
        return self

    @_save_section("history_route")
    def get_history_route(self, history_route_days: int = 40):
        """
        取历史轨迹 getVesselVoyage
        Args:
            history_route_days: 历史轨迹回溯天数 (最多 180, 内部按 90 天切片)。
        """
        self._check_mmsi()
        now = datetime.now()
        v_start, v_end = now - timedelta(days=history_route_days), now
        for m in self._mmsi_for_search:
            rows, cursor = [], v_start  # 每艘船独立切片, 单时间窗 ≤90 天
            while cursor < v_end:
                seg_end = min(cursor + timedelta(days=90), v_end)
                rows += self._get("/vessel2/ais/getVesselVoyage", {
                    "mmsi": m, "startTime": cursor.strftime(self._date_format),
                    "endTime": seg_end.strftime(self._date_format)})
                cursor = seg_end
            self._items["history_route"][m] = rows
        return self

    @_save_section("future_route")
    def get_future_route(self):
        """取船舶预测轨迹 /gis/route/plan/byvessel。

        目的港自动取该船 AIS 的 ``destcode``, 故应先调 :meth:`get_ais`;
        无 destcode 时接口无预测, 记为 []。
        """
        self._check_mmsi()
        for m in self._mmsi_for_search:
            dest = (self._items["ais"].get(m) or {}).get("destcode")
            params = {"vessel": m}
            if dest:
                params["pod"] = dest
            rec = self._get("/gis/route/plan/byvessel", params)
            self._items["future_route"][m] = (
                rec if isinstance(rec, dict) else [])
        return self

    @property
    def present(self):
        """
        调用Presentation，展示结果
        """
        return Present(api=self)



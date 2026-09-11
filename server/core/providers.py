# -*- coding: utf-8 -*-
"""行情数据源访问层(标准库 urllib 实现, 并发抓取, 多重试)。

数据服务地址全部来自 server/providers.json —— 代码内不出现任何静态/绝对地址。
默认链路: 新浪批量行情 + 新浪全量列表 + 腾讯日K; 若腾讯K线对个别标的失败则回退新浪K线。
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from .util import get_logger, load_json, PROVIDERS_PATH

log = get_logger("providers")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _prov(name: str) -> dict:
    cfg = load_json(PROVIDERS_PATH, {})
    return (cfg.get("providers") or {}).get(name) or {}


def http_get_text(url: str, headers: Optional[dict] = None, charset: str = "utf-8",
                  timeout: float = 12.0, retries: int = 3) -> str:
    """GET 并解码文本。失败重试, 最终失败抛异常。"""
    last: Exception = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                raw = resp.read()
                try:
                    return raw.decode(charset)
                except (UnicodeDecodeError, LookupError):
                    return raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"HTTP失败({url[:60]}...): {last}")


# ================= 新浪: 全市场股票列表 =================
def sina_fetch_universe_page(page: int, num: int = 100, timeout: float = 15.0) -> List[dict]:
    p = _prov("sina_universe")
    url = p.get("base", "") + "?" + urllib.parse.urlencode({
        "page": page, "num": num, "sort": "symbol", "asc": 1,
        "node": "hs_a", "symbol": "", "_s_r_a": "init"})
    txt = http_get_text(url, timeout=timeout, retries=3)
    txt = txt.strip()
    if not txt or txt.startswith("null") or txt.startswith("{"):
        return []
    if txt.startswith("["):
        return json.loads(txt)
    # 部分环境返回带前缀的 jsonp
    s = txt[txt.find("["): txt.rfind("]") + 1]
    return json.loads(s) if s else []


def sina_fetch_all_universe(max_items: int = 20000) -> List[dict]:
    """并发分页抓取沪深京A股全量(含代码/名称/市值/换手/成交量等)。"""
    page_size = 100
    first = sina_fetch_universe_page(1, page_size)
    if not first:
        return []
    total_pages = min(max_items // page_size + 1, 200)
    pages = list(range(2, total_pages + 1))

    def fetch(pg: int) -> List[dict]:
        try:
            return sina_fetch_universe_page(pg, page_size)
        except Exception as e:  # noqa: BLE001
            log.warning("universe page %s 失败: %s", pg, e)
            return []

    result: List[dict] = list(first)
    with ThreadPoolExecutor(max_workers=4) as ex:      # 并发适中, 降低被限流概率
        for chunk in ex.map(fetch, pages):
            result.extend(chunk)
            if len(chunk) < page_size:
                break
    return result


# ================= 东方财富: 列表接口(可选补充源: B股/兜底) =================
def eastmoney_fetch_list(fs: str, max_pages: int = 30, page_size: int = 200) -> List[dict]:
    """按 fs 条件分页抓取代码列表。返回 [{f12代码,f14名称,f2价格,f20总市值,f21流通市值}]。
    源地址与公共参数来自 providers.json; 不可达时抛异常由调用方跳过。"""
    p = _prov("eastmoney_universe")
    base = p.get("base", "")
    common = dict(p.get("params") or {})
    out: List[dict] = []
    for pn in range(1, max_pages + 1):
        q = {**common, "pn": pn, "pz": page_size, "fs": fs}
        url = base + "?" + urllib.parse.urlencode(q)
        txt = http_get_text(url, headers=p.get("headers") or {}, timeout=15, retries=2)
        data = json.loads(txt)
        diff = (data.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        out.extend(diff)
        if len(diff) < page_size:
            break
    return out


# ================= 新浪: 批量实时行情(全市场/指定集) =================
def sina_fetch_spot(codes: List[Tuple[str, str]]) -> Dict[str, dict]:
    """codes: [(code,'sh'/'sz'/'bj'), ...] -> {code: quote}。分块并发抓取 hq.sinajs.cn。"""
    p = _prov("sina_quote")
    base = p.get("base", "")
    headers = p.get("headers") or {}
    charset = p.get("charset", "gbk")
    if not codes:
        return {}
    symbols = [m + c for c, m in codes]
    chunk_size = 500
    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]

    def fetch(chunk: List[str]) -> Dict[str, dict]:
        url = base + ",".join(chunk)
        try:
            txt = http_get_text(url, headers=headers, charset=charset, timeout=15, retries=3)
        except Exception as e:  # noqa: BLE001
            log.warning("sina spot chunk 失败: %s", e)
            return {}
        out: Dict[str, dict] = {}
        for line in txt.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            var, _, body = line.partition("=")
            m = var.rsplit("_", 1)
            sym = m[-1] if len(m) == 2 else ""
            body = body.strip().strip('"').strip(";").strip()
            f = body.split(",")
            if len(f) < 32:
                continue
            name = f[0]
            open_ = _f(f[1]); prev = _f(f[2]); price = _f(f[3])
            high = _f(f[4]); low = _f(f[5])
            volume = _f(f[8]); amount = _f(f[9])
            dt = (f[30] + " " + f[31]) if len(f) > 31 else ""
            if price <= 0:
                price = prev  # 集合竞价前/停牌: 以昨收计
            pct = (price - prev) / prev * 100 if prev > 0 else 0.0
            chg = price - prev if prev > 0 else 0.0
            code = sym[2:] if len(sym) > 2 and sym[:2].isalpha() else sym  # 统一6位数字键
            out[code] = {"symbol": sym, "code": code, "name": name, "price": price,
                         "prev_close": prev, "open": open_, "high": high, "low": low,
                         "pct_chg": pct, "change": chg, "volume": volume, "amount": amount,
                         "ts": dt}
        return out

    merged: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for part in ex.map(fetch, chunks):
            merged.update(part)
    return merged


def _f(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


# ================= 腾讯: 批量实时行情(新浪403时的自动切换源) =================
def tencent_fetch_spot(codes: List[Tuple[str, str]]) -> Dict[str, dict]:
    """codes: [(code,'sh'/'sz'/'bj')] -> {code: quote}。
    腾讯行情 q=sh600519,sz000001,... (GBK), 单次约60只, 分块并发。
    字段(按 ~ 分隔): 3现价 4昨收 5今开 6成交量(手) 30时间 31涨跌 32涨跌% 33最高 34最低 37成交额(万)。"""
    p = _prov("tencent_spot")
    base = p.get("base", "")
    charset = p.get("charset", "gbk")
    batch = int(p.get("batch_size", 60))
    if not codes:
        return {}
    symbols = [m + c for c, m in codes]
    chunks = [symbols[i:i + batch] for i in range(0, len(symbols), batch)]

    def fetch(chunk: List[str]) -> Dict[str, dict]:
        url = base + ",".join(chunk)
        try:
            txt = http_get_text(url, charset=charset, timeout=15, retries=2)
        except Exception as e:  # noqa: BLE001
            log.warning("腾讯行情chunk失败: %s", e)
            return {}
        out: Dict[str, dict] = {}
        for line in txt.splitlines():
            line = line.strip()
            if '="' not in line:
                continue
            var, _, body = line.partition("=")
            sym = var.rsplit("_", 1)[-1].strip()
            if not sym or sym in ("pv_none_match",):
                continue
            f = body.strip().strip('"').strip(";").split("~")
            if len(f) < 40:
                continue
            code = sym[2:] if len(sym) > 2 and sym[:2].isalpha() else sym
            price = _f(f[3]); prev = _f(f[4]); open_ = _f(f[5])
            vol_hand = _f(f[6]); high = _f(f[33]); low = _f(f[34])
            t = str(f[30])
            ts = (f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"
                  if len(t) >= 14 and t.isdigit() else "")
            if price <= 0:
                price = prev                       # 停牌/集合竞价前
            pct = (price - prev) / prev * 100 if prev > 0 else 0.0
            chg = price - prev if prev > 0 else 0.0
            out[code] = {"symbol": sym, "code": code, "name": str(f[1]).strip(),
                         "price": price, "prev_close": prev, "open": open_,
                         "high": high, "low": low, "pct_chg": pct, "change": chg,
                         "volume": vol_hand * 100.0,          # 手 -> 股
                         "amount": _f(f[37]) * 10000.0,       # 万元 -> 元
                         "ts": ts, "src": "tencent"}
        return out

    merged: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(fetch, chunks):
            merged.update(part)
    return merged


# ================= 腾讯: 日K线(不复权 day / 前复权 qfqday) =================
def _tencent_kline_raw(prov_name: str, symbol: str, start: str, end: str, count: int = 800,
                       fq: str = "") -> Optional[List[List]]:
    p = _prov(prov_name)
    base = p.get("base", "")
    param = f"{symbol},day,{start},{end},{count},{fq}"
    url = base + "?" + urllib.parse.urlencode({"param": param})
    txt = http_get_text(url, timeout=15, retries=2)
    data = json.loads(txt)
    d = ((data.get("data") or {}).get(symbol) or {})
    rows = d.get("qfqday") if fq else d.get("day")
    return rows


def fetch_kline(symbol: str, start: str, end: str, count: int = 800,
                fq: str = "", is_index: bool = False) -> List[dict]:
    """返回 [{'date','open','high','low','close','vol_shares'}]。
    symbol 形如 sh600519 / sz000001 / bj920000。
    链路: 腾讯proxy主源 → 腾讯web备源 → 新浪money回退(均支持北交所)。"""
    for prov_name in ("tencent_kline", "tencent_kline_alt"):
        try:
            rows = _tencent_kline_raw(prov_name, symbol, start, end, count, fq)
            out = []
            for r in rows or []:
                if len(r) < 6:
                    continue
                try:
                    vol_hand = float(r[5])
                except Exception:
                    vol_hand = 0.0
                out.append({"date": r[0], "open": _f(r[1]), "close": _f(r[2]),
                            "high": _f(r[3]), "low": _f(r[4]),
                            "vol_shares": vol_hand * 100.0 if not is_index else vol_hand})
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            log.warning("腾讯K线(%s)失败 %s: %s", prov_name, symbol, e)
    return _sina_kline_fallback(symbol, count)


def _sina_kline_fallback(symbol: str, count: int) -> List[dict]:
    """新浪 money 域名日K(scale=240): JSON数组[{day,open,high,low,close,volume}],
    volume单位=股, 支持北交所(bj)。地址与请求头取自 providers.json。"""
    p = _prov("sina_kline")
    base = p.get("base", "")
    headers = p.get("headers") or {}
    n = min(max(int(count or 300), 60), 450)      # 取近段, 控制数据量
    url = base + "?" + urllib.parse.urlencode(
        {"symbol": symbol, "scale": 240, "ma": "no", "datalen": n})
    try:
        txt = http_get_text(url, headers=headers, timeout=15, retries=2)
        s = txt[txt.find("["): txt.rfind("]") + 1]
        if not s:
            return []
        arr = json.loads(s)
        out = []
        for r in arr:
            if not isinstance(r, dict):
                continue
            out.append({"date": str(r.get("day", ""))[:10], "open": _f(r.get("open")),
                        "close": _f(r.get("close")), "high": _f(r.get("high")),
                        "low": _f(r.get("low")),
                        "vol_shares": _f(r.get("volume"))})
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("新浪K线回退失败 %s: %s", symbol, e)
    return []

# -*- coding: utf-8 -*-
"""行情数据源访问层(标准库 urllib 实现, 并发抓取, 多重试)。

数据服务地址全部来自 server/providers.json —— 代码内不出现任何静态/绝对地址。
多源冗余 + 自动切换:
  行情: 新浪 → 腾讯 → 腾讯备用域名 → 东方财富
  日K : 腾讯proxy → 腾讯web → 东方财富 → 新浪money
  列表: 新浪 → 东方财富
切换策略: 记录每个源的成功/失败; 最近成功的源优先使用(避免每次都先撞已失效的主源),
          10分钟后自动回探主源, 以便主源恢复后自动切回。
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


# ---------------- 多源健康与切换 ----------------
_STICKY: Dict[str, tuple] = {}      # chain -> (provider_name, ts)
_HEALTH: Dict[str, dict] = {}       # provider -> {ok, fail, last_ok, last_err}
_STICKY_TTL = 600                   # 秒: 期间优先使用最近可用源, 之后回探主源


def _mark(name: str, ok: bool, err: str = "") -> None:
    h = _HEALTH.setdefault(name, {"ok": 0, "fail": 0, "last_ok": "", "last_err": ""})
    if ok:
        h["ok"] += 1
        h["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")
        h["last_err"] = ""
    else:
        h["fail"] += 1
        h["last_err"] = str(err)[:200]


def _sticky(chain: str, name: str) -> None:
    _STICKY[chain] = (name, time.time())


def chain_order(chain: str) -> List[str]:
    """返回该链的尝试顺序: 最近成功源优先, 超时后回到配置顺序(回探主源)。"""
    cfg = load_json(PROVIDERS_PATH, {})
    names = list((cfg.get("chains") or {}).get(chain) or [])
    st = _STICKY.get(chain)
    if st and (time.time() - st[1] < _STICKY_TTL) and st[0] in names:
        return [st[0]] + [n for n in names if n != st[0]]
    return names


def health() -> dict:
    return {"sticky": {k: v[0] for k, v in _STICKY.items()},
            "sticky_ttl_sec": _STICKY_TTL, "providers": _HEALTH}


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
                         "ts": dt, "src": "sina_quote"}
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


# ================= 腾讯: 批量实时行情(行情源2/3) =================
def tencent_fetch_spot(codes: List[Tuple[str, str]],
                       prov_name: str = "tencent_spot") -> Dict[str, dict]:
    """codes: [(code,'sh'/'sz'/'bj')] -> {code: quote}。
    腾讯行情 q=sh600519,sz000001,... (GBK), 单次约60只, 分块并发。
    字段(按 ~ 分隔): 3现价 4昨收 5今开 6成交量(手) 30时间 31涨跌 32涨跌% 33最高 34最低 37成交额(万)。"""
    p = _prov(prov_name)
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
            log.warning("腾讯行情(%s)chunk失败: %s", prov_name, e)
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
                         "ts": ts, "src": prov_name}
        return out

    merged: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(fetch, chunks):
            merged.update(part)
    return merged


# ================= 东方财富: 批量实时行情(行情源4) =================
def eastmoney_fetch_spot(codes: List[Tuple[str, str]]) -> Dict[str, dict]:
    """东方财富 ulist.np/get 批量行情(JSON): 沪深/北交所。
    字段: f12代码 f14名称 f2现价 f3涨跌% f4涨跌额 f5成交量(手) f6成交额(元)
          f15最高 f16最低 f17今开 f18昨收 f124行情时间戳(秒)。"""
    p = _prov("eastmoney_spot")
    base = p.get("base", "")
    common = dict(p.get("params") or {})
    mm = p.get("market_map") or {}
    batch = int(p.get("batch_size", 50))
    if not codes:
        return {}
    chunks = [codes[i:i + batch] for i in range(0, len(codes), batch)]

    def fetch(chunk: List[Tuple[str, str]]) -> Dict[str, dict]:
        secids = ",".join(f"{mm.get(m, '0')}.{c}" for c, m in chunk)
        prefix_by_code = {c: m for c, m in chunk}
        url = base + "?" + urllib.parse.urlencode({**common, "secids": secids})
        try:
            txt = http_get_text(url, timeout=15, retries=2)
            data = json.loads(txt)
        except Exception as e:  # noqa: BLE001
            log.warning("东方财富行情chunk失败: %s", e)
            return {}
        diff = (data.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        out: Dict[str, dict] = {}
        for it in diff:
            code = str(it.get("f12") or "").zfill(6)
            if not code.strip("0") or not it.get("f14"):
                continue
            price = _f(it.get("f2")); prev = _f(it.get("f18"))
            if price <= 0:
                price = prev
            if price <= 0:
                continue
            ts = ""
            f124 = it.get("f124")
            try:
                if f124 not in (None, "", "-"):
                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(f124)))
            except Exception:  # noqa: BLE001
                ts = ""
            sym = prefix_by_code.get(code, "") + code
            out[code] = {"symbol": sym, "code": code, "name": str(it.get("f14")).strip(),
                         "price": price, "prev_close": prev, "open": _f(it.get("f17")),
                         "high": _f(it.get("f15")), "low": _f(it.get("f16")),
                         "pct_chg": _f(it.get("f3")), "change": _f(it.get("f4")),
                         "volume": _f(it.get("f5")) * 100.0,     # 手 -> 股
                         "amount": _f(it.get("f6")), "ts": ts, "src": "eastmoney_spot"}
        return out

    merged: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(fetch, chunks):
            merged.update(part)
    return merged


# ================= 多源行情调度(自动切换) =================
_QUOTE_FETCHERS = {
    "sina_quote": lambda need: sina_fetch_spot(need),
    "tencent_spot": lambda need: tencent_fetch_spot(need, "tencent_spot"),
    "tencent_spot_alt": lambda need: tencent_fetch_spot(need, "tencent_spot_alt"),
    "eastmoney_spot": lambda need: eastmoney_fetch_spot(need),
}


def fetch_spot_chain(codes: List[Tuple[str, str]]) -> Dict[str, dict]:
    """按 chains.quote 顺序多源抓取行情, 缺失代码逐源补齐; 返回 {code: quote}。
    主源失效时自动切换(next 源), 并记住最近可用源优先, 10分钟后自动回探主源。"""
    if not codes:
        return {}
    result: Dict[str, dict] = {}
    used: List[str] = []
    order = chain_order("quote")
    for idx, name in enumerate(order):
        need = [(c, m) for c, m in codes if c not in result]
        if not need:
            break
        fetch = _QUOTE_FETCHERS.get(name)
        if not fetch:
            continue
        try:
            # 非首选源先做1只探测, 避免失效源拖慢整轮抓取
            if idx > 0:
                probe = fetch(need[:1])
                if not probe:
                    _mark(name, False, "preflight empty")
                    continue
                part = dict(probe)
                rest = [(c, m) for c, m in need[1:]]
                if rest:
                    part.update(fetch(rest))
            else:
                part = fetch(need)
            if part:
                result.update(part)
                used.append(f"{name}:{len(part)}")
                _mark(name, True)
            else:
                _mark(name, False, "empty")
        except Exception as e:  # noqa: BLE001
            _mark(name, False, str(e))
            log.warning("行情源 %s 失败: %s", name, e)
        if len(result) >= len(codes):
            _sticky("quote", name)
            break
    if used:
        log.info("行情抓取: 需求%s 成功%s [%s]", len(codes), len(result), " ".join(used))
    if not result:
        log.error("全部行情源均失败(请检查 /api/diag 中的 provider 连通性)")
    return result


# ================= 日K线: 腾讯(源1/2) =================
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


def _tencent_kline(prov_name: str, symbol: str, start: str, end: str,
                   count: int, fq: str, is_index: bool) -> List[dict]:
    rows = _tencent_kline_raw(prov_name, symbol, start, end, count, fq)
    out = []
    for r in rows or []:
        if len(r) < 6:
            continue
        vol_hand = _f(r[5])
        out.append({"date": r[0], "open": _f(r[1]), "close": _f(r[2]),
                    "high": _f(r[3]), "low": _f(r[4]),
                    "vol_shares": vol_hand * 100.0 if not is_index else vol_hand})
    return out


# ================= 日K线: 东方财富(源3) =================
def _eastmoney_kline(symbol: str, start: str, end: str, count: int,
                     fq: str, is_index: bool) -> List[dict]:
    """东方财富日K(klt=101, fqt=0不复权): klines 行格式
    '日期,开,收,高,低,成交量(手),成交额,振幅,涨跌幅,涨跌额,换手率'。"""
    p = _prov("eastmoney_kline")
    base = p.get("base", "")
    common = dict(p.get("params") or {})
    mm = p.get("market_map") or {}
    prefix = symbol[:2]
    code = symbol[2:]
    secid = f"{mm.get(prefix, '0')}.{code}"
    q = {**common, "secid": secid, "beg": start.replace("-", ""),
         "end": end.replace("-", ""), "lmt": max(60, min(int(count or 800), 1000))}
    if fq:
        q["fqt"] = 1
    url = base + "?" + urllib.parse.urlencode(q)
    txt = http_get_text(url, timeout=15, retries=2)
    data = json.loads(txt)
    klines = (data.get("data") or {}).get("klines") or []
    out = []
    for k in klines:
        parts = str(k).split(",")
        if len(parts) < 6:
            continue
        vol_hand = _f(parts[5])
        out.append({"date": parts[0], "open": _f(parts[1]), "close": _f(parts[2]),
                    "high": _f(parts[3]), "low": _f(parts[4]),
                    "vol_shares": vol_hand * 100.0 if not is_index else vol_hand})
    return out


def fetch_kline(symbol: str, start: str, end: str, count: int = 800,
                fq: str = "", is_index: bool = False) -> List[dict]:
    """返回 [{'date','open','high','low','close','vol_shares'}]。
    symbol 形如 sh600519 / sz000001 / bj920000。
    多源链: 腾讯proxy → 腾讯web → 东方财富 → 新浪money(均支持北交所/B股)。"""
    for name in chain_order("kline"):
        try:
            if name in ("tencent_kline", "tencent_kline_alt"):
                out = _tencent_kline(name, symbol, start, end, count, fq, is_index)
            elif name == "eastmoney_kline":
                out = _eastmoney_kline(symbol, start, end, count, fq, is_index)
            elif name == "sina_kline":
                out = _sina_kline_fallback(symbol, count)
            else:
                continue
            if out:
                _mark(name, True)
                _sticky("kline", name)
                return out
            _mark(name, False, "empty")
        except Exception as e:  # noqa: BLE001
            _mark(name, False, str(e))
            log.warning("日K源 %s 失败 %s: %s", name, symbol, e)
    return []


def _sina_kline_fallback(symbol: str, count: int) -> List[dict]:
    """新浪 money 域名日K(scale=240): JSON数组[{day,open,high,low,close,volume}],
    volume单位=股, 支持北交所(bj)。地址与请求头取自 providers.json。"""
    p = _prov("sina_kline")
    base = p.get("base", "")
    headers = p.get("headers") or {}
    n = min(max(int(count or 300), 60), 450)      # 取近段, 控制数据量
    url = base + "?" + urllib.parse.urlencode(
        {"symbol": symbol, "scale": 240, "ma": "no", "datalen": n})
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
    return out

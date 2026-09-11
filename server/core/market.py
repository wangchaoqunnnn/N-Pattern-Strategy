# -*- coding: utf-8 -*-
"""市场数据管理: 股票列表(universe)、板块分类、K线缓存(SQLite + 内存)、实时行情、指数环境。

- 全市场扫描范围: 沪主板/深主板/科创板/创业板/北交所/沪B/深B(全部交易所), 仅剔除ST/退市与无法归类代码。
- 股票代码: 6位数字; 日K持久化于 SQLite(daily_bars), 启动加载近段到内存用于盘中高频筛选。
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from . import db
from . import providers as P
from .util import (clean_code, get_logger, is_st_name, num, now_cn, today_str)

log = get_logger("market")

INDEX_SYMBOLS = ["sh000300", "sh000001"]  # 沪深300 / 上证指数(默认源providers.json可改)

# 自动筛选范围: 覆盖全部交易所/板块
SCREEN_BOARDS = ["沪主板", "深主板", "科创板", "创业板", "北交所", "沪B", "深B"]


def classify(code: str, prefix: str = "") -> str:
    """按代码段归类板块。"""
    c = clean_code(code)
    if prefix == "bj" or c.startswith(("43", "83", "87", "88", "92", "4", "8")):
        return "北交所"
    if c.startswith("60"):
        return "沪主板"
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith("900"):
        return "沪B"
    if c.startswith(("000", "001", "002", "003")):
        return "深主板"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith("200"):
        return "深B"
    if prefix == "sh":
        return "沪主板"
    if prefix == "sz":
        return "深主板"
    return "其他"


def symbol_of(code: str) -> str:
    """代码 -> 带市场前缀的行情符号(sh/sz/bj)。
    优先用股票列表里的真实前缀(内存缓存, 避免逐股查库), 否则按代码段启发判断。"""
    c = clean_code(code)
    known = market.symbols.get(c) if market.symbols else ""
    if known:
        return known
    if c.startswith(("43", "83", "87", "88", "92", "4", "8")):   # 北交所: 430/83x/87x/88x/920等
        return "bj" + c
    if c.startswith(("6", "9", "5")):                            # 沪市: 6股票/900B股/5基金
        return "sh" + c
    return "sz" + c


def market_prefix(code: str) -> str:
    return symbol_of(code)[:2]


# ------------------------------------------------------------------ universe
def _row_from_sina(it: dict, now: str):
    """新浪列表项 -> universe 行。"""
    sym = str(it.get("symbol", ""))
    prefix = sym[:2]
    code = clean_code(sym)
    name = str(it.get("name", "")).strip()
    if not code or not name:
        return None
    board = classify(code, prefix)
    price_eff = num(it.get("trade")) or num(it.get("settlement")) or 0.0
    nmc = num(it.get("nmc"))                      # 万元
    float_shares = (nmc * 10000.0 / price_eff) if (price_eff > 0 and nmc > 0) else 0.0
    return (code, sym or symbol_of(code), name, board,
            float_shares if float_shares > 0 else 0.0, now)


def _row_from_eastmoney(it: dict, now: str):
    """东方财富列表项 -> universe 行(f12代码,f14名称,f2价格,f21流通市值[元])。"""
    code = clean_code(it.get("f12"))
    name = str(it.get("f14") or "").strip()
    if not code or not name:
        return None
    price = num(it.get("f2"))
    nmc = num(it.get("f21"))                      # 流通市值(元)
    float_shares = (nmc / price) if (price > 0 and nmc > 0) else 0.0
    board = classify(code)
    return (code, symbol_of(code), name, board,
            float_shares if float_shares > 0 else 0.0, now)


def _probe_b_shares(now: str) -> List[tuple]:
    """B股(沪B 900901-900999 / 深B 200002-200999)代码段探测。
    新浪行情接口支持B股实时报价, 用它枚举出真实存在的B股, 无需额外列表源。"""
    sh_codes = [f"9009{i:02d}" for i in range(1, 100)]
    sz_codes = [f"200{i:03d}" for i in range(2, 1000)]
    syms = [(c, "sh") for c in sh_codes] + [(c, "sz") for c in sz_codes]
    try:
        quotes = P.fetch_spot_chain(syms)      # 多源行情(新浪/腾讯/东财), 任一只可用即可
    except Exception as e:  # noqa: BLE001
        log.warning("B股探测失败(已跳过): %s", e)
        return []
    out = []
    for code, q in quotes.items():
        name = str(q.get("name") or "").strip()
        prev = num(q.get("prev_close"))
        if not name or prev <= 0:
            continue                                   # 空代码/无行情 → 非上市B股
        board = classify(code)
        if board not in ("沪B", "深B"):
            continue
        out.append((code, symbol_of(code), name, board, 0.0, now))
    if out:
        log.info("B股探测完成: %s 只(沪B/深B)", len(out))
    return out


def sync_universe(max_items: int = 30000) -> Dict:
    """全市场股票列表同步(覆盖全部交易所/板块, 无遗漏)。

    链路:
      1) 新浪 沪深京A股列表(主源) —— 沪主板/深主板/科创板/创业板/北交所;
      2) 东方财富列表(可选) —— B股补充; 不可达时用3)兜底;
      3) B股代码段探测(新浪行情) —— 不依赖额外列表源即可纳入沪B/深B;
      4) 主源失败时东方财富A股列表兜底; 全部失败则保留本地缓存继续运行。
    """
    now = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    rows: List[tuple] = []
    # A股列表: 按 chains.universe 顺序多源尝试(新浪 → 东方财富)
    sina_rows: List[tuple] = []
    for name in P.chain_order("universe"):
        try:
            if name == "sina_universe":
                items = P.sina_fetch_all_universe(max_items)
                got = [r for r in (_row_from_sina(it, now) for it in items) if r]
            elif name == "eastmoney_universe":
                fs_a = P._prov("eastmoney_universe").get("fs_a_share") or ""
                items = P.eastmoney_fetch_list(fs_a, max_pages=40)
                got = [r for r in (_row_from_eastmoney(it, now) for it in items) if r]
            else:
                continue
            if got:
                P._mark(name, True)
                P._sticky("universe", name)
                sina_rows = got            # 记录首个成功的A股列表源
                log.info("A股列表来源: %s (%s 只)", name, len(got))
                break
            P._mark(name, False, "empty")
        except Exception as e:  # noqa: BLE001
            P._mark(name, False, str(e))
            log.warning("列表源 %s 失败: %s", name, e)
    rows.extend(sina_rows)
    # B股补充: 先试东方财富(含流通市值), 不可用则代码段探测
    b_added = 0
    try:
        p = P._prov("eastmoney_universe")
        fs_b = p.get("fs_b_share") or ""
        if fs_b:
            for it in P.eastmoney_fetch_list(fs_b, max_pages=3):
                r = _row_from_eastmoney(it, now)
                if r:
                    rows.append(r)
                    b_added += 1
            if b_added:
                log.info("B股补充完成(东方财富): %s 只", b_added)
    except Exception as e:  # noqa: BLE001
        log.info("东方财富B股源不可用, 改用代码段探测: %s", e)
    if b_added == 0:
        probed = _probe_b_shares(now)
        rows.extend(probed)
        b_added = len(probed)
    # 去重(后写覆盖)
    dedup = {r[0]: r for r in rows}
    final = list(dedup.values())
    if not final:
        existing = db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0
        if existing > 0:
            log.warning("股票列表源暂不可用(可能被限流), 保留现有 %s 只列表继续运行", existing)
            return {"count": existing, "cached": True}
        log.error("股票列表抓取失败且本地无缓存, 请检查数据源连通性(/api/diag)")
        return {"count": 0, "cached": False}
    db.executemany(
        "INSERT INTO universe(code,symbol,name,board,float_shares,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
        "symbol=excluded.symbol,name=excluded.name,board=excluded.board,"
        "float_shares=excluded.float_shares,updated_at=excluded.updated_at", final)
    total = db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0
    log.info("universe 同步完成: 本次更新 %s 条, 列表合计 %s 只(A股全部交易所 + B股)",
             len(final), total)
    return {"count": total, "updated": len(final)}


def universe_list() -> List[dict]:
    return db.rows("SELECT * FROM universe ORDER BY code")


def universe_codes() -> List[str]:
    return [r["code"] for r in db.rows("SELECT code FROM universe ORDER BY code")]


def find_stocks(kw: str, limit: int = 50) -> List[dict]:
    kw = kw.strip()
    if not kw:
        return []
    q = f"%{kw}%"
    return db.rows("SELECT code,name,board,symbol FROM universe "
                   "WHERE code LIKE ? OR name LIKE ? ORDER BY code LIMIT ?",
                   (q, q, limit))


# ------------------------------------------------------------------ K线缓存
def load_history_codes(start_calendar: str = "") -> None:
    """占位: 见 MarketState.load_from_db。"""
    raise NotImplementedError


class MarketState:
    """单例: 内存行情。 bars: {code: rows(list[dict] 升序)}; index_bars: {symbol: rows}。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bars: Dict[str, List[dict]] = {}
        self.index_bars: Dict[str, List[dict]] = {}
        self.quotes: Dict[str, dict] = {}
        self.quotes_ts: str = ""
        self.universe_name: Dict[str, str] = {}
        self.symbols: Dict[str, str] = {}      # code -> sh/sz/bj 前缀(来自股票列表)
        self.loaded = False

    # ---------- 内存加载 ----------
    def load_from_db(self, keep_bars: int = 320) -> None:
        rows = db.rows("SELECT code,date,open,high,low,close,vol_shares,pct_chg,turnover "
                       "FROM daily_bars ORDER BY code,date")
        bars: Dict[str, List[dict]] = {}
        idx: Dict[str, List[dict]] = {}
        for r in rows:
            code = r["code"]
            if len(code) > 6 or not code.isdigit():   # 指数等符号类数据
                idx.setdefault(code, []).append(r)
                continue
            arr = bars.setdefault(code, [])
            if arr and arr[-1]["date"] >= r["date"]:
                continue
            arr.append(r)
        for code in list(bars.keys()):                 # 仅保留最近若干根, 控制内存
            arr = bars[code]
            if len(arr) > keep_bars:
                bars[code] = arr[-keep_bars:]
        with self.lock:
            self.bars = bars
            self.index_bars = idx
            uni = db.rows("SELECT code,name,symbol FROM universe")
            self.universe_name = {r["code"]: r["name"] for r in uni}
            self.symbols = {r["code"]: (r.get("symbol") or symbol_of(r["code"])) for r in uni}
        self.loaded = True
        log.info("内存K线加载完成: 股票 %s 只, 符号类(指数) %s 组, 股票列表 %s 只(含北交所/B股)",
                 len(bars), {k: len(v) for k, v in idx.items()}, len(self.symbols))

    # ---------- 序列访问 ----------
    def series(self, code: str, max_len: int = 260) -> Optional[dict]:
        """返回 {dates, open, high, low, close, vol, pct} numpy风格list。"""
        arr = self.bars.get(code)
        if not arr:
            return None
        if len(arr) > max_len:
            arr = arr[-max_len:]
        return {
            "dates": [r["date"] for r in arr],
            "open": [r["open"] for r in arr],
            "high": [r["high"] for r in arr],
            "low": [r["low"] for r in arr],
            "close": [r["close"] for r in arr],
            "vol": [r["vol_shares"] for r in arr],
            "pct": [r["pct_chg"] or 0.0 for r in arr],
        }

    def last_bar_date(self, code: str) -> Optional[str]:
        arr = self.bars.get(code)
        return arr[-1]["date"] if arr else None

    def upsert_bars(self, code: str, rows: List[dict]) -> None:
        """写库并更新内存。rows: {date,open,high,low,close,vol_shares,pct_chg,turnover} 升序。"""
        if not rows:
            return
        insert = []
        for r in rows:
            insert.append((code, r["date"], r["open"], r["high"], r["low"], r["close"],
                           r["vol_shares"], r.get("pct_chg") or 0.0, r.get("turnover")))
        db.executemany(
            "INSERT INTO daily_bars(code,date,open,high,low,close,vol_shares,pct_chg,turnover) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET "
            "open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,"
            "vol_shares=excluded.vol_shares,pct_chg=excluded.pct_chg,turnover=excluded.turnover", insert)
        with self.lock:
            cur = self.bars.get(code) or []
            have = {r["date"]: i for i, r in enumerate(cur)}
            for r in rows:
                if r["date"] in have:
                    cur[have[r["date"]]] = r
                else:
                    cur.append(r)
            cur.sort(key=lambda x: x["date"])
            self.bars[code] = cur[-320:]

    # ---------- 实时行情 ----------
    def refresh_spot(self, codes: Optional[List[str]] = None) -> Dict[str, dict]:
        """抓取实时行情。codes=None 时取全市场(universe, 含北交所/B股)。

        行情源自动切换: 新浪批量行情 → 若不可用/部分失败(常见: 云服务器IP被新浪403),
        自动用腾讯行情补齐缺失代码(沪深/北交所/B股通用)。
        采用"合并更新": 抓取失败的代码保留上一次快照, 避免瞬时失败清空内存行情。"""
        if codes is None:
            syms = [(r["code"], (r.get("symbol") or symbol_of(r["code"]))[:2])
                    for r in universe_list()]
        else:
            syms = [(c, self.symbols.get(c, symbol_of(c))[:2]) for c in codes]
        spot: Dict[str, dict] = {}
        try:
            spot = P.fetch_spot_chain(syms)
        except Exception as e:  # noqa: BLE001
            log.warning("行情多源抓取异常: %s", e)
        now = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        # 清理历史遗留的带前缀键(兼容旧版存储)
        if codes is None:
            db.execute("DELETE FROM watch_quotes WHERE length(code)>6")
        with self.lock:
            if spot:
                self.quotes.update(spot)
                self.quotes_ts = now
            elif not self.quotes:
                self.quotes = {}
        if spot:
            snap = [(c, q.get("name", ""), q.get("price", 0), q.get("pct_chg", 0),
                     q.get("change", 0), q.get("open", 0), q.get("high", 0),
                     q.get("low", 0), q.get("prev_close", 0), q.get("volume", 0),
                     q.get("amount", 0), now)
                    for c, q in spot.items()]
            db.executemany(
                "INSERT INTO watch_quotes(code,name,price,pct_chg,change,open,high,low,prev_close,"
                "volume,amount,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
                "name=excluded.name,price=excluded.price,pct_chg=excluded.pct_chg,change=excluded.change,"
                "open=excluded.open,high=excluded.high,low=excluded.low,prev_close=excluded.prev_close,"
                "volume=excluded.volume,amount=excluded.amount,ts=excluded.ts", snap)
        return spot

    # ---------- 当日实时K线(盘中合成) ----------
    def live_bar(self, code: str) -> Optional[dict]:
        """基于实时行情合成"今日"日K(仅当行情日期=今天且价格有效)。"""
        q = self.quotes.get(code)
        if not q:
            return None
        today = today_str()
        ts = str(q.get("ts", ""))[:10]
        if ts != today:
            return None
        price = num(q.get("price"))
        if price <= 0:
            return None
        return {"date": today, "open": num(q.get("open")), "high": num(q.get("high")) or price,
                "low": num(q.get("low")) or price, "close": price,
                "vol_shares": num(q.get("volume")), "pct_chg": num(q.get("pct_chg")),
                "turnover": None}

    # ---------- 指数 ----------
    def refresh_index(self, fq: str = "") -> None:
        """抓取指数日K(默认沪深300/上证), 写入daily_bars(symbol键)并更新内存。"""
        today = now_cn().strftime("%Y-%m-%d")
        start = (now_cn().date().replace(year=now_cn().date().year - 2)).strftime("%Y-%m-%d")
        for sym in INDEX_SYMBOLS:
            try:
                rows = P.fetch_kline(sym, start, today, 900, fq=fq, is_index=True)
                if not rows:
                    continue
                insert = [(sym, r["date"], r["open"], r["high"], r["low"], r["close"],
                           r["vol_shares"], 0.0, None) for r in rows]
                db.executemany(
                    "INSERT INTO daily_bars(code,date,open,high,low,close,vol_shares,pct_chg,turnover) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET "
                    "open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close",
                    insert)
                with self.lock:
                    self.index_bars[sym] = [
                        {"code": sym, **r, "pct_chg": 0.0} for r in rows]
                log.info("指数刷新完成 %s (%s 根)", sym, len(rows))
            except Exception as e:  # noqa: BLE001
                log.warning("指数刷新失败 %s: %s", sym, e)

    def index_closes(self, symbol: str = "sh000300") -> List[dict]:
        arr = self.index_bars.get(symbol) or []
        return [{"date": r["date"], "close": r["close"]} for r in arr]

    def trading_dates(self) -> List[str]:
        """从指数K线推导交易日列表(含历史)。"""
        seen: List[str] = []
        for sym in INDEX_SYMBOLS:
            for r in self.index_bars.get(sym) or []:
                seen.append(r["date"])
        seen = sorted(set(seen))
        return seen

    def today_in_market_quote(self) -> bool:
        """实时行情时间戳是否等于今天(说明今天开市)。"""
        ts = str(self.quotes_ts or "")[:10]
        return ts == today_str()

    def is_trading_today(self) -> bool:
        """今天是交易日?: 指数K线最新已含今天(收盘后) 或 实时行情时间戳为今天。"""
        today = today_str()
        idx_last = ""
        for sym in INDEX_SYMBOLS:
            arr = self.index_bars.get(sym) or []
            if arr:
                idx_last = arr[-1]["date"]
                break
        return idx_last == today or self.today_in_market_quote()


market = MarketState()


# ------------------------------------------------------------------ 历史同步
def sync_history(codes: List[str], start_date: str, end_date: str, fq: str = "",
                 workers: int = 8, progress_key: str = "sync_progress") -> Dict:
    """并发同步日K(增量: 跳过已最新代码)。返回统计。"""
    total = len(codes)
    done = 0
    errors: List[str] = []
    last_rows: Dict[str, str] = {}

    def last_cache(code: str) -> Optional[str]:
        r = db.row("SELECT MAX(date) d FROM daily_bars WHERE code=?", (code,))
        return r["d"] if r and r["d"] else None

    def work(code: str) -> None:
        nonlocal done
        try:
            lb = last_cache(code)
            if lb and lb >= end_date:
                return
            rows = P.fetch_kline(symbol_of(code), start_date, end_date, 900, fq=fq)
            if rows:
                if lb:
                    rows = [r for r in rows if r["date"] > lb]
                rows = _finalize_rows(code, rows)
                if rows:
                    market.upsert_bars(code, rows)
                    last_rows[code] = rows[-1]["date"]
            else:
                errors.append(code)
        except Exception as e:  # noqa: BLE001
            log.debug("history sync %s 失败: %s", code, e)
            errors.append(code)
        finally:
            done += 1
            if done % 200 == 0 or done == total:
                db.meta_set(progress_key, {"total": total, "done": done,
                                           "updated": now_cn().strftime("%Y-%m-%d %H:%M:%S")})

    with P.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, codes))
    res = {"total": total, "ok": total - len(errors), "errors": len(errors),
           "done_at": now_cn().strftime("%Y-%m-%d %H:%M:%S")}
    db.meta_set(progress_key, res)
    log.info("历史同步完成: %s/%s, 错误 %s", res["ok"], total, res["errors"])
    return res


def _finalize_rows(code: str, rows: List[dict]) -> List[dict]:
    """补算 pct_chg(相对前收) 与 turnover(基于流通股本估计)。"""
    out = []
    prev_close = None
    fshares = db.scalar("SELECT float_shares FROM universe WHERE code=?", (code,)) or 0.0
    for r in rows:
        close = num(r["close"])
        pct = ((close - prev_close) / prev_close * 100) if prev_close and prev_close > 0 else 0.0
        tu = None
        if fshares and fshares > 0 and r.get("vol_shares"):
            tu = num(r["vol_shares"]) / fshares * 100.0
        out.append({"date": r["date"], "open": num(r["open"]), "high": num(r["high"]),
                    "low": num(r["low"]), "close": close,
                    "vol_shares": num(r["vol_shares"]), "pct_chg": round(pct, 4),
                    "turnover": round(tu, 3) if tu is not None else None})
        prev_close = close
    return out


def auto_screen_candidates() -> List[dict]:
    """返回参与自动筛选的股票(code,name,board): 沪主板/深主板/科创板/创业板/北交所/沪B/深B,
    仅剔除 ST/退市 与无法归类的代码 —— 覆盖所有交易所, 不做板块遗漏。"""
    uni = universe_list()
    out = []
    for r in uni:
        name = r.get("name") or ""
        board = r.get("board") or classify(r["code"])
        if is_st_name(name):
            continue
        if board not in SCREEN_BOARDS:
            continue
        out.append({"code": r["code"], "name": name, "board": board,
                    "symbol": r.get("symbol")})
    return out

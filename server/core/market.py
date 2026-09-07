# -*- coding: utf-8 -*-
"""市场数据管理: 股票列表(universe)、板块分类、K线缓存(SQLite + 内存)、实时行情、指数环境。

- 全市场扫描范围: 沪深主板/科创板/创业板(可选北交所), 默认剔除ST/退市与B股。
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

# 板块名 -> 是否纳入自动筛选(默认含沪深主板/科创/创业)
SCREEN_BOARDS = ["沪主板", "深主板", "科创板", "创业板"]


def classify(code: str, prefix: str = "") -> str:
    """按代码段归类板块。"""
    c = clean_code(code)
    if prefix == "bj" or c.startswith(("4", "8", "92")):
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
    c = clean_code(code)
    if c.startswith(("6", "9", "5")):      # 沪: 6股票 9B股 5基金等
        return "sh" + c
    if c.startswith(("4", "8", "92")):     # 北交所
        return "bj" + c
    return "sz" + c


def market_prefix(code: str) -> str:
    return symbol_of(code)[:2]


# ------------------------------------------------------------------ universe
def sync_universe(max_items: int = 30000) -> Dict:
    """全市场列表同步(新浪 getHQNodeData), 估算流通股本, 写入 universe 表。"""
    items = P.sina_fetch_all_universe(max_items)
    now = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for it in items:
        sym = str(it.get("symbol", ""))
        prefix = sym[:2]
        code = clean_code(sym)
        name = str(it.get("name", "")).strip()
        if not code or not name:
            continue
        board = classify(code, prefix)
        price_eff = num(it.get("trade")) or num(it.get("settlement")) or 0.0
        nmc = num(it.get("nmc"))                      # 万元
        float_shares = 0.0
        if price_eff > 0 and nmc > 0:
            float_shares = nmc * 10000.0 / price_eff   # 元 / 元每股 -> 股
        rows.append((code, sym, name, board, float_shares if float_shares > 0 else 0.0, now))
    if rows:
        db.executemany(
            "INSERT INTO universe(code,symbol,name,board,float_shares,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
            "symbol=excluded.symbol,name=excluded.name,board=excluded.board,"
            "float_shares=excluded.float_shares,updated_at=excluded.updated_at", rows)
    log.info("universe 同步完成: %s 只", len(rows))
    return {"count": len(rows)}


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
            self.universe_name = {r["code"]: r["name"]
                                  for r in db.rows("SELECT code,name FROM universe")}
        self.loaded = True
        log.info("内存K线加载完成: 股票 %s 只, 符号类(指数) %s 组",
                 len(bars), {k: len(v) for k, v in idx.items()})

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
        """抓取实时行情(新浪批量). codes=None 时取全市场(universe)。
        采用"合并更新": 抓取失败的代码保留上一次快照, 避免瞬时失败清空内存行情。"""
        if codes is None:
            uni = universe_list()
            codes = [r["code"] for r in uni]
        syms = [(c, market_prefix(c)) for c in codes]
        spot = P.sina_fetch_spot(syms)
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
    """返回参与自动筛选的股票(code,name,board): 主板/科创/创业 且非ST/退。"""
    uni = universe_list()
    out = []
    for r in uni:
        name = r.get("name") or ""
        board = r.get("board") or classify(r["code"])
        if is_st_name(name):
            continue
        if board in ("沪B", "深B", "其他"):
            continue
        if board not in SCREEN_BOARDS:
            continue
        out.append({"code": r["code"], "name": name, "board": board,
                    "symbol": r.get("symbol")})
    return out

# -*- coding: utf-8 -*-
"""服务编排层: 单例 Service。

职责:
- 启动引导: 首次运行自动同步股票列表/指数/历史K线(后台, 带进度);
- 盘中(交易日 9:30-11:30 / 13:00-15:00, 北京时间): 高频刷新行情→卖出检查→扫描全市场→入池/出池→按信号与风控买入;
- 收盘后(15:00+): 收盘扫尾买卖→每日复盘→规则5自优化检查→月末月度复盘与优化→增量历史同步;
- 每日维护: 股票列表/流通股本刷新、次日盘前数据就绪检查。
所有交易操作严格使用当前执行版本的交易系统参数。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional

from .core import db
from .core import market as mk
from .core.util import (ensure_config, get_logger, hhmm_now, now_cn, now_str,
                        today_str, trading_clock_state)
from .engine import optimizer, pool as poolmod, review as reviewmod
from .engine import scan as scanmod
from .engine import stats as statsmod
from .engine import strategy as strat
from .engine import trader as trader

log = get_logger("service")


class Service:
    def __init__(self) -> None:
        self.cfg = ensure_config()
        self.engine_cfg = self.cfg.get("engine") or {}
        self.loaded_universe = False
        self.bootstrap_done = False
        self.sync_running = False
        self._lock = threading.Lock()
        self._uni_lock = threading.Lock()
        self.last_uni_try = 0.0
        self.last_fast_ts = ""
        self.last_scan_ts = ""
        self.last_scan_count = 0
        self.last_scan_signals: Dict[str, dict] = {}
        self.last_sell_ts = ""
        self.close_done_today = ""
        self.env_snapshot = {"mode": "full", "reason": "尚未评估"}
        self._stop = False

    # ---------------- 股票列表自愈 ----------------
    def ensure_universe(self, force: bool = False) -> bool:
        """确保全市场股票列表存在(为空时自动重试抓取)。返回是否就绪。"""
        if self.loaded_universe:
            return True
        if (db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0) > 0:
            self.loaded_universe = True
            return True
        with self._uni_lock:
            now = time.time()
            if not force and now - self.last_uni_try < 25:
                return False
            self.last_uni_try = now
        try:
            n = mk.sync_universe()
            if (n.get("count") or 0) > 0:
                self.loaded_universe = True
                db.log_event("INFO", "股票列表就绪")
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("股票列表抓取失败(将自动重试): %s", e)
            db.log_event("WARN", f"股票列表抓取失败: {e}")
        return False

    # ---------------- 工具 ----------------
    def eff_cfg(self) -> dict:
        """运行时有效配置(引擎开关可被用户在线切换, 存meta)。"""
        eng = dict(self.engine_cfg)
        v = db.meta_get("engine_enabled")
        eng["enabled"] = (v.lower() == "true") if v is not None else bool(eng.get("enabled", True))
        out = dict(self.cfg)
        out["engine"] = eng
        return out

    def set_engine_enabled(self, on: bool) -> None:
        db.meta_set("engine_enabled", "true" if on else "false")
        db.log_event("INFO", f"交易引擎手动{'开启' if on else '暂停'}")

    def engine_on(self) -> bool:
        return bool(self.eff_cfg()["engine"].get("enabled", True))

    # ---------------- 启动引导 ----------------
    def startup(self) -> None:
        db.init_db()
        strat.ensure_initial_version(self.cfg)
        self._bootstrap_thread()

    def _bootstrap_thread(self) -> None:
        threading.Thread(target=self._bootstrap, daemon=True, name="bootstrap").start()

    def _bootstrap(self) -> None:
        try:
            if db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0 == 0:
                try:
                    mk.sync_universe()
                except Exception as e:  # noqa: BLE001
                    log.error("股票列表初始同步失败: %s", e)
            self.loaded_universe = db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0 > 0
            try:
                mk.market.refresh_index()
            except Exception as e:  # noqa: BLE001
                log.warning("指数初始同步失败: %s", e)
            # 历史K线: 若完全无数据或过期则后台全量/增量同步
            stale = self._need_history_sync()
            if stale:
                self._start_history_sync(block=True)
            mk.market.load_from_db()
            self.bootstrap_done = True
            db.log_event("INFO", "系统启动引导完成: 数据就绪, 等待交易时段")
        except Exception as e:  # noqa: BLE001
            log.exception("启动引导异常: %s", e)

    def _need_history_sync(self) -> bool:
        n = db.scalar("SELECT COUNT(*) FROM daily_bars WHERE code NOT LIKE 's%'", (), 0) or 0
        if not n:
            return True
        latest = db.scalar("SELECT MAX(date) FROM daily_bars WHERE date LIKE '20%'", (), None)
        idx = mk.market.index_bars.get("sh000300") or []
        last_td = idx[-1]["date"] if idx else ""
        # 若最近一个交易日的指数已存在而个股最新 < 该日期 → 过期
        return bool(latest and last_td and latest < last_td)

    def _start_history_sync(self, block: bool = False) -> None:
        if self.sync_running:
            return
        self.sync_running = True

        def job() -> None:
            try:
                uni = mk.auto_screen_candidates()
                codes = [u["code"] for u in uni]
                days = int(self.cfg.get("history_calendar_days", 320))
                start = (now_cn().date().replace(
                    year=now_cn().date().year)).toordinal()  # noqa
                from datetime import timedelta
                start_d = (now_cn().date() - timedelta(days=days)).strftime("%Y-%m-%d")
                end_d = now_cn().strftime("%Y-%m-%d")
                db.meta_set("sync_progress", {"total": len(codes), "done": 0,
                                              "updated": now_str(), "running": True})
                mk.sync_history(codes, start_d, end_d, fq="",
                                workers=int(self.cfg.get("sync_workers", 10)),
                                progress_key="sync_progress")
                mk.market.load_from_db()
                db.meta_set("last_history_sync", now_str())
                db.log_event("INFO", "历史K线增量同步完成")
            except Exception as e:  # noqa: BLE001
                log.exception("历史同步任务异常: %s", e)
            finally:
                self.sync_running = False

        t = threading.Thread(target=job, daemon=True, name="history-sync")
        t.start()
        if block:
            t.join(timeout=60 * 60 * 3)

    # ---------------- 扫描 ----------------
    def watch_codes(self) -> List[str]:
        codes = {p["code"] for p in trader.open_positions()}
        codes.update(poolmod.active_watch_codes())
        codes.update(self.last_scan_signals.keys())
        return sorted(codes)

    def run_full_scan(self, with_buy: bool = True) -> dict:
        """全市场实时行情刷新 + N字扫描 + 池更新 + 尝试买入。"""
        if not self.loaded_universe:
            self.ensure_universe(force=True)   # 手动触发时先重试股票列表
        if not self.loaded_universe:
            return {"ok": False, "reason": "股票列表未就绪(抓取失败, 请检查数据源或稍后重试)"}
        # 1 全市场行情
        try:
            mk.market.refresh_spot(None)
        except Exception as e:  # noqa: BLE001
            log.warning("全市场行情刷新失败: %s", e)
            return {"ok": False, "reason": f"行情刷新失败 {e}"}
        # 2 扫描
        version = strat.current_version()
        params = version["params"] if version else strat.default_params_v1()
        today = today_str()
        results: Dict[str, dict] = {}
        fresh_codes: set = set()
        # 环境闸门
        env = scanmod.env_gate(params, mk.market.index_closes(
            (params.get("env") or {}).get("index", "sh000300")))
        self.env_snapshot = env
        screen = mk.auto_screen_candidates()
        sc = 0
        for u in screen:
            code = u["code"]
            series = mk.market.series(code)
            if not series:
                continue
            lb = series["dates"][-1]
            live = mk.market.live_bar(code)
            if live and lb < live["date"]:
                series = scanmod.append_live(series, live)
                lb = live["date"]
            if lb != today:
                continue
            fresh_codes.add(code)
            sig = scanmod.evaluate_code(code, u["name"], u["board"], params, series)
            if sig:
                results[code] = sig.to_dict()
            sc += 1
        # 3 池更新
        new_add = 0
        for code, sig in results.items():
            if poolmod.upsert_auto(sig):
                new_add += 1
        poolmod.remove_outdated(results, fresh_codes)
        # 4 尝试买入
        buys = []
        if with_buy and self.engine_on():
            buys = trader.attempt_buys(
                list(results.values()), version, params, self.eff_cfg(), env)
        self.last_scan_ts = now_str()
        self.last_scan_count = len(results)
        self.last_scan_signals = results
        db.meta_set("last_scan", json.dumps(
            {"ts": self.last_scan_ts, "count": len(results), "new_add": new_add,
             "env": env}, ensure_ascii=False))
        n_sig = len(results)
        log.info("扫描完成: 扫描%s只 信号%s个 新入池%s 买入%s笔 (环境:%s)",
                 sc, n_sig, new_add, len(buys), env.get("mode"))
        return {"ok": True, "scanned": sc, "signals": n_sig, "new_add": new_add,
                "buys": buys, "env": env, "results": list(results.values())[:50]}

    # ---------------- 高频交易动作 ----------------
    def fast_pass(self) -> dict:
        """盘中快速刷新(自选集: 持仓+池+信号), 执行卖出规则与尝试买入。"""
        now = now_str()
        self.last_fast_ts = now
        version = strat.current_version()
        params = version["params"] if version else strat.default_params_v1()
        codes = self.watch_codes()
        try:
            mk.market.refresh_spot(codes)
        except Exception as e:  # noqa: BLE001
            log.warning("快照行情刷新失败: %s", e)
            return {"ok": False, "reason": str(e)}
        sells = []
        buys = []
        if self.engine_on():
            # 卖出优先级最高
            sells = trader.check_sells(version, params, self.eff_cfg())
            if sells:
                self.last_sell_ts = now
            # 买入: 若距上次扫描不远且有当日新鲜信号
            hm = hhmm_now()
            eng = self.eff_cfg()["engine"]
            b_start = int(eng.get("buy_start_hhmm", "0935"))
            b_end = int(eng.get("buy_end_hhmm", "1445"))
            if b_start <= hm < b_end and self.last_scan_signals:
                sigs = list(self.last_scan_signals.values())
                fresh = [s for s in sigs if trader.buyable(s, today_str())]
                if fresh:
                    env = scanmod.env_gate(params, mk.market.index_closes(
                        (params.get("env") or {}).get("index", "sh000300")))
                    buys = trader.attempt_buys(fresh, version, params, self.eff_cfg(), env)
        return {"ok": True, "sells": sells, "buys": buys}

    # ---------------- 手动动作(异步提交, 秒回, 避免超时504) ----------------
    def _run_once(self, name: str, fn, busy_attr: str) -> dict:
        if getattr(self, busy_attr, False):
            return {"ok": True, "accepted": True, "busy": True}
        setattr(self, busy_attr, True)

        def job() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("%s 后台执行异常", name)
            finally:
                setattr(self, busy_attr, False)

        threading.Thread(target=job, daemon=True, name=name).start()
        return {"ok": True, "accepted": True, "busy": False}

    def kick_scan(self) -> dict:
        self._scan_busy = getattr(self, "_scan_busy", False)
        return self._run_once("kick-scan",
                              lambda: self.run_full_scan(with_buy=True), "_scan_busy")

    def kick_sync(self) -> dict:
        self._sync_busy = getattr(self, "_sync_busy", False)
        return self._run_once("kick-sync",
                              lambda: (self.ensure_universe(force=True),
                                       self._start_history_sync()), "_sync_busy")

    def kick_close(self) -> dict:
        self._close_busy = getattr(self, "_close_busy", False)
        return self._run_once("kick-close",
                              lambda: self.close_pass(), "_close_busy")

    def kick_universe(self) -> dict:
        self._uni_busy = getattr(self, "_uni_busy", False)
        return self._run_once("kick-universe",
                              lambda: self.ensure_universe(force=True), "_uni_busy")

    # ---------------- 收盘任务链 ----------------
    def close_pass(self, date: str = "") -> dict:
        date = date or today_str()
        if self.close_done_today == date:
            return {"ok": True, "reason": "今日收盘流程已执行"}
        self.close_done_today = date
        db.log_event("INFO", f"开始执行 {date} 收盘流程")
        # 1 终盘行情(收盘价)与扫描/买卖扫尾
        try:
            mk.market.refresh_spot(None)
        except Exception as e:  # noqa: BLE001
            log.warning("收盘行情刷新失败: %s", e)
        self.run_full_scan(with_buy=True)
        # 2 每日复盘 + 失败归因备注
        daily = reviewmod.run_daily_review(date)
        # 3 规则5: 连续5日盈亏比<0.6 且 成功率<40% → 自行优化
        rule5 = optimizer.check_rule5_and_optimize()
        # 4 月末 → 月度复盘与(必要时)优化
        monthly = {"ok": True, "trigger": False, "reason": "非月末"}
        if self._is_month_end(date):
            monthly = optimizer.monthly_optimize(date[:7])
        # 5 增量历史同步(后台)
        if not self.sync_running:
            self._start_history_sync()
        db.meta_set("last_close_pass", date)
        db.log_event("INFO", f"{date} 收盘流程完成: 复盘OK 规则5触发={rule5.get('trigger')} "
                             f"月末优化触发={monthly.get('trigger')}")
        return {"ok": True, "date": date, "daily": daily.get("stats", {}),
                "rule5": rule5, "monthly": monthly}

    def _is_month_end(self, date: str) -> bool:
        """date 是否为本月最后一个交易日(通过指数交易日历判断)。"""
        tdates = mk.market.trading_dates()
        if not tdates or date not in tdates:
            return False
        i = tdates.index(date)
        if i >= len(tdates) - 1:
            return True
        return tdates[i + 1][:7] != date[:7]

    # ---------------- 维护 ----------------
    def daily_maintenance(self) -> None:
        try:
            mk.sync_universe()
            mk.market.refresh_index()
        except Exception as e:  # noqa: BLE001
            log.warning("每日维护失败: %s", e)

    # ---------------- 调度主循环 ----------------
    def interval(self, key: str, dflt: int) -> int:
        try:
            v = db.meta_get(key)
            return max(1, int(v)) if v is not None else dflt
        except Exception:
            return dflt

    def scheduler(self) -> None:
        last_fast = 0.0
        last_scan = 0.0
        last_close = ""
        last_maint = ""
        while not self._stop:
            try:
                clk = trading_clock_state()
                nowf = time.time()
                refresh_sec = max(1, self.interval("data_refresh_sec",
                                                   int(self.cfg.get("data_refresh_sec", 5))))
                scan_sec = max(10, self.interval("scan_interval_sec",
                                                 int(self.cfg.get("scan_interval_sec", 60))))
                has_universe = (db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0) > 0
                if not has_universe:
                    # 股票列表为空 → 自动重试抓取(限频25s), 就绪前暂停市场动作
                    self.ensure_universe()
                    has_universe = (db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0) > 0
                # 收盘流程: 15:00 后(且今天确为交易日)执行一次
                if clk["phase"] == "post" and clk["date"] != last_close \
                        and mk.market.is_trading_today():
                    self.close_pass(clk["date"])
                    last_close = clk["date"]
                    last_fast = 0.0
                    last_scan = 0.0
                if (clk["in_session"] or clk["phase"] == "post") and self.bootstrap_done \
                        and has_universe:
                    if nowf - last_fast >= refresh_sec:
                        self.fast_pass()
                        last_fast = nowf
                    if nowf - last_scan >= scan_sec:
                        if clk["in_session"]:
                            self.run_full_scan(with_buy=True)
                        last_scan = nowf
                else:
                    # 非交易时段维护: 每天 08:40-08:45 完整维护一次(盘前数据就绪),
                    # 00:10 另刷一次股票列表
                    hm = clk["hhmm"]
                    if clk["date"] != last_maint and 840 <= hm <= 845:
                        self.daily_maintenance()
                        db.meta_set("last_maint", now_str())
                        last_maint = clk["date"]
                time.sleep(0.5)
            except Exception:  # noqa: BLE001
                log.exception("调度循环异常")
                time.sleep(2)

    # ---------------- 状态 ----------------
    def status(self) -> dict:
        clk = trading_clock_state()
        version = strat.current_version()
        sync = json.loads(db.meta_get("sync_progress") or "{}")
        pool_items = db.scalar("SELECT COUNT(*) FROM pool WHERE status='in'", (), 0) or 0
        open_n = db.scalar("SELECT COUNT(*) FROM positions WHERE status='open'", (), 0) or 0
        return {
            "clock": clk,
            "server_time": now_str(),
            "engine_enabled": self.engine_on(),
            "bootstrap_done": self.bootstrap_done,
            "sync": sync,
            "last_fast_ts": self.last_fast_ts,
            "last_scan_ts": self.last_scan_ts,
            "last_scan_count": self.last_scan_count,
            "env": self.env_snapshot,
            "pool_count": pool_items,
            "open_positions": open_n,
            "version": {"id": version["id"], "no": version["version_no"],
                        "name": version["name"]} if version else None,
            "universe_count": db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0,
            "bar_codes": len(mk.market.bars),
        }


svc = Service()


def start_scheduler() -> None:
    threading.Thread(target=svc.scheduler, daemon=True, name="scheduler").start()

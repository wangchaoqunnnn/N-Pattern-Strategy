# -*- coding: utf-8 -*-
"""基础工具: 北京时区(UTC+8, 无夏令时, 避免依赖系统时区/zoneinfo数据库)、日志、配置读写。"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

# ---------------- 路径(全部相对解析, 禁止绝对/静态路径) ----------------
try:
    _PKG_DIR = Path(__file__).resolve().parent          # server/core
except Exception:                                       # pragma: no cover
    _PKG_DIR = Path(".")

SERVER_DIR = _PKG_DIR.parent                            # server/
PROJECT_DIR = SERVER_DIR.parent                         # 项目根
WEB_DIR = PROJECT_DIR / "web"
DATA_DIR = Path(os.environ.get("NSTRAT_DATA", str(PROJECT_DIR / "data")))
CONFIG_DIR = SERVER_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "nstrategy.db"
PROVIDERS_PATH = SERVER_DIR / "providers.json"
CONFIG_PATH = SERVER_DIR / "config.json"
CONFIG_EXAMPLE_PATH = SERVER_DIR / "config.example.json"

# ---------------- 北京时间 ----------------
CN_TZ = _dt.timezone(_dt.timedelta(hours=8))  # 中国标准时间 UTC+8


def now_cn() -> _dt.datetime:
    return _dt.datetime.now(CN_TZ)


def now_str() -> str:
    return now_cn().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return now_cn().strftime("%Y-%m-%d")


def hhmm_now() -> int:
    return int(now_cn().strftime("%H%M"))


def parse_dt(s: str) -> _dt.datetime:
    return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)


def ts_str(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_date(s: str) -> _dt.date:
    return _dt.datetime.strptime(s, "%Y-%m-%d").date()


def add_days_cn(d: _dt.date, n: int) -> _dt.date:
    return d + _dt.timedelta(days=n)


def trading_clock_state() -> dict:
    """返回当前交易时段状态字典(基于北京时间)。"""
    now = now_cn()
    hm = now.hour * 100 + now.minute
    weekday = now.weekday()
    pre = 900 <= hm < 925                      # 集合竞价
    am = 930 <= hm < 1130                      # 上午连续竞价
    lunch = 1130 <= hm < 1300
    pm = 1300 <= hm < 1500                     # 下午连续竞价
    post = 1500 <= hm < 1525                   # 收盘后短暂窗口(复盘用)
    night = 1500 <= hm or hm < 900             # 休市
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.weekday(),
        "hhmm": hm,
        "is_weekday": weekday < 5,
        "phase": ("pre" if pre else "am" if am else "lunch" if lunch else "pm"
                  if pm else "post" if post else "closed"),
        "in_session": am or pm,
        "in_auction": pre,
    }


# ---------------- 日志 ----------------
_LOG_LOCK = threading.Lock()
_LOGGERS: dict = {}


def get_logger(name: str = "app") -> logging.Logger:
    with _LOG_LOCK:
        if name in _LOGGERS:
            return _LOGGERS[name]
        logger = logging.getLogger("nstrategy." + name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(LOG_DIR / ("nstrategy.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
        _LOGGERS[name] = logger
        return logger


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------------- 配置 ----------------
_CFG_LOCK = threading.Lock()


def load_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def ensure_config() -> dict:
    """首次启动时将 config.example.json 复制为 config.json, 之后以 config.json 为准(环境变量优先)。"""
    with _CFG_LOCK:
        if not CONFIG_PATH.exists() and CONFIG_EXAMPLE_PATH.exists():
            import shutil
            shutil.copyfile(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
        cfg = load_json(CONFIG_PATH) or {}
        if os.environ.get("NSTRAT_HOST"):
            cfg["host"] = os.environ["NSTRAT_HOST"]
        if os.environ.get("NSTRAT_PORT"):
            cfg["port"] = int(os.environ["NSTRAT_PORT"])
        return cfg


def providers() -> dict:
    return load_json(PROVIDERS_PATH, {}).get("providers", {})


# ---------------- 文本/数字工具 ----------------
def is_st_name(name: str) -> bool:
    if not name:
        return True
    n = str(name).upper()
    return "ST" in n or "退" in n


def num(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v
    except Exception:
        return default


def fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def pct_str(v: float, signed: bool = True) -> str:
    s = "+" if signed and v > 0 else ""
    return f"{s}{v:.2f}%"


def clean_code(c: str) -> str:
    return re.sub(r"[^0-9]", "", str(c))

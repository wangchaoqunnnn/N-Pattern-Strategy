# -*- coding: utf-8 -*-
"""FastAPI 应用入口: 生命周期启动服务与调度, 挂载同源前端(全部相对路径)。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import api
from .core.util import WEB_DIR, get_logger
from .service import svc, start_scheduler

log = get_logger("main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("系统启动: 初始化数据库/交易系统/数据引导...")
    try:
        # 清理上次异常退出遗留的运行中任务
        from .core import db
        from .core.util import now_str
        db.execute("UPDATE backtests SET status='failed', error='服务重启, 任务中断', "
                   "updated_at=? WHERE status IN ('running','cancelling')", (now_str(),))
        svc.startup()          # 内部后台线程引导数据, 不阻塞
        start_scheduler()
    except Exception:  # noqa: BLE001
        log.exception("启动初始化异常(继续运行, 调度器将重试)")
    yield
    log.info("系统停止")


app = FastAPI(title="N字战法·自动选股与自我执行交易系统",
              description="沪深全市场N字战法实时筛选 / 版本化交易系统自动买卖 / 复盘回测 / 自我优化",
              version="1.0.0", lifespan=lifespan)

app.include_router(api)


@app.get("/")
def index_page():
    f = WEB_DIR / "index.html"
    if f.exists():
        return FileResponse(str(f))
    return {"message": "前端文件缺失, 请检查 web/ 目录"}


if (WEB_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

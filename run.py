# -*- coding: utf-8 -*-
"""启动入口(置于项目根目录运行): python3 run.py
监听地址与端口: 环境变量 NSTRAT_HOST / NSTRAT_PORT 优先, 否则取 server/config.json, 无静态/绝对地址。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 保证 server 包可导入(相对自身定位)

import uvicorn  # noqa: E402

from server.core.util import ensure_config  # noqa: E402


def main() -> None:
    cfg = ensure_config()
    host = os.environ.get("NSTRAT_HOST", cfg.get("host", "0.0.0.0"))
    port = int(os.environ.get("NSTRAT_PORT", cfg.get("port", 8000)))
    print(f"N字战法交易系统启动中 -> http://{host}:{port}  (按 Ctrl+C 停止)")
    uvicorn.run("server.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

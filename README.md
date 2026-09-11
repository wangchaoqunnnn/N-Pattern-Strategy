# N字战法 · 自动选股与自我执行交易系统

基于《N字战法》双文档量化出的**全自动交易系统**：实时筛选全A股 → 推荐买入池 → 系统按"版本化交易系统"自主买卖 → 每日收盘复盘 → 连续5日不达标自动优化 → 月度复盘 → 历史版本可切换回测。

> ⚠️ 免责声明：本项目为量化策略研究与自动化演示（纸面交易），所有买卖均为系统模拟成交，不构成任何投资建议。股市有风险。

---

## 一、功能总览

| # | 需求 | 实现 |
|---|------|------|
| 1 | 实时自动筛选符合条件的个股进入**推荐买入池**（可手动增删） | 交易时段内定时全市场行情+形态扫描，自动入池/过期出池；页面提供手动添加(搜索)与移除 |
| 2 | 给出可执行的**交易系统**，自主判断买卖、记录买卖点与理由，统计盈亏比/成功率，可回测可复盘 | 交易系统以**版本**管理（含人读规则+量化参数）；买卖点全部落库(理由/版本号)；统计面板；回测模块 |
| 3 | **交易系统买卖池**：系统自行买入的股票操作全部入池，严格执行系统 | `positions`+`executions` 两表记录每一笔买/卖/分批止盈及触发原因，所有执行带版本号 |
| 4 | 每日收盘后**复盘**，分析买入失败原因并写入备注 | 15:00 后自动执行收盘流程：复盘报告、每笔亏损自动归因(`failure`)写入备注；可手工触发 |
| 5 | **连续5天**盈亏比<0.6且成功率<40% → 系统自行识别并**自我优化**交易系统 | 收盘流程内检查近5个交易日滚动窗口；触发后按失败归因自动调整参数生成并激活新版本 |
| 6 | 每月**月度复盘**，判断是否优化，需要则自行优化 | 月末最后一个交易日收盘流程触发月度复盘；指标不达标自动优化并生成新版本 |
| 7 | 历史交易系统版本记录，用户可**手动选择任一版本执行** | 版本表自包含参数快照；页面一键切换 |
| 8 | 买卖池操作记录**所依据的版本号** | 买卖记录均含 `version_id/version_no`（人工干预也标注） |
| 9 | 选股范围：**全部交易所/板块，无遗漏** | 沪主板 + 深主板 + 科创板 + 创业板 + **北交所** + 沪B + 深B（A股列表为主源，东方财富为B股补充源/A股兜底源；仅剔除ST/退市） |

## 二、代码约束落实

1. **无静态/绝对地址**：前端仅用同源相对路径（`/api/...`、`app.js` 等）；外部数据源地址只出现在 `server/providers.json`（代码零硬编码URL）；监听地址/端口由环境变量或 `server/config.json` 提供；数据目录默认相对项目根 `./data`，可用 `NSTRAT_DATA` 覆盖。
2. **红涨绿跌**：上涨红色、下跌绿色统一作用于名称/现价/涨跌额/涨跌幅/浮盈等所有涨跌类单元格（前端 `updown()/pn()/nm()`）。
3. **云端部署于 /root**：见 `deploy/deploy.sh`（systemd 常驻 + 开机自启）。
4. **交易时间实时监控**：调度器按北京时间工作（不依赖服务器时区），交易日 9:30-11:30 / 13:00-15:00 高频刷新（默认5秒行情、60秒全市场扫描），节假日/周末自动跳过。

## 三、数据源与行情引擎

- 无 pandas / akshare 依赖，仅 `fastapi + uvicorn`（标准库 `urllib` 抓取）。
- **行情**：新浪批量行情（全市场约8次请求）；**日K**：腾讯 proxy 主源（不复权，与实盘价格一致，沪深/指数/**北交所**通用）→ 腾讯 web 备源 → 新浪 money 回退源；指数：沪深300/上证指数。
- **股票列表**：新浪沪深京A股列表为主源（沪主板/深主板/科创板/创业板/北交所），东方财富列表为 **B股(沪B/深B)补充源** 与 A股兜底源；源被限流时自动保留本地缓存继续运行。
- K线落 SQLite `daily_bars` 本地缓存（约320根/股，增量同步），内存常驻用于盘中秒级扫描。
- 首次启动自动引导：同步股票列表 → 指数 → 逐股历史K线（后台任务带进度）；监控范围内缺K线的股票会自动补齐。
- 数据源地址集中配置于 `server/providers.json`；若云端某源不可达，可替换为该文件内可达源而无需改代码。

## 四、N字形态判定（量化口径，与文档一致）

```
点火阳线: 近12根内, 涨幅≥7% 且 成交量≥前5日均量×2(倍量), 上影线≤实体35%
回调洗盘: 2~6天; 单日|涨跌|≤5%; 区间最低≥点火低点×0.99(不破底);
          区间日均量≤点火量×50%(缩量); 回调深度≤第一波涨幅50%
买点B1(回踩): 回调2天以上当日止跌(|涨跌|≤2.2% 且缩量), 价格贴近MA10/20支撑带,
              RSI∈[30,70], MA20向上, 收盘站上MA20
买点B2(突破): 收盘放量(≥5日均量×1.2)突破回调期最高点, 涨幅≥5%, RSI≤80, MA20向上
环境闸门: 沪深300近20日≥-3% 且 MA20斜率≥0.05% → 正常; 仅斜率不足 → 震荡半仓;
          20日<-3% → 退潮暂停开新仓(持仓仍按规则退出)
卖出: ①止损=形态止损(N型回调低点×0.99)与 -8%硬止损 孰高; ②+25%分批止盈50%,
      剩余保本+移动止盈(高点回撤15%); (可选)尾盘跌破昨收离场
T+1: 当日买入的持仓当日禁止卖出, 最早下一交易日方可卖出(自动卖出/人工平仓均强制)
风控: 单票≤20%账户资金, 同时≤5只(半仓模式减半), 每日新开≤3只, 同股冷却10个交易日
```

## 五、目录结构

```
├── run.py                     # 启动入口(项目根执行 python3 run.py)
├── requirements.txt
├── server/
│   ├── config.example.json    # 首次启动复制为 config.json
│   ├── providers.json         # 唯一的数据源地址配置(代码无静态地址)
│   ├── core/                  # util(北京时间/日志/路径) db(建表/迁移) providers(抓取) market(列表/K线/内存行情)
│   ├── engine/
│   │   ├── strategy.py        # 交易系统版本模型(参数快照+可读规则)
│   │   ├── scan.py            # N字形态扫描 + 环境闸门
│   │   ├── pool.py            # 推荐买入池(自动/手动)
│   │   ├── trader.py          # 执行买卖/仓位/风控/人工平仓
│   │   ├── stats.py           # 成功率/盈亏比/权益/月度/窗口统计
│   │   ├── review.py          # 每日复盘/失败归因/月度复盘
│   │   ├── optimizer.py       # 规则5自优化/月度优化/手动优化
│   │   └── backtest.py        # 历史回测(同一套规则)
│   ├── service.py             # 调度器(盘中/收盘/维护) + 扫描编排
│   ├── api.py                 # REST API(相对路径)
│   ├── main.py                # FastAPI 应用
├── web/                       # 纯静态SPA(无CDN/无外部库)
├── tools/                     # 离线引擎自检脚本(形态扫描/单元冒烟)
├── deploy/deploy.sh           # 云服务器 /root 部署脚本
├── data/                      # SQLite/日志(运行时生成, 相对路径)
└── 项目需求表.md
```

## 六、快速开始（本地）

```bash
python3 -m venv .venv && source .venv/bin/activate    # Windows: py -3 -m venv .venv
pip install -r requirements.txt
python3 run.py                                        # 默认 0.0.0.0:8000
# 打开 http://服务器IP:8000  (首次启动自动同步全市场历史K线, 后台进行)
# 常用控制: NSTRAT_PORT=9000 python3 run.py
```

首次启动到数据就绪前，界面显示"数据同步中…"；同步完成后（页面仪表盘进度100%）进入自动运行。

## 七、云服务器部署到 /root

```bash
# 本机打包
tar czf nstrategy.tgz --exclude=data --exclude=.venv --exclude='*.pyc' NPatternStrategy/
scp nstrategy.tgz root@<服务器IP>:/
# 服务器上(以 root 执行)
cd / && tar xzf nstrategy.tgz          # 解出 /root/NPatternStrategy 前先 mv 到 /root
mv NPatternStrategy /root/ && cd /root/NPatternStrategy
bash deploy/deploy.sh                  # 创建venv/安装依赖/systemd服务并启动
systemctl status nstrategy
```

`deploy.sh` 全部使用脚本自身相对定位与变量，无硬编码路径；默认安装目录 `/root/NPatternStrategy`（可用 `INSTALL_DIR=... bash deploy/deploy.sh` 覆盖）。

### 通过 nginx 子路径对外（与其它项目共存，如 /N/）

前端已内置**子路径自动识别**：只要把整站挂在任意前缀下，`/api/*` 请求会自动带上前缀，无需改代码。nginx 只需在既有 server 块中加：

```nginx
location ^~ /N/ {
    proxy_pass http://127.0.0.1:8000/;   # 尾随 / 会将 /N/api/x 转发为 /api/x
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_http_version 1.1;
    proxy_connect_timeout 10s;
    proxy_read_timeout 300s;             # 全市场扫描/同步等重活耗时较长, 避免504
    proxy_send_timeout 300s;
}
location = /N { return 301 /N/; }
```

- 不要用 `server_name _` 与其它项目抢流量；多项目按域名(server_name)或多IP(listen IP:80)区分，或用独立端口。
- 若希望后端仅本机可访问，可将 systemd 服务环境变量改为 `NSTRAT_HOST=127.0.0.1`。
- 完整示例见 `deploy/nginx.example.conf`（A域名/B多IP/C独立端口三种写法）。

## 八、自动运行时间表（北京时间）

| 时间 | 动作 |
|------|------|
| 00:00 后首个空闲 | （可选）数据自检 |
| 08:40-08:45 | 每日维护：刷新股票列表/指数，确保盘前数据就绪 |
| 09:30-11:30 / 13:00-15:00 | 每5秒刷新自选行情并检查卖出/尝试买入；每60秒全市场扫描更新推荐池 |
| 15:00-15:25 | 收盘流程：终盘买卖扫尾 → 每日复盘(失败归因备注) → 规则5自优化检查 → 月末则月度复盘与优化 → 增量同步日K |
| 交易日历 | 基于指数K线自动判定；周末/节假日自动跳过 |

## 九、自优化语义

- **成功率** = 盈利平仓笔数 / 已平仓笔数；**盈亏比** = 平均盈利% / 平均亏损%。
- **规则5触发**：连续 **5 个交易日**中，每个"近5日窗口"（窗口末日=当日）满足：窗口平仓 ≥3 笔 且 盈亏比<0.6 且 成功率<40% → 收盘流程内自动生成优化版本并切换执行（同日限1次）。数据不足时自动延后判断，日志可查。
- **优化方向**由最近30笔亏损的**失败归因**驱动：假突破多→提高突破确认门槛；买点过早/破位→回调买点更谨慎+均线斜率要求↑；大盘拖累→收紧环境闸门；归因不明→温和收紧仓位与止损。
- 每次优化记录 `optimizations`（旧版→新版、触发原因、变更明细），**历史版本始终可选回退**。
- 月度复盘：平仓≥3笔且（成功率<50% 或 盈亏比<1 或 月收益<0）判定需要优化 → 自动生成新版本。

## 十、API（相对路径，示例）

```
GET  /api/meta                系统状态/时钟/引擎开关/环境/同步进度
GET  /api/pool                推荐买入池(带实时价)
POST /api/pool/manual         手动加池 {code,reason}
POST /api/pool/remove         移除 {code,reason}
GET  /api/positions?status=   持仓/历史平仓
GET  /api/executions          买卖操作流水(版本号+理由)
POST /api/positions/close     人工平仓 {id,note}
GET  /api/stats               账户与成功率/盈亏比统计
GET  /api/stats/monthly       月度统计
GET  /api/stats/rule5         规则5连续5日监测窗口
GET  /api/strategy/current    当前执行版本(可读规则+参数)
GET  /api/strategy/versions   版本历史
POST /api/strategy/activate   切换执行版本 {id}
GET  /api/reviews?type=       复盘记录(daily/monthly)
POST /api/reviews/run-daily   手动每日复盘
POST /api/optimize/manual     手动发起优化 {reason}
POST /api/backtests           创建回测 {start,end,version_id,capital}
GET  /api/backtests/{id}      回测结果(摘要/交易明细/权益曲线)
POST /api/engine/toggle       引擎开关 {enabled}
POST /api/engine/scan         手动立即扫描
POST /api/engine/close        手动收盘流程
GET  /api/kline/{code}        个股K线+系统买卖标记
GET  /api/logs                运行日志
GET/PUT /api/config           运行配置
```

## 十一、注意事项

- 回测/扫描用**不复权**价格（与实盘成交价一致）；分红除权会产生自然跳空，属正常价格行为。
- 历史K线缓存默认最近 320 自然日；回测超出范围会自动联网补拉所需个股。
- 本地(非交易日)测试：可用页面"手动扫描/收盘复盘/回测"按钮体验全部流程。
- 详细需求实现对照见 [项目需求表.md](./项目需求表.md)。

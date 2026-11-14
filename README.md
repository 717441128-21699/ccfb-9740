# 医药挂网价格采集系统（本地仿真 + 对账验证）

面向医药流通采购场景：自动采集多省医保挂网平台价格，归一不同省的规格/厂家写法，
跟踪调价与撤网，重算毛利所需的变更历史可逐条追溯。**全部平台均为本地仿真站，
只监听 127.0.0.1，不访问任何真实网站。**

技术栈：Python 3.14 + Playwright（驱动系统 Chrome/Edge）+ Flask + SQLite（WAL）。

## 目录结构

```
configs/platforms.json   4 个仿真平台配置（地址/认证/限流参数）
sim/                     仿真挂网平台
  generator.py           确定性数据生成：12 标准品种×4省 + 4 个难解析品种，
                         预置 4 拍调价/撤网事件
  server.py              Flask 仿真站：表单登录/API登录、HTML/JSON 两种取数、
                         429 限流、会话过期、v1/v2/v3 三版页面布局、
                         /admin/tick 推进事件、/admin/state 导出真值
collector/               采集系统
  db.py                  SQLite 建表与全部持久化（水位/断点/变更/告警/映射）
  normalize.py           规格文本结构化解析 + 通用名/剂型/厂家四要素归一
  parsers.py             HTML/JSON 解析，未知结构抛 ParseError，绝不写空数据
  client.py              Playwright 客户端：节流、429 退避、登录过期自动重登
  engine.py              采集引擎：全量/增量、断点续跑、变更落库、平台隔离
web/                     Flask 控制台（平台配置/采集任务/药品库/价格变更/告警）
reconcile/reconcile.py   与仿真真值逐条对账（字段 + 归一 + 变更事件）
tests/test_e2e.py        端到端测试，7 个场景，最终要求差异为 0
run_sim.py               启动 4 个仿真平台（端口 8101-8104）
run_collect.py           采集 CLI
run_reconcile.py         对账 CLI
```

## 快速开始

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 浏览器优先用系统已装的 Chrome/Edge，无需额外下载；
# 干净环境可执行： .\.venv\Scripts\python.exe -m playwright install chromium

# 1) 启动仿真平台（保持窗口开着）
.\.venv\Scripts\python.exe run_sim.py

# 2) 另开终端：同步配置 → 全量采集 → 对账
.\.venv\Scripts\python.exe run_collect.py sync-configs
.\.venv\Scripts\python.exe run_collect.py full
.\.venv\Scripts\python.exe run_reconcile.py

# 3) 推进仿真站的调价/撤网事件后做增量采集
#    （或直接跑端到端测试，它会自动 tick）
.\.venv\Scripts\python.exe run_collect.py inc

# 4) Web 控制台 http://127.0.0.1:8200
$env:COLLECTOR_DB='data\collector.db'
.\.venv\Scripts\python.exe -m flask --app web.app run --port 8200
```

## 一键端到端验证

```powershell
.\.venv\Scripts\python.exe tests\test_e2e.py
```

覆盖场景（全部通过且最终对账差异为 0）：

1. 全量采集中途 `kill` 进程 → 重启自动续跑，不重复不漏抓；
2. 重复全量幂等，变更历史不产生重复记录；
3. 4 拍调价/撤网事件 + 增量采集，9 个事件/省（7 调价 + 2 撤网）逐事件匹配；
4. 页面/接口改版（v3 未知结构）→ parse 告警、任务中止、零数据写入；
5. 登录会话过期 → 自动重新登录后继续；
6. 单平台宕机 → platform_down 告警、任务标 failed，其他平台不受影响，恢复后续跑；
7. 4 个难解析规格全部停在人工确认，未被错误自动归一。

## 关键设计如何对应需求

| 需求 | 实现 |
| --- | --- |
| 全量/增量、水位断点落库 | `job_checkpoint` 存列表页、已完成详情编码集合、心跳；`watermark` 存每平台最新 `source_updated_at`。增量只抓新条目或更新时间高于水位的条目 |
| 杀进程续跑、不重不漏 | 所有进度在每条处理后立即落库（WAL），重启识别 `running` 任务从断点恢复；`raw_drug` 唯一键 upsert、`price_change` 唯一键 `INSERT OR IGNORE` 保证幂等 |
| 四要素归一 | `normalize.py` 把 `0.25g*24片`、`250mg×2板×12片`、`24粒/盒(250mg)` 等解析为同一标准规格；通用名/剂型/厂家别名统一；置信度 < 0.9 进 `drug_mapping.status=pending` 人工确认 |
| 调价/撤网历史 | `price_change` 记录 change_type(price/delist/relist)、原价、新价、平台变更时间；控制台显示调价额与幅度 |
| 限流/登录过期 | 客户端按平台窗口主动节流；429 读 `Retry-After` 退避；302 跳登录或 401 时自动重登一次 |
| 平台故障隔离 | 单平台异常只标记本平台任务 failed + 告警，调度器内其他平台继续 |
| 改版不写空数据 | 解析器对必填字段和在售价格做校验，未知结构直接抛 `ParseError`；列表失败中止任务，单条详情失败跳过该条，均不写库 |
| 真值对账 | 仿真站 `/admin/state` 导出全量条目与事件日志，对账三层：逐字段、归一来源、变更事件，差异数非零即退出码 1 |

## 仿真平台管理接口（仅本机 + X-Admin-Key）

| 接口 | 作用 |
| --- | --- |
| `POST /admin/tick` | 推进一拍预置事件（调价/撤网） |
| `GET /admin/state` | 导出当前全量真值与事件日志（对账用） |
| `POST /admin/layout` | 切换 `html`/`api` 通道布局版本 v1/v2/v3（模拟改版） |
| `POST /admin/expire` | 清空全部会话（模拟登录过期） |

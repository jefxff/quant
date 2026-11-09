# quant — QUANT-001

**可运行、可复现、零付费金融 API、A 股日频**的量化研究与模拟盘系统。

- 历史日线主源 **Baostock**（匿名、无 token），备用行情与行业分类 **AKShare**；无任何付费接口（R-001/R-002）。
- 回测与模拟盘**只读本地 SQLite**，循环内绝不联网（R-004）；网络更新只作为手动 `data update` 命令。
- **不接入券商、不发送真实委托、不需要 miniQMT、不产生任何付费 API 费用**（R-007）。
- 事件驱动回测严格模拟 A 股约束：T 日收盘后出信号 → T+1 开盘成交、100 股整数倍、佣金/印花税/滑点、涨跌停、停牌顺延、交易日历。

> ⚠️ 本项目仅用于研究与模拟，**不构成投资建议**。报告中的绩效数字来自近似规则与有限样本，请勿用于实盘决策。

---

## 1. 打开即看：运行监控台（主界面）

**直接打开 `dashboard/index.html`** —— 单个自包含 HTML 文件，双击即可，不需要启动服务、不需要联网、不需要点任何按钮。

首屏就是一个跑在真实行情上的模拟盘：6 个核心指标、一行策略摘要（含未来 5 日 P50 与区间）、一行外围市场快照（美日韩四大指数真实收盘涨跌与数据滞后标记）、策略净值 vs 沪深300 曲线（右侧带可见的 5 日推演曲线）、当日持仓、订单状态流转、运行事件与异常告警，全部在一屏内；少量滚动即可看到完整的策略参数/规则/逐标的信号、放大的 5 日情景曲线与推演明细表，以及政策 · 外围市场 · 传导区块。时间轴可拖动或点「▶ 回放」（进入页面会自动回放最近 45 个交易日），「仅异常」按钮可只看风控/拒绝/数据告警。

数据不是演示占位：`dashboard/build.py` 联网采集沪深300 **当前成分**中 28 只主板标的的真实不复权日线（AKShare / 东方财富，新浪备用源 + 指数退避重试），用新浪沪深300真实指数作为基准与真实交易日历，再按项目同一套 `costs.py` / `rules.py` 跑事件驱动回测。结果固化为 `dashboard/snapshot.json` 并内嵌进 HTML，因此页面离线也能完整预览。

**当前快照（2026-11-09 实跑）**：2024-01-02 ~ 2026-11-09 共 **689 个真实交易日**、28 只主板成分、**18,452 条真实 K 线**、840 条真实缺失（新成分未上市/停牌，未填补）。策略净值 **1.4276**、沪深300 同期 1.1978；全期 +42.76%（夏普 1.08、最大回撤 -16.34%、胜率 47.7%、367 笔平仓），样本内 +53.06%（551 天）、样本外 **-6.70%**（138 天）—— 样本外走弱是真实结果，没有为了好看调参。715 笔订单、954 条运行事件，含东财限流 → 4 次退避 → 降级新浪、涨跌停行情观察、回撤刹车触发/解除、行情覆盖缺失，以及最新收盘 8 笔 `waiting_for_next_bar` 待成交订单。

### 策略展示与后续 5 日推演

页面把「策略」拆成三层可见信息，而不是只给一条净值曲线：

- **首屏摘要条**：策略 ID、双均线 20/60 + 波动率目标仓位、截至所选交易日的多头信号数 / 目标总仓位 / 风控刹车状态 / 待成交笔数，以及未来 5 日 P50 与 P10–P90 区间。
- **策略说明卡**：9 项参数（均线、入场条件、波动率窗口与目标、单票权重、周频调仓、死区、样本外比例、初始资金）、10 条执行与风控规则（T+1 开盘、复权口径、100 股整数倍、成本、±10% 涨跌停、停牌顺延、仓位上限、回撤刹车及解除、数据纪律），以及最新收盘的逐标的信号明细（MA20 / MA60、均线偏离、多头或空仓、年化波动、目标与当前权重、加仓/减仓/持有）。
- **未来 5 日情景推演卡**：顶部是一张独立放大的 5 日情景曲线（P10–P90 阴影带、P50 实线、基准 P50 虚线、逐日节点与末端分位标签），下方为逐日 P10 / P50 / P90 / 基准 P50 净值与累计涨跌，5 日概率指标（下跌概率、跑赢基准、年化波动、短期回撤 ≥3% 情景），以及由真实规则推导的「策略动作计划」——T+1 待成交订单明细与涨跌停/停牌处理、下次周频调仓日、风控刹车触发距离与 P10 下行情景、行情未到时的 `waiting_for_next_bar`。

资金曲线右侧固定留出 5 个交易日的推演区，绘制 P10–P90 区间、P50 与基准 P50。**两个未来区都使用独立局部纵轴**：全历史净值轴跨度约 0.94~1.78，而真实 5 日情景只有 ±2%，若共用同一坐标轴曲线只有 3~5px 高、肉眼不可见；局部轴以「当前净值 = 0%」重新缩放，并在框内/图题明确标注轴范围与口径，不与历史净值轴混用。拖动时间轴时，推演会**只用截至该日的真实历史收益重新计算**（严格无未来函数），因此数值与曲线随回放变化。历史样本不足 30 个交易日时显示「暂不推演」，不会编造数据。

> **预测边界（重要）**：这是基于**真实历史收益分布**的 5 日连续收益块自助抽样（block bootstrap，滚动 250 日窗口、800 条路径、固定种子 `20261109`；策略与基准共用同一抽样块以保留相关性）得到的**统计情景分布**，不是行情预测，不含任何未来真实行情。它不写入行情库、不生成订单、不参与回测与绩效计算；预测日期按工作日推算并标注「预计」，法定节假日顺延。历史分布不代表未来，**不构成投资建议**。该边界同时写在页面免责声明与页脚，避免与 R-005「禁止伪造/填补行情」混淆。

页面交互保持简单：时间轴拖动/回放、「仅异常」筛选、明暗主题、外围走势「全期 / 近 120 日」切换；键盘 `←` `→` 逐日、空格播放/暂停。KPI、曲线、持仓、订单状态、事件、5 日推演与外围传导视图都随回放日期重算，不是静态截图。

### 政策 · 外围市场 · 传导（2026-11 真实事件）

原回测是纯 A 股量价规则，这一层补上宏观与跨市场视角，但**不改写历史绩效**：

- **真实外围日线**：新浪美股指数（标普500 `.INX`、纳斯达克 `.IXIC`）与新浪环球市场（日经225、首尔综合 KOSPI），按 A 股真实交易日历对齐，缓存在 `dashboard/cache/global/`。异常值如实剔除——当前 KOSPI 2026-11-09 收盘价为 0（源未更新），页面标为「† 日线未更新」，不填补、不插值。
- **真实政策事件**：只收录日期可核实、带来源的公开报道要点（2026-11-02 三部门稳市组合拳与证监会四项安排、11-03 第三次互换便利 31 家机构申请超 1000 亿元、11-04 回购增持再贷款利率 1.25%→1% 并转长期制度），以及 11-05 美股 AI 链抛售、11-06 日韩熔断、11-09 亚太分化。政策条目是文字事实，**不是行情数据**。
- **跨市场传导统计**：口径为「严格早于该 A 股交易日的已收盘外围收益 → 次日 A 股（沪深300）收益」，保证无未来函数；给出 60 日滚动相关、隔夜 β、外围跌 ≥2% 后 A 股次日均值与样本数、平稳日对照。当前真实结果：标普/纳指 60 日相关约 0.11 / 0.12、β 0.20 / 0.13，外围大跌后次日均值 −0.88%（13 次）/ −0.57%（41 次）；日韩同期相关接近 0，同日冲击多已在 A 股当日消化。**相关不代表因果**。
- **事件窗口真实反应**：用本地真实指数与策略净值计算 T+1 / T+3 累计涨跌，后续行情未到则留空。例如 11-05 美股暴跌后沪深300 次日 −1.44%，而策略净值 +0.32%（当时低仓位 + 风控）；11-04 政策后次日沪深300 +0.29%。
- **进入 5 日推演**：新增「外围承压」条件情景——只从历史「已知外围冲击 ≤ −2%」的起点抽 800 条 5 日连续收益块，与无条件推演并列；最新检查点有 23 个历史起点，P50 −0.05%（无条件 −0.07%）、下跌概率 57.6%。样本不足 8 个起点时显示「不推演」，绝不合成行情。

> **边界**：政策与外围数据**未进入回测信号**，原回测仍是双均线 + 波动率目标仓位，历史绩效未被改写。不直接把政策写进信号，是为了避免事后看新闻调参与政策难以规则化导致的过拟合；如要纳入，应作为新策略变体重新做样本外验证。外围走势图默认「近 120 日」并按窗口起点重新归一化，政策与冲击窗口才看得清；可切到「全期」看累计表现。

```bash
# 刷新到最新真实行情并重建页面（唯一需要联网的动作）
./.venv/Scripts/python.exe dashboard/build.py
./.venv/Scripts/python.exe dashboard/build.py --refresh   # 强制重拉全部标的
./.venv/Scripts/python.exe dashboard/build.py --offline   # 只用缓存复现，不联网
```

采集到的真实日线按标的缓存在 `dashboard/cache/`（外围指数在 `dashboard/cache/global/`），可断点续传；采集期的限流/降级事件也持久化，`--offline` 能在无网络时从缓存完整重建页面，数据仍是之前采集的真实行情，不会退化成合成数据。数据口径（前复权仅用于信号、主板 ±10% 近似、样本外切分、幸存者偏差、5 日推演非行情数据、未接入实盘）如实写在页脚。

### 环境与安装（仅刷新数据 / 跑测试时需要）

```bash
cd quant
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# .venv/bin/python -m pip install -e ".[dev]"           # Linux/macOS
cp config.example.yaml config.yaml                      # 可选：不复制则用内置默认值
bash scripts/smoke_offline.sh                           # 完全离线的端到端 smoke + pytest
```

`pip install -e ".[dev]"` 会安装 `pandas / numpy / PyYAML / baostock / akshare / matplotlib / rich / pytest`。
`pyproject.toml` 中所有依赖都给了**下限 + 上限**，防止上游字段漂移。

### 进阶：CLI 操作台（可选）

需要逐条执行 CLI 命令并查看原始 stdout / 退出码时，再用浏览器操作台：所有 CLI 操作变成按钮，参数集中填写，点击后原样展示命令、退出码、耗时与 stdout / stderr，还能直接浏览报告与资金曲线。**日常查看运行状态请用上面的主界面。**

```bash
./.venv/Scripts/python.exe -m quant ui                       # 默认 http://127.0.0.1:8765
./.venv/Scripts/python.exe -m quant ui --port 9000 --open    # 指定端口并自动打开浏览器
```

- **运行离线闭环**：按 7 步依次执行「灌数据 → 数据审计 → 回测 → 模拟盘预演 → 模拟盘运行 → 幂等复跑 → 重渲染报告」，顶部用一行文字与细进度线显示进度；任一步失败即停止并保留错误回执。
- **离线演示区间（重要）**：`tests/fixtures` 只覆盖 **2022-01-04 ~ 2022-12-30**。按 R-005 禁止伪造行情，把页面日期改成其他年份时，「灌入离线数据」会被服务端钳制到该区间并在回执里写明原因；「运行离线闭环」7 步统一使用演示区间，与表单日期无关。想跑其他年份，请先用联网更新把真实行情写入本地库，再单独执行回测。参数区提供「恢复离线默认参数」按钮。
- **运行回执**：每次操作生成一张卡片，含完整可复制命令、成功/失败与退出码、耗时、STDOUT / STDERR 切换与关键指标摘录（累计收益按 A 股习惯**红涨绿跌**）。
- **报告浏览**：列出 `reports/` 下的 run，直接渲染 Markdown、显示 `equity.png`、下载 CSV，并可展开程序化「假设与限制」原始数据。
- 纯标准库 HTTP 服务 + 原生 HTML/CSS/JS，**零新增依赖、零 CDN、断网可用**；只监听 `127.0.0.1` 且校验 Host，命令走白名单 argv（不经过 shell），报告文件读取做了目录逃逸校验，`Ctrl+C` 停止。
- 联网按钮（Baostock / AKShare）会二次确认；回测与模拟盘按钮始终只读本地 SQLite。

### 常用命令

```bash
# 本地 Web 操作台（把下面的命令变成按钮）
python -m quant ui

# 手动联网更新（唯一允许访问网络的入口；失败自动限流重试 + 降级）
python -m quant data update --source baostock --pool hs300 --limit 30 --start 2018-01-01 --end 2025-12-31
python -m quant data update --source akshare  --pool hs300 --limit 30 --start 2018-01-01 --end 2025-12-31
python -m quant data status --pool hs300

# 回测（只读本地库）
python -m quant backtest --strategy dual_ma --start 2019-01-01 --end 2025-12-31 --limit 30

# 模拟盘（只写本地 SQLite）
python -m quant paper run --date latest --dry-run
python -m quant paper run --date latest
python -m quant paper status

# 报告
python -m quant report --run-id latest
```

完全离线（CI / 无网络）时，用 `--source fixture` 载入 `tests/fixtures/` 中**明确标注的合成测试数据**：

```bash
python -m quant data update --source fixture --pool hs300 --limit 5 --start 2022-01-04 --end 2022-12-30
python -m quant backtest --start 2022-01-04 --end 2022-12-30 --limit 5
python -m quant paper run --date latest
```

`--limit N` 的语义是**按股票池顺序取前 N**（`ORDER BY symbol`），不是随机抽 N，保证可复现。

**配置优先级**：`CLI > 环境变量(QUANT_*) > config.yaml > 内置默认值`，加载后做 schema 校验。
环境变量支持 `QUANT_DATA__LIMIT=10` 通用形式，以及 `QUANT_DB_PATH` / `QUANT_LIMIT` 等简写。

**退出码**：`0` 成功 / `2` 数据缺失 / `3` 网络失败 / `4` 校验失败 / `1` 其他错误。

---

## 2. 目录结构

```
src/quant/
  cli.py                 全部子命令 + 退出码（含 ui 本地操作台）
  webui.py               本地 Web 操作台：命令白名单、串行执行、HTTP 接口
  web/                   index.html / app.css / app.js（零 CDN 静态页面）
  config.py              优先级合并 + schema 校验
  errors.py              DataUnavailableError / NetworkFailureError / ...
  data/
    sources.py           代码格式契约、BaostockSource / AKShareSource / FixtureSource、
                         retry_call 指数退避、DataRouter 降级链
    store.py             SQLite schema、board CHECK、幂等 UNIQUE、分页读写、审计
    update.py            股票池/行业/日历/行情更新、expected/actual/missing 审计
    calendar.py          交易日历落库与查询（禁止 bdate_range）
  strategy/base.py       Strategy 接口 + 强制 no-lookahead 校验 + 前复权换算
  strategy/dual_ma.py    双均线(20/60) + 波动率目标仓位，输出目标权重
  backtest/costs.py      佣金/印花税/滑点/最大可买手数（回测与模拟盘共用）
  backtest/rules.py      T+1 / 100 股整数倍 / 涨跌停 / 停牌
  backtest/execution.py  共享执行内核：T-003 固定事件顺序
  backtest/engine.py     事件驱动主循环（按交易日推进、按日分页读行情）
  risk/position.py       单票/总仓位上限 + 回撤刹车状态机
  paper/engine.py        事务内「先结算再下单」、幂等、dry-run、状态机
  report/metrics.py      指标公式（夏普/胜率/换手/成本占比/分段）
  report/render.py       Markdown + CSV + PNG、程序化「假设与限制」、latest 指针
```

---

## 3. 硬约束实现对照

| 编号 | 实现位置 |
| --- | --- |
| R-001 禁止付费 API | 仅有 Baostock / AKShare 两个适配器，`sources.py` |
| R-002 免费源分工 | Baostock 主日线、AKShare 行业+备用+交易日历备用 |
| R-003 失败顺序 | `DataRouter.fetch_daily`: `local_cache → baostock → akshare → DataUnavailableError` |
| R-004 回测不联网 | `BacktestEngine` / `PaperEngine` 直接读 `Store`，从不构造网络源；操作台只把白名单 CLI 命令包成 HTTP，不改变执行路径 |
| R-005 禁止填补 | `_finalize` / `store.upsert_daily` 遇 NaN 或非正价格直接报错；`to_qfq` 剔除缺失 adj_factor 并记 warning |
| R-006 禁止未来函数 | `Strategy.generate_signals` 内强制 `check_no_lookahead`；信号 T 日生成、`valid_from=T+1` |
| R-007 不接实盘 | 无券商/下单网关；`paper_trades` 只是本地表 |
| R-008 测试离线 | `pytest.ini` 默认 `-m "not network"`；网络测试标记 `@pytest.mark.network` |
| R-009 轻量 | 仅 pandas/numpy + 标准库 argparse，不用 backtrader/vnpy/qlib |
| R-010 不询问、最简方案 | 所有取舍写入下面「假设与限制」；默认取最简单可运行实现（股票池 hs300 当前成分、降仓式刹车、SQLite 单库） |
| R-011 程序化假设 | `report/render.py::build_assumptions` 按运行时 cfg + 审计结果生成，并落 `assumptions.json` |
| R-012 限流退避 | `retry_call`: `base * 2**retry_count`，`max_retries` 可配；耗尽后 `RetryExhausted` → 降级下一源 |
| R-013 分页 | `Store.iter_daily` / `iter_daily_on_date` keyset 分页；`page_size` 默认 500；策略历史用有界 `deque` |
| R-014 board 枚举 | `universe.board CHECK(board IN (...))`；`get_universe(main_only=True)` 首版只取 MAIN |
| T-001 代码格式 | `sources.py` 顶部「转换契约」注释 + `normalize_symbol/to_baostock/to_plain` |
| T-002 复权口径 | 存不复权 OHLCV + `adj_factor`；信号/均线用 `raw × adj_factor`；成交/涨跌停/滑点用不复权 |
| T-003 事件顺序 | `ExecutionContext`: `settle → mark_to_market → append_history/target_weights → place_orders` |
| T-004~T-008 | `rules.py` + `costs.py`（含 T-006 含手续费最大手数公式） |
| T-009 涨跌停 | 仅主板 ±10%，非主板在 universe 打标签并剔除；涨停买/跌停卖不成交；clip 到涨跌停区间 |
| T-010 停牌 | 无行情或 `volume=0` → 顺延最多 3 个交易日 → `expired`；持仓按最后有效收盘价，`stale_price=1` |
| T-011 交易日历 | `trade_calendar(date,is_open,source)`；T+1、顺延、样本外切分全部查表 |
| D-001~D-003 | 数据不可用抛异常；涨跌停为规则近似；报告「假设与限制」由程序生成 |

---

## 4. 交易语义摘要

- 内部代码 `600000.SH`；Baostock `sh.600000`；AKShare `600000`。
- 佣金万 2.5（最低 5 元）、印花税卖出单边 0.05%、滑点万 1（买向上/卖向下）。
- 成交价 `clip(open × (1±slippage), 跌停价, 涨停价)`；涨停买入 / 跌停卖出不成交。
- 买入 100 股整数倍；现金不足按 `floor((cash - 预估费用)/(open×(1+slippage))/100)×100` 成交，
  T+1 用实际现金修正差额，不二次调仓。
- 同日 pending 订单按 **SELL 优先、再按代码排序**撮合（确定性，写入报告假设）。
- 风控：单票 ≤10%、总仓位 ≤80%；回撤 ≥10% → 上限降至 40%（降仓策略，非暂停开仓）；
  回撤修复 ≤5% **或** 刹车满 `release_days`(默认 10) 个交易日解除。

### 模拟盘

`paper run --date latest` 中 `latest` = **本地库 `daily_price` 的最大交易日**（不是自然日）。
同一事务内：① 结算所有 `valid_from <= latest` 的 pending；② 基于 latest 生成新 pending。
`--dry-run` 执行完整流程后 `ROLLBACK`，不写库。状态机
`pending → filled | rejected | cancelled | expired`，`paper_orders` 有
`UNIQUE(trade_date,strategy,symbol,side)`、`paper_trades` 有 `UNIQUE(order_id,fill_date)`。
数据未到 T+1 时 pending 保留并输出 `waiting_for_next_bar`，不报错。

若交易日历里**还没有 T+1**（例如 `latest` 是库中最后一个交易日），pending 订单的
`valid_from` 写 `NULL` 而非回退成信号当日；等新交易日落库后，由 `trade_calendar`
解析出真正的 T+1 再撮合。这样即使重放历史也绝不会在 T 日以 T 日开盘价成交（R-006）。

---

## 5. 假设与限制（README 版；报告中的同名章节由程序自动生成）

> 报告里的「假设与限制」不是这段文字，而是 `build_assumptions()` 依据**本次实际运行的
> config 与数据审计结果**生成，并同时写入 `assumptions.json` / `run_meta.json`。此处的文字用于说明设计取舍。

1. **幸存者偏差（重要）**：股票池默认「沪深300 当前成分」。当前免费数据源可能缺失退市股票，
   用今天的成分回溯历史会系统性高估收益。报告会**程序化显示**这条声明。首版不提供历史成分快照。
2. **复权口径**：`adj_factor` 由免费源的前复权/不复权收盘价之比推导（只有相对比例有意义），
   不是交易所官方复权因子；不含分红再投资现金流。
3. **涨跌停近似**：首版只做沪深主板 ±10%，按昨收四舍五入到分；ST 5%、创业板/科创板 20%、
   北交所 30% 均不支持，相关标的在 `universe.board` 打标签后默认剔除。
   样本首日无昨收时用当日开盘价近似昨收（属交易规则近似 D-002，不是行情填补）。
4. **停牌**：无行情或 `volume=0` 视为停牌；只做 3 日顺延与 `last valid close` 估值，
   不模拟真实复牌集合竞价。
5. **日历**：交易日历来自 Baostock `query_trade_dates`（AKShare 备用）；离线 fixture 模式下
   由已落库的真实交易日派生并标记 `source='fixture'`，**从不使用 `pd.bdate_range`**。
6. **样本外**：按日历交易日把最后 20% 划为样本外。固定参数跑**全程**后只做分段报告，
   禁止「前 80% 调参 + 后 20% 单独跑」的两段式泄漏。首版未实现 walk-forward。
7. **过拟合风险**：示例 `dual_ma` 只是可运行范例，参数未经任何优化，也不得为让回测好看而调参。
   任何参数搜索都应只用前 80%，并把过拟合风险记录在此。
8. **数据缺失**：单个标的全区间无行情时抛 `DataUnavailableError`；部分缺失按 T-010 停牌规则处理，
   并在 `expected_days / actual_days / missing_days` 审计中披露。禁止 NaN 前向填充。
9. **未实现**：分红送转现金处理、融券、T+0、集合竞价微观结构、真实逐笔撮合、实盘网关、miniQMT。
10. **指标**：夏普 `sqrt(252)`、无风险利率 0；胜率按 FIFO 已平仓交易；换手率 `成交额/日均权益`；
    成本占比 `总费用/初始资金`。

---

## 6. 测试

```bash
pytest                       # 默认跳过 network 标记，全离线
pytest -m network             # 显式联网测试（需要 Baostock/AKShare 可访问）
bash scripts/smoke_offline.sh # 离线端到端：更新 -> 回测 -> 模拟盘(dry-run/真实/幂等) -> 报告 -> pytest
```

`pytest.ini` 注册 `network` marker 并用 `addopts = -m "not network"` 默认跳过。
离线测试只使用 `tests/fixtures/*.csv` 与临时 SQLite，不依赖网络、不修改真实行情库。
fixture 为确定性合成数据（12 个标的 × 259 个交易日，含 1 个停牌样本、1 个涨停样本、
非 MAIN 板块标签、2 个非 1.0 的 `adj_factor`），写入 DB 时 `source='fixture'`，
绝不与真实行情混淆。

测试覆盖：成本/整数手/涨跌停/停牌、无未来函数（含「未来价格冲击不改变历史订单」）、
回测分段与固定参数全程跑、模拟盘 T+1 结算与幂等/dry-run、风控刹车状态机、
数据错误与 board CHECK、指数退避重试与降级链、CLI 帮助与退出码、报告程序化假设、
指标公式、离线端到端闭环，以及 `test_webui.py` 的命令白名单 / 参数注入 / 路径穿越 /
Host 校验 / HTTP 端点 / 报告读取 / 离线演示区间钳制（28 个用例，全部离线）。

**本机验证结果**：`pytest` → `110 passed, 1 deselected`（约 35 秒）；
`bash scripts/smoke_offline.sh` → 7 步全部成功 + `SMOKE OK`，全程无网络、无付费 API、无真实委托。

---

## 7. 交付边界

**未接入实盘、未使用付费 API、miniQMT 不在首版范围。**
所有报告都会显示这一声明。数据只写入本地 `data/quant.db` 与 `reports/`。
Web 操作台只是本地命令的可视化入口：只监听 `127.0.0.1`、不开放外部访问、不新增依赖、不产生任何 API 费用。

界面定调：极简简约风 —— 大量留白、低饱和黑白灰配单一主色，正文与标题统一无衬线字体（命令和数字用等宽保证对齐），层次只靠字号、字重、间距与 1px 细线建立。无渐变、噪点、阴影、轮播、装饰图形与复杂动画；默认浅色，右上角可切换深色，支持 `prefers-reduced-motion` 降级与 375 / 768 / 1440px 三档响应式布局。字体全部使用本机字体栈，零 CDN，断网可用。

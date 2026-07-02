# 当前状态 (STATE.md)

> 每次工作会话**结束时**更新本文件。读它 = 立刻知道项目停在哪、下一步干啥。

---

## 当前阶段

**Phase 1 后半段**

---

## 已完成

- [x] GitHub 私有仓库 `a-share-quant` 建立
- [x] clone 到本地，README / .gitignore(Python) 就位
- [x] PROJECT / DECISIONS / STATE 三份活文档创建并放入仓库根目录
- [x] Python 3.11 虚拟环境 `.venv` 建立，基础依赖 akshare / pandas / pyarrow 安装完成
- [x] Phase 1 数据管道：单只股票取数、沪深300批量取数、十年后复权日线数据落盘
- [x] 数据质检：生成逐只明细与质检报告，极端值确认为真实市场事件
- [x] 回测引擎设计规范：`docs/BACKTEST_DESIGN.md` 冻结为 V3.0.2 实施基准
- [x] src 骨架、hfq 隔离、未复权 L1 面板重建、交易日历完成
- [x] L1 四关验收：8 个单元测试通过、699453 正常行、除权日跳变坐实未复权、`event_ts` / `available_at` 为 `+08:00` 时区 timestamp
- [x] security master：300/300 建档，板块由代码前缀判定可靠，当前 ST 真实快照为 0；历史时点 ST 仍缺，待 Tushare 等时点数据源补齐
- [x] PITDataPortal：作为唯一 PIT 只读访问层，按 `asof_ts` 与字段级 `available_at` 做 as-of 过滤，缺 `available_at` fail-closed
- [x] Phase 0 整体完成：四件套齐备（未复权 L1 面板、security master、交易日历、PITDataPortal as-of 闸门），全部验收通过、可复现
- [x] 事件驱动时钟骨架：按真实交易日历逐日推进；回调侧只能用固定 `asof_ts` 的 ctx.portal，不能自行传 `asof_ts`；双道防未来验证完成（Portal `available_at` 过滤 + `daily_bar_raw.trade_date <= T` 兜底）；22 个单元测试通过
- [x] 哑策略 + `OrderIntent`：固定确定性、可复现、非收益型订单发生器完成；可交易池只用当日可见字段（`daily_bar_raw.trade_date == T` 与 `trade_status == 正常`），未使用污染字段 `security_master.is_st`；选股防未来验证通过；26 个单元测试通过
- [x] 执行引擎：T+1 开盘成交 + 账本骨架完成；T 日决策、T+1 开盘成交；成交只取 T+1 `open`，不取次日 high/low/close/volume 等全天数据；无 T+1 或无开盘价不顺延偷价，分别分类为 `NO_NEXT_SESSION` / `NO_OPEN_PRICE`；30 个单元测试通过
- [x] 涨跌停开盘拒单：按板块限幅（主板 10% / 双创 20% / 北交所 30%），前收盘通过 PIT 同口径取得，涨跌停价用 `Decimal` 四舍五入到分；开盘触涨停拒买、触跌停拒卖；新股前 5 个交易日不设涨跌幅限制；34 个单元测试通过
- [x] 停牌不成交处理：停牌 `SUSPENDED / NO_TRADE_SUSPENDED` 与数据缺失 `NO_OPEN_PRICE` 显式区分；停牌订单零成交；持仓冻结待持仓账本后补测；36 个单元测试通过
- [x] 交易费用：`FeeSchedule` 按生效日 resolve；佣金可配置，默认万 2.5、单笔最低 5 元；印花税卖出单边 0.05%；过户费万 0.1；所有金额用 `Decimal` 到分；42 个单元测试通过
- [x] L2 公司行动台账(best-effort)：沪深300 295/300 有记录，总 3075 条；`CASH_DIVIDEND=2756` / `STOCK_DIVIDEND=300` / `RIGHTS_ISSUE=19`；标记 `EXPLORATORY_TAINTED`；贵州茅台 2026-06-26 分红约 28.02 元/股，与 L1 除息缺口约 2.56% 互相印证；45 个单元测试通过
- [x] 审计修复：印花税历史费率 + 涨跌停版本化完成；印花税 2015-2023 卖出曾被静默算 0，已修为 2008-09-19 起卖出 0.1%、2023-08-28 起卖出 0.05%，且 fail-closed 生效；涨跌停限幅改为按板块 + 生效日解析；四个历史限幅测试通过；50 个单元测试通过
- [x] 审计修复包一完成（fb0468c/6becd18）：`calendar` 改名为 `market_calendar`、数据依赖测试可移植化、统一入口 `run_tests.py`、阶段标签补齐；discover 测试入口恢复
- [x] 审计修复包二完成（235db56/5b07e36）：CA `available_at` 改公告日基准、限价检查显式留痕、买侧 lot size 引擎级强制；`.venv\Scripts\python.exe run_tests.py` 全量 58 个测试全绿，0 skipped

---

## 全面审计结论(2026-07,双向独立验证:Claude克隆仓库直查 + Codex本地取证,结论互相印证)

1. 核心引擎 33 个 fixture 测试独立环境验证通过，31 commits 完整。
2. EX 矩阵覆盖：EX-001/002/003/006/007 已完成；EX-004/010/011/012/013/014 在既定计划内（持仓账本 + CA 步）；EX-005(lot size)/EX-008(容量约束)为计划外漏项；EX-009/LT-009 缺显式测试；LT-011/LT-002-Phase1 作用域未建。
3. 代码级发现：限价检查 fail-open（数据缺失即跳过检查）；CA 台账 available_at=ex_date 致除权日参考价修复被自身闸门阻塞（announcement_date 100% 非空、领先 0-17 天，修复可行）；测试套件不可移植（数据依赖测试无 skip）；calendar 撞标准库名。

---

## 进行中

- [ ] Phase 1 — 事件驱动引擎(轨道A)
- [ ] Track B：启动券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步（按顺序）

1. 持仓 + 现金账本：先设计讨论后实现，需覆盖 EX-004/009/010 与 LT-009/011 显式测试；其后 lot size 卖侧规则、容量约束 EX-008、CA 账本逻辑 EX-011~014、LT-002 Phase1 作用域，Phase 1 收官

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金

---

## 备注

- GitHub 国内访问需代理；如不稳可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的那层执行。

# 当前状态 (STATE.md)

> 每次工作会话**结束时**更新本文件。读它 = 立刻知道项目停在哪、下一步干啥。

---

## 当前阶段

**Phase 1 — 事件驱动引擎(轨道A)**

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

---

## 进行中

- [ ] Phase 1 — 事件驱动引擎(轨道A)
- [ ] Track B：启动券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步（按顺序）

1. 执行引擎续：停牌不成交处理

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金

---

## 备注

- GitHub 国内访问需代理；如不稳可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的那层执行。

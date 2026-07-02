# a-share-quant

A 股日频量化回测系统。当前 Phase 1 已完成毕业审计：项目重点是构建可审计、可复现、可防未来函数的事件驱动回测基础设施，而不是证明任何策略收益。

## 核心架构

系统以 PIT 双时间戳数据闸门为入口，所有数据读取必须满足 `available_at <= decision_ts`；事件驱动引擎按真实交易日推进，覆盖 T+1 开盘执行、涨跌停拒单、停牌不成交、交易费用、容量约束等 A 股真实摩擦；FIFO 持仓账本是现金、lot、应收与公司行动处理的事实源。LT-002 未来突变不变量和快路径等价哨兵用于防止缓存层或数据突变污染历史闭合输出，当前统一测试入口保留 105 个测试哨兵。

## 快速开始

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_tests.py
.\.venv\Scripts\python.exe run_backtest.py
```

## 目录结构

| 路径 | 内容 |
|---|---|
| `src/data/` | akshare 适配、L1/L2 数据构建、PITDataPortal、数据契约测试 |
| `src/engine/` | 事件时钟、哑策略、订单锁定、T+1 执行、账本、公司行动处理、runner |
| `src/market_calendar/` | 交易日历与交易日辅助函数 |
| `docs/` | 回测规范、历史归档与项目说明材料 |
| `data/` | 本地数据、manifest 与回测输出；大体量 parquet / backtest output 不入 git |
| `scripts/` | 可重复运行的辅助脚本，如净值曲线可视化 |

## 关键文档

- [docs/BACKTEST_DESIGN.md](docs/BACKTEST_DESIGN.md)：强制工程规范、验收标准与实现顺序
- [DECISIONS.md](DECISIONS.md)：只追加的架构与口径决策日志
- [PROJECT.md](PROJECT.md)：项目宪法与当前事实来源
- [STATE.md](STATE.md)：会话级当前状态快照

## 免责与污染标记

当前 universe 为“当前 HS300 非时点成分”，存在幸存者偏差；L2 公司行动台账仍标记 `EXPLORATORY_TAINTED`。仓库内回测结果仅用于验证引擎链路、账本不变量和防未来机制，不代表投资表现，不构成投资建议。

# 当前状态 (STATE.md)

> 每次工作会话**结束时**更新本文件。读它 = 立刻知道项目停在哪、下一步干啥。

---

## 当前阶段

**Phase 0 — 数据与时钟地基**

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

---

## 进行中

- [ ] Phase 0 数据与时钟地基
- [ ] 四个 gate 决策定稿（见 `PROJECT.md` §3）
- [ ] Track B：启动券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步（按顺序）

1. security master：证券生命周期（上市日 / 退市日）、板块、ST 状态
2. 填定 gate 决策（券商档位、执行层 Windows 部署、确认数据源与频率）
3. Track B：继续推进券商开户 / miniQMT 权限 / 程序化报备流程

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金

---

## 备注

- GitHub 国内访问需代理；如不稳可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的那层执行。

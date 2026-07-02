# 当前状态 (STATE.md)

> 每次工作会话结束时更新本文档。读完 = 立刻知道项目停在哪、下一步干什么。

---

## 当前阶段

**Phase 1 技术工作完成,待全局扫描审查后毕业**

---

## 已完成

- [x] GitHub 私有仓库 `a-share-quant` 建立，本地仓库与三份活文档就位
- [x] Phase 0 数据底座完成：未复权 L1 面板、security master、交易日历、PITDataPortal as-of 闸门
- [x] Phase 1 事件驱动引擎主链完成：EventDrivenClock、DummyRebalanceStrategy / DummyStrategy、T+1 开盘执行、涨跌停拒单、停牌不成交、交易费用、容量约束、PortfolioLedger、CorporateActionHandler、除权日限价参考价
- [x] Phase 1 后半段完成：EX-001 至 EX-014、LT-011 全部完成；第 7.5 步除权日涨跌停参考价 TODO 已清除；基线曾为 `.venv\Scripts\python.exe run_tests.py` 全量 102 测试全绿
- [x] 第8步 LT-002 + 十年回测runner(0ab4e28)、快路径等价性证明、amount单位校验(1495327)
- [x] 当前统一测试入口 `.venv\Scripts\python.exe run_tests.py`：105 个测试全绿，0 skipped

---

## 进行中

- [ ] Phase 1 全局扫描审查
- [ ] Track B：券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步

净值曲线可视化→全局扫描审查→Phase 1毕业→Phase 2(PIT复权服务+截面动量+黄金切片)

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金
- 历史时点 ST 状态仍需更完整 PIT 数据源补齐（如 Tushare）

---

## 备注

- GitHub 国内访问需要代理；如不稳定可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的这一层执行。

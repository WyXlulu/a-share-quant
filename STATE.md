# 当前状态 (STATE.md)

> 每次工作会话结束时更新本文档。读完 = 立刻知道项目停在哪、下一步干什么。

---

## 当前阶段

**Phase 1 已毕业(全局清册审计通过),Phase 2 未启动**

---

## 已完成

- [x] GitHub 私有仓库 `a-share-quant` 建立，本地仓库与三份活文档就位
- [x] Phase 0 数据底座完成：未复权 L1 面板、security master、交易日历、PITDataPortal as-of 闸门
- [x] Phase 1 事件驱动引擎主链完成：EventDrivenClock、DummyRebalanceStrategy / DummyStrategy、T+1 开盘执行、涨跌停拒单、停牌不成交、交易费用、容量约束、PortfolioLedger、CorporateActionHandler、除权日限价参考价
- [x] Phase 1 后半段完成：EX-001 至 EX-014、LT-011 全部完成；第 7.5 步除权日涨跌停参考价 TODO 已清除；基线曾为 `.venv\Scripts\python.exe run_tests.py` 全量 102 测试全绿
- [x] 第8步 LT-002 + 十年回测runner(0ab4e28)、快路径等价性证明、amount单位校验(1495327)
- [x] 毕业审计完成：双向独立验证，EX/LT 全矩阵对号，快路径截断代码级确认
- [x] 清册整备完成(dd77030)：requirements、遗留归档、README、PROJECT 对齐、规范勘误、可移植性收尾
- [x] 终态生产蓝图入档(da6a5bf)：日频生产工作流、UI 五页面、三条架构不变边界、延后项 retrofit 接缝
- [x] 当前统一测试入口 `.venv\Scripts\python.exe run_tests.py`：105 个测试全绿，0 skipped

---

## 进行中

- [ ] Phase 2 启动准备
- [ ] Track B：券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步

Phase 2:①PIT复权服务(用L2台账按公告日可见性还原真实收益,解锁多日收益特征)→②第一个规则信号:截面动量(无参数拟合,验证三层链路与IC评估语言)→③黄金切片(十几只可人工核验票,目标首个BACKTEST_VALIDATED)→全程EXPLORATORY_TAINTED直至时点成分数据接入

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金
- 历史时点 ST 状态仍需更完整 PIT 数据源补齐（如 Tushare）

---

## 备注

- GitHub 国内访问需要代理；如不稳定可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的这一层执行。

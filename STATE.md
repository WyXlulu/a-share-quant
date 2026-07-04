# 当前状态 (STATE.md)

> 每次工作会话结束时更新本文档。读完 = 立刻知道项目停在哪、下一步干什么。

---

## 当前阶段

Phase 2 第一步(PIT复权服务)进行中,接近第一步收尾

---

## 已完成(Phase 2 新增,接续Phase 1毕业f29d083)
- [x] 共享除权除息参考价规则模块 src/domain/corporate_action_pricing.py(08f8c24):官方公式(上交所4.3.2/深交所4.4.2,深交所§4.4.2已从源文件核实,检索2026-07-03),含配股项,按2023-02-17版本化、早于此fail-closed
- [x] 参考价组合测试(a430796):七条期望值全官方手算非实现反推,含D/s/r三者非零守门算例(分母1+s+r,期望107/15)
- [x] execution.py迁移消费共享模块(6c044b5):删私有公式兑现决策1,茅台1184.08/送转6.67双路径逐值不变
- [x] 可见性判定同源抽取+PITAdjustmentService本体第一刀(968cb10):evaluate_corporate_action_visibility单源(domain层),Service三方法(daily/cumulative/factor),逐点OK/BLOCKED/NO_DATA,security-date级硬阻断+反向守门,evidence_status钉死EXPLORATORY_TAINTED
- [x] Service判定层asof口径修正(f4c012e):判定改用derivation_asof_ts(非09:00),修复盘后除权日被误BLOCK;handler侧09:00边界逐点不变

---

## 进行中

- [ ] Phase 2 启动准备
- [ ] Track B：券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步

Phase2第一步收尾测试批:①LT-002B(cutoff=derivation_asof_ts)②LT-002C复用(拒VENDOR_ADJUSTED)③快慢portal等价哨兵延伸到复权服务④UNPROCESSED_BOUNDARY直接补测+handler边界哨兵 → 第二步:截面动量信号 → 第三步:黄金切片(排除5只次新股001280/688047/688506/688521/688981,目标首个BACKTEST_VALIDATED/GOLDEN_SLICE_PIPELINE)

---

## 收官审计待验清单(转Public直查,累积)
- execution.py迁移源码逐字(6c044b5):确认私有_ex_right_reference_price已删
- 可见性单源逐字(968cb10):corporate_action_visibility.py唯一判定处、handler内无漏网内联
- APPLICATION_CUTOVER_TIME=09:00常量(f4c012e):评估是否从判定函数外置
- handler边界哨兵(测试批加):锁死09:00返回UNPROCESSED_BOUNDARY防未来无声改动

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金
- 历史时点 ST 状态仍需更完整 PIT 数据源补齐（如 Tushare）

---

## 备注

- GitHub 国内访问需要代理；如不稳定可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的这一层执行。

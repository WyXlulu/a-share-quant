# 当前状态 (STATE.md)

> 每次工作会话结束时更新本文档。读完 = 立刻知道项目停在哪、下一步干什么。

---

## 当前阶段

Phase 2 第一步(PIT复权服务)已收官,第二步(截面动量信号)未启动

---

## 已完成(Phase 2,接续Phase 1毕业f29d083)
- [x] 共享除权除息参考价规则模块(08f8c24)+组合测试(a430796):官方公式含配股项、版本化fail-closed、D/s/r三者非零守门算例
- [x] execution.py迁移消费共享模块(6c044b5):兑现决策1,茅台/送转双路径逐值不变
- [x] PITAdjustmentService本体+可见性判定同源抽取(968cb10):三方法,security-date级硬阻断+反向守门,evidence_status钉死EXPLORATORY_TAINTED
- [x] Service判定层asof口径修正(f4c012e):判定用derivation_asof_ts,handler侧09:00边界逐点不变
- [x] Service哨兵加固(21aee51):price_basis校验修复(拒vendor-adjusted)+LT-002B/LT-002C/快慢portal等价/未来CA不可见/handler边界五组测试;121测试全绿

---

## 进行中

- [ ] Phase 2 启动准备
- [ ] Track B：券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步

Phase2第二步:截面动量信号——第一个规则信号(无参数拟合),消费复权收益,验证特征→信号→OrderIntent三层链路与IC评估语言;全程EXPLORATORY_TAINTED → 第三步:黄金切片(排除5只次新股001280/688047/688506/688521/688981,目标首个BACKTEST_VALIDATED/GOLDEN_SLICE_PIPELINE)

---

## 收官审计待验清单(转Public直查,累积)
- execution.py迁移源码逐字(6c044b5):私有_ex_right_reference_price已删
- 可见性单源逐字(968cb10):corporate_action_visibility.py唯一判定处、handler内无漏网内联
- APPLICATION_CUTOVER_TIME=09:00常量(f4c012e):评估是否从判定函数外置
- price_basis校验路径(21aee51):确认_assert_raw_unadjusted覆盖Service所有daily_bar读取入口,无旁路

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金
- 历史时点 ST 状态仍需更完整 PIT 数据源补齐（如 Tushare）

---

## 备注

- GitHub 国内访问需要代理；如不稳定可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的这一层执行。

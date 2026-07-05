# 当前状态 (STATE.md)

> 每次工作会话结束时更新本文档。读完 = 立刻知道项目停在哪、下一步干什么。

---

## 当前阶段

Phase 2 第二步(截面动量信号)已收官,第三步(黄金切片)未启动;第二步收官全库审计待做

---

## 已完成(Phase 2,接续Phase 1毕业f29d083)
- [x] 第一步 PIT复权服务收官:参考价模块+组合测试(08f8c24/a430796)、execution迁移兑现决策1(6c044b5)、PITAdjustmentService本体+可见性同源(968cb10)、asof口径修正(f4c012e)、price_basis校验+五组哨兵(21aee51)
- [x] 第二步 截面动量信号收官:
  - 信号本体(ec53288):12-1动量(动量窗口231=252回看skip21),消费cumulative_adjusted_return,BLOCKED/NO_DATA票剔除不混入排名,全程EXPLORATORY_TAINTED
  - IC评估+LabelDataPortal隔离(1d9a177):RankIC/ICIR/分位单调性,未来收益经LabelDataPortal与信号函数结构隔离,block bootstrap非IID置信区间(block=21),LT-003成熟度门禁
  - 信号驱动策略接执行边界(1b1affc):SignalDrivenMomentumStrategy等权top_n,只产OrderIntent经既有锁定/执行边界,执行层零改动(train-serve同源),BLOCKED不冻结已有持仓正常调仓
- [x] 当前全量基线:136测试全绿

---

## 进行中

- [ ] Phase 2 启动准备
- [ ] Track B：券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步

第二步收官全库审计(转Public,克隆直查收官审计待验清单+第二步新代码结构断言+独立复跑136测试)→ 第三步:黄金切片(十几只可人工核验票,排除5只次新股001280/688047/688506/688521/688981,目标首个BACKTEST_VALIDATED/GOLDEN_SLICE_PIPELINE)

---

## 收官审计待验清单(转Public直查,累积)
- execution.py迁移源码逐字(6c044b5):私有_ex_right_reference_price已删、无第二份参考价公式
- 可见性单源逐字(968cb10):corporate_action_visibility.py唯一判定处、handler内无漏网内联、两处分类路由确调用共享函数
- APPLICATION_CUTOVER_TIME=09:00常量(f4c012e):评估是否从判定函数外置
- price_basis校验路径(21aee51):_assert_raw_unadjusted覆盖Service所有daily_bar读取入口、无旁路
- 信号防泄露结构隔离(1d9a177):信号函数确无未来收益通路、LabelDataPortal与信号双向隔离
- 执行层零改动核实(1b1affc):动量信号接入后执行/账本/CA/runner层确未改动

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金
- 历史时点 ST 状态仍需更完整 PIT 数据源补齐（如 Tushare）

---

## 备注

- GitHub 国内访问需要代理；如不稳定可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的这一层执行。

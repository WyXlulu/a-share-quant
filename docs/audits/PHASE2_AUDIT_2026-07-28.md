# Phase 2 收官全面审计报告

- 审计对象：本地仓库 `a-share-quant`，工作树状态 = `2ee2207` + 仅行尾符差异（见 E-01）
- 审计日期：2026-07-28
- 审计姿态：找错，不确认。所有"无问题"结论均单独列出并注明检出手段的覆盖范围。
- 只读：未修改、未提交、未推送任何文件。唯一副作用是在云端副本上跑了一次 `run_tests.py`（生成 `.pyc`），未触碰你的磁盘。

---

## 〇、总判定（先给结论）

**黄金切片的"数据证据链"可信，"报告表述"与"门禁机制"不可信。**

三句话：

1. **CA 证据链是本项目质量最高的部分，我逐条复核通过。** 252 份官方 PDF 与 `listing_manifest.csv` 的 sha256 集合双向差集为 0；`ca_verified_88.csv`、`listing_manifest.csv` 的哈希与冻结 manifest 记录一致；`manifest_hash` 独立重算一致；88 行 → 76 条冻结记录逐字段比对 **0 处不一致**；抽查 4 份 PDF 原文（超出要求的 3 份）金额/日期**逐字符相符**，包括 000651 的 `2.784787` 折算值与 601318 的 `1.3946`，两者都在公告正文里原样写着。人工核验没有抄错、没有漏条。

2. **但已交付的 `report.md` 里有一条会改变结论的实现缺陷（G-04），另有两条会导致读者过度解读（G-02/G-03）。** `quantile monotonicity: NOT_MONOTONIC` 是分位分组余数规则的产物，不是数据事实——我独立复算完全复现了报告的五个均值与样本数，换一个同样合理的余数分配规则，顶端反转消失、前三档单调。DECISIONS 已经用"分位非单调"作为"IC 不得援引为 alpha 证据"的理由之一，**保守方向是对的，但依据不成立，需要重写**。

3. **十年回测层面有三条会产出错误数字的缺陷（M-03 配股字段名、M-04 过户费生效日、X-04 同日多笔分红去重）。** 它们都不影响黄金切片（切片内 76 条 CA 无配股、无重复键），但意味着 Phase 1 冻结的"十年四类账本哈希基线"是**可复现的，不是正确的**。

**判定：在修完 G-01～G-05 之前，不建议把黄金切片对外表述为"管线诚实已验证"。M-03/M-04/X-04/D-02 必须在 Phase 3 全市场化之前修，因为它们在全市场规模下必然产出错值。**

发现总数 **57 条**：致命 0、严重 20、中等 27、轻微 10。（我把三个 subagent 报的 4 条"致命"全部降级——降级理由逐条写在对应条目里，都是"当前无生产消费者"或"黄金切片路径不可达"。）

---

## 一、微观层：逐文件实现正确性

### 严重

**M-01｜CA 在除权日不可见时，复权服务静默产出未除权收益率；`UNPROCESSED_BOUNDARY` 在该路径不可达**
`src/features/pit_adjustment_service.py:206,222,684-697`

- 证据：`_block_reason(actions, derivation_asof)` 遍历的 `actions` 来自 `self.portal.query("corporate_actions", derivation_asof, ...)`，portal 已在物理层按 `available_at <= asof` 过滤。因此 `available_at > asof` 的行**根本进不了判定层**，而 `UNPROCESSED_BOUNDARY` 的定义恰恰要求 `available_at > asof`。CA 不可见 → `ca_rows` 为空 → `reference_price = previous_close` → 产出 `status=OK` 的未除权收益率。
- 关联缺陷：`src/data/corporate_action_availability.py:25-33` 的路径 C（`disclosure_time_known` 字段缺失 → 直接返回原始 `available_at`，无 `available_at < ex_date` 守卫）在生产 L2 上覆盖 **3075/3075 行**——唯一有该守卫的路径 B 实际覆盖率是 0。
- 黄金切片影响：**无**。方案 X 对 76 条全部走 `disclosure_time_known=False`，`resolve_ca_available_at` 有硬断言 `available_at < ex_date`，76/76 预检通过。
- 十年回测影响：生产 L2 中 `available_at >= ex_date` 的只有 1 条（`601318 / 2015-09-09`，已知口径存疑），且 `PITAdjustmentService` 未参与 Phase 1 十年回测 → **当前未产出错值**。
- 不修的后果：全市场化后一旦引入真实公告时间戳（走路径 A，无守卫），或数据源列名变化，这条通道就会在没有任何告警的情况下打开。A 股分红集中 5-7 月，失真方向系统性为负。
- **置信：已验证**（我直接读了 `_block_reason` 与 `_corporate_actions` 的取数路径确认）。

**M-02｜`adjustment_factor_series` 跨 BLOCKED 日继续累乘，并把缺口后的点标成 OK**
`src/features/pit_adjustment_service.py:356-378`

- 证据：BLOCKED/NO_DATA 点只把自己那一个点置 None，局部变量 `factor` 未失效；下一个 OK 点继续从旧值累乘。缺口日的价格变动被静默跳过，输出仍是 `status=OK` 带数值。
- **降级理由**：`grep -rn "adjustment_factor_series" --include=*.py` 在非测试代码中**零命中**——目前没有任何生产消费者。故从 subagent 报的"致命"降为"严重（潜伏）"。
- 不修的后果：任何未来用它构造后复权价或长期净值的模块，会得到"链条断了却报完好"的因子。这比返回 None 危险，因为下游看到 OK 就会用。
- **置信：已验证**。

**M-03｜执行层读 `rights_ratio` / `rights_price`，生产 parquet 里这两列不存在 → 19 条配股除权日零调整，并报"当日无公司行动"**
`src/engine/execution.py:978-979`（+`:983` 的 `continue`）

- 证据（我实测）：`data/l2_corporate_actions/corporate_actions.parquet` 的 16 列是 `... share_ratio ... rights_price_per_share ...`，**无 `rights_ratio`、无 `rights_price`**。两个 `getattr` 默认值恒定命中 `Decimal("0.00")`，且 `RIGHTS_ISSUE` 分支 `continue` 掉，连 `share_ratio` 都不累加。
- 后果链：四项调整全为 0 → `adjusted_reference == previous_close` → `_limit_reference_context` 返回 `status="NONE"`，即在真实除权日声称"当日无公司行动"。涨跌停带整体不做除权下移。
- 对照：`src/features/pit_adjustment_service.py:713-717` 对同一份数据**做了 schema 适配**（`rights_ratio` 回退 `share_ratio`，`rights_price` 回退 `rights_price_per_share`），且缺 `rights_price` 时 `raise DataContractError`。同一语义两份实现、失败策略相反。
- 黄金切片影响：**无**（76 条冻结记录 `rights_ratio` 全为 0）。十年回测影响：19 条配股行 100% 命中。
- 数值量级（subagent 用生产行实算）：`600030 / 2022-01-27`，10 配 3 @ 14.43，设前收 24.00 → 正确参考价 `(24.00 + 14.43×0.15)/1.15 = 22.7517`，执行层给 `24.00`，**偏高 5.2%**。
- **置信：已验证**（parquet schema 与代码逐行核对）。

**M-04｜过户费生效日写成 2025-04-29，实际是 2022-04-29；且 1900-01-01 起的 0.002% 无依据**
`src/engine/execution.py:239-242`

- 外部事实源：中国结算自 **2022 年 4 月 29 日**起将股票交易过户费由成交金额 0.02‰ 双向下调至 0.01‰（多家媒体同日报道，见文末来源）。代码写的是 2025-04-29。
- 后果：**2022-04-29 至 2025-04-28 近三年，每一笔成交的过户费是真实值的 2 倍**。
- 附带：`EffectiveRate(date(1900,1,1), 0.00002)` 同样无依据——0.02‰ 的统一标准自 2015-08-01 起施行，此前沪市按面值计收、深市不收；回测起点 2015-06-30，首月费率基准是编造的。且此处**没有像印花税那样 fail-closed**（`_resolve_rate` 对 2008-09-19 前会抛错并有测试覆盖）。
- 更糟的是 `src/engine/test_execution.py:104-111` 用 `2025-04-28 / 2025-04-29` 把这个错误日期钉成了"有测试保护的正确行为"，且无任何出处链接。这正是 DECISIONS:140 立的纪律所禁止的形态。
- **置信：已验证**（我自己做了外部检索确认，见文末来源）。

**M-05｜ST/\*ST ±5% 涨跌幅规则在引擎中从未实现过**
`src/engine/execution.py:117-171`（`LimitRuleTable`）

- 证据：`LimitRuleTable` 只按 `board` 解析，全表**没有任何 ST 分支**，也没有退市整理期规则。`is_st` 字段读进 portal 只作污染标记，从不进入限价判定。
- 语义错位：`STATE.md:50` 把这件事框成"2026-07-06 规则由 ±5% 调整为 ±10%、黄金切片不触发"，掩盖了更基础的事实——**在任何历史时期引擎都没实现过 ST 限价**。
- 不修的后果：全市场回测一旦纳入 ST 标的，涨跌停带系统性放宽一倍，本该被拒的委托会成交，而 `limit_check` 会记 `"APPLIED"`（看起来做过检查）。
- **置信：已验证**（全库 grep 确认无 ST 分支）。

**M-06｜实付超过预留时账本凭空创造现金**
`src/engine/portfolio_ledger.py:518-526`

- 证据：`refund = _money(reserved_cash - net_amount)`，随后 `if refund > Decimal("0.00"):` 才回补可用现金。`refund` 为负时整条分支被跳过——现金只扣了 `reserved_cash`，成本基却记了 `net_amount`，差额变成凭空现金。`min(reserved, frozen)` 是第二处静默降级。
- **降级理由**：`reserved_cash = fee_schedule.calculate(..., reservation_price, qty).net_amount`（含费），所以只有成交价 > 预留价才可能触发；而 `reservation_price = price_cap or reference`，需要 `price_cap is None`——即 `SKIPPED_NO_MASTER` / `SKIPPED_NO_RULE` / `EXEMPT_NEW_LISTING` 三条路径之一。黄金切片 12 只票全部有 master 与规则，未触发（`maximum accounting identity deviation: 0.00` 佐证）。故从"致命"降为"严重"。
- 不修的后果：在跑 `_assert_accounting_identity` 的主 runner 里会崩（不是静默错），但在 `src/golden_slice/deterministic_ledger_audit.py:248` 这类不跑该断言的调用方、以及未来实盘 adapter 里，就是静默的错误净值。`PortfolioLedger.assert_invariants()` **完全不校验现金**，抓不到。
- **置信：已验证**（我读了代码确认；subagent 另有实跑复现）。

**M-07｜缺前收盘时，涨跌停 / 容量 / 现金预留三道风控同时静默失效**
`src/engine/execution.py:344-348, 378, 411-432`

- 证据：`reference is None` → `limit_check = "SKIPPED_NO_PREV_CLOSE"`（贴标签但订单继续）；`if locked_quantity > 0 and reference is not None:` 使**容量检查整段跳过**（`adv_window_status` 停在 `NOT_EVALUATED`）；`reservation_price = price_cap or reference` 为 None → `reserved_cash` 保持 0.00 → `reserved_cash > available_cash` 恒为假，`CASH_INSUFFICIENT` 也失效。
- 不修的后果：无可见前收盘的股票（新上市 / 长停复牌 / L1 缺口）可以下任意大小的买单，不受涨跌停、不受 ADV 容量、不预留现金。对照 `CAPACITY_NO_ADV_DATA` 是本文件唯一真正 fail-closed 的拒单路径——正确写法就在隔壁。
- `src/engine/test_open_round_execution.py:86-89` 把 `reserved_cash == 0.00` 断言成了预期行为。
- **置信：已验证**。

### 中等

| ID | 位置 | 问题 | 置信 |
|---|---|---|---|
| M-08 | `portfolio_ledger.py:611-614` | 无日历时 `trade_date + timedelta(days=1)` 推 T+1 可卖日。`calendar=None` 是默认值，`deterministic_ledger_audit.py:248` 就是不传日历的生产构造点。**这是"缺依赖时自行降级"的第五次同类**（DECISIONS:258 记录了第四次），且这次在生产代码里 | 已验证 |
| M-09 | `execution.py:733-735` | CA 表 `DataContractError` → `return pd.DataFrame()` → 与"当日确实无 CA"**完全不可区分**，`limit_reference_status` 记 `"NONE"`。对照 `corporate_action_handler.py:156` 同场景返回 None 并走 `UNPROCESSED_CA` 阻断——同一仓库两套失败策略 | 已验证 |
| M-10 | `corporate_action_handler.py:131-133` | `next_trading_day` 抛 IndexError → `continue`，应收现金**永久滞留 `pending_cash_dividends`，永不入账、无事件、无告警**；净值恒等式因应收仍计入而不会报警 | 已验证 |
| M-11 | `pit_adjustment_service.py:720-721` | `cash_dividend_per_share` / `share_ratio` 为 NaN 时 `_decimal_or_zero` 静默当 0。同一函数 `:715` 对 `rights_price` 缺失是 `raise DataContractError`——**作者知道该守，只是漏了两个字段** | 已验证 |
| M-12 | `pit_adjustment_service.py:267-271` | `cumulative_adjusted_return` 的可见性用 `derivation_asof`、窗口截断只用 `asof.date()`（丢时分），二者无约束关系。正确性完全由调用方约定维持（`run_pipeline.py` 传 `asof=derivation_asof`），不是架构约束 | 已验证 |
| M-13 | `corporate_action_visibility.py:87-94,45` | 全库唯一接受 naive datetime 的模块（`corporate_action_availability.py` 与 `pit_data_portal.py` 都强制 `utcoffset()==+08:00`）；`:45` 把 asof 的墙钟时间与 `APPLICATION_CUTOVER_TIME` 比较时剥离 tzinfo 且不校验时区 | 已验证 |
| M-14 | `pit_adjustment_service.py:585-593 vs 765-775` | `trade_status` 只在开盘价路径检查，`_daily_bars` 的 columns 列表里根本没有它 → 停牌日 `daily_adjusted_return_series` 返回 OK 带数值，`open_to_open` 返回 NO_DATA。同一"bar 是否有效"语义两套规则 | 已验证 |
| M-15 | `execution.py:336-341 → 710-716` | `lock_order`（T 日决策）经 `_limit_reference_context(..., next_session, ...)` 读 **T+1 15:00** 的 CA 表来决定 `limit_reference_status`，而该字段是 `LockedOrder`/`FillLedgerEntry` 的字段、按蓝图必须在 `decision_ts` 完全确定。`test_execution.py:414-446` 用 `available_at="...T+1 15:00"` 把这个未来读固化成期望 | 已验证 |
| M-16 | `backtest_runner.py:356-395` | 停牌估值沿用最近可见收盘价，**无 STALE_MARK 标记、无陈旧上限**。BACKTEST_DESIGN §9.6 明文要求标记并在绩效报告披露 | 已验证 |
| M-17 | `execution.py:117-171` | 涨跌幅规则表 4 个板块的费率与生效日全部硬编码、**零注释零 URL**；主板 `FIRST_DAY_ASYMMETRIC 44%/36%` 挂 `effective_date=2015-01-01` 但该政策实为 2014 年初实施，且 44%/36% 是相对**发行价**而非前收盘（代码里没有发行价概念）。对照 `corporate_action_pricing.py` 有完整规则 URL——标准是知道的 | 数值大概率正确、**出处缺失已验证** |

### 轻微

`settled_cash` 声明后从不更新（`portfolio_ledger.py:45,51`，§9.4 要求分开记录）；送股 `int()` 截断丢弃零碎股且无账本记录（`:288-290`）；拒单释放锁时写 `date.min` 哨兵导致按日期分组的账本审计错位（`:424,466`）；`filled_quantity=0` 仍收 5 元最低佣金（`execution.py:266-268`）；负成交额被静默丢弃而非报错（`:777-781`）；参考价舍入位置散落在调用点而非共享函数内，因子侧与执行侧对同一事件得到不同参考价（`corporate_action_pricing.py:95` vs `execution.py:692-701`）；`ca_events_applied` 返回该证券全部可见 CA 而非窗口内（`:729-754`）；`_field_capabilities` 用 `_first_non_null` 把多证券能力元数据塌缩成标量（`pit_data_portal.py:166-192`）；`_is_st_name` 漏 `SST`/`S*ST`/`PT` 前缀（`akshare_adapter.py:847`）；`trading_days_between(inclusive=False)` 语义与参数名不符。

### 时区与自然日推交易日专项

- **生产代码只有一处违反**：`portfolio_ledger.py:614` 的 `timedelta(days=1)`（见 M-08）。
- 测试 fixture 有四处：`test_momentum_strategy.py:444-450`、`test_cross_sectional_momentum.py:329-335`（`weekday() < 5` 造 253/255 个"交易日"）、`test_backtest_runner_fast_path.py:168`（`pd.bdate_range`，且**快慢路径等价哨兵与 LT-002 未来突变测试共用这个含春节假日的日历**）、`test_capacity_constraints.py:111,115,186`。正例是 `test_pit_adjustment_service.py:799-812` 的显式交易日列表——同名函数在三个文件里两种写法。
- `tools/fetch_cninfo_qyfp.py:453` 用 15 自然日窗配对更正公告（取证启发式，非交易日推导，跨长假会漏配）——存疑。
- 时区：除 M-13 外，全链路 `ZoneInfo("Asia/Shanghai")` + tz-aware，`_require_asia_shanghai_timestamp` 强制 +08:00，**未发现 naive 残留**。

---

## 二、中观层：跨模块接缝

### 严重

**X-01｜双字段路由存在被遗漏的第三个消费者，且消费者侧完全无守卫**
`src/features/pit_adjustment_service.py:720`

- `execution.py:985-994` 有正确分流（优先 `ex_right_cash_deduction_per_share`，缺列/NaN 回退）；`corporate_action_handler.py` 正确只读 `cash_dividend_per_share`。但 `_pricing_inputs` **无条件读 `cash_dividend_per_share`**，既不检查快照用途，也不在 deduction 列存在时优先取用；`_require_columns` 也只要求前者。
- 实测（subagent 用两份真实快照跑）：把 4b EXECUTION 快照喂给 `PITAdjustmentService`，`000651/2021-08-23` 参考价 43.315213 → 43.10、单日收益 0.038665 → 0.043852，**注入约 52bp 虚假单日收益，`status=OK`、零告警**。76 条中 22 条两口径不同。
- **置信：已验证**。

**X-02｜4a 快照的字段名重载只由加载器边界保护，绕过加载器即失效**
`src/golden_slice/snapshot.py:242-245`

- 4a `ADJUSTMENT_ONLY` 快照把 `ex_right_cash_deduction_per_share` 的值写进 `cash_dividend_per_share` 列（代码自己注释为 "Naming debt"）；4b `EXECUTION` 快照两列分开。防线是 `_assert_adjustment_only_snapshot_id`、必需列断言、`_assert_cash_field_split` 的 22/54 硬编码比对——**全在加载器层**。`deterministic_ledger_audit.py:88-91` 就是直接构造 portal 的例子。
- 幸运的是危险方向只有一个：把 4a 表喂给执行器会因缺 deduction 列回退读 `cash_dividend_per_share`，而 4a 该列装的正是折算值——**恰好正确**。反向（执行表 → 复权服务）才是雷。
- **置信：已验证**。

**X-03｜"CA 输入提取"同一语义两份实现且已实际分叉**
`execution.py:969-1003` vs `pit_adjustment_service.py:705-726`

- 分叉点：字段名（`rights_ratio` vs `share_ratio` 回退）、缺失策略（`getattr` 默认 0 vs `raise`）、未知 `action_type`（`continue` 静默 0 vs 照常累加 cash/share）、`ex_right_cash_deduction` 支持（有 vs 无）、舍入（`_money` vs 不量化）。
- DECISIONS:234 的"execution.py 已迁移至消费本模块、删除私有公式，兑现决策 1'两处不得各自维护'" **只对公式成立，对输入提取不成立**（见 D-03）。
- **置信：已验证**。

**X-04｜L2 存在同键的两条经济上可加的现金分红，三个消费者一律去重丢弃一条**
`data/l2_corporate_actions/corporate_actions.parquet` + `execution.py:1010-1011` / `corporate_action_handler.py:254-255` / `pit_adjustment_service.py:654-655`

我实测的三组：

| security_id | ex_date | 两行金额 | `drop_duplicates(keep="last")` 保留 | 静默丢弃 |
|---|---|---|---|---|
| 300760 | 2025-05-29 | 0.56 / 1.41 | 1.41 | **0.56** |
| 300760 | 2026-05-28 | 0.31 / 1.25 | 1.25 | **0.31** |
| 301308 | 2026-06-02 | 0.346760 / 0.643984 | 0.643984 | **0.346760** |

- 三组的 `available_at` **完全相同**（同一 `announcement_date` 15:00），所以存活者由 parquet 行序而非任何规则决定——**重跑数据即可能换一个结果**。
- 根因是两个各自正确的设计撞在一起：`build_corporate_actions` 既不合并同除权日多笔、也不写 revision 键；下游把"同键多行"一律当修订版本折叠。
- 黄金切片影响：**无**（76 条无重复键）。

- **置信：机制已验证**（我实跑确认）；"两笔可加"由价格证据支持（2025-05-29 前收 230.51、开盘 228.22，合计口径参考价 228.54 明显更贴近），**未回官方实施公告闭环，标存疑**。

> 处置见 `DECISIONS.md` 2026-07-29 条：三组已由官方 PDF 取证闭环（`6a19796` / `f441cf2`），定案为 fail-closed + 人工确认白名单，**不改去重键**。本报告不作事后改写。

### 中等

| ID | 位置 | 问题 | 置信 |
|---|---|---|---|
| X-05 | `backtest_runner.py:466,479,487-505` vs `pit_data_portal.py:204-213` | `resolve_ca_available_at` 确实单一实现（`test_corporate_action_availability.py:46` 用 `assertIs` 锁住，做得好），但**可见性过滤逻辑本身是两份**：快路径只 `to_datetime` 不强制 +08:00、抛 `ValueError` 而非 `DataContractError`（导致 `except DataContractError` 兜底不触发）、`field_capabilities` 恒为 `{}`。等价性哨兵只比对 happy path 的 DataFrame | 已验证 |
| X-06 | `momentum_strategy.py:77-84` vs `run_pipeline.py:251` | `prediction_universe` 被 `tradable_universe` 覆盖：策略先按 T 日可交易过滤再打分，IC 评估恒传全 12 只 → **IC 报告评估的信号与策略实际下单用的信号不是同一个横截面**。不是未来泄露，但 IC 数字与回测绩效无法互相解释 | 已验证 |
| X-07 | `momentum_strategy.py:72` vs `precomputed_signals.py:186` | 调仓时点两套语义：生产类按 `_bar_index % n`（计数器，断点续跑/跳 bar/重建对象都会移相，且 `_bar_index` 是可外部设置的构造参数），执行流水线按月度首交易日日期集合 | 已验证 |
| X-08 | `cross_sectional_momentum.py:160-190` | 用**全股票池 bar 日期并集**自造日历推 skip-21，而不是用 `PITAdjustmentService` 自带的真实 `TradingCalendar`。信号时间锚因此依赖传入的 universe。测试 `:196` 的 `assertNotIn(".portal", module_source)` 只挡字符串——模块正是通过私有方法 `_daily_bars` 摸到底层 portal 的。本切片 970/970 与真实日历零偏差 | 已验证 |
| X-09 | `label_return_calculator.py:52-57` / `label_data_portal.py:54` | `LabelDataPortal` **全库无生产调用点**（`run_pipeline.py:49` 导入的是 calculator）。标签隔离靠"把 `derivation_asof` 传成 T+22 15:00"这一约定，用的是与信号侧**完全相同的 `PITAdjustmentService` 实例**。BACKTEST_DESIGN:619 写的是"必须使用 LabelDataPortal"。测试 `assertNotIn("LabelDataPortal", signal_source)` 守的是一个没人用的类 | 已验证 |
| X-10 | `label_return_calculator.py:79` vs `label_data_portal.py:82` | `LabelSpec` 校验实现两遍，且 calculator 那份**漏了 `name` 校验** | 已验证 |
| X-11 | `momentum_ic_evaluation.py:84,165-167` | 评估器不校验信号时序单调、不去重 asof、不校验 `label_spec`（只查 `price_basis`）。信号乱序传入会让 block bootstrap 的块结构失效而 CI 照常输出；混入不同持有期的标签会静默按 21 日口径统计。`test_momentum_ic_evaluation.py:175` 传 `[signal]*22` 同 asof 顺利通过 | 已验证 |

### 契约首尾一致性：确认无问题的部分

- 标签 entry=T+1 / exit=T+22 交易日，`entry_lag/exit_lag/holding` 三者互洽且有 fail-closed 断言；970 天逐日核对**无 off-by-one**，入场价永远取 T+1 开盘。
- 标签跨除权日复权口径正确（我复核 subagent 的手算：600276 signal=2020-05-11，参考价 `(95.88−0.23)/1.2 = 79.708333…`，因子 `0.83133431`，收益 `0.06587881…`，与 `predictions.parquet` 报的 `0.065879` 一致——分子分母同一复权基准）。
- 标签成熟度门禁 `label_observed_at` 取出场日 **15:00** 而非开盘，堵住了半天泄露窗口，有专门测试钉死出场日 13:00 仍判未成熟。
- 动量窗口不含 asof 当日；被排除证券不做任何插补（有"若静默填 0/均值则 rank 会变"的反向哨兵）。
- 策略层**只产 `OrderIntent`**：源码级断言测试确认无 `LockedOrder`/`lock_order`/`execute_open_round` 引用。

---

## 三、宏观层：证据链、架构边界、工件一致性

### 严重

**G-01｜冻结门禁不覆盖流水线实际消费的 snapshot，而 4a 交付产物正是走的复用路径**
`src/golden_slice/run_pipeline.py:144-158` + `snapshot.py:148-222`

- `artifacts/golden_slice_momentum_ic_.../progress.log` 第一行：`frozen manifest gate PASS; snapshot=reused; portal=CachedPITDataPortal`。
- `load_adjustment_only_snapshot()` 校验的是：文件存在、76 行、`disclosure_time_known` 全 False、无 `available_at` 列、`snapshot_id` 一致、`price_basis` 全 RAW、逐证券行数。**从不比对 CA 金额/送股比例/除权日与 `manifest["verified_corporate_actions"]`，也从不校验 L1 价格。**
- `compute_manifest_hash()` 只对 manifest 字典自身取 sha256——它证明的是"这份 json 没被改过"，不是"这次运行读到的数据来自这份 json"。绑定只在 `build_*_snapshot()` 构造路径上成立。
- Subagent 实测（临时目录，未动你的仓库）：把 snapshot 的 `cash_dividend_per_share` 改成 99.0、`share_ratio` 改成 0.9、某根 bar 的 close 改成 1.0，**4a 复用路径全部接受**，report 照常打印 `frozen manifest gate: PASS`。
- **重要澄清**：我与 subagent 各自独立比对过，**交付的 snapshot 与冻结 manifest 逐列一致，L1 切片与 `data/l1_raw/daily_bar_raw.parquet` 逐值一致**。数字是对的——但这个"对"是人工验的，不是门禁验的。
- 不修的后果：§12.3 第 8 条"从冻结工件一键复现"实际不成立。
- **置信：已验证**。

**G-02｜`report.md` 系统性把"未触发"印成"0"，且 Explicit Limitations 漏了最重要的那一条**

`artifacts/golden_slice_execution_.../report.md:58,60` 印出 `limit_up_or_down_rejected: 0`、`suspended: 0`、`no_open_price: 0`、`capacity capped: 0`、`lot-size adjustments: 0`，**无任何"未触发"标注**。实测：

| 声称 | 实际 | 判定 |
|---|---|---|
| `capacity_rejected: 0` | 窗口内 20 日 ADV 全局最小 1.928e8 × `opening_liquidity_fraction=0.05` = 9.64e6；单笔典型委托 3.33e5。**余量 29 倍，100 万本金下数学上不可能 binding** | 未触发且结构上不可能 |
| `lot-size adjustments: 0` | 策略侧 `momentum_strategy.py:146` 已 `_round_down_lot(qty,100)`，执行器遇 `quantity % 100 == 0` 原样返回。**该计数恒为 0，是死代码** | 未触发且结构恒零 |
| `limit_up_or_down_rejected: 0` | 切片内**确有跌停开盘日**：2020-02-03（疫情复市）000333 −10.0000%、000858 −10.0032%、601668 −9.87%；600519 2023-11-01 +9.82%。只是从未落在 47 个成交日上 | 未触发，切片内确有该事件 |
| `suspended: 0` / `no_open_price: 0` | 切片内**确有 28 根停牌 bar**（open/close 均 NaN），未落在成交日上 | 未触发，切片内确有该数据 |

- `DECISIONS.md:334-335` 把这五条写得非常清楚（"未触发≠已验证"），**但这段纪律一句也没进 report.md 或 diagnostics.json**。`Explicit Limitations` 列了 broker_profile、派息时序、L1 洁净度三项，唯独漏了"执行路径覆盖不全"——而这恰是 §12.3 第 6 条的核心。
- 不修的后果：report.md 是会被单独引用的工件。单看它会读成"四条执行路径在真实数据上跑过且无异常"。
- **置信：已验证**。

**G-03｜幸存者偏差 / 当前成分股污染在整条证据链上零提及**

- `data/l1_raw/manifest.json` 上游明写 `"universe": {"source": "当前沪深300成分(EXPLORATORY_TAINTED,非时点成分)"}`；12 只票全部选自这个集合。
- `MomentumICEvaluationResult.survivor_bias_warning` 被赋值（`momentum_ic_evaluation.py:153`），但 `_diagnostics_payload()` 与 `_report_markdown()` **都不消费它**。`grep -rli survivor artifacts/` → **零命中**。
- 缺失文件/字段：`universe_manifest.json`（§12.2 必需）、`universe_version`、`taint_reasons`（§12.1 必需）、`label_universe_manifest_hash` + `taint_status`（§4.4 对横截面标签强制要求，而 IC 恰好产出分组收益）。§4.3 原文要求"系统必须**自动**将该 taint 写入 `universe_manifest` 并传播到所有下游"。
- 报告首页写了 `evidence_status: EXPLORATORY_TAINTED` 却**没写 tainted 的原因**，同一页给出 RankIC 0.109、ICIR 0.249、CI 不跨零、分位收益表。免责句只提"12 只票样本小/噪音大"。
- 更值得注意：`DECISIONS.md:315` 写了三条"不得援引为 alpha 证据"的理由，**这三条一条也没进 report.md，而且它们同样没提幸存者偏差**。
- **置信：已验证**。

**G-04｜分位单调性结论是分组余数规则的产物（唯一一条直接改变已交付结论的发现）**
`src/features/momentum_ic_evaluation.py:204-206`

```python
quantile = min(5, (index * 5 // n) + 1)
```

桶大小 = `ceil(nb/5) − ceil(n(b−1)/5)`：n=11 → [3,2,2,2,2]；n=12 → [3,2,3,2,2]；n=10 → [2,2,2,2,2]。**余数永远给最靠前的桶。**日频 n 分布实测 {11: 614 天, 12: 257 天, 10: 99 天} → q1 在 871/970 天里有 3 只，q5 恒 2 只。

我用 `predictions.parquet` 独立逐日复算，**完全复现 report.md**：

| quantile | 现规则 mean | n | 余数给中间桶 mean | n |
|---:|---:|---:|---:|---:|
| 1 | 0.008026 | 2811 | **0.011035** | 1940 |
| 2 | 0.013847 | 1940 | **0.009275** | 2197 |
| 3 | −0.001225 | 2197 | **0.001760** | 2811 |
| 4 | −0.004864 | 1940 | −0.004864 | 1940 |
| 5 | −0.002794 | 1940 | −0.002794 | 1940 |

- 现规则下 q2 > q1 造成"顶端反转"；换一个同样合理的余数分配，前三档单调递减，不单调点移到 q4/q5（那里桶大小相等，是真信号）。**q1 均值仅因余数放哪儿就变动 +37%。**
- 更根本的问题：**10~12 只票分 5 档，分位数在统计上没有良定义**，任何单调性判定都不稳健。
- 不修的后果：`DECISIONS.md:315` 用"分位收益非单调（第二档高于第一档）"作为"IC 不得援引为 alpha 证据"的第 ② 条理由。**保守方向是对的，依据不成立**——这条判定需要重写为"截面样本过小、分位数无良定义"，而不是"观察到非单调"。
- **置信：已验证**（我亲自复算，五个均值与样本数与 report.md 逐位相同）。

**G-05｜CI 不跨零对 `block_length=21` 完全敏感**
`src/features/momentum_ic_evaluation.py:17`（`CI_BLOCK_LENGTH = HOLDING_PERIOD_DAYS = 21`）

Subagent 用同一实现同一 seed 复现报告 CI 后做敏感性：

| block | 95% CI | 含 0 |
|---|---|---|
| **21（现用）** | [0.0052, 0.1976] | **否** |
| 42 | [−0.0008, 0.2261] | 是 |
| 63 | [−0.0058, 0.2191] | 是 |
| 126 | [−0.0045, 0.2082] | 是 |

Newey-West 同向：L=21 → t=2.09；L=42 → t=1.85；L=63 → t=1.84。

- block=21 只针对"标签重叠"这一个依赖源，但 RankIC 序列还继承信号自身的持续性（231 日回看窗每天只挪 1 天），实测 lag-126 自相关仍有 **0.27**。
- 不修的后果：报告里唯一一个"正面"数字（CI 下界 > 0）对一个未声明依据的超参完全敏感，是最容易被当成 alpha 证据误用的一条。
- **置信：已验证**（可复现敏感性 + NW 交叉验证）。

### 中等

| ID | 位置 | 问题 | 置信 |
|---|---|---|---|
| G-06 | `run_pipeline.py:57` / `run_execution_pipeline.py:61` / `precomputed_signals.py:34-41` | `evidence_status` 每一层都是模块常量，**不是从输入推导**；4a 的 `predictions.parquet` 确实带 taint 三列，但 `SIGNAL_PROJECTION_COLUMNS` 只投影 6 列，**物理丢弃这三列**，4b 用自己的常量重新声明。§0.4 的"沿依赖图向下传播"在实现上不存在，方向上安全只是因为常量恰好等于最保守值 | 已验证 |
| G-07 | 全仓 | §12.1 要求但两个 artifact 目录完全没有的字段：`code_commit_hash`、`environment_lock_hash`、`ruleset_version`、`universe_version`、`taint_reasons`、`label_version`、`random_seed_bundle`、`run_mode`、四类账本 hash 等 20 项；§12.2 要求但缺失的文件 11 个。`.gitignore:221-228` 排除了 L1 价格、88 行核验 CSV、252 份 PDF、全部 parquet 产物 | 已验证 |
| G-08 | `run_pipeline.py:790-832,1186-1198` | 报告印 `NO_DATA 46/15516`（0.3%，逐日复权点口径），而 `predictions.parquet` 实测**信号层排除 781/11640 = 6.7%**（231 日累计窗口口径），781 这个数在任何工件里都没直接出现。`exclusion_reason_counts` 算了但不写出。另 `BLOCKED reasons: {}` 是在 `derivation_asof=2024-02-29 15:00` 的**全知视角**下扫的，不是逐日 PIT 视角 | 已验证 |
| G-09 | `corporate_action_handler.py:58-69` | `UNPROCESSED_CA_count: 0` 的覆盖率是 **14/61**——handler 只检查当日持仓名的 CA，执行窗口内共 61 条 CA 事件，账本只自然触发 14 条 | 已验证 |
| G-10 | `backtest_runner.py:289` | 31 条 `CASH_INSUFFICIENT` = **买单拒单率 40%**（31/77），成因是 T 日决策时刻校验 `available_cash` 而同日卖单要 T+1 开盘才回款 → "卖出腾仓位再买入"的调仓必然一半失败，组合 4 年**从未达到过目标权重**。report 只印数字不印归因，也未列入限制 | 已验证 |
| G-11 | `precomputed_signals.py` / `run_execution_pipeline.py:429,449-450` | ordered hash binding 的报告措辞**诚实、不夸大**（明确写了"not a hash recomputed from parquet contents"、`4a trusted baseline: False`）——这是本次工件里最规范的一段。但 diagnostics 里 `unique_signal_dates_match_calendar` / `unique_signal_security_keys` 是**写死的 `True`**，`manifest_gate: "PASS"` 是字面量；`_require_projection_columns()` 在已投影成 6 列的 frame 上查禁用列，是**恒真死断言** | 已验证 |
| G-12 | `run_execution_pipeline.py:647-671` | `orders.parquet` 丢掉了 `reference_price_ts`、`ruleset_version`、`ttl`、`trailing_adv_notional`、`max_order_notional`——恰是 §9.2 "T 日锁定"的证据字段。且 4b **没把本次四类账本的 sha256 写进任何 manifest**（§12.1 要求），本次结果自身不可 diff 复核 | 已验证 |
| G-13 | `run_pipeline.py:858` | akshare 双向差集区间硬编码 `2019-02-25..2023-12-20`（= 核验台账自身的 ex_date 极值），窄于依赖窗口 `2018-12-19..2024-01-31`。manifest 声称"无 A 股普通股除权事件"的两个缺口区间恰好落在这条机器交叉核对之外，只由人工 PDF 扫描支撑（subagent 独立跑了区间外 akshare 也是 0 条，结论未被推翻；但 akshare 已知会漏特别分红，不构成强证明） | 已验证 |

### 三条架构不变边界的现状

| 边界 | 判定 | 依据 |
|---|---|---|
| ① `LockedOrder` / `FillLedgerEntry` 是共同语言 | **站得住** | 4b 复用 Phase 1 的 `T1OpenExecutor`/`PortfolioLedger`/`CorporateActionHandler`，直接调 `phase1_runner._lock_intent`；五个契约 dataclass 全部 `frozen=True`，`object.__setattr__` 只出现在 `__post_init__`，**无任何事后 mutate**。瑕疵见 G-12（落盘字段丢失） |
| ② 信号函数回测实盘同源 | **4b 破了，但报告说清楚了** | `PrecomputedMomentumStrategy` 不调用 `calculate_cross_sectional_momentum_signal`，从 parquet 的 float 重建信号对象，`adjustment_service=None`。**已排除一个可能的分歧点**：线上"过滤后排名" vs 4b"全集排名后过滤"，因序数排名单调，选出的 top-3 集合相同。真正的破口是 `_filter_signal_to_tradable` 过滤后仍沿用未过滤签名的 `signal_manifest_hash` → orders.parquet 里的 hash 指向的不是实际产单的那个 signal 对象。另见 X-06/X-07 |
| ③ 账本事件流不可变可 diff | **站得住** | `_append_entry` 是唯一写入点，`event_id` 单调；逐日 `_assert_accounting_identity` + `_assert_non_negative_state`，**断言用 Decimal 分位精确相等（容差 = 0）**，最大偏差 0.00。`run_default_backtest` 跑两遍比对 `nav_daily.csv` 字节相等。唯一顺序风险：`corporate_action_handler.py:182-206` 的 `_mark_same_day_unavailable_actions` 未排序遍历，同日多条 UNPROCESSED_CA 的追加顺序 = 源文件行序。缺失：本次账本 hash 未入 manifest |

---

## 四、元层：文档正确性（重点）

### 严重

**D-01｜`verified absence` 的更正只修了三分之一**
`DECISIONS.md:280`（2026-07-22）："12只黄金切片证券在冻结窗口内 `配股=0`、`转增=0`、`送股=0`"

- 2026-07-27 的更正条目（`:290-295`）只推翻了 `送股=0`（恒瑞 3 条），并顺带断言"配股确为 0 条"，**但从未复检 `转增=0`**。三个结论出自同一次标题关键词族筛查，检出手段相同、失效理由相同（送转信息写在正文"权益分派实施公告"里，标题捞不到）。
- 同一文档 `:295` 立下的纪律正是："任何'未发现 X'的结论，必须写明其检出手段的覆盖范围，不得跨越手段边界升级为 verified absence"。存活的 `转增=0` 是这条纪律的现行违例。
- **置信：已验证**（错误成因由 DECISIONS:293 自述）。

**D-02｜"volume 口径确认为股"被实测推翻，且永久哨兵结构上抓不到**
`DECISIONS.md:199-201`（2026-07-02）

我按 DECISIONS 自己的判据 `amount/(((open+close)/2)*volume)` 分源实测：

| 数据源 | 证券数 | 比值中位数 | 结论 |
|---|---|---|---|
| `akshare.stock_zh_a_daily_sina` | 297 | **1.000151** | volume = 股 |
| `akshare.stock_zh_a_hist`（EM） | 3（688082/688111/688126） | **100.078534** | volume = **手** |

- 永久哨兵 `src/data/test_l1_amount_units.py` 固定抽 600519 / 300750 / 688981，我核过这三只**全部来自 sina**（比值 1.000029 / 0.999924 / 1.001039）——测试永远绿。DECISIONS:200 原文已写"若 volume=手 比值会约为 100"，**判据是对的，样本选偏了**。
- 当前无实际错值：`amount` 两源都是元，EX-008 容量约束读的正是 `amount` 而非 `volume`。
- 不修的后果：任何未来的换手率、参与率、ILLIQ、股数级容量约束一上线，这 3 只就 100 倍错，而测试是绿的。**已冻结的错误单位断言比未知更危险。**
- **置信：已验证**（我亲自分源统计）。

**D-03｜"兑现决策 1'两处不得各自维护'"只对公式成立**
`DECISIONS.md:234`（2026-07-03）→ 见 X-03。公式确已同源，**输入提取仍是两份且已实际分叉**。这是"以偏概全"在文档层的形态：把"公式已合一"推广成"两处不再各自维护"。

**D-04｜被后续事实推翻但未追"取代"条**
`DECISIONS.md:251`（2026-07-03）："该态在 Service 盘后 asof 语境下不可自然触发（它是 handler 09:00 cutover 层的态）"

- 该判断只在"公告日永远早于除权日"成立时为真。生产 L2 **没有任何断言保证这一点**（3075/3075 走 `corporate_action_availability.py:25-33` 的无守卫路径），且已有 1 条越界（`601318/2015-09-09`）。见 M-01。
- 按 PROJECT.md:114 的规则，推翻旧决策应"新写一条'取代 X'"。目前既没有取代条，也没有把该判断降级为待确认。
- **置信：已验证**。

**D-05｜PROJECT.md 与代码实况漂移（第二次同类）**
`PROJECT.md:14`："**当前阶段：Phase 2 前两步…已收官并通过全库审计，第三步黄金切片未启动**"

- 实况：黄金切片块 1-3 已收官、块 4a/4b 已完成并产出工件（`STATE.md:9`、DECISIONS 三条 2026-07-28 记录、两个 artifacts 目录）。
- 这是"PROJECT.md 曾长期写着 Phase 2 未启动"的第二次同类。PROJECT.md 自称"唯一事实来源""每次开新对话先读"——**过时的宪法比没有宪法更糟**。
- **置信：已验证**。

### 中等

| ID | 位置 | 问题 | 置信 |
|---|---|---|---|
| D-06 | `README.md:7` | 三处失真：①"保留 144 个测试哨兵"实际 178（`def test_` 计数）；②"覆盖 T+1 开盘执行、涨跌停拒单、停牌不成交、交易费用、容量约束"与 DECISIONS:335"一次未触发"直接冲突；③"所有数据读取必须满足 `available_at <= decision_ts`"——`execution.py:733` fail-open 绕过、`precomputed_signals` 直接 `pd.read_parquet` 不走 portal | 已验证 |
| D-07 | `STATE.md:9` | "178 测试全绿"未标 skip 数。我在干净环境（无 akshare）实跑：`total=175, passed=162, skipped=12, failures=0, errors=1`。被跳过的 12 个恰好是 **TradingCalendar 全类 + L1 数据契约 + security_master 契约**——其中 `test_l1_raw_price_basis_is_unadjusted_only`、`test_trade_status_is_explicit` 等**纯读 parquet、根本不需要网络**的测试，只因文件顶部 `import akshare` 而全类跳过。DECISIONS:160 曾规范地写出 `skipped=0`，此处退化 | 已验证 |
| D-08 | 参数留痕 | **有留痕**：`top_n=3`、初始资金 1,000,000、月度调仓（DECISIONS:330）。**无留痕**：`max_single_name_weight`、`min_cash_buffer`、`lot_size`、`CI_BLOCK_LENGTH=21`、`iterations=500`、bootstrap `seed=0`。前三个不进 manifest → 改 lot_size 会改变订单流而 manifest hash 不变；后三个直接决定 G-05 的 CI 结论 | 已验证 |
| D-09 | `DECISIONS.md:243` | 2026-07-03 标注"`APPLICATION_CUTOVER_TIME=09:00` 作为常量写入共享判定函数…**列入 Phase2 收官审计**"。本次审计即为该复检点，结论：该常量确实使除权日 09:30 之后落入 `NOT_YET_VISIBLE`（"未来事件，无害"语义），而那正是价格已跳空的时刻；因子推导恰在 15:00。**存疑**——需与 M-01 一并评估，我没有构造完整复现 | 存疑 |
| D-10 | `verified_ca_ledger.py:329,527-528,204` | `"rights_issue_verified_count": 0` 是同义反复：源 CSV 无配股字段 → 每行硬写 `rights_ratio=0, rights_price=0` → 再断言"必须无配股"。这个 0 不携带任何关于现实的信息，却以 `_verified_count` 命名进入冻结 manifest 与 hash | 已验证 |
| D-11 | `data/golden_slice/ca_verified_88.csv` | 表头**没有"转增"字段**；77 行 A 股普通股中 **74 行"送股比例_每股"为空**。空 = 未填写，代码当 0。**人工核验层没有任何字段能区分"核验为零"与"未检查"** ——600276 的送股漏检能发生一次，就能以同样机制在转增上发生第二次（呼应 D-01） | 已验证 |
| D-12 | `tools/scan_missing_ca_titles.py:45-60` | `BUSINESS_TERMS` 14 个词全是分红配股家族，缺"合并/吸收合并/换股/分拆/要约/减资/注销/退市"。实测 `all_titles_manifest.csv`：命中业务词的 234 条全部已在 listing（报告"missing_total=0"），但**另有 212 条不重复标题命中合并/换股/分拆等词却不命中任何业务词**，含 000333 2019 年完整的"换股吸收合并无锡小天鹅"系列。同仓库的 `fetch_cninfo_qyfp.py:55-68` 的关键词族**是**含"合并/吸收合并/要约/退市"的——扫描工具把它们丢了。工具名与 `missing_total` 字段读起来是完备性结论 | 已验证 |
| D-13 | `tools/extract_ca_fields.py:18,456-482` | 正文提取仍以标题子串 `"实施公告"` 开闸（"实施的公告"这类写法不命中）；`NO_TRANSFER_ONLY_PATTERNS` 分支自带注释"原文仅明确不转增，不明确写出不送股"**却仍返回 `is_nonzero=False`**——把"只否认了转增"当成"也否认了送股"，与 600276 历史错误同型且仍在线。实测评审 CSV 77 行中 70 行 low confidence、60 行 share_ratio 为空 | 已验证 |
| D-14 | `data/golden_slice/ca_extraction_for_review.csv` | 冻结的评审 CSV 只有 77 行，对应 `scan_missing_ca_titles` 追加 103 条之前的 149 行 listing 版本；按当前 252 行 listing 重跑会得到 102 行。**"评审清单→人工核验→冻结台账"的可追溯链在中间断了一节**（最终 88 行核验表内容更全，说明人工当时看到了更多 PDF，但从仓库里的评审 CSV 复原不出来） | 已验证 |
| D-15 | `data/golden_slice/cninfo_raw/all_titles_manifest.csv` | 文件已被 Excel 往返改写：第一列是 `333` 而非 `000333`（前导零丢失），`disclosure_ts` 为 `2018/12/1 0:00` 格式。工具用 `.zfill(6)` 和多格式解析**静默容忍**，掩盖了"唯一的 ch3 全量标题证据文件已不是工具产物的字节级复制品"这一事实 | 已验证 |
| D-16 | `data/l1_raw/security_master_manifest.json` + `build_security_master.py:174` | `master["delist_date"] = None` 无条件置空，却在 manifest 里挂着 `point_in_time_capability: ONE_TIME_EVENT_BEST_EFFORT` + `evidence_level: BEST_EFFORT_RELIABLE`。**给一个恒为空的字段颁发可靠性评级**。同段 `st_count: 0` 是在两个 ST 数据源 ConnectionError 之后由第三源"名称前缀扫描"得出（`source_errors` 自证两次失败） | 已验证 |
| D-17 | `akshare_adapter.py:224-304` + `build_corporate_actions.py:79,202-206` | provider 崩溃被记成"该股无公司行动"：生产 manifest 显示 `security_count_no_actions_observed=5, security_count_failed=0`，而其中 4 只（688047/688506/688521/688981）的 `source_errors` 是**真崩溃**（`TypeError: 'NoneType' object is not subscriptable`、`KeyError: '实施方案公告日期'`）。DECISIONS:116 把这 5 只写成"无记录"，属于把未验证结论落笔 | 机制已验证；本次漏账风险**存疑** |
| D-18 | `src/golden_slice/ca_verification_checklist.md:28` | `"None from ledger rules (no count > 12 and no non-cash/non-stock action in this slice)"` —— 这个 "None" 的检出范围是"当前 akshare L2 台账内容"，而该台账已被 DECISIONS:320 自己证实存在系统性遗漏（茅台 2 次特别分红整条缺失） | 已验证 |

### 元层的正面记录（这些声称我复核为真）

- `DECISIONS.md:284`"逐份打开巨潮官方 PDF 原件…76/76 哈希校验通过"——**成立**（见第五节）。
- `DECISIONS.md:262-265`（切点从 2023-02-17 退至 2011-02-28）——含**审计者判断失误复盘**与"方法论留痕"，是本文档质量最高的一条，做法应作为模板。
- `DECISIONS.md:329` 对 ordered hash binding 的"【非内容密码学校验】"标注与代码完全一致。
- `DECISIONS.md:334-339` 的五条"本步未验证的边界"是本次审计中最诚实的记录（问题是它没进工件，见 G-02）。
- `DECISIONS.md:321-324`（茅台特别分红缺口）是本次工件里质量最高的一条负面诊断。

### 元层的结构性结论

四类历史错误在本仓库不是散点，而是**同一个机制的四种表现：声明层（DECISIONS / README / manifest / 测试名）跑在实现层前面**。

- 送股 verified absence 更正了，同一句里的转增没有（D-01）；
- "两处不得各自维护"写进 DECISIONS，输入提取仍两份且已分叉（D-03 / X-03）；
- "所有数据读取必须满足 PIT 闸门"写进 README，执行器 CA 读取 fail-open（D-06 / M-09）；
- "制度数字必须有外部事实源"写进 DECISIONS:140，制度数字最密集的 `FeeSchedule` / `LimitRuleTable` 零出处且已错一条（M-04 / M-17）；
- "不得用周一到周五推交易日"写进 BACKTEST_DESIGN §1.1，生产账本里还留着 `timedelta(days=1)`（M-08）。

**DECISIONS.md 里已经有三条自我更正记录，每条都写了教训。但这些教训没有被反向施加到已通过的旧代码上——纪律写在文档里，没变成对存量的复检清单。**

建议：Phase 2 收官时增加一个动作——每条 DECISIONS 的"纪律/教训"必须附一次全库回溯扫描的结果，否则该条只是记录，不是防线。

---

## 五、本地数据核查（全部由我亲自执行）

### 全部通过 ✅

| 检查项 | 结果 |
|---|---|
| PDF 数量 vs `listing_manifest.csv` | 252 = 252 |
| PDF sha256 集合 ↔ manifest `pdf_sha256` 集合 | 双向差集 **0 / 0**；252 个 sha256 全部唯一；无空值 |
| `listing_manifest.csv` sha256 | `428d8d8e…` = 冻结 manifest 记录值 |
| `ca_verified_88.csv` sha256 | `c4e52830…` = 冻结 manifest 记录值 |
| `manifest_hash` 独立重算 | `94e025b6a0b259c56751c6d3f3953c4a804aeea1c85b3b73dac9f9b2f468d4ae` = 记录值 |
| 88 行 → 76 条的去重逻辑 | 88 = 77 A股普通股 + 11 优先股；去重 `000333_2021-05-26_1.pdf` → 76。与冻结 76 条按 `(code, 除权息日)` **完全重合，0 frozen-only、0 csv-only** |
| 76 条逐字段比对 | `cash_dividend_per_share` / `ex_right_cash_deduction_per_share` / `share_ratio` / `record_date` / `disclosure_date` / `source_pdf_filename` 全部一致，**0 处不一致** |
| 折算节计数 | CSV 中"有"24 条（含被丢弃的重复件）→ 去重后 23 = manifest `adjustment_section_count: 23` |

### PDF 原文抽查（4 条，超出要求的 3 条）

| # | 记录 | 冻结台账值 | PDF 原文 | 判定 |
|---|---|---|---|---|
| 1 | 000651 / ex 2021-08-23 | cash 3.0，deduct **2.784787**，rec 2021-08-20 | 「每 10 股派发现金股利 30 元」；六、关于除权除息价的计算原则及方式：「每股现金红利应以 **2.784787** 元/股计算（=16,752,531,495 元÷6,015,730,878 股）…除权除息价格=股权登记日收盘价-2.784787 元/股」；「股权登记日为：2021 年 8 月 20 日，除权除息日为：2021 年 8 月 23 日」 | **逐字符相符** |
| 2 | 600276 / ex 2020-05-25 | cash 0.23，deduct 0.23，share 0.2，rec 2020-05-22 | 「A 股每股现金红利 **0.23** 元」「每股派送红股 **0.2** 股」「差异化分红送转：**否**」；表格 股权登记日 2020/5/22、除权（息）日 2020/5/25 | **逐字符相符** |
| 3 | 600519 / ex 2022-12-27（akshare 缺失的特别分红） | cash 21.91，deduct 21.91，rec 2022-12-26 | 「2022 年度回报股东特别分红实施公告」「A 股每股现金红利 **21.91** 元（含税）」「差异化分红送转：**否**」；表格 2022/12/26、2022/12/27 | **逐字符相符** |
| 4 | 601318 / ex 2021-04-22（沪市差异化折算） | cash 1.40，deduct **1.3946**，rec 2021-04-21 | 「每股派发现金红利人民币 **1.40** 元（含税）」「差异化分红送转：**是**」；正文「=18,210,234,607×1.40÷18,280,241,410≈**1.3946** 元」「综上，本次除权除息参考价格=前收盘价格-1.3946 元/股」 | **逐字符相符** |

**结论：人工核验环节没有抄错。这是整个项目里证据质量最高的一环。**

### L1 / L2 parquet 画像

**`data/l1_raw/daily_bar_raw.parquet`**：801,600 行 × 17 列｜2015-06-30 ~ 2026-06-30｜300 只 × 2,672 日**完整面板**｜重复 `(security_id, trade_date)` = **0**｜`price_basis` 单一 `RAW_UNADJUSTED`｜无 vendor 复权列（EM 返回的涨跌幅/换手率/振幅全被 `column_map` 丢弃）｜**除权跳空保留实证不复权**（000338 2015-08-20 前收 24.47 → 开盘 12.00）｜有 bar 的 699,453 行中 `close≤0`/`high<low`/`volume≤0`/`amount≤0` 均为 0。
问题见 D-02（volume 单位跨源不一致）、M-16 相关（`trade_status` 的"停牌"是从"供应商无此行"反推的，与数据缺口不可区分，`has_bar & volume==0` 分支 11 年 300 只**命中 0 行**，本身可疑）。

**`data/l2_corporate_actions/corporate_actions.parquet`**：3,075 行 × 16 列｜295 只（5 只无记录，其中 4 只源自 provider 崩溃，见 D-17）｜`CASH_DIVIDEND` 2,756 / `STOCK_DIVIDEND` 300 / `RIGHTS_ISSUE` 19｜**有 `rights_price_per_share`（99.38% 为空），无 `rights_ratio`、无 `rights_price`**（M-03 根因）｜`STOCK_DIVIDEND` 中 cash>0 的 278 条，双字段共存未被 action_type 吞掉｜`available_at >= ex_date 00:00` 仅 **1 条**（601318/2015-09-09）｜`ex_date − announcement_date`：min 0 / 中位 7 / max 17，无负值｜重复键 **3 组 6 行**（X-04）。

**污染字段检查：L1/L2 均未发现供应商复权价或未来字段流入。** 供应商复权文件已隔离在 `data/quarantine/vendor_adjusted/` 并标 `FORBIDDEN_FOR_L1_FEATURE_EXECUTION`，生产代码路径无任何读取。

---

## 六、无法核查 / 受限项（如实说明）

1. **容器未安装 akshare**，`src/data/test_akshare_adapter.py` 的 3 个测试无法运行（在你机器上应可跑，178 = 175 + 3）。因此我给出的 `total=175, skipped=12` 是**干净环境**的数字，不是你机器上的数字；D-07 的要点是"clean clone 不可复现"，不是"你机器上没跑绿"。
2. **未重跑 4a/4b 流水线**（4a 缓存路径实测约 4,147 秒）。所有数字对账基于交付产物与源数据的独立复算，不是端到端重跑。
3. **2026-07-06 ST 涨跌幅新规仍缺交易所公告文号的一手直引**（STATE.md:50 已自标）。我这次也没能补上——但 M-05 说明这一条其实次要，因为 ST 规则从来就没实现过。
4. **300760 / 301308 两笔同除权日分红"可加"的判断由价格证据支持，未回官方实施公告闭环**（X-04 标存疑；审计后已由 DECISIONS 2026-07-29 闭环）。
5. **`corporate_action_pricing.py` 的 2011/2013 两档规则只有深交所出处**，却用于沪市证券（切片 8/12 是 600xxx/601xxx），覆盖 2011-02-28 ~ 2023-02-16 即回测窗口绝大部分。公式跨所很可能一致，但这是**推定不是取证**。2026 版规则条文我也无法访问，其公式是否与 2023 版一致未经核验。
6. **`.gitignore` 排除了 L1 价格、88 行核验 CSV、252 份 PDF、全部 parquet 产物**，所以本次审计的很多结论只能在你这台机器上复现——这本身就是 G-07 的证据。

---

## 七、需要你决策的三点（按项目宪法 8(b)）

> 收敛为"推荐 X + 一句话理由，回'确认'即通过"。

**① 过户费生效日 2025-04-29 → 2022-04-29（`execution.py:241`）**
推荐：修正常量、给 `FeeSchedule` 全部费率补文号/URL、把 `test_execution.py:104-111` 改成引用出处的手算断言。
**需你知情确认的后果：这是有真实金钱后果的修复，会改变十年回测的费用与四类账本哈希，现有"与 f29d083 逐字节一致"的基线必须重跑。**

**② 配股字段名与生产 schema 不匹配（`execution.py:978-979`）**
推荐：不打 `getattr` 默认值补丁，而是把 CA 输入提取整体上收到 `src/domain/`，两条链路强制同源、缺列一律 `DataContractError`。
**需你知情确认的后果：同样动摇十年账本哈希基线（19 条配股日的限价带会变）。**

**③ ST/\*ST ±5% 涨跌幅规则从未实现（`LimitRuleTable`）**
推荐：从 STATE.md 的"未决问题"升级为 **Phase 3 全市场回测的硬前置**，与 PIT ST 数据源并列，而不是"2026-07-06 规则变更待办"。
理由：当前表述掩盖了"任何历史时期都没实现"这个更基础的事实。

---

## 八、修复优先级建议（技术决策，我已按 8(a) 直接给出）

**P0 — 修完才能对外表述黄金切片结论：**
G-04（分位余数规则 + 重写 DECISIONS:315 第②条理由）、G-05（CI 对 block_length 敏感性入报告）、G-02（report 区分"未触发"与"已验证为0"、补"执行路径覆盖不全"限制）、G-03（幸存者偏差入 report 与 universe_manifest）、G-01（复用路径校验 snapshot 内容对齐 manifest）。

**P1 — Phase 3 全市场化前必须修（否则必然产出错值）：**
M-03（配股字段）、M-04（过户费）、M-05（ST 限价）、X-04（同日多笔分红）、D-02（volume 单位 + 哨兵改跨源抽样）、M-01 + `corporate_action_availability` 路径 A/C 补 `available_at < ex_date` 守卫。

**P2 — 架构性欠债，建议 Phase 2 收官一并处理：**
X-01/X-02/X-03（CA 输入提取上收 domain、消费者侧加口径判据）、M-08（`calendar=None` 默认值删除，改必传）、M-07/M-09（三处 fail-open 转 fail-closed）、X-09（LabelDataPortal 变成真架构边界或删掉这个类）、D-01/D-11（转增复检 + 核验 CSV 增加"已核验为零"与"未检查"的区分字段）、D-05/D-06/D-07（三份文档对账）。

**P3 — 留痕与可复现：**
G-06（evidence_status 改为从输入推导）、G-07/G-12（补 §12.1 字段与四类账本 hash）、D-08（六个未留痕参数入 manifest）、D-14（评审 CSV 重生成或标注其对应的 listing 版本）。

---

## 附：总判定（重述）

**黄金切片"可信"的结论：数据层成立，报告层与门禁层不成立。**

- 没有发现任何一条会推翻"76 条 CA 台账是准确的"——PDF、CSV、冻结记录、哈希四方对账全通过，4 份 PDF 原文抽查逐字符相符。
- 但已交付的 `report.md` 有一条实现产物被当成了数据事实（G-04），一条唯一的正面数字不稳健（G-05），两条会让读者过度解读（G-02/G-03），以及"frozen manifest gate: PASS"在本次实际走的复用路径上是空的（G-01）。
- 十年回测层面有三条会产出错误数字的缺陷，意味着 Phase 1 的哈希基线是可复现的、不是正确的。

**因此：黄金切片证明的是"CA 人工核验这条证据链诚实"，尚不能证明"管线诚实"。**

---

## 来源

- [中国结算：4月29日起将股票交易过户费总体下调50%（财联社）](https://www.cls.cn/detail/1001111)
- [中国结算：4月29日起股票交易过户费总体下调50%（中国日报网）](https://cn.chinadaily.com.cn/a/202204/29/WS626b36f0a3101c3ee7ad319f.html)
- [4月29日起股票交易过户费总体下调50%（人民网）](http://finance.people.com.cn/n1/2022/0429/c1004-32411923.html)
- [股票交易过户费收取减半，4月29日起双向收取0.01‰（澎湃新闻）](https://www.thepaper.cn/newsDetail_forward_17848885)
- [利好全体股民！A股交易过户费下调50%，明日起统一降至0.01‰（证券时报网）](https://www.stcn.com/data/djsj/202204/t20220428_4472824.html)

---

### 附录 E：工作树状态

**E-01｜工作树有 40 个文件显示为已修改，全部是行尾符（CRLF↔LF）整文件churn**
`git diff --stat` 显示 `15682 insertions(+), 15682 deletions(-)`，逐字对称。内容与 `HEAD` 语义一致。
后果：`git status` 作为变更检测器已失效——真正的改动会淹没在噪音里；任何一次 `git add -A` 都会把 40 个文件的行尾符改动提交进去。建议加 `.gitattributes`（`* text=auto eol=lf`）并做一次一次性归一化提交。
另：`tools/extract_ca_fields.py`（610 行，产出 `ca_extraction_for_review.csv` 的工具）**未纳入 git**，`baseline_hashes.txt` 同样未纳入——证据链上的一环不在版本控制里。

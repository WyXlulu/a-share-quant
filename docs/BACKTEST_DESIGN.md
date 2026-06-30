# A 股日频量化研究、ML/DL 训练与事件驱动回测规范（V3.0.2）

> **文件定位**：本文件是项目的强制工程规范、验收标准与实现顺序。它面向 Codex Agent 和开发者，不是策略灵感笔记。任何数据接入、特征、标签、训练、回测、报告或实盘/模拟盘代码，均必须遵守本文件。
>
> **核心不变量**：在任一决策时刻 `decision_ts`，策略、特征函数、模型、优化器和订单生成器只能读取 `available_at <= decision_ts` 的信息；任何使用未来信息的路径必须在数据访问层被拒绝，而不是依赖人工自觉。
>
> **优先级排序**：真实性与可审计性 > 可复现性 > 风险控制 > 速度 > 策略复杂度 > 回测收益。
>
> **基础规则日期**：2026-06-30（Asia/Shanghai）。市场规则、费率及数据供应商字段会变化；它们必须配置化、按生效日期版本化，禁止写死在 Python 条件判断中。
>
> **V3 日期**：2026-06-30（Asia/Shanghai）。V3 在 V2.1 工程骨架完全保留的前提下，修复一处时序矛盾、补齐若干被忽略的偏差与自由度，并加入一条务实的可信化路径。
>
> **V3.0.1 日期**：2026-06-30（Asia/Shanghai）。本版本不重排架构，只修复 V3 冻结前的五项工程语义遗漏：阶段门禁同步、黄金切片的验证范围、公司行动缺失时的特征边界、研究者自由度的配置化，以及历史版本残留清理。
>
> **V3.0.2 日期**：2026-06-30（Asia/Shanghai）。本版本为**冻结实施基准**；不再重排架构或放宽任何 PIT / 公司行动（CA）真实性要求，仅消除 Phase 1 的实现歧义，避免在 CA 台账尚不完整时误把动量等多日收益策略当作执行层基线。
>
> **V3.0.2 最后澄清（Phase 1 的 CA 分期）**：
> 1. **不放宽 CA**：特征/标签侧在缺少合格 PIT 公司行动台账时，仍禁止多日收益、动量、波动率与相应标签；不得以“先跑通”为由静默忽略除权除息或拆股。
> 2. **Phase 1 用哑策略，不用动量**：Phase 1 的首个订单发生器必须是固定、确定性、非收益型的 dummy strategy（例如按预先冻结的 ticker 顺序定期选择 N 只证券），其目的只是驱动订单→成交→账本；简单截面动量只能在 Phase 2 的 CA/PIT 特征链路合格后引入。
> 3. **公司行动分期实现但不静默跳过**：Phase 1 必须支持现金分红及拆股/送转的账本处理；配股、合并、退市等复杂事件可暂不完整实现，但必须被识别、审计并以 `UNSUPPORTED_CORPORATE_EVENT` 阻断可信回测。EX-013 的“通过”可为正确阻断，不等于必须在首刀实现全部复杂事件。
> 4. **fixture 与真实策略分离**：EX-011–EX-014 使用构造 fixture 覆盖公司行动路径；若历史集成 fixture 被刻意限制在无公司行动窗口，只能标记 `EXECUTION_FIXTURE`，不得基于未来公司行动筛选形成真实交易规则或策略业绩结论。
>
> **V3.0.1 关键补丁**：
> 1. **Phase 门禁同步**：Phase 1–3 的退出条件现在与 LT/EX/RG 验收矩阵严格对应；不得在未验证公司行动账本、订单锁定或阶段对应 LT-002 作用域前推进。
> 2. **验证范围（`validation_scope`）**：`BACKTEST_VALIDATED` 不再单独表达“验证了什么范围”；黄金切片结果必须显式标为 `GOLDEN_SLICE_PIPELINE`，不得被误读为全市场 alpha 证明。
> 3. **无 PIT 公司行动台账的边界收紧**：即使是探索级，也禁止使用可能跨公司行动日的多日收益、动量、波动率、相对强弱或依赖它们的标签；仅允许同日、非收益型原始量价特征作受污染探索。
> 4. **自由度配置化**：`declared_safety_latency`、开盘流动性代理/折扣和 outer-test 查看预算必须在配置、审计工件和敏感性分析中显式声明。
> 5. **文档版本清理**：当前强制规范文件名统一为 `BACKTEST_DESIGN_V3_0_2.md`；V2.1 仅作为历史修订记录保留，不再作为目录入口。
>
> **V3 关键订正（在 V2.1 之上叠加）**：
> 1. **黄金切片（golden slice）机制**：在完整 PIT 数据不可得时，允许构造一个刻意小、可完全 PIT 净化的子集，使**至少一个端到端结果**能合法到达 `BACKTEST_VALIDATED`，证明整条机器可信；其余全市场数据继续 `EXPLORATORY_TAINTED`。见 §0.5。
> 2. **LT-002 作用域随阶段扩张**：LT-002A/B 的"未来突变不改历史闭合输出"不变量，其断言对象随管道成长而扩张；Phase 0 仅断言数据可见集与调整因子，订单/成交/训练样本在其存在的阶段才纳入。修正 §3.3.5、§11.1、§13 退出条件。
> 3. **多日价格特征对 L2 公司行动台账的硬依赖**：任何 lookback>1 且可能跨除权除息日的收益类特征，缺少 PIT 公司行动因子时不是"低证据"，而是**机械失真**（分红/拆股被当成收益）。即便探索级也须 best-effort CA 因子。见 §1.2.2、§3.3.2。
> 4. **`declared_safety_latency` 纳入治理**：该旋钮会平移所有 PIT 边界，必须有保守默认值、强制审计并进入敏感性分析，不得由研究者随手设小。见 §1.2.1。
> 5. **开盘容量不得用全日 ADV**：V1 在 T+1 开盘集合竞价成交，其流动性仅为全日一小部分；容量上限须参照开盘竞价量级或对全日 ADV 显著折扣。见 §9.7。
> 6. **outer test 是有限预算**：基于当前数据长度，outer test 的"查看次数"是被显式管理的稀缺资源，从第一天起配给。见 §7.5、§11.3。
> 7. **有效可用样本远小于名义**：预热窗 + lookback + 持有期 + embargo 会吃掉大半窗口，叠加幸存者污染后真正可信的体量很小；须对"地基能盖多高"保持清醒。见 §7.3、§15。

> **V2.1 历史记录**：2026-06-30（Asia/Shanghai）。本行以下为 V2.1 原始订正记录，仅保留作为历史，不构成当前强制规范入口。
>
> **V2.1 关键订正（必须在任何模型训练前实施）**：
> 1. 原始市场价格层仅允许保存**未复权** OHLCV；`hfq`、`qfq` 和供应商复权序列只能处于隔离的派生/研究层，绝不作为 raw 或 PIT 真相来源。
> 2. LT-002 被拆为原始数据、公司行动和派生价格三类未来突变测试；比较对象按“离散工件精确相等 / 浮点工件容差相等 / 严格确定性哨兵模式”分层，不要求普通 GPU 深度学习重训的权重逐字节一致。
> 3. 所有订单的股数、价格上限、现金预留和有效期必须在 `decision_ts` 锁定；T+1 开盘只允许执行、拒绝或按已声明规则取消，不得根据实际开盘价重新凑目标权重。
> 4. 公司行动同时影响特征/标签和执行账本；必须由同一份 PIT 公司行动台账驱动，分别使用 PIT 派生服务与 Corporate Action Handler。
> 5. 数据因果性测试不能替代研究治理。探索性、受污染研究允许存在，但 taint 必须传播且只能阻止“晋级”，不能被包装为可信证据。

---

## 0. 使用范围、边界与非目标

### 0.1 本规范支持的首个真实使用场景

**主场景（V1）**：

- 中国境内普通 A 股，日频 OHLCV 数据；
- 长仓、无杠杆、无融券、无期权、无期货、无 ETF、无可转债；
- 收盘后生成信号，最早于下一交易日开盘执行；
- 以横截面选股 / 多股票排序为主要任务；
- 可配置日频、周频或月频调仓，但首个可信基线建议为**周频调仓**，降低成本、容量和日频噪声的影响；
- 回测引擎为所有可报告业绩的唯一真相层。

**允许的后续扩展，但必须单独增加规则适配器与测试套件**：创业板、科创板、北交所、ETF、融资融券、股指期货、盘中数据、VWAP、止损单、行业中性组合、优化器、在线训练、实盘接口。

### 0.2 明确不做的事情

以下内容不得在 V1 代码中“顺手假设”或半实现：

- 将当前沪深 300 / 中证 500 成分股回测历史的结果表述为可信或用于晋级；若为受污染探索，必须走 `EXPLORATORY_TAINTED` 管道；
- 用日线数据假装精确模拟盘中 VWAP、盘口排队、开板概率或止损触发顺序；
- 用未带发布时间的财务、公告、行业分类或指数成分数据；
- 把模型预测结果直接解释为投资建议；
- 用回测表现反向修改测试集、规则表、特征集或交易成本，再继续把同一测试集当“样本外”。

### 0.3 研究与实盘的关系

回测只能检验历史上的规则一致性，不能证明未来盈利。模型上线前至少要经过：

1. 全部防泄露和执行真实性测试；
2. 冻结的最终样本外评估；
3. 纸面交易 / 模拟盘逐日比对；
4. 小规模、可随时止损和可审计的实盘试运行。

### 0.4 研究状态、污染标记与“允许探索、禁止晋级”

本项目把“研究能否运行”和“结果能否作为证据”严格区分。数据不完美时，不允许绕过系统做影子研究；但也不把早期探索误当作可发布证据。

每个数据集、特征、标签、模型、回测与报告必须带一个 `evidence_status`，并沿依赖图向下传播：

```text
EXPLORATORY_TAINTED
  可用于：跑通管道、调试、提出候选假设、人工排错、生成非结论性探索图。
  不可用于：策略/特征/参数“获胜”证明、正式业绩结论、模拟盘/实盘晋级。

VALIDATION_ELIGIBLE
  数据血缘、可得性、样本切分和执行假设满足本规范；
  可用于：开发区和验证区的候选比较、模型/参数选择。

BACKTEST_VALIDATED
  通过完整 PIT、执行、研究治理与锁定 outer test 门禁；
  可用于：模拟盘/实盘晋级决策，但仍不等于未来盈利保证。

BLOCKED_BY_DATA_PROVENANCE / UNSUPPORTED
  数据时间语义或执行规则无法证明；不得进入训练、回测或任何可解释为业绩的报告。
```

### 0.4.1 验证范围（`validation_scope`）：`BACKTEST_VALIDATED` 必须说明“验证了什么”

`evidence_status` 表示该结果是否通过相应的证据门禁；`validation_scope` 表示这些门禁覆盖的**市场范围与验证目的**。两者必须同时存在，且 `validation_scope` 不得在下游报告中被扩大、替换或省略。

```text
EXECUTION_FIXTURE
  仅验证某一执行规则、账本事件或回归 fixture；不得用于任何 alpha、策略容量或全市场结论。

GOLDEN_SLICE_PIPELINE
  在成员和时间窗口已预先冻结、逐条 PIT 核验的黄金切片上验证端到端管线；
  可以达到 BACKTEST_VALIDATED，但只证明该范围内的管线/账本诚实可信，
  不证明策略在全市场有效，也不得用于全市场业绩表述。

FULL_UNIVERSE_BACKTEST
  在 PIT 合格的完整目标股票池、历史状态和执行规则上验证；
  才可作为全市场历史策略表现的证据，仍须受 outer test、研究治理和敏感性门禁约束。

EXPLORATORY_UNIVERSE
  使用受污染、能力不完整或仅为工程探索的数据；必须与 EXPLORATORY_TAINTED 一致，
  不得用于晋级。
```

强制规则：

- 每个 `experiment`、报告、API 响应和晋级命令必须保存 `validation_scope`、`validation_scope_manifest_hash`；
- `GOLDEN_SLICE_PIPELINE` 不得被任何聚合报告、图标题或 API 简写为“全市场 backtest validated”；
- `FULL_UNIVERSE_BACKTEST` 只能由完整 PIT universe manifest、历史状态、公司行动与执行规则共同支持；
- 任一依赖受污染 universe 的产物即使使用了黄金切片模型，也不能改变其自身的 `validation_scope` 或 taint。

**taint 传播规则**：只要某个上游输入、股票池、横截面计算、标签或规则表是 `EXPLORATORY_TAINTED`，其下游特征、标签、模型、回测和报告默认同样 tainted；只有使用完整合格输入重新运行，才可解除。不得通过“只在最后一步换一个干净文件”洗白先前的模型选择。

**出口门禁而非入口封死**：系统允许运行 `EXPLORATORY_TAINTED` 实验，但所有产物必须在文件名、报告首页、指标表和 API 返回值中显式携带该状态；任何晋级命令遇到 taint 必须 fail closed。

### 0.5 黄金切片（golden slice）：在数据不完整时也能证明机器可信

完整 PIT 数据（历史成分、退市股、首次披露 vintage、完整公司行动台账）在免费/低价数据源下基本不可得。若严格执行本规范，可能长期没有任何产物能离开 `EXPLORATORY_TAINTED`，使"先做出可信进展"在实践中无法达成。V3 用**黄金切片**解决这个死结，而**不放松任何真实性标准**。

定义：

```text
黄金切片 = 一个刻意小、但每一项 PIT 事实都可逐条人工核验的证券子集 + 时间窗口。
典型构造：
  - 十几只从不退市、极少财务重述的超大盘股；
  - 一段较短、可负担人工建账的窗口；
  - 手工建立并双人核验的 L2 公司行动台账（除权除息、拆股、送转）；
  - 明确记录的 available_at 与保守 safety latency。
```

用途与边界：

```text
允许：在黄金切片上运行完整 PIT、执行、治理门禁，使该切片的结果合法到达 BACKTEST_VALIDATED。
目的：证明整条流水线（数据→特征→标签→切分→执行→账本→报告）端到端诚实可信。
禁止：把黄金切片上验证过的"机器可信"外推为"策略在全市场可信"。切片证明的是管线，不是 alpha 的普适性。
```

黄金切片必须在任何策略结果、因子排名、模型比较或参数选择之前冻结，形成不可变 `golden_slice_manifest`：

```text
golden_slice_manifest_id / hash
validation_scope = GOLDEN_SLICE_PIPELINE
security_id 清单、时间窗口、交易日历版本
选择理由与排除理由
公司行动逐条来源、available_at、复核人和复核时间
安全延迟配置版本、规则表版本、允许的特征类别
selection_frozen_at
selection_independent_of_strategy_results = true
```

禁止依据某个策略、模型、参数或收益曲线“挑出表现更好”的黄金切片。若成员、窗口、公司行动核验方式或可见性假设发生变化，必须创建新 manifest，并使旧结果不能与新切片结果混合比较。

`evidence_status` 仍逐切片判定：黄金切片产物可为 `VALIDATION_ELIGIBLE` / `BACKTEST_VALIDATED`；同一实验中的全市场产物若仍用受污染股票池，照常 `EXPLORATORY_TAINTED`，taint 不因黄金切片存在而被洗白。

### 0.6 双轨并行：执行层不依赖 PIT 台账，可先行

并非所有工作都被 PIT 数据缺口阻塞。明确区分两轨，避免在地基数据齐备前空等：

```text
轨道 A（执行层，立即可做，不依赖完整 PIT）：
  事件驱动时钟、T+1 库存与现金账本、开盘保守成交、涨跌停拒单、停牌冻结、
  lot size、费用/税费、容量约束、订单 T 日锁定、CorporateActionHandler 骨架。
  这些只需 L1 未复权日线 + 规则表，可在 EXPLORATORY 数据上先把执行真实性测通（EX-*）。

轨道 B（PIT 可信层，受数据进度约束）：
  L2 公司行动台账、历史成分/退市/ST、首次披露 vintage、黄金切片净化。
  完成度决定哪些结果能晋级，按数据可得性逐步推进，不阻塞轨道 A。
```

### 0.6.1 Phase 1 的公司行动分期：不放宽，只隔离实现目标

Phase 1 的目标是证明执行时钟和账本诚实，不是证明信号有 alpha。因此必须采用以下分工：

```text
A. 主执行路径：使用固定、确定性、非收益型 dummy strategy 产生订单。
   它不得调用动量、累计收益、波动率、相对强弱或任何依赖 PITAdjustmentService 的多日收益特征。

B. 公司行动路径：EX-011 至 EX-014 必须用独立的构造 fixture 覆盖。
   Phase 1 最小支持集 = 现金分红（除息→应收→派息到账）+ 拆股/送股/转增（股数、成本基、可卖日期）。
   配股、合并、退市可先实现“识别 + 审计 + 显式阻断”；正确阻断即满足 EX-013，禁止静默当作未发生。

C. 历史集成 fixture：可以为了验证订单/成交主干而使用预先声明的、已知无公司行动窗口，
   但该窗口只能作为 EXECUTION_FIXTURE；不得把“未来无公司行动”变成真实策略的股票筛选条件，
   也不得以此窗口的净值作任何 alpha 或全市场业绩结论。
```

简言之：**Phase 1 不需要放宽 CA，也不需要先有动量；它只需要用哑策略把诚实的执行账本跑通。**

---

## 1. 术语、时钟与不可违反的时间语义

### 1.1 统一时区和交易日

- 所有时间戳使用带时区的 ISO-8601：`Asia/Shanghai`。
- `trade_date` 是交易所交易日，不是自然日；不得用“周一到周五”自行推断交易日。
- 所有跨日、滚动窗口、持有期、禁运期、上市天数均按**交易日历**计算。
- 同一个证券在同一个交易日的同类事件必须有确定的排序键：`event_ts, source_sequence, ingested_at`。

### 1.2 数据时间字段、版本能力与价格语义

#### 1.2.1 每条可进入 PIT 链路的数据至少拥有的字段

| 字段 | 含义 | PIT 中的作用 |
|---|---|---|
| `event_ts` | 事件实际发生 / 市场会话所属时刻，例如某交易日收盘 | 不能晚于该记录描述的事件 |
| `published_at` | 交易所、公司或数据源首次向市场公开的时间 | 不能用报告期末替代 |
| `effective_at` | 规则、成分、证券状态或公司行动实际开始生效的时刻 | 不自动等于市场已知时刻 |
| `provider_delivery_at` | 合格订阅者最早可稳定取得该记录的时间 | 用于构造可用时间 |
| `available_at` | 历史回放时合格市场参与者可使用该记录的最早时间 | **唯一 PIT 可见性门槛** |
| `ingested_at` | 本系统实际接收/写入记录的时间 | 仅用于运行审计，不等于历史可知时间 |
| `revision_id` | 同一事实的版本/修订编号；若源不支持则显式为 `UNKNOWN` | 防止静默覆盖历史值 |
| `source_vintage_capability` | `FULL_AS_FIRST_REPORTED` / `PARTIAL` / `NONE` | 决定字段可否用于可信研究 |
| `snapshot_id` | 实验冻结使用的数据快照 | 可复现与审计 |
| `price_basis` | `RAW_UNADJUSTED` / `PIT_DERIVED` / `VENDOR_ADJUSTED` | 防止复权语义混淆 |

**注意：可见性不是所有表统一使用 `event_ts <= asof_ts`。** `event_ts` 只描述事件/观测时点；不同表的合法可见性和生效性必须由表级策略决定：日线 bar 需要会话结束后可见；公告/财报可在 `available_at` 后使用；已公告但尚未除权的公司行动可作为“已知未来事件”读取，但不得被提前用于历史价格调整；规则与成分还需同时满足有效区间。

构造规则：

```text
available_at = max(published_at, provider_delivery_at) + declared_safety_latency
ingested_at  = 本系统实际接收时间（仅用于实盘/模拟盘运行审计）
```

历史回放以 `available_at` 为准；实盘和模拟盘额外要求 `ingested_at <= decision_ts`。不能把今天下载历史数据的 `ingested_at` 当作十年前的不可得证明。

**`declared_safety_latency` 治理（V3）**：该值会整体平移所有 PIT 可见性边界，偏小即系统性乐观（假设比现实更快可得/可反应）。因此它不是研究者可随手填的旁参数：

- 必须有**保守默认值**（如日线收盘后字段：至少推迟到当日收盘可稳定获取之后；公告/财报类：在 `published_at` 基础上再加保守缓冲）；
- 每个数据源/字段的 latency 设定必须写入 `data_source_capability` 并附依据，进入实验工件审计；
- 必须对 latency 做**敏感性分析**（与成本、容量同等对待）：把 latency 调大若结论崩溃，说明结论建立在乐观可得性假设上；
- 禁止为改善回测表现而调小 latency；任何变更产生新的能力声明版本。

若数据源无法提供可信的 `published_at` / `provider_delivery_at`：

- 交易所日线 O/H/L/C/V：可在明确且保守的“日线可用时间”假设下使用；
- 财务、公告、指数成分、历史行业分类、ST 状态、股本、自由流通市值：默认 `EXPLORATORY_TAINTED` 或 `BLOCKED_BY_DATA_PROVENANCE`；
- 严禁以报告期末、文件名日期、下载日期、当前网页内容或数据商最新值代替公开时间。

#### 1.2.2 版本能力不等于字段存在

`revision_id` 是架构预留字段，不代表免费数据源一定能提供“首次披露值—后续修订值”的完整 vintage 历史。每个来源必须在 `data_source_capability` 中声明：

```text
FULL_AS_FIRST_REPORTED  : 可回放首次披露及修订序列，可用于重述敏感的基本面/公告 PIT。
PARTIAL                 : 有部分发布时间或版本信息；只能用于经批准的有限字段。
NONE                    : 无可靠 vintage；不得把重述敏感字段用于可信基本面训练。
```

系统禁止把 `NONE` 伪装成 `FULL`。V1 可以只做价格/成交量型研究；等拿到合格数据后再启用基本面、公告和历史成分特征。

**重要订正（V3）：价格-only ≠ 不需要公司行动数据。** 任何 `lookback>1` 且可能跨越除权除息日的收益类特征（动量、波动率、相对强弱等），用未复权价直接计算会把分红/拆股造成的价格跳变**误当成真实收益**——这不是"证据等级低"，而是**机械失真**（A 股分红集中于 5–7 月，会在因子里注入季节性假信号）。因此：

- 即便只做价格/成交量研究，任何多日收益特征也必须经 `PITAdjustmentService` 用 **best-effort PIT 公司行动因子**还原真实收益；
- 缺少足够完整的 L2 公司行动台账时，这些特征**默认 `EXPLORATORY_TAINTED`**，且报告须显式标注"跨除权日收益可能失真"；
- 仅 `lookback==1` 且不跨除权日的当日量价特征，可在无 CA 台账时作有限使用。

### 1.3 四个关键时刻

每个样本、信号和订单都必须显式保存：

```text
feature_asof_ts   # 特征窗口最后允许观察的时刻
signal_ts         # 模型产生预测 / 排名的时刻
decision_ts       # 策略锁定订单意图的时刻
execution_ts      # 委托实际进入模拟撮合 / 真实市场的时刻
```

V1 的标准日频约定：

```text
T 日收盘数据成为可用信息
  -> T 日收盘后生成特征、预测与目标组合
  -> 生成仅对 T+1 开盘有效的订单意图
  -> T+1 开盘按固定、保守的日线成交规则尝试成交
```

**禁止**：用 T 日收盘价或 T 日任何收盘后得到的字段，在 T 日收盘成交。

---

## 2. 强制架构：每一层的职责、输入和拒绝条件

```text
原始数据与快照
      │
      ▼
PIT Data Portal ──> 特征库 ──> 样本/标签库 ──> 训练与验证 ──> 信号服务
      │                  │               │                │              │
      │                  └── 时间契约 ───┴── 切分契约 ───┴── 模型工件 ──┘
      ▼
事件驱动交易时钟 ──────────────────────────────────────────────> 订单 / 成交 / 账本 / 绩效
```

### 2.1 真相来源与单向数据流

1. **PIT Data Portal** 是唯一允许直接读原始数据的模块。
2. 特征模块只能通过 `PITDataPortal` 按 `asof_ts` 查询数据；不得读取完整 DataFrame 后自行筛日期。
3. 标签模块可以访问未来价格来计算结果，但标签必须标记成熟时间；训练器只可使用在重训时点之前已成熟的标签。
4. 回测策略只能得到 `StrategyContext(asof_ts=clock.now)`，不能获得任意结束日期的数据表。
5. 执行引擎只能接收已锁定的订单意图；它不得改写信号排序，也不得因为当天后续行情自动“换一只更容易成交的股票”。
6. 任何模块读取的数据都必须带 `snapshot_id`、`available_at` 和来源字段；缺失则抛出 `DataContractError`。

### 2.2 建议目录结构

```text
project/
├── configs/
│   ├── experiments/
│   ├── rulesets/
│   ├── universes/
│   └── data_sources/
├── src/
│   ├── domain/           # timestamps, contracts, events, enums, errors
│   ├── calendar/         # exchange sessions and trading dates
│   ├── data/             # ingestion, snapshots, PITDataPortal
│   ├── universe/         # security master, eligibility, membership histories
│   ├── features/         # feature definitions, compiler, manifests
│   ├── labels/           # targets, maturity logic, label manifests
│   ├── ml/               # splitters, pipelines, tuning, model registry
│   ├── execution/        # broker simulator, rulebook, fees, fills, ledger
│   ├── portfolio/        # target weights, constraints, rebalancing
│   ├── reporting/        # performance, diagnostics, audit report
│   └── orchestration/    # reproducible experiment runner
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── regression/
│   └── leakage_sentinels/
├── data_manifest/
├── artifacts/            # immutable experiment outputs
└── BACKTEST_DESIGN_V3_0_2.md
```

### 2.3 工具边界：工具用于什么，不能替代什么

| 工具 / 组件 | 合法用途 | 禁止替代的职责 |
|---|---|---|
| Parquet + DuckDB / PostgreSQL | 冻结数据快照、PIT 查询、审计与哈希 | 不能因“查询很快”就向策略暴露完整历史表 |
| Pandas / Polars / NumPy | 特征研究、面板计算、数据校验 | 不能作为最终回测成交真实性的替代 |
| scikit-learn Pipeline | 折内 imputer / scaler / selector / 模型串联 | 不能使用普通随机 K-fold 处理金融时间序列 |
| LightGBM / XGBoost | V1 的非线性基线模型 | 不能绕过自定义时间 splitter 或外层样本外锁定 |
| PyTorch | 经门禁批准后的序列 / 深度学习模型 | 不能用无因果 mask 的 Transformer 或全样本预训练 |
| Optuna / 搜索器 | 内层训练区中的超参数搜索 | 不能读取 outer test 指标作为优化目标 |
| MLflow / W&B / 本地 registry | 试验、参数、模型与工件记录 | 不能替代冻结数据、代码哈希和测试门禁 |
| vectorbt / backtrader / Qlib 等第三方框架 | 快速研究、对照或数据工具参考 | 不能作为本项目唯一可信执行真相层；最终成交账本仍由本项目事件引擎生成 |

### 2.4 Agent 实施硬规则

Codex Agent 必须：

- 先实现数据契约、交易日历、事件时钟、规则表和测试，再实现任何 ML 模型；
- 每新增一个字段、特征、标签或规则，补充其 manifest 与单元测试；
- 不得用 `TODO`、静默回退、吞异常或“临时全量读取”绕过 PIT；
- 遇到缺少历史时间戳的数据，标记 `BLOCKED_BY_DATA_PROVENANCE`，而非猜测；
- 每次实验只从配置文件读取参数；不得在 notebook / 脚本中散落硬编码交易费率、日期边界或特征列表；
- 在任何报告产生前运行完整测试门禁；测试失败则不得生成“策略收益结论”。

---

## 3. PIT 数据契约：从源头防止未来函数

### 3.1 必备数据表、数据层级与最小字段

#### A. `security_master_pit`

```text
security_id, ticker, exchange, board, instrument_type,
list_date, delist_date, name, status_flag, is_st, is_risk_warning,
record_effective_at, event_ts, published_at, available_at,
revision_id, source_vintage_capability, snapshot_id
```

用途：识别证券生命周期、板块、最小交易单位、涨跌幅规则、ST / 风险警示状态、退市状态。

#### B. `daily_bar_raw`（唯一价格真相层）

```text
security_id, trade_date, open, high, low, close, volume, amount,
trade_status, event_ts, available_at,
price_basis=RAW_UNADJUSTED,
source_id, revision_id, snapshot_id
```

强制规则：

- 只保存未经未来复权重写的真实市场 O/H/L/C/V；
- 数据库约束：`price_basis == RAW_UNADJUSTED`，否则拒绝写入；
- 缺失行不等于成交量为零；必须用 `trade_status` 区分停牌、未上市、退市、源数据缺失和异常；
- `hfq`、`qfq`、供应商前/后复权、未知复权因子均不得写入本表。

#### C. `corporate_actions_pit`（公司行动真相层）

```text
security_id, action_id, action_type,
announcement_at, record_date, ex_date, entitlement_date,
payable_date, shares_available_date,
cash_dividend_per_share, stock_dividend_ratio, split_ratio,
rights_ratio, rights_price, merger_terms, delisting_terms,
published_at, available_at, event_ts, revision_id,
source_vintage_capability, snapshot_id
```

该表是两套不同但一致的组件共同依赖的来源：

1. `PITAdjustmentService`：生成特征/标签中的 PIT 收益与调整因子；
2. `CorporateActionHandler`：更新真实持仓股数、成本基、应收现金与可卖状态。

#### D. `fundamental_release_pit`（未满足 PIT 前不得启用）

```text
security_id, report_period_end, announcement_at, event_ts, available_at,
metric_name, metric_value, currency, filing_version,
revision_id, source_vintage_capability, snapshot_id
```

#### E. `index_membership_pit`（未满足 PIT 前不得作为可信交易池）

```text
index_id, security_id, member_effective_from, member_effective_to,
announcement_at, event_ts, available_at, revision_id,
source_vintage_capability, snapshot_id
```

#### F. `market_rule_pit`

```text
rule_id, exchange, board, instrument_type, status_flag,
effective_from, effective_to, event_ts, available_at,
price_limit_rule, lot_size_rule, settlement_rule, fee_schedule_id,
source_reference, ruleset_version, snapshot_id
```

#### G. `data_source_capability`

```text
source_id, table_name, field_name,
source_vintage_capability,
published_time_quality, provider_delivery_time_quality,
price_basis, known_limitations, approved_research_tier,
assessed_at, assessor, snapshot_id
```

任何字段没有能力声明时，默认 `BLOCKED_BY_DATA_PROVENANCE`。

### 3.2 绝对禁止的数据替代方式

以下替代行为一律视作泄露或未审计数据：

- 用财报报告期末代替公告时间；
- 用“今天下载到的历史数据”默认当作当时可得；
- 用当前行业分类、当前证券简称、当前 ST 标记回填历史；
- 用后复权绝对价格、今日成分股或只存活股票池直接训练；
- `bfill()`、跨证券填充、在上市前填充、用未来财报向前填补；
- 在模型特征中直接使用数据商未解释版本语义的复权因子。

### 3.3 复权、公司行动与“原始层不可变”的强制分层

这是 V2.1 的首要订正。`hfq` / `qfq` / 数据商调整价不是“原始价格”，而是以某个未来截点、公司行动版本和供应商算法为条件的**派生量**。它们不得进入 raw 层，也不得作为 LT-002 的原始真相对象。

#### 3.3.1 四层数据模型

```text
L0 Source Landing（供应商落地层）
  原样保存下载文件与字段说明；可包含 hfq/qfq，但不可直接供策略使用。

L1 Immutable Raw Market Layer（不可变原始市场层）
  仅 RAW_UNADJUSTED OHLCV、原始成交量、原始交易状态；不可被未来公司行动回写。

L2 PIT Corporate-Action / Reference Layer（PIT 事实层）
  公司行动、证券状态、规则、公告和指数成员；每项带 available_at 与版本能力。

L3 Derived PIT Research Layer（按 as-of 计算的派生层）
  PIT 总回报、收益率、因子、标签、横截面统计；必须保存 derivation_asof_ts、输入快照和规则版本。

Q Vendor-Adjusted Quarantine（隔离区）
  hfq/qfq/不明调整价只能用于人工对照、数据质量检查或明确标记的探索；
  不得写入 L1，不得作为特征/标签/执行或 LT-002 依据。
```

#### 3.3.2 价格、特征、标签与执行的不同语义

**执行层**：永远使用 L1 的原始可交易 O/H/L/C、`RuleBook` 和 `CorporateActionHandler` 后的真实持仓数量/现金流。不得用任意复权价格模拟成交。

**特征层**：可以使用对数收益率、滚动收益率、波动率、成交额、相对成交量、同日横截面排名和 PIT 总回报；但在 `feature_asof_ts` 的调整因子只能包含：

```text
ex_date <= feature_asof_ts
AND available_at <= feature_asof_ts
AND source_vintage_capability 满足该特征的声明要求
```

**标签层**：未来收益可以使用未来发生的公司行动，因为标签本来描述未来结果；但该标签只能在 `label_observed_at` 后进入训练，且必须记录其公司行动版本/快照。

> 补充（V3）：上述特征层与标签层的"干净"程度，**完全受制于 L2 公司行动台账的完整性**。任何跨除权日的多日收益，其可信度不超过背后 CA 因子的可信度；台账不全 → 这些特征/标签按 §1.2.2 默认 taint，不得仅因"用了收益率"而视为安全。

#### 3.3.3 旧数据迁移与 firewall

任何既有或新下载的 `adjust='hfq'`、`adjust='qfq'` 或供应商调整价格文件必须执行：

```text
1. 从 data/raw/ 或任何被 PITDataPortal 读取的位置移出；
2. 写入 data/quarantine/vendor_adjusted/；
3. 标记 price_basis=VENDOR_ADJUSTED、source_semantics=UNVERIFIED_FOR_PIT；
4. 重建 L1 的 RAW_UNADJUSTED 日线数据与 L2 公司行动台账；
5. 在重新构建前，所有依赖该文件的回测仅允许 EXPLORATORY_TAINTED。
```

不得通过“给 hfq 文件改名为 raw”或在特征中只取收益率来规避该要求。收益率是否安全取决于它的调整因子能否按 as-of 重建，而不是文件名。

#### 3.3.4 公司行动的两个消费者必须同源

- `PITAdjustmentService.adjusted_return(asof_ts, start_ts, end_ts)` 只读取 `available_at <= asof_ts` 的公司行动，输出带 `adjustment_manifest_hash` 的派生收益。
- `CorporateActionHandler.apply(event, ledger, event_clock_ts)` 使用已生效公司行动更新真实股份、应收/可用现金、成本基和可卖资格。
- 两者不可共享“hfq 收盘价”作为快捷方式；二者共享的是 L1 原始价格与 L2 PIT 公司行动台账。

#### 3.3.5 不变量

对任意 cutoff `C`：在不改变 `available_at <= C` 的 L1/L2 记录的前提下，`C` 之后的新公司行动或供应商复权重算，不得改变任何 `decision_ts <= C` 的特征、训练样本选择、订单、成交或执行账本。该不变量由 LT-002A/B/C 验证。

**作用域随阶段扩张（V3 订正）**：该不变量断言的"历史闭合输出"集合，**随管道成长而扩张**，不能在产物尚不存在的阶段要求其全量通过：

```text
Phase 0（仅数据层）：断言对象 = 数据可见集 + PITAdjustmentService 调整因子。
                     此阶段无特征/订单/成交可断言。LT-002C（派生价格防火墙）此阶段即可全测。
Phase 1（事件驱动基线）：断言对象扩到 订单意图 / 成交 / 公司行动账本 / 净值。
Phase 2（特征/标签库）：断言对象扩到 特征矩阵 / 样本与标签选择。
Phase 3（walk-forward ML）：断言对象扩到 训练样本集 / fold / 预测 / 组合账本。
```

每个阶段的 LT-002A/B 只对"该阶段已存在的闭合输出"要求逐字节/容差不变；退出条件按此分阶段读取（见 §13）。

### 3.4 Data Portal、PIT 派生与隔离区的强制接口

伪代码：

```python
class PITDataPortal:
    def query(self, table: str, *, security_ids, asof_ts, columns, purpose, filters=None):
        assert table in APPROVED_PIT_TABLES
        rows = storage.read_snapshot(table, snapshot_id=self.snapshot_id)
        if table == "daily_bar_raw":
            assert set(rows.price_basis.dropna().unique()) <= {"RAW_UNADJUSTED"}
        visible = apply_table_visibility_policy(
            table=table,
            rows=rows,
            asof_ts=asof_ts,
            purpose=purpose,  # e.g. feature, universe, rule_resolution, corporate_action_notice
        )
        visible = apply_filters(visible, filters)
        assert_table_specific_visibility_invariants(table, visible, asof_ts, purpose)
        return immutable_view(
            visible[columns],
            asof_ts=asof_ts,
            snapshot_id=self.snapshot_id,
            lineage=build_lineage(rows, filters, purpose),
        )

# Table policy examples:
# daily_bar_raw: available_at <= asof_ts AND event_ts <= asof_ts
# fundamental_release_pit: available_at <= asof_ts
# corporate_actions_pit notice query: available_at <= asof_ts
# PITAdjustmentService adjustment query: available_at <= asof_ts AND ex_date <= derivation_asof_ts
# market_rule_pit: available_at <= asof_ts AND effective_from <= asof_ts < effective_to
# index_membership_pit: available_at <= asof_ts AND member_effective_from <= asof_ts < member_effective_to

class LabelDataPortal:
    def query_future_outcome_inputs(self, *, security_ids, start_ts, end_ts, label_spec):
        # 只能被 labels/ 模块调用；返回对象不可传入 strategy/features。
        ...

class PITAdjustmentService:
    def total_return_series(self, *, security_id, start_ts, end_ts, derivation_asof_ts):
        # 原始价格来自 L1；公司行动必须满足 available_at <= derivation_asof_ts。
        ...
```

实现要求：

- 策略、特征模块、组合构建器和执行引擎不得直接读取完整历史文件；
- 所有返回对象必须携带 `asof_ts`、`snapshot_id`、查询谓词摘要、输入哈希、`price_basis` 与 capability 状态；
- 不支持 `end_date=None` 的裸查询；
- 默认拒绝 `available_at`、所需的事件/生效字段、`snapshot_id`、`price_basis` 或来源能力声明缺失的数据；
- 不得把 `event_ts <= asof_ts` 当作所有表的通用过滤器；必须调用声明过的 `apply_table_visibility_policy`；
- `VendorAdjustedQuarantine` 无公开 API 可供策略/训练调用；只有数据质量工具可读；
- 任何“为了快而先读全表，再在下游筛选”的实现均不合格；
- 调用方无法通过公开 API 获取未来行；若确有标签计算需要，必须使用 `LabelDataPortal`，并且标签对象不能传入策略或特征模块。

---

## 4. 股票池与可交易资格：它本身是时变特征

### 4.1 两个集合必须分开

```text
prediction_universe(T)  # 模型在 T 可打分的股票集合
tradable_universe(T)    # 策略在 T 决定、并尝试于 T+1 开盘交易的集合
```

交易池不得用 T+1 的停牌、涨跌停、成交量或开板情况反向筛选。`tradable_universe(T)` 的每个过滤条件必须在 `decision_ts=T` 已知。

### 4.2 V1 可交易资格模板

所有参数配置化，以下仅为默认结构而非固定值：

```yaml
universe:
  instrument_type: A_SHARE_COMMON_STOCK
  exchanges: [SSE, SZSE]
  include_boards: [MAIN]
  include_delisted_history: true
  min_listed_trading_days: 250
  exclude_if_st_or_risk_warning: true
  require_not_suspended_at_decision: true
  min_trailing_amount_20d: REQUIRED
  min_price: REQUIRED
  max_missing_feature_ratio: REQUIRED
  use_index_membership: false  # only turn on with PIT constituent history
```

说明：

- V1 建议先在沪深主板普通股跑通全链路；引擎本身必须支持多板块规则，但创业板、科创板、北交所的结果只有在对应规则表和测试完备后才可报告。
- `include_delisted_history=true` 是防幸存者偏差的基础要求。
- 股票在 T 日可交易，不代表 T+1 能成交；T+1 实际成交由执行层处理，不能在 T 日预知。

### 4.3 指数成分股使用门槛与受污染探索

只有同时具备 `member_effective_from/to`、`announcement_at`、`available_at`、历史证券状态与退市证券时，才能做可信历史指数成分股回测。

若当前数据源只有“今天的成分股名单”，则：

```text
evidence_status = EXPLORATORY_TAINTED
允许：跑通接口、检查策略逻辑、提出候选研究问题。
禁止：将结果作为模型/特征/参数优劣证据；晋级模拟盘/实盘；对外或对自己表述为可信业绩。
```

系统必须自动将该 taint 写入 `universe_manifest` 并传播到所有下游标签、模型、回测和报告。只有用完整 PIT 成分与退市历史重新运行，才能产生 `VALIDATION_ELIGIBLE` 工件。

### 4.4 股票池、横截面与标签的 taint 传播

以下情况即使价格本身无未来行，也会使横截面研究受到偏差影响：

- 仅存活股票池；
- 当前指数成分回填历史；
- 当前行业、ST 或自由流通市值回填历史；
- 未来可交易性被用于当天排名或删除样本。

若标签是未来收益的**横截面分位、排名、相对基准排名或分组收益**，其计算股票池也必须记录 `label_universe_manifest_hash` 与 `taint_status`。不允许只给特征打 taint、却把受污染横截面标签标为干净。

---

## 5. 特征工程规范：特征必须可追溯、可审计、可拒绝

### 5.1 每个特征必须有 Feature Manifest

每一个特征定义都必须由机器可读 manifest 描述：

```yaml
feature_id: mom_20d
version: 1.0.0
owner: research
input_tables: [daily_bar_raw]
input_columns: [close, corporate_action_factor_pit]
lookback_trading_days: 20
feature_asof_rule: "inclusive through feature_asof_ts only"
availability_lag: "0 sessions after daily-close availability"
cross_sectional_operation: none
requires_fit: false
uses_label: false
price_basis: pit_total_return
missing_policy: null_and_indicator
allowed_in_live: true
required_source_vintage_capability: NONE_OK_FOR_PRICE_ONLY
input_taint_policy: propagate
output_evidence_status: derived_from_inputs
unit_test_ids:
  - FT-MOM-001
  - LT-002A
  - LT-002C
```

没有 manifest 的字段不得进入训练矩阵。

### 5.2 因果性规则

允许：

- `rolling(window, min_periods=...)` 仅使用当前及过去行；
- `shift(+k)` 表示滞后；
- 同日横截面排名 / z-score，前提是全部证券的输入都在当日决策时间可知；
- 仅在训练折拟合的 scaler、PCA、缺失值填充器、目标编码器和特征选择器。

禁止：

- `shift(-k)`；
- `rolling(..., center=True)`；
- `expanding()` / `rolling()` 后把未来统计量回填历史；
- 整段样本的均值、标准差、最值、分位数进入历史特征；
- 用完整十年数据训练无监督 embedding、PCA、autoencoder，再对早年样本作预测；
- 一切以“数据已经下载到本地”为理由的未来预训练。

### 5.3 横截面处理的正确边界

同一交易日的横截面 rank / robust z-score 是允许的，但必须：

1. 仅在 `prediction_universe(T)` 或明确定义的历史可见集合中计算；
2. 只使用 `available_at <= T` 的值；
3. 对缺失值保留缺失掩码，不得默认为 0；
4. 不得将 T+1 是否可成交作为 rank 的过滤条件；
5. 所有横截面成员、过滤条件和极值裁剪参数写入实验工件。

### 5.4 缺失值处理

- `NaN`、停牌、未上市、数据源缺失和“不适用”必须是不同状态；
- 禁止跨证券填补；
- 禁止向过去 `bfill`；
- 对财务类信息仅可从最近一次**已披露且已可用**数值向后填充，并保留 `age_since_release`；
- imputer 必须在训练折 `fit`，验证 / 测试折仅 `transform`；
- 缺失率超过阈值的日期或证券必须被记录，而不是静默删除。

### 5.5 特征计算后的通用断言

```python
assert feature.event_ts <= feature.feature_asof_ts
assert feature.source_available_at <= feature.feature_asof_ts
assert feature.window_max_ts <= feature.feature_asof_ts
assert feature.snapshot_id == experiment.snapshot_id
assert feature.price_basis != "VENDOR_ADJUSTED"
assert feature.evidence_status == propagate_status(feature.input_statuses)
assert not feature_uses_future_shift(feature_definition)
```

---

## 6. 标签规范：未来可以存在于标签中，但不能提前进入训练

### 6.1 标签与真实执行必须同一时间线

V1 推荐将主标签定义为“从下一交易日开盘到固定持有期结束开盘”的收益，而不是模糊的“未来 N 日收盘收益”。示例：

```text
特征截止：T 日收盘
订单意图：T 日收盘后
理论入场：T+1 开盘
理论出场：T+H+1 开盘
标签：入场至出场的 PIT 总回报 / 相对基准收益
```

每个标签至少记录：

```text
security_id, sample_id, decision_ts, feature_asof_ts,
entry_ts, exit_ts, label_value, label_type,
label_start_ts, label_end_ts, label_observed_at,
entry_price_basis, exit_price_basis,
corporate_action_manifest_hash,
label_universe_manifest_hash, evidence_status,
source_snapshot_id, revision_id
```

### 6.2 标签成熟度（最重要的训练门禁）

一个样本只有当以下条件全部满足时，才能被用于在 `refit_ts` 的训练：

```text
label_end_ts < refit_ts
label_observed_at <= refit_ts
all feature inputs available_at <= feature_asof_ts <= refit_ts
all label-source records required to compute label are observable by label_observed_at
```

训练器必须以**每条样本的实际 `label_end_ts`**检查，而非仅使用“持有期 N 天”的粗略全局 embargo。停牌、延迟退出、路径依赖标签、公司行动和缺失数据可能使成熟时间不同。

若退出价来自日线文件，而该文件只在当日收盘后可稳定获得，则 `label_observed_at` 必须晚于该文件可用时间，不能把“当天开盘价”当成训练器在开盘时已获知的日线信息。

### 6.3 标签禁止事项

- 不能因为 T+1 涨停、停牌、未成交或未来流动性差而把样本从训练标签中删除；这会用未来可交易性改变训练分布；
- 不能在标签计算后把无法交易股票从历史中悄悄去掉；
- 不能使用含未来公司行动的后复权绝对价格；
- 不允许用“未来最高价是否超过阈值”做分类标签后，再以日线数据模拟精确止盈路径；若做 path-dependent 标签，必须增加更高频数据与路径规则；
- 不得在 `label_observed_at` 之前让训练器、特征选择器、超参搜索器或阈值选择器访问标签。

### 6.4 标签 taint 传播

标签不仅是数值，也依赖其计算集合和价格语义：

- 单证券未来原始/PIT 总回报标签：继承价格源、公司行动源和处理规则的 taint；
- 横截面收益分位、排名、超额分组标签：额外继承 `label_universe` 的 taint；
- 若标签需要指数、行业或全市场基准，则该参考序列同样必须 PIT 合格；
- 标签 status 必须通过 `LabelManifest` 传播到训练集、模型、回测和报告。

### 6.5 标签版本化

任何变更以下内容都必须产生新 `label_version`，不得复用旧实验：

- 入场和出场价格；
- 持有期；
- 基准；
- 是否包含固定成本、税费或滑点；
- 复权 / 公司行动计算方式；
- 股票池与横截面分组定义；
- 缺失、停牌和退市处理；
- `label_observed_at`、价格可用性或版本能力假设。

## 7. 训练、验证、超参搜索：时间优先，折内拟合一切

### 7.1 不可使用的切分方式

以下方式一律禁止：

```python
train_test_split(..., shuffle=True)
KFold(..., shuffle=True)
StratifiedKFold(...)
random_split(dataset)
```

深度学习中，**训练集确定之后**可以对训练样本顺序做固定随机种子 shuffle；但不得随机决定哪些日期进入训练、验证或测试。

### 7.2 唯一允许的基础切分器

实现自定义 `WalkForwardPurgedSplitter`。它必须以**决策日期组**为基本单位，不得把同一天不同股票分到训练与验证两边。

输入：

```text
sample_id, security_id, decision_ts, label_end_ts, feature_asof_ts
```

输出的每个 fold 必须保证：

```text
max(train.label_end_ts) < min(validation.decision_ts)
train samples whose label interval overlaps validation decision interval are purged
embargo is applied after validation/test block as configured
all train dates < all validation dates < all outer-test dates
```

### 7.3 推荐的嵌套时间结构

```text
历史数据
├── 开发 / 训练区：用于特征开发与内层 walk-forward 调参
├── 验证区：用于候选策略、阈值、交易频率、持有期的最终选择
└── 锁定样本外测试区：只在研究冻结后运行一次
```

示例（实际由配置决定）：

```yaml
split:
  min_train_trading_days: 756        # 约 3 年
  validation_block_days: 126         # 约 6 个月
  step_days: 21                      # 约 1 个月
  embargo_days: "derived_from_label_span"
  outer_test_start: REQUIRED
  outer_test_locked: true
```

`embargo_days` 不能盲目等于固定 N；它至少覆盖标签最远终点与任何会导致样本依赖的持有期 / 事件窗口。实现时应根据每条样本的 `label_end_ts` 进行 interval purge，而不是只靠一个粗糙 gap。

**有效可用样本远小于名义（V3）**：名义"十年×数百只"会严重高估可信体量。`min_train_trading_days`（约 3 年首训练窗）+ 特征 lookback + 标签持有期 + embargo 共同吃掉相当大一段前缀；剩余真正进入 walk-forward 样本外检验的跨度可能只有数年。叠加幸存者污染（若用今日成分），可信样本进一步缩水。实现与解读时必须：(a) 在实验工件中显式记录"名义跨度 / 预热消耗 / 有效样本外跨度"三个数字；(b) 据此对"地基能支撑多复杂的模型"保持清醒——有效样本越少，越应停留在线性/树模型，越要警惕深度学习的过拟合。

### 7.4 所有可学习步骤必须折内拟合

以下对象必须在每个训练折重新创建和 `fit`：

- imputer；
- scaler / winsorizer / quantile transformer；
- PCA / autoencoder / embedding；
- feature selector；
- target encoder；
- 采样器、类别权重和样本权重估计器；
- 超参数搜索器；
- early-stopping 的训练 / 内部验证切分；
- 任何行业 / 市值中性化回归中的拟合系数。

验证和测试只能调用已拟合对象的 `transform` / `predict`。

### 7.5 研究治理、多重检验与“LT-002 不是万能证明”

LT-002 只能验证“未来数据变化不会反向改变历史闭合输出”；它是必要条件，但**不能**发现以下人为流程泄露：

- 反复查看同一验证/测试区结果后挑选特征、阈值、模型或调仓频率；
- 从大量 trial 中只汇报最优者；
- 根据 outer test 的失败原因改规则后继续把它叫 outer test；
- 把受污染探索结果固化为代码常量，再用干净数据“验证”。

因此必须建立独立于数据防火墙的研究治理：

1. **假设登记**：每个候选策略在运行前记录研究问题、输入、目标、预期机制、决定规则和允许的搜索空间；
2. **全 trial 日志**：一个实验 run = 一个不可变配置、一个数据快照、一个代码提交哈希、一个随机种子集合。失败和差结果也必须保留；
3. **开发区 / 验证区 / 锁定 outer test 分离**：开发区用于写特征和调试，验证区用于候选比较，outer test 只能在研究冻结后访问一次；
4. **污染后的处理**：若 outer test 已参与任何设计选择，它立即标记 `CONTAMINATED`；必须重新划出未来时间段作为新 outer test；
5. **模型选择单位**：以决策日期、折和独立时间块为单位，而不是把每日数千股票样本误当 IID 重复证据；
6. **多重检验披露**：记录已尝试的模型/特征/阈值/频率/持有期组合数量；在可行时报告 deflated Sharpe、Reality Check / SPA、block bootstrap 或其他对串相关和试验次数稳健的诊断；
7. **受污染探索**：`EXPLORATORY_TAINTED` 允许产生候选假设，但不得作为优胜证据。候选必须在 `VALIDATION_ELIGIBLE` 数据上从定义到回测重新运行；
8. **固定决策规则**：晋级/淘汰阈值必须在 outer test 前写入配置，不得根据最终净值图临时修改。
9. **outer test 是有限预算（V3）**：基于当前数据长度，可用的独立样本外段数极少（可能仅 1–2 段）。outer test 的"查看次数"必须作为**稀缺资源从第一天起配给**：维护一个 outer-test 访问账本，记录每次查看的时间、目的与当时冻结的配置；每查看一次即消耗预算，污染后向未来滚动的新段会进一步缩短剩余可用历史。严禁把 outer test 当作可反复试错的开发集。


---

## 8. ML / 深度学习专项规范

### 8.1 模型推进顺序

任何深度学习模型之前，必须依次完成并保存比较结果：

1. **Phase 1 执行基线**：固定、确定性、非收益型 dummy strategy，仅用于驱动并验证订单、成交与账本；
2. **Phase 2 起的无 ML 信号基线**：在 PIT 公司行动链路足以支撑多日收益后，才允许简单截面动量等规则信号；
3. 线性 / 正则化线性模型；
4. 树模型（例如梯度提升树）；
5. 仅在前述模型已通过全部门禁且显示稳定增益后，才引入 MLP、LSTM、TCN、Transformer 或图模型。

深度学习不是“更高级的默认选择”。它扩大了参数搜索空间和过拟合风险，也更容易隐藏归一化、序列拼接和注意力掩码错误。

### 8.2 序列模型的额外约束

对于每个序列样本 `X(i, t-L+1 ... t)`：

```text
max(token.event_ts) <= feature_asof_ts(t)
max(token.source_available_at) <= feature_asof_ts(t)
label begins strictly after feature_asof_ts(t)
```

- Transformer 必须使用**因果 attention mask**；禁止双向注意力读取未来 token。
- RNN / LSTM 的 hidden state 不得跨越训练、验证、测试边界传递。
- padding、mask、归一化统计量和序列截断规则必须保存在 model artifact 中。
- 图模型的边、相似度、协方差、行业映射必须在 `asof_ts` 可得；不得用全样本未来相关性建图。
- 无监督预训练、对比学习、autoencoder 和 embedding 的训练数据也只能来自当前训练折；“没有标签所以没关系”是错误的。

### 8.3 BatchNorm、随机性、确定性模式与比较规则

- BatchNorm 的统计量只能来自训练折；验证 / 测试必须使用已冻结的推理统计量；
- 保存 Python、NumPy、PyTorch、DataLoader、CUDA 的随机种子和所有确定性设置；
- 固定依赖版本、设备信息、CUDA / cuDNN 版本、代码提交哈希和环境锁文件；
- 若某算法在当前硬件 / 版本无法确定性执行，应明确记录为 `NONDETERMINISTIC`，并至少运行多种种子报告分布；不得只报告最好的 seed。

系统必须区分两种运行模式：

```text
STRICT_SENTINEL_MODE
  用于 LT/EX 回归、未来突变和小型 fixture；优先 CPU 或明确支持的确定性算法；
  固定 seed、固定线程、固定库版本；要求离散工件精确一致。

RESEARCH_TRAINING_MODE
  用于真实规模 ML/DL；允许经声明的 GPU 非确定性，但必须记录环境、seed 集合和结果分布；
  禁止以“GPU 浮点差异”为理由跳过数据因果性测试。
```

**比较规则**：

- 训练样本 `sample_id` 集、fold 边界、feature/label manifest、股票名单、订单 ID、订单股数、拒单原因等离散工件：`STRICT_SENTINEL_MODE` 下必须精确相等；
- 预测浮点值、损失、净值、风险指标：在声明的 `abs_tol` / `rel_tol` 内比较；
- 模型权重哈希：只在严格确定性模式下允许作为回归门槛；普通 GPU 重训不得以权重逐字节哈希作为 LT-002 是否通过的依据。

### 8.4 模型输出与组合构建分离

模型只输出可解释的预测对象，例如：

```text
score / expected_excess_return / probability / uncertainty
```

模型不得直接隐藏交易成本、最大权重、行业约束、换手约束或不可交易规则。组合构建器应作为独立模块，输入为：

```text
scores + tradable_universe(T) + previous_holdings + risk_constraints + cost_model
```

这样可以单独审计“预测有效”与“能否交易”两个问题。

---

## 9. A 股日线执行模拟：只模拟日线能够诚实支持的事情

### 9.1 交易规则必须配置化，而非写死

不同交易所、板块、证券状态和历史日期适用的涨跌幅、申报单位、停牌、退市整理和费率规则不同；规则还可能变化。

实现 `RuleBook.resolve(security_id, execution_ts)`，从 `market_rule_pit` 返回：

```text
price_limit_up, price_limit_down,
lot_size, odd_lot_sell_rule,
settlement_rule, shorting_allowed,
fee_schedule_id, trading_status_rule
```

禁止下列脆弱写法：

```python
if is_st: limit = 0.05
elif board == "MAIN": limit = 0.10
```

规则表必须按生效区间、证券类型和板块版本化。当前规则示例只可作为测试 fixture，不能替代历史规则数据。

### 9.2 V1 的订单生命周期：T 日锁定，不允许 T+1 反解

```text
T close
  1. 获得截至 T 收盘可用的特征、分数与 prediction_universe；
  2. 生成目标权重；
  3. 用 T 日及以前可见的信息、RuleBook、持仓与保守定价假设计算订单；
  4. 锁定 quantity、price_cap、cash_reservation、fees_reservation、ttl 和 order_id；
  5. 写入不可变 OrderIntent Ledger。

T after close
  订单仅作为 T+1 开盘有效的委托意图；不得再排名、换股或修改数量。

T+1 open
  撮合器只能读取锁定订单、T+1 开盘时点可观察的交易状态和开盘价；
  结果只能为 fill / reject / cancel / declared partial-fill，不得生成新目标。

T+1 post-open
  更新账本；不得基于未成交股票重跑模型、补买候选下一名或用当日后续行情改写订单。
```

每个 `OrderIntent` 至少包含：

```text
order_id, security_id, side, decision_ts, execution_ts,
locked_quantity, lot_size, price_cap, price_floor,
reference_price_ts, reference_price, gap_buffer_rule,
reserved_cash, reserved_fees, ttl, evidence_status,
feature_manifest_hash, signal_artifact_hash, ruleset_version
```

**数量锁定规则**：

```text
locked_quantity = floor_to_lot(
    min(target_notional / conservative_reference_price,
        capacity_notional(T) / conservative_reference_price,
        affordable_notional(T) / conservative_reference_price)
)
```

其中 `conservative_reference_price`、`gap_buffer_rule`、`price_cap` 和现金预留必须在 T 日确定。不得使用 T+1 实际开盘价计算 `locked_quantity = target_notional / open` 来凑目标权重。

V1 默认 `order_ttl = NEXT_OPEN_ONLY`。若 T+1 开盘无法成交，订单取消，不滚动到 T+2。任何不同有效期必须在 T 日由策略配置固定，并有独立测试。

### 9.3 日线数据下的成交原则

V1 只支持 **T+1 开盘成交模型**。禁止声称使用日线准确模拟 VWAP、盘口排队、开板概率、盘中触发顺序或精确部分成交。

对在 T 日形成的买入 / 卖出订单：

1. 在 T+1 开盘前检查该股票是否停牌、退市或交易状态不允许；不允许则拒绝；
2. 从 `RuleBook` 得到 T+1 的涨跌停价、手数和状态规则；
3. 检查锁定股数、库存和现金预留；
4. **保守开盘规则**：
   - 买单且 `open >= limit_up`：默认不成交；
   - 卖单且 `open <= limit_down`：默认不成交；
   - 买单且开盘价高于 T 日锁定 `price_cap`：取消；
   - 无涨跌幅限制、停牌、缺失开盘价：使用单独批准规则 / 默认不成交；
5. 若允许成交，`fill_quantity <= locked_quantity`；成交价由锁定模型定义，例如 `open ± declared_slippage`；
6. 只要使用开盘成交，就不得用 T+1 的 `high`、`low`、`close` 或全日成交量决定开盘是否成交、成交价格、成交数量或订单替换；
7. 默认 full-or-none。若未来引入 partial fill，其份额规则必须只使用 T 日及以前已知的流动性代理，且在 T 日随订单锁定；不得用 T+1 全天成交量倒推。

### 9.4 T+1、仓位、现金与结算

- A 股普通股票 V1 不允许当日买入后当日卖出；每笔买入 lot 必须保存 `sellable_from_trade_date`；
- 卖出订单只可使用截至开盘前已可卖的库存；不允许短卖；
- 账本必须分开记录：`settled_cash`、`available_cash`、`frozen_cash`、`receivable_cash`、`locked_shares`、`sellable_shares`、`pending_corporate_action_entitlements`；
- 为避免券商资金可用规则差异，V1 默认按“先处理开盘卖单，再计算同一开盘轮次可用买入现金”的保守账本流程；实际经纪商接口上线前必须以账户对账单验证并替换为 broker-specific adapter；
- 实际成交价低于预留上限时，只能释放多余预留现金；不得因“开盘比预计便宜”在同一开盘轮次扩大已锁定订单股数。

### 9.5 Corporate Action Handler：跨公司行动持仓的独立账务规范

公司行动不是只影响复权特征的数学问题；它会改变真实可卖股数、成本基、现金权益和估值路径。必须实现独立、可审计的 `CorporateActionHandler`。

事件时钟顺序至少区分：

```text
announcement_at         : 仅决定信息何时可进入特征/资格判断；不自动改变持仓。
record_date             : 确定权益归属（如适用）。
ex_date                 : 原始价格除权/除息；更新价格语义与 entitlement 状态。
payable_date            : 现金股息从 receivable_cash 转为 available/settled cash（按 broker adapter）。
shares_available_date   : 送股、转增、拆股后的新增股份何时进入 sellable_shares。
rights_subscription     : 配股必须有预先声明的参与/放弃/资金冻结规则。
merger/delisting date   : 按具体条款执行换股、现金、强制退出或 block 回测。
```

每个 handler 事件必须输出不可变审计记录：

```text
action_id, security_id, event_clock_ts, prior_position,
share_delta, cost_basis_delta, receivable_cash_delta,
available_cash_delta, sellable_date, applied_rule, source_manifest_hash
```

**禁止**：

- 只靠 hfq/qfq 价格让净值“看起来连续”，却不更新股数/现金；
- 把除息日现金立刻当成可交易现金，若实际到账在后；
- 忽略送股、拆股、配股、合并、退市对库存和成本基的影响；
- 未支持公司行动时静默按最后收盘价永久持有。未支持事件必须标为 `UNSUPPORTED_CORPORATE_EVENT` 并阻断可信回测。

### 9.6 涨跌停、停牌、新股和退市

- 不用“触及涨停 / 跌停”笼统判断成交能力；日线无法看到盘口和封单。V1 只应用上节的保守开盘拒绝规则；
- 不允许因为 T+1 某股票未成交而根据 T+1 后续行情买入排名下一只股票；
- 停牌持仓按最近可得价格估值，但标记为 `STALE_MARK`，并在绩效报告披露；
- 新股上市初期、无涨跌幅限制期及最小上市天数均由规则表 / universe 配置处理；V1 默认过滤上市时间不足阈值的证券；
- 退市、合并和现金选择权必须调用 Corporate Action Handler；未实现则阻断可信回测。

### 9.7 成交量、容量与市场冲击

下单规模必须在 T 日以历史可得流动性限制：

```text
max_order_notional(T, i) = participation_cap × trailing_ADV_notional(T, i)
```

- `trailing_ADV_notional` 仅使用 T 及以前的成交额；
- **开盘成交的容量不得用全日 ADV（V3 订正）**：V1 在 T+1 **开盘集合竞价**成交，而开盘竞价成交量通常仅为全日的个位数百分比。直接用全日 `trailing_ADV` 作上限会**高估**你在开盘真能吃下的规模。须改用更保守的代理：以**历史开盘竞价成交额**（若可得）为基准，或对全日 `trailing_ADV` 施加一个声明过的显著折扣系数 `opening_liquidity_fraction`（如全日的 X%），并把该系数纳入容量敏感性分析；
- 订单数量在生成时先按 lot size 向下取整并写入 `locked_quantity`；
- 若目标订单超过容量上限，必须在 T 日缩小订单，而非 T+1 看实际成交量后再决定；
- 所有未达成目标权重、拒单、容量截断、冻结仓位和现金残余必须输出到报告；
- 组合规模必须做容量敏感性测试，例如 1x / 5x / 10x 资金规模与不同参与率上限。

### 9.8 费用和税费

费用模型必须独立于策略代码，并按照日期和市场规则读取：

```python
fee = FeeSchedule.resolve(execution_ts, security_id, broker_profile)
```

最低要求：

- 券商佣金、最低收费、卖出印花税、过户费、交易规费分别建模；
- 历史费率采用生效日期表，不能把当前费率覆盖十年历史；
- 个人实际佣金以本人券商对账单或明确费率配置为准；
- `broker_profile` 为空时，回测必须拒绝执行，不得偷偷使用“约万几”的猜测；
- 每笔成交保存费用明细，不只保存总成本。

### 9.9 不同资产必须不同引擎

股票、股票 ETF、债券 ETF、跨境 ETF、港股通、可转债、期货和期权的回转交易、税费、涨跌幅、结算和最小单位不同。不得为了统一接口而把它们按 A 股普通股票规则回测。

## 10. 组合构建与风险约束

### 10.1 顺序必须固定

```text
预测分数
  -> 决策时点可见的交易池过滤
  -> 风险 / 流动性 / 仓位约束
  -> 目标权重
  -> 与现有可卖库存比较
  -> 订单生成、lot rounding、容量截断
  -> T+1 实际成交
```

不得在成交失败后重新运行模型、重新排序或利用实际成交结果挑选替代股票。

### 10.2 V1 必备约束

全部由配置指定：

```yaml
portfolio:
  long_only: true
  leverage_limit: 1.0
  max_single_name_weight: REQUIRED
  max_sector_weight: OPTIONAL_UNTIL_PIT_SECTOR_AVAILABLE
  max_turnover_per_rebalance: REQUIRED
  min_cash_buffer: REQUIRED
  max_order_as_pct_trailing_adv: REQUIRED
  rebalance_frequency: weekly
  benchmark_id: REQUIRED
```

### 10.3 风险分析不能只看 Sharpe

每个合格回测至少报告：

- 总收益、年化收益、年化波动、最大回撤、Calmar；
- 年化换手、持仓数、单票最大权重、现金占比；
- 成交率、拒单率、停牌冻结比例、涨跌停拒单比例；
- 税费、佣金、滑点、冲击成本的拆分；
- 多空并不适用时，至少对比可交易 benchmark 与全市场基准；
- 按年份、市场状态、板块、流动性分组的表现；
- 预测层：日度 Rank IC、ICIR、覆盖率和分位组合单调性；
- 成本、滑点、容量、延迟和交易频率的敏感性分析；
- 不依赖“所有交易独立”的置信区间；对重叠持有期使用 block bootstrap 或其他串相关稳健方法。

---

## 11. 防泄露、真实性与研究治理验收：不通过即禁止晋级

### 11.1 核心数据因果性哨兵

| ID | 测试 | 严格预期 |
|---|---|---|
| LT-001 | 未来字段拒绝：注入 `available_at > feature_asof_ts` 的字段 | Data Portal / Feature Compiler 立即报错 |
| LT-002A | 原始未来数据突变：只修改 `available_at > C` 的 L1 `RAW_UNADJUSTED` 记录 | `decision_ts <= C` 的可见输入、特征、训练样本集、信号、订单、成交与执行账本不变 |
| LT-002B | 未来公司行动突变：只修改 `available_at > C` 的 L2 公司行动 | `decision_ts <= C` 的 PIT 特征、训练样本、订单、成交与账本不变 |
| LT-002C | 派生价格防火墙：向 raw/PIT 链路注入 hfq/qfq/未知调整价 | 写入或读取被拒绝；不得因“只是计算收益率”放行 |
| LT-003 | 标签成熟度：把尚未结束或尚未可观察的标签塞入训练 | trainer 立即拒绝 |
| LT-004 | 全样本 scaler | 审计器发现 test/future rows 参与 fit 即失败 |
| LT-005 | 非法特征算子 | `shift(-k)`、`center=True`、`bfill` 或未来 merge 被静态/运行时检测拒绝 |
| LT-006 | 日期组切分 | 同一 decision date 的所有证券不可分布在 train 与 validation/test 两侧 |
| LT-007 | 标签置换 | 预测与组合绩效应退化至近随机；若仍显著则失败并人工审计 |
| LT-008 | 一日滞后 | 所有特征额外滞后一个交易日后，结果不得异常跃升；跃升触发调查 |
| LT-009 | 收盘到开盘 | 用 T 日 close 生成信号时，首笔可能成交只能在 T+1 |
| LT-010 | 开盘执行前视 | 修改 T+1 high/low/close/full-day volume 不得改变 T+1 opening fill 决策 |
| LT-011 | T 日订单锁定 | 修改 T+1 open 后，`locked_quantity`、`price_cap`、现金预留和候选名单不得改变；只允许成交/拒单结果变化 |
| LT-012 | taint 传播 | 受污染股票池或横截面标签必须自动使下游模型/回测变为 `EXPLORATORY_TAINTED` |

**LT-002 的比较规则**：

- **作用域随阶段（V3）**：每个阶段的 LT-002A/B 只对该阶段已存在的闭合输出断言不变（Phase 0 = 可见集 + 调整因子；Phase 1 += 订单/成交/账本；Phase 2 += 特征/标签选择；Phase 3 += 训练样本/fold/预测）。不得在产物尚不存在的阶段判其失败或要求其通过；
- 在 `STRICT_SENTINEL_MODE`：训练样本 `sample_id` 集、fold、特征/标签 manifest、订单 ID、订单股数、拒单原因、成交数量和账本事件必须精确相等；
- 浮点预测、净值、指标使用预先声明的 `abs_tol` / `rel_tol`；
- 普通 GPU/DL 训练不以模型权重逐字节哈希作为 LT-002 通过条件；若需精确回归，使用冻结模型、CPU 或确定性 fixture；
- 所有突变测试必须证明“被改动记录确实在 C 后可见”，否则测试无效。

### 11.2 A 股执行与公司行动测试

| ID | 场景 | 预期结果 |
|---|---|---|
| EX-001 | T 日信号，T+1 开盘涨停买入 | 默认拒单，订单取消，不选替代股 |
| EX-002 | T 日信号，T+1 开盘跌停卖出 | 默认拒单，保留可卖库存 |
| EX-003 | T+1 停牌 | 不成交，持仓冻结/现金不动 |
| EX-004 | T 日买入后同日卖出尝试 | 被 T+1 库存规则拒绝 |
| EX-005 | 订单不满足 lot size | 向下取整或拒绝，行为可配置且有记录 |
| EX-006 | 单笔佣金低于最低收费 | 应收取最低费用 |
| EX-007 | 费率跨历史生效日 | 前后交易采用不同 fee schedule |
| EX-008 | 容量超过 trailing ADV 上限 | T 日就缩减下单量，不读取 T+1 全日成交量 |
| EX-009 | T+1 开盘跳空 | 订单数量不变；按锁定 cap/现金/规则成交或取消，不反解股数 |
| EX-010 | 现金卖出后同开盘买入 | 与声明的 broker adapter 账本规则一致 |
| EX-011 | 除息日 + 派息日 | 除息时确认应收；派息日才按规则进入可用/结算现金 |
| EX-012 | 拆股/送股 | 股数、成本基、可卖日期按行动条款调整，净值不靠 hfq 伪连续 |
| EX-013 | 配股/合并/退市 | 调用显式 handler；未支持则标记并阻断可信回测 |
| EX-014 | 历史公司行动修订 | 仅在其 `available_at` 前后影响对应时点；不得逆向改写更早闭合输出 |

### 11.3 研究治理与晋级测试

| ID | 测试 | 预期结果 |
|---|---|---|
| RG-001 | outer test 访问审计 | 首次读取 outer test 后写入不可撤销访问记录；之后任何设计改动使其标记 `CONTAMINATED` |
| RG-002 | trial 注册完整性 | 报告中 trial 总数与 registry 一致，不能只保留赢家 |
| RG-003 | tainted-to-promotion gate | `EXPLORATORY_TAINTED` 工件不能触发 `BACKTEST_VALIDATED`、模拟盘或实盘命令 |
| RG-004 | 决策规则锁定 | outer test 前必须存在已版本化的晋级阈值和配置 |
| RG-005 | 结果声明审计 | 报告标题、图表和 API 不得把探索级结果表述为“可交易业绩” |
| RG-006 | 验证范围与黄金切片审计 | `validation_scope`、scope manifest 与报告声明一致；黄金切片必须在任何策略结果前冻结，且不得被表述为全市场 alpha 证据 |

### 11.4 回归测试、哈希与容差

- 每次合并代码后，固定小型历史 fixture 在 `STRICT_SENTINEL_MODE` 必须产生完全相同的：数据可见集哈希、特征哈希、fold 哈希、订单哈希、成交哈希、公司行动账本哈希；
- 浮点净值与风险指标采用版本化容差；容差超出必须给出变更说明、关联 issue、规则版本/数据快照差异；
- 任何“性能变好了”但无法解释可见数据、订单、执行账本或工件差异的提交不得合并；
- 任何试图以关闭 LT/EX/RG 测试来继续产出业绩的分支，必须自动获得 `BLOCKED_BY_GOVERNANCE` 状态。

---

## 12. 实验可复现、可审计、证据等级与报告工件

### 12.1 每次实验必须保存

```text
experiment_id
created_at
code_commit_hash
environment_lock_hash
data_snapshot_id
ruleset_version
universe_version
feature_manifest_hash
label_version
label_universe_manifest_hash
source_capability_manifest_hash
corporate_action_manifest_hash
split_definition_hash
research_registry_id
evidence_status / taint_reasons
validation_scope
validation_scope_manifest_hash
golden_slice_manifest_hash (when validation_scope=GOLDEN_SLICE_PIPELINE)
availability_profile_id / declared_safety_latency_version
opening_liquidity_model / opening_liquidity_fraction
outer_test_view_budget / outer_test_access_ledger_id
hyperparameter_config
random_seed_bundle
run_mode (STRICT_SENTINEL_MODE / RESEARCH_TRAINING_MODE)
model_artifact_hash (when applicable)
order_ledger_hash
fill_ledger_hash
corporate_action_ledger_hash
metrics_report_hash
```

### 12.2 结果目录的最小结构

```text
artifacts/{experiment_id}/
├── config.resolved.yaml
├── data_lineage.json
├── source_capability.json
├── feature_manifest.json
├── label_manifest.json
├── universe_manifest.json
├── corporate_action_manifest.json
├── folds.json
├── research_registry_link.json
├── validation_scope.json
├── golden_slice_manifest.json          # required when scope=GOLDEN_SLICE_PIPELINE
├── data_availability_profile.json
├── outer_test_access_ledger.json
├── model/
├── predictions.parquet
├── orders.parquet
├── fills.parquet
├── corporate_action_ledger.parquet
├── portfolio_ledger.parquet
├── diagnostics.json
├── leakage_test_report.json
├── execution_test_report.json
├── governance_test_report.json
└── report.md
```

### 12.3 证据等级与“可信回测”发布门槛

一个结果只有同时满足以下条件，才可标记为 `BACKTEST_VALIDATED`：

1. 所有 LT / EX / RG 测试通过；
2. 数据血缘完整，PIT 缺口无未声明使用；
3. 原始价格层为 `RAW_UNADJUSTED`，公司行动/派生收益语义可审计；
4. 未使用当前成分股或仅存活股票池，或结果未被错误标为可信；
5. 最终 outer test 未被反复调参污染；
6. 成交、成本、容量、限制、公司行动和未成交订单已纳入净值；
7. 订单数量、价格上限、现金预留均在决策时点锁定；
8. 结果可以从冻结工件一键复现；
9. `validation_scope`、scope manifest 与证据声明一致；黄金切片结果必须明确标为 `GOLDEN_SLICE_PIPELINE`，不得被表述为全市场 alpha 或全市场策略验证；
10. 报告同时展示负面诊断、失败场景、trial 数量和敏感性分析，不只展示最佳收益图。

若任一条件不满足，报告只能是 `EXPLORATORY_TAINTED` 或 `BLOCKED`，并必须在首页明确说明。`BACKTEST_VALIDATED` 不是脱离范围的万能标签：只有 `validation_scope=FULL_UNIVERSE_BACKTEST` 才可支持全市场历史策略结论；`GOLDEN_SLICE_PIPELINE` 仅支持该切片上的端到端管线验证结论。

## 13. 研发路线：必须按门禁推进

### Phase 0 — 数据和时钟地基

交付：交易日历、PIT 数据契约、原始日线快照、security master、规则表接口、Data Portal、基础单元测试。

**退出条件**：L1 未复权原始层、L2 公司行动台账（或其 best-effort 版本 + 能力声明）和 quarantine firewall 已验收；LT-001、LT-005 通过；**LT-002C 全测通过**；**LT-002A/B 按 Phase 0 作用域通过**（仅断言数据可见集与调整因子的未来突变不变性，订单/特征等留待其存在阶段）。

> 双轨提示（V3，见 §0.6）：Phase 1 的执行层（轨道 A）不依赖完整 PIT，可在 `EXPLORATORY_TAINTED` 数据上与轨道 B（L2 台账完善、黄金切片净化）并行推进，不必空等数据齐备。但任何 `BACKTEST_VALIDATED` 声明必须落在黄金切片或完整 PIT 数据上（见 §0.5）。

### Phase 1 — 无 ML 的事件驱动基线

交付：长仓账本、T+1、开盘成交、停牌、涨跌停保守规则、费用、lot size、容量约束；**一个固定、确定性、非收益型 dummy strategy**。该策略只负责产生可复现订单（例如按预先冻结的证券排序定期选取 N 只），不得调用多日收益、动量、波动率、相对强弱或任何尚未通过 CA/PIT 门禁的特征。

**退出条件**：EX-001 至 EX-014 全部通过；LT-002A/B 按 **Phase 1 作用域**通过（订单意图、成交、公司行动账本与净值）；LT-009、LT-011 通过；回测可生成可审计订单、成交和公司行动账本。EX-011/EX-012 必须覆盖现金分红与拆股/送转；EX-013 对未支持的配股、合并或退市事件，允许以正确的 `UNSUPPORTED_CORPORATE_EVENT` 阻断通过。任何未支持公司行动必须显式阻断可信回测，不得静默跳过。历史无公司行动窗口仅可作为 `EXECUTION_FIXTURE`，不得成为真实交易筛选或策略业绩证据。

### Phase 2 — PIT 特征和标签库

交付：Feature Manifest、Label Manifest、价格 / 成交量特征、成熟标签、特征哈希和完整血缘。

**退出条件**：任一特征/标签可追溯到字段级时间戳、价格语义和 taint 状态；LT-002A/B 按 **Phase 2 作用域**通过（新增特征矩阵、样本与标签选择）；LT-003、LT-004、LT-006、LT-010、LT-012 通过。无合格 PIT 公司行动台账时，可能跨公司行动日的多日收益类特征和标签不得进入本 Phase 的 `VALIDATION_ELIGIBLE` 路径。

### Phase 3 — Walk-forward ML

交付：自定义日期组 splitter、purge / embargo、折内 preprocessing、线性模型和树模型、预测 / 组合分层报告。

**退出条件**：冻结 outer test、完整研究注册与 trial 日志；LT-002A/B 按 **Phase 3 作用域**通过（新增训练样本集、fold、预测与组合账本）；LT-007、LT-008 通过；RG-001 至 RG-006 全部通过；性能在多折、成本、容量和 latency 敏感性下仍有可解释稳定性。outer test 的查看预算、访问账本和任何污染状态必须随实验工件保存。

### Phase 4 — 深度学习（可选）

交付：仅在 Phase 3 明确获得稳健增益需求后实施；序列模型、因果掩码、训练折内预训练、确定性与多 seed 报告。

**退出条件**：相对最强非深度学习基线的增益在 outer test 和保守成本下仍存在，且不以更高不可解释换手换取表面收益。

### Phase 5 — 模拟盘与真实对账

交付：相同的信号代码、真实数据可用时间日志、日终订单 / 成交 / 持仓对账、漂移监控。

**退出条件**：连续运行期内，模拟盘与预定义回测执行模型的偏差在可接受范围内，所有偏差可解释。

---

## 14. V1 默认实验配置模板

> 下列值是结构模板，不是默认投资参数。凡标记 `REQUIRED` 的值必须在运行前明确填写。

```yaml
experiment:
  name: REQUIRED
  timezone: Asia/Shanghai
  mode: backtest
  data_snapshot_id: REQUIRED
  ruleset_version: REQUIRED
  broker_profile: REQUIRED
  random_seed_bundle: REQUIRED

market:
  instrument_scope: A_SHARE_COMMON_STOCK
  exchanges: [SSE, SZSE]
  boards: [MAIN]
  trade_calendar: CN_A_SHARE

data_contract:
  raw_price_basis: RAW_UNADJUSTED
  vendor_adjusted_prices_allowed_in_pit: false
  source_capability_manifest: REQUIRED
  corporate_action_manifest: REQUIRED
  evidence_status_required: true

data_availability:
  availability_profile_id: REQUIRED
  declared_safety_latency_version: REQUIRED
  latency_sensitivity_profiles: REQUIRED

signal:
  decision_time: "T close after daily-bar availability"
  execution_time: "T+1 opening auction"
  order_ttl: NEXT_OPEN_ONLY
  rebalance_frequency: weekly

universe:
  include_delisted_history: true
  min_listed_trading_days: 250
  min_trailing_amount_20d: REQUIRED
  min_price: REQUIRED
  exclude_st_or_risk_warning: true
  require_not_suspended_at_decision: true

label:
  type: forward_excess_return
  entry: "T+1 open"
  exit: "T+H+1 open"
  holding_period_trading_days: REQUIRED
  label_version: REQUIRED

execution:
  fill_model: conservative_next_open
  no_fill_if_buy_open_at_or_above_limit_up: true
  no_fill_if_sell_open_at_or_below_limit_down: true
  use_same_day_high_low_close_volume_for_open_fill: false
  order_quantity_locked_at_decision_ts: true
  conservative_reference_price_rule: REQUIRED
  price_cap_rule: REQUIRED
  cash_reservation_rule: REQUIRED
  max_order_as_pct_trailing_adv: REQUIRED
  opening_liquidity_model: REQUIRED
  opening_liquidity_fraction: REQUIRED_IF_NO_AUCTION_VOLUME
  opening_capacity_sensitivity_profiles: REQUIRED
  slippage_model: REQUIRED
  corporate_action_handler: REQUIRED

portfolio:
  long_only: true
  leverage_limit: 1.0
  max_single_name_weight: REQUIRED
  max_turnover_per_rebalance: REQUIRED
  min_cash_buffer: REQUIRED

validation:
  splitter: WalkForwardPurgedSplitter
  date_grouped: true
  nested_tuning: true
  outer_test_locked: true
  outer_test_start: REQUIRED
  outer_test_view_budget: REQUIRED
  outer_test_access_ledger_id: REQUIRED
  research_registry_id: REQUIRED
  promotion_rule_version: REQUIRED
  validation_scope: REQUIRED  # EXECUTION_FIXTURE / GOLDEN_SLICE_PIPELINE / FULL_UNIVERSE_BACKTEST / EXPLORATORY_UNIVERSE
  validation_scope_manifest_hash: REQUIRED
  golden_slice_manifest_hash: REQUIRED_IF_SCOPE_GOLDEN_SLICE_PIPELINE
  run_mode_for_sentinels: STRICT_SENTINEL_MODE
```

---

## 15. 已知数据风险、来源能力与处理决策

| 数据能力 / 风险 | 允许状态 | 处理 |
|---|---|---|
| 未复权原始日线 OHLCV + 可信交易日 | 可用于 V1 | 必须快照、完整性校验、`price_basis=RAW_UNADJUSTED` |
| 任何 hfq/qfq/vendor-adjusted 序列 | 仅隔离研究 | 进入 quarantine；不得做 raw/PIT/执行/LT-002 真相来源 |
| PIT 公司行动 | 可用于 PIT 收益特征/标签与真实账本 | 无合格台账时：仅允许同日、非收益型原始量价特征作 `EXPLORATORY_TAINTED` 探索；禁止任何可能跨公司行动日的多日收益、动量、波动率、相对强弱、累计回报及依赖其输入的标签；跨行动持仓不做可信结论 |
| PIT 财报公告时间与首次披露值 | 可用于基本面特征 | `NONE` vintage 时禁用基本面可信研究；不要伪造 revision 历史 |
| 历史指数成分与生效时间 | 可用于指数成员池 | 无则可探索但 status=`EXPLORATORY_TAINTED`，不可晋级 |
| ST / 停牌 / 退市 / 板块历史状态 | 可用于真实交易资格 | 无则策略仅可研究级，不能标记可信 |
| 开盘集合竞价量 / 盘口 | 可支持更精细开盘成交 | 日线缺失时仅使用保守 full-or-none/no-fill 模型 |
| 逐笔 / 分钟数据 | 可支持 VWAP、盘中策略 | 不能由日线替代 |
| GPU 非确定性 | 可用于研究训练 | 不得关闭数据因果性测试；改用严格 sentinel fixture 与容差比较 |
| 已被查看的 outer test | 不再是独立证据 | 标记 `CONTAMINATED`，向未来滚动新的 outer test |

### 15.1 数据迁移待办（任何建模前）

1. 扫描所有 `data/raw/`、特征缓存和 notebook 依赖，识别任何 `adjust=hfq/qfq` 或未知价格语义文件；
2. 将上述文件移入 quarantine 并建立清单；
3. 重新获取/构建 L1 未复权日线；
4. 建立 L2 公司行动台账与能力声明；
5. 执行 LT-002C 的最小 fixture，并执行 LT-002A/B 在当前已存在产物上的作用域子集；
6. 只有通过后，才允许把价格数据标为 `VALIDATION_ELIGIBLE`；
7. **构造黄金切片（V3）**：选取十余只可逐条核验的超大盘股 + 一段可负担的窗口，手工双人核验建立其 L2 公司行动台账，作为"第一个可达 `BACKTEST_VALIDATED` 的端到端证明"目标；全市场数据在 PIT 历史补齐前维持 `EXPLORATORY_TAINTED`。
8. 在任何策略结果、因子排名或参数选择前冻结 `golden_slice_manifest`，并写入 `validation_scope=GOLDEN_SLICE_PIPELINE`；切片结果只能证明管线，不得作为全市场 alpha 证据。
9. 为 latency、开盘流动性代理与 outer test 查看预算创建版本化配置和敏感性场景；缺失时不得产生 `BACKTEST_VALIDATED` 报告。

## 16. 监管与技术参考（实施前复核，不硬编码）

- A 股普通股票的 T+1 回转交易约束与 ETF 等其他品种并不完全相同；V1 仅对普通 A 股股票启用本文件的库存规则。
- 上交所、深交所及北交所的涨跌幅、上市初期、风险警示、申报单位和停复牌规则存在板块和日期差异；使用 `market_rule_pit` 按日期解析。
- 证券交易印花税、过户费、佣金最低收费和券商实际费率均可能随日期、市场、券商及账户协议变化；以历史有效费表和个人券商对账单为准。
- 机器学习实现参考时间序列切分、折内 preprocessing、可复现训练的官方文档；但本项目必须使用比普通 `TimeSeriesSplit` 更严格的、按 `label_end_ts` purge 的日期组切分器。

---

## 17. 最终不可妥协清单

在任何人试图“先跑出收益再补工程”时，以本清单为准：

1. 没有 `available_at` 和来源能力声明，就不是可用于可信回测的数据。
2. `hfq/qfq` 不是 raw；未复权价格与 PIT 公司行动台账才是数据真相底座。
3. 没有成熟标签检查，训练就可能偷看尚未发生的结果。
4. 没有日期组 walk-forward + interval purge，横截面样本再多也不能替代时间外推。
5. 没有 T+1、涨跌停、停牌、lot、费用、容量、公司行动和未成交账本，收益就不是可交易收益。
6. 订单股数、价格上限、现金预留与有效期必须在 T 日锁定；不得用 T+1 开盘价反算仓位。
7. 日线数据不能模拟它没有记录的盘中路径，也不能用收盘后全天数据替开盘决策背书。
8. 当前指数成分股、当前 ST 状态、后复权绝对价格和只存活股票池都是高危偏差来源；可探索，不可洗白为证据。
9. LT-002 能证明数据因果性，不等于已经解决研究者自由度、多重检验或回测过拟合。
10. 一切可拟合对象都只在训练折拟合；无监督学习也不例外。
11. 最终测试集一旦参与设计选择，就不再是最终测试集。
12. **先证明原始数据、时钟与账本诚实，再尝试让模型聪明；先通过晋级门禁，再讨论收益。**
13. 数据不完整时，通往可信结论的唯一合法路径是**黄金切片**（小而可核验、可达 `BACKTEST_VALIDATED`），而非把全市场受污染结果悄悄当证据；执行层可在受污染数据上先行，但晋级声明不行。
14. outer test 是**有限预算**：查看一次即消耗，污染即作废向后滚动；从第一天起配给，绝不当开发集反复试。
15. `BACKTEST_VALIDATED` 必须同时带 `validation_scope`：黄金切片只能证明 `GOLDEN_SLICE_PIPELINE` 管线可信；只有完整 PIT 股票池的 `FULL_UNIVERSE_BACKTEST` 才可支持全市场历史策略结论。
16. **Phase 1 先用 dummy strategy 证明执行诚实**；在 PIT 公司行动链路合格前，不得用动量等多日收益信号替代这一执行基线，也不得为此静默放宽 CA 处理。

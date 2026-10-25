# 借鉴 OpenWiki 改造 Zest 的项目文档生成与持续维护

日期：2026-10-23。状态：源码研究与改造建议，尚未修改运行逻辑。

后续进度：阶段 A 见 [M130 项目范围读取](project-source-access.md)，阶段 B 见 [M131 工程文档](engineering-documents.md)，阶段 C 的稳定事实记录、局部更新和共享清单见 [M132 增量维护](incremental-knowledge.md)。Writer 仍复用调查事实及原文，独立新事实由调查流程处理；真实模型质量尚待验收。下文保留研究当时的观察与建议。

研究对象：`langchain-ai/openwiki`，固定提交 `305599f8e44203f9ca6b74bb6b38eebaf0b0405e`。
通过 GitHub 连接器读取该提交的源码、测试定义及仓库自带 Wiki；没有安装或运行 OpenWiki，没有测量其真实模型效果。
Zest 对照对象为当前工作区，包括尚未提交的实现；HEAD 为 `605e3ac7cce36decb335ce8bfc38ba4b8f6e9440`，不能用 HEAD 单独复现当前行为。

## 1. 本轮目标与结论

用户本轮提出三个要求：减少阶段之间的人工干预，实现全自动生成项目文档；文档应同时服务软件工程阅读与 Agent 项目理解；研究并借鉴 OpenWiki 的增量更新方式。

这更新了早期“每个固定资料包及请求扩展分别批准”的文档生成方向。后续设计应以项目级持续授权为基础，在同一项目与模型目标内自主调查、编写和维护。资料包、源码版本、请求收据继续用于追踪与复现，不再把每次技术分批变成用户审批。

主要改造不是加大 Prompt，也不是换 Agent 框架，而是同时改变三个组织单位：

- 权限按项目与操作类型管理，源码读取按需进行。
- 生成与恢复按工程文档页面管理，事实账本作为正文背后的依据。
- 增量维护按页面基线及事实证据状态管理，未变化内容保留，模型只处理受影响部分。

内部仍然可以分为调查、写作、核对、发布等阶段，用户看到的是连续推进的一次工作。

## 2. Zest 当前为什么容易停下、文档为什么像调查记录

| 当前实现 | 对实际使用的影响 | 定位 |
| --- | --- | --- |
| 摸底最多 200 文件、4 MiB，资料包由摸底已经读到的文件构成 | 摸底的技术上限同时成了模型可访问资料的上限，仓库内相关文件也可能成为“包外资料” | `zest/survey.py:22`、`zest/cognition_store.py:42` |
| 默认单请求 32000 字符，32000–48000 走扩展批准，超过 48000 停止 | 上下文装配问题被提升为授权或终止问题 | `zest/cognition.py:117` |
| 每轮 32 次调用、30 分钟，每个任务 4 轮、16 次工具动作 | 多模块调查、报告、逐项核对共同竞争固定配额；自动批准并不能突破这些限制 | `zest/cognition_contracts.py` 的 `POLICY` |
| 自动批准绑定完整策略、Prompt、工作流、质量规则和模型配置版本 | 规则内容变化也会使持续授权失效；当前是给旧审批流程自动点击，而非重新设计自治范围 | `zest/understanding_approval.py:19`、`:61`、`:_permission` |
| 发布器固定输出六类正文和一份索引，把 finding 逐条追加为列表 | 调查结果直接成为最终正文，缺少以读者任务组织内容的写作阶段 | `zest/initialization.py:456` |
| 调查错误、核对失败、知识缺口一起进入 pending 并输出到正文 | 技术执行记录混进软件工程文档，阅读被重复错误和核对说明打断 | `zest/cognition_store.py:749`、`zest/initialization.py:486` |
| 新 commit 下旧 finding 都需要重新核对，完整旧状态保存在新调查背景中 | 增量更接近“带历史线索重新调查”，缺少稳定事实身份及页面级覆盖版本 | `zest/cognition_refresh.py:109`、`:198` |

这里不能说 Zest 完全没有自动化。已有 `UnderstandingApproval`、自动补读、后台 worker、调用收据、上下文压缩及维护事件，均可复用。
M129 已缩小历史线索视图；但研究任务累积的原文仍会反复进入上下文，`compact_results` 不移除已有正文，超长后可能被迫提前报告。

已阅读 BlindBox 当前生成的 `architecture.md` 与 `api.md`。可见重复的路由/请求层描述、密集配置细节、调查 UUID、逐条来源，以及 `cognition_prompt_budget` 等技术错误。架构页缺少先讲清系统整体与模块职责的开篇。这是当前输出结构造成的问题，不能只靠换标题解决。

## 3. OpenWiki 的文档实际是什么样

固定提交的 `openwiki/` 内共有 30 份 Markdown：22 份主题正文，7 份目录索引，1 份 `INSTRUCTIONS.md`。这是 OpenWiki 自身仓库的实例，不是每个项目必须生成的固定数量。

| 文档类别 | 实际页面举例 | 帮助读者完成的事 |
| --- | --- | --- |
| 快速接手 | `quickstart.md` | 定位项目、开发命令、入口，以及“我要做什么应该读哪页” |
| 架构 | `architecture/overview.md`、`agent-runtime.md`、`source-map.md` | 理解组件职责、边界和执行路径，定位实现 |
| 领域概念 | `concepts/grounded-claims.md`、`okf-output.md`、`two-modes.md` | 理解项目特有的数据与约定 |
| 核心流程 | `workflows/repository-generation.md`、`claims-reconciliation.md` | 解释正常路径、状态变化、失败和恢复 |
| 操作与配置 | `operations/configuration.md`、`cli-reference.md`、`ci-scheduling.md` | 配置、运行、维护、排查问题 |
| 集成 | `integrations/connectors.md`、`coding-agents.md`、`visualizer.md` | 说明对外接口、扩展与协作方式 |
| 测试与评估 | `testing/overview.md`、`evals.md` | 说明如何验证、不同测试证明什么 |

### 3.1 生成方式影响可读性

规划 Prompt 要求按系统职责、运行领域及跨系统流程组织，而非复制目录树。`seedPaths` 只是研究起点，Worker 可以追踪调用方、被调方、状态拥有者和测试。[OW-PROMPTS]

页面 Worker 直接撰写有层次的 Markdown，同时提交支撑其重要论述的 Claims；程序维护事实状态、来源、时间信息与索引。事实核对的粒度不强制成为正文句子或段落的粒度。[OW-PROMPTS] [OW-CLAIMS]

实际正文使用解释段落、对照表、命令、流程图/状态图和相关页面链接。例如测试页同时解释完整验证链与按模块选择最小验证的方法；快速接手页按读者意图提供路由表。[OW-QUICKSTART] [OW-TESTING]

收尾程序处理 Mermaid、目录、内部链接、来源及生成元数据；不能让模型自己填写“已核实”时间等控制字段。[OW-FINALIZER]

OpenWiki 的生成结果也不应原样视为文档质量标准：该提交的测试总览已有 1230 行，前置来源元数据很长，并可见重复小节。Zest 应借鉴主题组织、叙述与导航，同时控制长页面和重复内容。这里没有做独立语义正确性评分。

### 3.2 建议 Zest 的工程文档结构

六部分认知保留为覆盖检查维度，页面数量、名称和层级按项目实际生成。一个面向 Web 业务项目的候选结构如下，并非已生成成果：

```text
.zest/docs/
  index.md                         阅读入口与任务导航
  quickstart.md                    新人接手：用途、主实现、安装启动、首次验证
  architecture/
    overview.md                    系统边界、模块职责、依赖关系
    source-map.md                  关键实现位置与扩展入口
  concepts/                        项目特有业务规则与概念
  workflows/                       代表性业务流程与异常恢复
  api/                             实际存在的 API/CLI/库接口及调用示例
  data/                            数据模型、约束、状态变化、存储关系
  operations/                      配置、运行、排错；按证据说明部署方式
  testing/                         测试分层、命令、覆盖和未验证部分
  decisions/                       已记录决策、约束及有来源的历史理由
```

对 BlindBox，规划可以包含“抽奖事务”“幂等与结果查询”“库存概率”“缓存与实时库存”“认证与请求保护”等专题；先核对当前主实现，再决定页面划分。库、CLI 或数据分析项目使用自己的职责与流程，不强套鉴权、数据库和部署目录。

正文要做到：先解释整体和用途，再解释机制；正常路径与失败路径连贯呈现；命令说明前提与结果；代码示例有来源；相关页面可以相互跳转。流程正确性仍须检查，流程图只能改善表达。

事实与证据存结构化账本，正文只在适当位置显示来源；页面保留观察版本和必要边界。运行错误、原始模型响应、请求哈希及全部核对意见放私有运行记录。与读者有关的重要未知可留在对应页面，避免把所有历史缺口重复铺满每页。

为防止“写作润色”引入新事实，Writer 输出正文以及重要论述到事实 ID 的关联；新增重要断言必须补充证据或保持待核实。复核同时检查正文与账本是否一致，不能只检查 JSON 字段和来源存在。

## 4. 权限与自动执行如何调整

### 4.1 从固定文件包转向项目级持续授权

建议授权内容为：项目根目录、允许的来源类型和排除规则、模型数据去向、文档写入位置、自动维护设置及可配置总预算。用户发起项目文档自动生成后，范围内的检索、读取、分页、补读、写作与核对连续执行。

同一项目内发现更多源码、需要查看方法后半段、某次请求变长、内部任务重新分批，都不再要求用户重新授权。内部 Prompt 或普通实现版本升级，也不应仅因哈希变化而使项目授权失效；涉及数据去向或实际能力边界变化时才重新核对授权。

固定 commit 可以继续保留：它保证证据一致，与全自动读取整个项目不冲突。读取接口按 commit 的完整目录按需访问，不必预先将整仓正文装入批准包。每次读取保存版本、路径、blob/内容哈希和行段，实际请求继续留收据。

首个改造版本可继续使用已提交快照；OpenWiki 会把已跟踪工作区修改及未跟踪文件也纳入源码指纹，Zest 若支持工作区快照，应另行实现可复现快照，不能把动态工作区冒充 commit 证据。来源模式应在项目中直观展示。

### 4.2 技术容量由调度器处理

- 文件目录分页列举，按模块逐步探索，不把 200 文件或 4 MiB 当作项目权限边界。
- 根据模型上下文容量组织单请求，长期证据保存在库中，用身份与索引按需取回；不要求每轮重复全部已读正文。
- 超长请求在发送前自动拆为页面或子目标；保存完整证据，但不能因压缩就宣称未处理部分已覆盖。
- 每页分配调查和写作额度，项目总预算、调用量与时间仍可观测且有上限。到达总额度时保存可恢复进度，明确哪些已交付。
- 已知可恢复的限流/格式等失败可有限补试，避免所有技术失败都变成业务知识缺口。是否已计费、是否返回完整响应分别记录。
- 对结果未知的只读模型请求，可设计为预算内的新 attempt 并保留不确定收据；这需要新的恢复策略与验证，不能直接重放旧调用身份，也不能承诺恰好计费一次。

项目外资料和其他数据去向仍是不同范围；读取密钥、执行源码、修改业务代码、提交/推送、部署也不是文档生成内部步骤。文档 Worker 的工具只需支持读取项目、写自己负责的生成页面，以及提交事实变更。

OpenWiki 同样有严格边界：原生页面 Worker 只能修改自己的页面，通用工具看不到 Claims 控制文件，文件排除规则在工具层执行，shell 只允许极少命令。它减少干预的方式是扩大任务内可自主完成的工作，而非移除所有工具约束。[OW-BACKEND]

### 4.3 用户感知的一次连续工作

```text
选择项目并开始自动生成
  → 自主调查并规划页面
  → 按页深入研究、写作和核对
  → 保存可用页面并生成导航
  → 汇报完成范围、重要未知、用量
  → 有新版本时自动维护相关页面
```

界面显示正在理解哪个模块/编写哪页、已完成哪些内容；分页与内部补读只作为可展开的执行细节。只有真正需要用户补充的业务信息才提问，其余页面继续推进。

## 5. OpenWiki 增量更新源码研究

### 5.1 三种持久状态各有用途

| 状态 | 内容 | 用途 |
| --- | --- | --- |
| `.claims/<page>.json` | 稳定事实 ID、陈述、源码资源及内容版本、页面正文哈希 | 找出过期/失去依据的事实，按页合并事实变更 |
| `.page-manifest.json` | 每页的 Git 基线、源码指纹、页面哈希、完成者与运行身份 | 判断每个页面真正覆盖到了哪个版本 |
| `.run.json` | 本轮计划、页面任务、阶段、源指纹、恢复信息 | 中断后继续未完成的页面 |

页面清单与事实状态属于可携带的项目状态，运行恢复 checkpoint 是本次执行状态，不能混为一个“项目已更新”布尔值。[OW-MANIFEST] [OW-RUN]

### 5.2 更新链路

1. **读取已有状态并检查源码。** 生成源码指纹；OpenWiki 的输入包括 HEAD、受允许的已跟踪/未跟踪文件内容及状态，排除生成 Wiki 和忽略路径。[OW-UTILS]
2. **逐个核对事实证据。** 证据内容变化为 `stale`，找不到为 `unresolved`；文件/范围内容版本比较由程序完成。行段支持内容与上下文锚点重定位，避免纯行号移动造成无意义失效。[OW-PREFLIGHT] [OW-RESOLVER]
3. **按每页自己的基线形成更新窗口。** 更新过一半的 Wiki 不会被压成一个统一成功版本；缺失页面基线标记为需要完整审查。[OW-RUN] [OW-MANIFEST]
4. **规划器判断变化影响。** 输入各页面更新范围、变化路径、证据问题及项目目标；可以考虑关联模块、跨页面一致性与导航变化。事实证据未变并不排除其他文件导致的间接影响。[OW-PROMPTS]
5. **程序补齐必须处理的页面。** 规划器漏掉存在 stale/unresolved 事实的页面，`addRequiredClaimIssueJobs` 会补任务；输出语言变化也可强制补写作任务。[OW-JOBS]
6. **Worker 先读现有页面，再局部修改。** 默认只收到已有事实数量和需要处理的事实；需要改写其他既有内容时，按需查看完整事实集合。[OW-PROMPTS]
7. **只提交事实变化。** `confirmedClaimIds` 明确确认已复查的问题事实；`claims` 更新原 ID 或新增事实；`retractedClaimIds` 撤回不再成立/不再表述的事实。没有问题且未提交的旧事实由程序保留；问题事实若没有明确决定则拒绝提交。[OW-JOBS]
8. **每页提交即形成持久边界。** 正文、事实、页面清单先保存并证明一致，再把页面任务标为完成。失败页面恢复旧内容，其他已完成页面保留。[OW-RUN]
9. **确定性收尾并处理源码漂移。** 维护目录、链接、图与来源。只为本轮真正更新的页面推进基线；跳过及未处理页面保留原基线。结束前后检查源码变化，发现漂移则记录尚需更新，不能直接宣布对应最新源码。[OW-RUN]
10. **严格无变化才跳过模型工作。** Git/工作区状态、历史完成状态、输出语言、事实问题和页面基线覆盖共同参与判断，不能只凭 HEAD 相同或工作区干净跳过。纯生成 Wiki 提交可推进覆盖而不重新研究业务源码。[OW-UTILS] [OW-RUN]

原生执行器支持页面并发，当前默认 1、配置范围 1–8；页面 Worker 不继续委派。失败恢复与限流降并发由执行器控制。并发是吞吐选择，不是理解准确性的证明。[OW-RUNNER]

定时更新依赖外部定时器/CI 触发。CI 实例使用完整 Git 历史，运行更新后有变化才开文档 PR。临时 runner 丢弃工作区后，未提交的 `.run.json` 不会自然保留，不能声称跨任意机器自动恢复。[OW-CI]

### 5.3 哪些不能照搬为质量结论

证据哈希证明内容是否变化，不证明一条解释正确或完整。稳定 Claim ID 和稀疏合并主要解决继承、成本和恢复。未被记录的重要事实也可能漏检；跨模块影响仍依赖调查与规划，源码未变也不等于真实运行环境未变。

OpenWiki 的证据状态核验和 Zest 当前的语义核对不是同一件事。借鉴执行方式时应保留有用的语义复核，调整其粒度和调度，不能用“证据还能解析”替代语义正确性。

## 6. 贴合 Zest 的实现拆分

### A. 项目级授权与自动资料访问

引入独立、稳定版本的项目文档授权；分离来源权限、读取容量和模型上下文容量。把 `survey` 降为目录/入口调查，把已提交源码读取改成完整项目范围内按需访问。范围内分页、补读和上下文拆分自动推进；兼容旧的按包批准记录，旧收据不改写。

主要落点：`understanding_approval.py`、`survey.py`、`cognition_store.py`、`cognition.py`、`cognition_context.py` 及对应 Web 入口。

### B. 工程文档规划、页面写作和页面完成账本

新增页面计划和持久任务；基于现有调查事实形成连贯正文。页面 Worker 负责一页，并可在授权内补查来源。事实、正文与语义复核结果关联；通过后发布该页，失败时保留前一版。六部分仅检查覆盖；索引按真实页面生成，必要时补图与接口示例。

页面状态至少区分待处理、执行中、完成、跳过/失败；运行状态不等于知识穷尽。知识缺口、执行错误、需用户决策分别存储。人工编辑冲突可输出候选文档，让其他页面继续，不能覆盖原文或阻塞整个项目。

主要落点：`initialization.py` 的发布职责拆分、新的页面计划/写作/清单模块、`documents.py`、项目文档导航。现有 SQLite、LangGraph、模型适配器与调用账本可继续使用；本研究没有发现为此更换 TypeScript/DeepAgents 的必要。

### C. 稳定事实身份与页面增量维护

新增跨版本事实继承关系、按内容核验结果、事实所属页面及页面更新基线。更新器组合 Git 变化、证据问题、未覆盖的新模块与跨页面影响，安排最小必要任务。

跨版本保留的含义是“记录过什么、哪些依据未变、何时检查、是否受影响”，不能把未重查结论包装为全项目已验证。页面记录最近实际核验版本、证据检查版本与需要复核的状态，可逐步提高复用率。

事实的原始调查记录和评审不覆盖；新版本建立继承/替代/撤回关系。团队共享文档需要携带可重建的页面与证据清单，放在 `.zest/docs/` 的程序管理子目录；个人请求、API 配置和执行账本继续私有。首次接入旧文档时逐页建立清单，不能只从全局版本猜出完整覆盖。

主要落点：`cognition_refresh.py`、`maintenance.py`、`cognition_store.py`、新页面账本与事实合并模块。

## 7. 验收场景

这些是后续实现的验收要求，本轮没有运行它们。

| 场景 | 期望 |
| --- | --- |
| 超过 200 个文件的项目，关键实现位于后续目录 | 自动分页找到实现；不因目录扫描上限要求用户追加权限 |
| 调查需要读取方法后半段与另一个模块 | 范围内自主补读，正常流程零追加审批 |
| 单次上下文超过内部容量 | 自动分批或按页收束；证据不丢、预算可追踪、进度可恢复 |
| 首轮文档生成 | 可从 quickstart 定位主实现和启动方式；架构/流程有连贯解释、来源及重要未知 |
| 阅读体验 | 不重复铺 UUID、错误码和核对日志；关键操作有流程与异常说明；链接可用、示例有据 |
| 单模块变化 | 更新相关页面和必要关联页；其他正文及页面基线不被伪更新 |
| 新增重要模块但没有既有 Claim 引用 | 规划识别新增覆盖需求，而非只检查旧事实 |
| 删除证据文件 | 关联事实明确复查、迁移或撤回；不能自动猜测功能已删除 |
| 只改变代码行号 | 内容证据可重定位；不要求重写所有页面 |
| 页面失败或进程重启 | 完成页保留，失败页恢复/继续，事实账本与正文一致 |
| 同一版本重复检查、只提交生成文档 | 满足完整无变化条件时零模型调用，不形成文档更新循环 |
| 人工编辑生成页 | 保留人工内容，输出可审阅候选；其余页面照常完成 |
| 运行中产生新提交 | 固定本轮快照，后续新版本排队；状态明确对应版本 |
| 内容正确性 | 以独立参考问题核对源码与生成正文，重点审查主实现、核心规则、协作流程和修改影响 |

调用量、token 用量、耗时和实际费用分别记录；费用没有供应商依据时保持未知。上述机制是否降低成本、提高正确性，必须以同一项目与问题集的实际对照验证，不能从架构推定。

## 8. 本轮交付与下一步

本轮完成：固定版本的 OpenWiki 源码研究、其自带工程文档样例检查、Zest 当前实现和 BlindBox 生成页对照，以及三项改造建议和验收场景。

本轮未进行：修改 Zest 运行逻辑、修改 BlindBox 资料、启动真实项目模型调查、运行 OpenWiki/自动化测试、提交、推送或部署。

建议先实施 A，使“自动生成项目文档”真正拥有连续的资料访问与请求组织能力；再实施 B、C。页面计划与持久账本应在 B 一起建立，避免先增加写作层、之后为增量重新拆一次状态。

## 源码与样例索引

所有 OpenWiki 链接固定到本轮读取的提交。测试文件只是已阅读的测试定义，不代表本轮执行通过。

- [规划与页面写作 Prompt][OW-PROMPTS]：信息架构、项目内继续调查、稀疏提交。
- [原生执行器][OW-RUNNER]：页面 worker、工具、并发与失败恢复。
- [资料工具边界][OW-BACKEND]：只写文档、页面归属、隐藏控制状态与 shell 限制。
- [事实质量与合并指引][OW-CLAIMS]：重要性、一致性及原子事实的含义。
- [页面计划和稀疏事实合并][OW-JOBS]：必需任务补齐、原 ID 更新、无变化继承。
- [生成生命周期][OW-RUN]、[每页完成清单][OW-MANIFEST]、[源码指纹与变化收集][OW-UTILS]。
- [证据预检查][OW-PREFLIGHT]、[文件/行段证据解析][OW-RESOLVER]、[文档收尾][OW-FINALIZER]。
- [实际快速接手页][OW-QUICKSTART]、[实际测试页][OW-TESTING]、[CI 与恢复限制][OW-CI]。
- [页面合并测试定义](https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/test/generation/page-jobs.test.ts)。
- [页面清单测试定义](https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/test/generation/page-manifest.test.ts)。
- [完整生命周期测试定义](https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/test/generation/repository-run.test.ts)。

[OW-PROMPTS]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/agent/repository-prompts.ts
[OW-RUNNER]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/agent/repository-runner.ts
[OW-BACKEND]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/agent/docs-only-backend.ts
[OW-CLAIMS]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/claims/guidance.ts
[OW-JOBS]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/generation/page-jobs.ts
[OW-RUN]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/generation/repository-run.ts
[OW-MANIFEST]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/generation/page-manifest.ts
[OW-UTILS]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/agent/utils.ts
[OW-PREFLIGHT]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/claims/brains/code/preflight.ts
[OW-RESOLVER]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/claims/evidence/repository/resolver.ts
[OW-FINALIZER]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/src/agent/wiki-finalizer.ts
[OW-QUICKSTART]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/openwiki/quickstart.md
[OW-TESTING]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/openwiki/testing/overview.md
[OW-CI]: https://github.com/langchain-ai/openwiki/blob/305599f8e44203f9ca6b74bb6b38eebaf0b0405e/openwiki/operations/ci-scheduling.md

# Zest

Zest 是面向单一项目、长期服务的项目维护 Agent。它结合项目知识、代码和历史记录，帮助开发者理解、诊断、维护和迭代项目，并在用户批准后执行修复与验证。

## 项目状态

当前重点是[代表性项目整体认知与问答验收（M080）](docs/PROJECT_ACCEPTANCE_M080.md)：已导入公开RealWorld样本，按主要模块、核心流程、档案与知识问答的实用性评价，轻微细节不作阻塞项。原[M086调查](docs/PROJECT_ACCEPTANCE_M086.md)因一批核对格式失败而failed；[M087批次隔离](docs/COGNITION_REVIEW_BATCH_ISOLATION.md)及[M089真实验证](docs/COGNITION_BATCH_ISOLATION_M089.md)已让新隔离运行交付20条可用已核对成果，原失败记录保留。M092修复上下文后，M094形成四份真实回答，[M105现已完成24项覆盖评分](docs/PROJECT_EVALUATION_M105.md)：19full/5partial，四问主流程与定位均full，主要缺口在档案的核心流程表达，未达到整体冻结门槛。[M106已调整报告生成与独立评分准则](docs/DOCUMENT_QUALITY_M106.md)：优先有据地讲清项目实际主干，不套样本功能清单，111项离线验证通过。[M107真实报告生成](docs/DOCUMENT_QUALITY_M107.md)已完成1次调用，10条候选明确补出主要处理与协作；[M108独立评分](docs/DOCUMENT_QUALITY_M108.md)给真实性/清晰度full、边界partial，但边界理由漏看实际输入中的未知说明，保留评分疑点，不自动改分或补跑。[M109已接通本地 Web 试用入口](docs/WEB_WORKSPACE.md)，无需 Web token 或登录；现有项目维护流程可在页面准备、审阅和批准。

[M092已调整问答上下文](docs/QA_CONTEXT.md)：来源预算默认128000字符并可配置，精简重复原文与核对审计，优先定位问题中的函数/类实现并合并原生窗口，142项相关用例通过。随后[M094四问真实复测](docs/PROJECT_QA_ACCEPTANCE_M094.md)完成4次调用、0补试，四问均answered且实际引用认知与源码；新答案补充了应用组装、发文保存和关注测试。模型状态不等于整体验收通过。

[M096执行了M095独立评分包](docs/PROJECT_EVALUATION_M096.md)：复用内置评分Skill和生产评分任务图，获批后2次调用、0补试；首批档案模块3项均full，第2批因空额外字段被结构校验拒收而停止。其余21项尚未评分，不能给出整体质量结论；原回答与知识保持。

[M097已修复评分额外字段接收](docs/COGNITION_EVALUATION_RECEPTION.md)：隔离非判定备注、保留原始收据，必需字段/身份/引用/冲突继续校验。56项相关用例与两份实际响应离线回放通过，未新增真实调用或改写旧成绩；后续真实评分须用新冻结包。

[M099已执行评分复测](docs/PROJECT_EVALUATION_M099.md)：批准M098后新增2次调用、0补试。额外字段兼容生效；第2次因full/partial没有候选关联而停止，另有判定/理由矛盾及前后评分变化。共接收6项模型full（含3项复用），其余18项（含四问）仍未完成评分；当前重点是评分Agent的候选关联与覆盖判断，不能宣称整体验收通过。

[M100已修评分候选关联与覆盖判断](docs/COGNITION_EVALUATION_CANDIDATES.md)：先记录候选实际表达，再比较源码支持与必要主干；明确findings为空仍可用candidates。61项相关用例通过，旧分数保留，语义效果待统一新版本的完整实测。

[M102已尝试完整实测](docs/PROJECT_EVALUATION_M102.md)：批准M101后真实调用1次、0补试。模型返回新候选观察，但首批只交付3个目标中的1个并漏必填字段，按规则停止，后8批未发；没有新版正式成绩，四问仍未完成独立评价。当前直接阻塞点是评分交付完整性，不能将漏项解释为项目认知质量不合格。

[M103已改为弹性接收](docs/EVALUATION_FLEXIBLE_RECEPTION.md)：常见字段差异由程序处理，有效项保留、漏项标未评，已知部分响应继续后续已批准批次，未知结果仍停。70项相关用例及原失败响应离线回放通过，未新增真实调用；完整实测另冻结审批。

[M105完整真实评分完成](docs/PROJECT_EVALUATION_M105.md)：9次调用、0补试，24项全部已评，19full/5partial，无未评或字段阻断。主要缺口是档案共享层协作和三条核心流程的保存/返回等主干；四问主流程与定位均full。下一步建议补齐档案主干收束，不继续围绕格式细节打磨。

已具备项目创建/导入、证据问答、词法 RAG、已审核知识、MCP 协作、模型修复建议、批准候选修改、容器验证和管理源码版本切换/回退。完整开发能力与实际部署继续分模块实现，进度和验证记录见开发日志。

固定格式模型输出统一使用带字段描述的 Pydantic 模型与 `with_structured_output`。完整 schema 和实际接口目标进入批准预览；认知工作流对已收到的格式失败可在批准总预算内补试一次，每次独立留存，结果未知不自动重发。实现与边界见 [结构化输出契约](docs/STRUCTURED_OUTPUTS.md)、[格式补试](docs/COGNITION_RETRIES.md)。

已确认同时保留 Web 与 CLI，前端已改为白色主调的简约设计。CLI 已提供以对话和真实阶段时间线为主的交互式问答工作台；现有子命令继续保留。新模块或重要设计先讨论方案再实现，范围与顺序见 [Web 与 CLI 功能开发文档](docs/INTERFACE_PLAN.md)。

## 项目资源目录

项目通过 `project init/import` 接入后，自动生成自己的 **`.zest/` 资源目录**，存放记忆、索引、调查和任务记录。目录内部的 `.gitignore` 忽略全部资源，不改动项目原有文件、根忽略规则或 Git 版本。旧记录保留原位兼容，已有登记可再次导入启用。[项目资源目录](docs/PROJECT_RESOURCES.md)

初始化已接入项目时只校验和连接，保留原有管理状态、批准与自定义内容。`.zest/AGENTS.md` 提供管理导航，索引引用原位文件；原生 README/docs 不另建副本。索引缓存及批准/证据审计保留必要原文。旧项目缺少入口可用 `zest project guide <project-id>` 显式补齐，已有入口不覆盖；新认知包将管理入口作为单独线索冻结后审阅，旧批准不变。

`zest project context <project-id> --format text --progress` 只读汇总当前版本可用的记忆、六部分认知、原生文件索引、调查待办与已结束调查的后续行动，历史与待核实内容单独标明；同样可通过项目 MCP 的 `project_management_context` 查询。不调用模型、重建索引或自动恢复任务。[管理上下文总览](docs/MANAGEMENT_CONTEXT.md)

## 交互式终端

```bash
uv sync --extra ai --extra mcp --extra tui
uv run --extra ai --extra mcp --extra tui zest ui
```

一次启动后选择项目、持续提问、审阅请求并查看真实阶段、引用和缺口。“问答历史”或 Ctrl+H 打开原任务；“项目认知”或 Ctrl+U 新建自主后台调查，返回主对话后仍继续。未知执行只核对、不重试，终端不执行修复。说明见 [终端工作台](docs/TERMINAL_WORKSPACE.md)。

## 自主项目认知

独立评分 Agent 已提供 `zest evaluation create/preview/approve/report`，用版本化 Skill 分析冻结参考、明确结论和原文，程序汇总覆盖与误收；评分须单独批准，不改原知识。[评分规则与使用](docs/COGNITION_EVALUATION.md)

`zest cognition followup-create <run-id> <finding-id>` 可从一条未解决发现准备同版本定向调查包，目标与完成标准单独冻结、批准后再调查，原发现保持原样。[定向调查](docs/COGNITION_FOLLOWUP.md)

补查完成后，`zest resolution create <followup-run-id>` 准备新旧结论对照包；另行批准后由模型提出逐项确认/纠正/未解决和冲突处理建议。用户审阅并显式 `adopt` 后，完整处理的问题才从行动清单移到解决记录，被替代的旧结论退出有效知识，历史报告保留。[补查结果与结案](docs/COGNITION_RESOLUTION.md)

原文核对现按独立属性留存判定与冲突；仅整条全部支持且无未解决冲突时接收，部分正确保留待调查。[核对规则](docs/COGNITION_ATTRIBUTE_REVIEW.md)

M065 已移除核对、评分和结案中由模型填写的字符位置，保留身份/证据校验及逐属性、条件和冲突判断；语义完整性仍由模型判断。[修改说明](docs/COGNITION_SEMANTIC_REVIEW.md)。随后 M066 获批的一次真实请求完成四条新协议核对，Schema 与生产接收器通过；响应仍有证据使用和支持边界疑点，未取得独立语义评分。[单次实测结果](docs/COGNITION_ACCEPTANCE_M066.md)

M068 随后使用内置评分 Skill 独立审查这四条：实际两次调用，一条取得有效支持判断；第二条响应将冲突线索混入待评对象并添加额外字段，按约停止为 partial。原先两条语义疑点未执行到，不能声称已完成质量验证。[独立评分实测](docs/COGNITION_EVALUATION_M068.md)

M069 已明确评分 Skill/Schema 中本批目标与冲突线索的角色，42 项定向回归通过。[职责修复](docs/COGNITION_EVALUATION_SCOPE.md)。随后获批的 M070 四次真实复测全部通过契约，评分模型整条支持三条、证据覆盖不足一条；单项标签与理由一致性、清洗测试支持边界仍有疑点，不代表整项目质量通过。[真实复测结果](docs/COGNITION_EVALUATION_M070.md)

M071 已区分“全部属性检查完”和“每项证据充分”，并要求按原句强度区分测试输入、结果断言和实际运行。79 项定向回归通过。[证据支持规则](docs/COGNITION_EVIDENCE_SUPPORT.md)。随后 M072 获批的三次真实复测均有效，过滤结论的证据不足标签改善、四项合成对照吻合；真实长结论的清洗概括仍有疑点，未取得全项目质量结论。[复测结果](docs/COGNITION_EVALUATION_M072.md)

M073 从报告生成端明确不同对象、证据要求和结论强度分条，保留有依据的概括、明确推断和未知；76 项相关回归通过，旧结果不拆改。[生成粒度](docs/COGNITION_FINDING_GRANULARITY.md)。随后 M074 两次获批生成均有效，真实报告明确保留清洗断言缺口，但仍混合多种规则，正式语义评分未执行，不能据此称质量通过。[生成结果](docs/COGNITION_REPORT_GENERATION_M074.md)

M075 离线冻结了这两份新报告的[独立语义评分包](docs/COGNITION_EVALUATION_M075.md)：保留全部15候选，按现有规则逐条评分13条observed，2条inferred和15条未知仅作冲突线索；预算最多13次、零补试，不计算项目覆盖率。

随后 [M076真实评分](docs/COGNITION_EVALUATION_M076.md) 获批执行13次且全部有效，模型将13条及57个check全判支持；但把含内部空格的 `mixed case` 误判为“无空格”，说明评分仍会语义误收。原标签和反例保留，未补跑、未计算准确率或项目覆盖分数，本包预算已消费。

M077 已增加[评分专用证据观察](docs/COGNITION_EVALUATION_OBSERVATION.md)：每项先描述获准原文，再比较具体属性；保留原文核对/结案协议与历史结果，108项相关回归通过。当前只验证协议和保存边界，0次新真实调用，尚不能证明误判减少。

M078 冻结了[七次真实复测包](docs/COGNITION_EVALUATION_M078.md)：原失败S:F4输入原样一次，加六个独立静态对照，预期答案只留本地；预算7次、零补试、无字符扩展，不计算项目覆盖或总体准确率。

随后 [M079真实复测](docs/COGNITION_EVALUATION_M079.md) 获批完成7次，全部有效；六对照符合本地粗粒度预期，但原S:F4仍把 `mixed case` 写成“无内部空格”并判支持，错误进入新观察字段。本轮没有解决原漏判，未补跑/改标签，本包预算已消费。

最近整轮实测：[M064 报告](docs/PHASE_REPORT_M064.md)。M063 后同一样本新冷启动在 20 次物理请求后因模型返回的字符范围未覆盖原结论而失败，44 条候选全部待核对；独立评分未执行，M066 单次协议验证没有改写该状态。历史 [M058 报告](docs/PHASE_REPORT_M058.md) 的事实 8/10、核心 3/3、流程 3/6 和语义误收结论保持原样，不移作本轮分数。当前仍不能声称完整理解或稳定维护能力。

```bash
uv run zest cognition create <project-id>
uv run zest cognition preview <run-id>
uv run zest cognition approve <run-id> --approval-sha256 <完整预览的哈希>
uv run zest cognition watch <run-id>
uv run zest cognition dossier <run-id> --format text
```

本地摸底后一次批准固定版本、完整资料包、模型接口和预算。主 Agent 规划，任务 Agent 用 MCP/RAG 自主检索和读取，核对后形成六部分档案；进度、原文依据与未知项在 CLI、终端和 Web 展示。同版本自动核对知识进入新问答、修改建议及后续调查的批准预览；失效和跨版本记录不沿用。关闭工作台不取消独立 worker，未知模型请求不自动重发。[使用、架构与边界](docs/COGNITION.md)

版本变化后可用 `zest cognition refresh-preview <project-id> --format text --progress`，或 Ctrl+U →“版本更新”，只读比较选定 commit 与既有认知证据，列出待刷新线索。不调用模型、不改变旧知识，也不把未变证据直接升级为新版本结论。[增量认知预览](docs/COGNITION_REFRESH.md)

`zest cognition refresh-create <project-id> --format text --progress` 可将清单与目标版本安全资料冻结为新的待批准调查包；审阅 `cognition preview <run-id>` 后另行批准，才会规划和执行增量调查。旧发现只是待核对线索，所有新知识仍需目标版本原文与正常核对；准备命令不调用模型。

完整原文与读取记录保留，模型上下文去除重复表达；末轮保留报告机会。完整请求体默认 32000 字符，适度超限先暂停，审阅后可批准本次扩展至最高 48000。[上下文、批准与恢复](docs/COGNITION_CONTEXT.md)

最近一次完整冷启动测试形成 10 条自动核对事实，关键覆盖 3/10、核心 1/3、流程 1/6，仍未通过；八条候选被“最多六条”整份拒收。该轮空读取/无效项隔离分支未触发。[实测结果与覆盖边界](docs/COGNITION_ACCEPTANCE_M042.md)

M042 已修正空读取仍分配正文编号的问题，并逐条隔离报告中的无效候选，让其他候选继续独立核对。目标异常分支已有离线验证，历史真实成绩不改写。[报告接收规则](docs/COGNITION_REPORT_RECEIPT.md)

M044 已把报告“最多六条”改为软建议。随后另行批准的单次定向真实验收返回 10 条候选，均完整接收，空文件没有正文引用；派生的本地合成坏引用被单独隔离。这验证了限定的报告接收路径，未重评整体质量、未发布知识；还发现包内未读资料被误报为包外需求。[本次结果与后续问题](docs/COGNITION_ACCEPTANCE_M044.md)

M046 将资料需求改为具名类型，由程序对照固定批准目录分类。随后单次真实复测的 5 项需求均正确归为包内；原 10 条候选中一条把空文件元数据误放入正文发现，已隔离，保留其余 9 条。资料分类目标通过，全部冻结判据未通过，没有额外调用或发布知识。[分类规则](docs/COGNITION_MATERIAL_REQUESTS.md)、[复测结果](docs/COGNITION_ACCEPTANCE_M046.md)

M048 将任务模型输入中的空读取元数据与正文证据显式分开，强化类型说明，原始历史和证据均保留。随后获具体批准的一次真实复测收到末尾多一个 `}` 的无效工具参数，保存原始收据后停止，未进入报告接收、未重试；线上元数据表达效果仍未知。[元数据边界](docs/COGNITION_REPORT_RECEIPT.md)、[本次复测](docs/COGNITION_ACCEPTANCE_M048.md)

M040 已取消来源线索的额外 6/8 数量限制，扩大包内阅读、目录与报告容量，资料较多或摸底不完整时提示确认范围；随后经独立批准完成上述一次真实验收，未补跑。[当前容量与验证边界](docs/COGNITION_CAPACITY.md)

## 本地项目摸底

```bash
uv run zest survey build <project-id> --format text
uv run zest survey list <project-id>
uv run zest survey show <survey-id> --format text
```

固定管理源码/HEAD，采集文档、嵌套依赖声明、入口/测试候选及 Python 声明地图，保存来源、版本、内容标识、覆盖与缺口。终端显示真实阶段和文件数，默认 JSON 输出仍可用于脚本。材料为主 Agent 后续制定计划准备，六个认知部分保持待调查；没有模型调用、项目执行或自动知识入库。[使用与边界](docs/SURVEY.md)

## 已确认的方向

- **开发模式**：从项目创建或早期开发开始参与，持续积累经过确认的需求、设计决策和维护记录。
- **导入模式**：接入已有项目，通过调查代码、文档、配置和历史记录建立初始理解，明确已查证的信息与知识缺口。
- 两种模式共用长期维护能力，支持项目理解、故障诊断、处理建议以及后续开发与维护。
- Zest 应自主建立并持续更新项目认知，用户介绍和选源只是可选补充；首轮静态调查、增量预览及待批准增量调查包已实现，自动触发与真实增量效果验收仍待完成，运行环境调查待设计，见 [长期项目认知方向](docs/PROJECT_UNDERSTANDING.md)。
- 修改方案须经用户批准；修复在隔离环境中进行，验证通过后才允许切换版本，并保留版本记录与回退能力。部署和数据回退的具体机制仍待设计。

## 架构方向

- **项目知识库与 RAG**：检索文档、设计决策和经过确认的历史案例，保留来源与适用版本。
- **代码与运行工具**：读取对应版本的代码、Git 记录、日志和运行状态，为分析提供证据。
- **长期记忆**：保存经过确认的项目约束和维护结论，不将未经验证的模型推测当作事实。
- **LangChain**：组织模型、检索和工具调用能力。
- **LangGraph**：编排有状态的任务流程、人工批准和恢复。
- **Multi-Agent 与 MCP**：通过专业分工完成任务，并接入项目工具；具体角色与工具范围逐步确定。

## 开发方式

使用 Git 进行版本控制、GitHub 托管仓库。当前基础模块使用 Python 3.11+、Git 和 Python 自带的 SQLite，运行不需要模型密钥或额外 Python 依赖。后续接口、模型和部署选型逐项确定。

总体设计、功能边界和后续讨论顺序见 [开发设计文档](docs/DEVELOPMENT.md)。文档区分已确认方向、建议方案与待确认事项。

完整本地使用路径见 [本地维护闭环验收](docs/LOCAL_ACCEPTANCE.md)。可先运行 `zest doctor` 检查 Git、Python、依赖和模型配置；需要容器验证时使用 `zest doctor --docker --image <已安装镜像>`。检查不会调用模型、下载镜像或执行项目代码，也不会打印密钥。就绪状态不是模型连通、项目测试或运行部署的成功证明。

## 运行当前模块

在 Zest 仓库根目录执行：

```bash
# 登记一个本地 Git 仓库，参数必须是仓库根目录
python3 -m zest project import /absolute/path/to/project

# 也可以登记 Zest 自身
python3 -m zest project import .

# 查询登记记录
python3 -m zest project list
python3 -m zest project show <project-id>

# 查看已登记项目的只读概览
python3 -m zest project overview <project-id>

# 提取固定版本的 Python 模块、类、函数与 import 声明（不执行源码）
python3 -m zest project map <project-id>
python3 -m zest project map <project-id> --commit <完整commit>

# 读取指定文件的前 20 行
python3 -m zest project read <project-id> README.md --end-line 20
```

命令输出 JSON。`import` 返回项目 ID、规范化路径、分支、commit、采集时间和工作区状态。重复登记同一路径会更新记录，保留原项目 ID。

`project map` 与 `/api/projects/<id>/structure` 提供 Python AST 声明地图，默认使用管理源码版本，附带工作区 HEAD、解析器版本、符号词法名称、作用域、装饰器起始行、import 别名/相对层数、commit/blob/原文件哈希和行号。只记录声明，不导入模块或执行装饰器，不解析动态调用/动态 import 或假定真实运行依赖；不读取未提交内容。

最多检查 100 个 Python 文件/4 MiB/4096 个树条目、单文件 20000 语法节点/200 声明、合计 3000 声明，沿用 256 KiB 文本/凭据/链接规则；解析器使用当前 Python 语法，不兼容语法及超预算文件计为跳过。30 秒为操作间检查预算，不是独立解析沙箱或硬终止保证；warnings/skipped_counts 和 python_only 限制说明未覆盖部分，不能称为理解了整个项目。[Python AST](https://docs.python.org/3.14/library/ast.html)

默认项目登记保存在**运行命令时所在目录**下的 `.zest/state.sqlite3`；新接入项目的资源分别存于**各项目根目录**的 `.zest/state/`。切换启动目录会使用不同的默认登记入口；需要固定入口时，将全局参数放在 `project` 前：

```bash
python3 -m zest --state-dir /absolute/path/to/zest-state project import /absolute/path/to/project
python3 -m zest --state-dir /absolute/path/to/zest-state project list
```

`list` 和 `show` 返回上次登记时的状态，不会重新采集仓库；再次执行 `import` 可以更新。仓库不存在、不是 Git 工作区或提供了子目录时，命令返回非零退出码，并将 JSON 错误写入 stderr。

`import` 不复制或索引源码，不执行项目脚本，不获取远程仓库，也不改变目标仓库的文件、分支和索引。登记记录不是完整的文件内容快照；脏工作区只记录变更状态与数量。`untracked_entries` 使用 Git 的普通状态统计，未跟踪目录可能作为一项，而不是逐文件计数。

## 创建新项目

```bash
python3 -m zest project init /absolute/path/to/new-project --name "示例项目" --goal "管理故障诊断与处理"
# 如果尚未配置 Git 作者，同时提供 --author-name 和 --author-email
```

父目录需要已经存在，目标目录必须全新；即使是空目录也拒绝覆盖。入口只生成 README 和 .gitignore，初始化 main 与一个 Git 提交，登记为 development；不预设业务语言或框架。作者使用已有 Git 配置或显式参数，新仓库局部保存作者，不改全局配置。创建过程忽略全局模板、过滤器、签名与 hooks，不执行项目代码或调用模型。

返回 project 和 goal_note。目标以 project 范围的 draft 保存，可用 `knowledge confirm <note-id>` 确认后加入长期知识；README 明确这是用户声明的目标，功能尚未实现。后续 import 刷新保留模式和 ID，两种入口共用全部维护能力。创建发生部分失败时保留文件和 Git 状态，需检查后恢复，不自动删除或覆盖目录。本地创建不自动新建 GitHub 仓库、配置远程或推送。

## 项目概览

`overview` 在查询时选择管理版本（尚未选择时用 HEAD），始终按这个 commit 读取 Git 对象，不读取暂存区、工作区或未跟踪文件。Git 工作区 HEAD 单独报告；如果 HEAD 与上次登记不同，会返回 `registration_head_changed` 提示。

当前输出包括：

- `source`：本次选定的 commit，明确不包含未提交修改。
- `root_directories`：根目录列表，排除常见依赖和生成目录，最多 50 项。
- `manifests`：根目录中的依赖/构建声明。解析 `pyproject.toml` 的标准 `[project]` 信息和 `package.json`；其他已识别声明只记录位置。
- `declared_commands`：Python console scripts 和 Node scripts 声明，作为信息返回，不执行。
- `documents`：根目录 README，以及 `docs/`、`doc/` 直接子文件中的 Markdown、RST 和 TXT，每个文档目录最多 50 项，不读取文档正文或递归扫描。
- `entrypoint_candidates`：少量常见入口文件候选，如 `main.py`、`src/index.ts` 和根目录包的 `__main__.py`，不表示已确认的业务入口。
- `warnings`：未提交修改、登记过时、声明解析失败、大小/数量限制等信息。

文件来源包含 `path`、`commit`、`blob_id`。单个解析声明限制为 256 KiB；超限只记录位置。符号链接不跟随，`.env` 等非白名单文件内容不收集。元数据仍可能包含项目自行写入的敏感信息，后续向模型或外部服务传输前需要另行设计脱敏。

这是确定性目录和声明概览，尚不分析业务逻辑、依赖调用关系，也不保证声明的命令能成功运行。未提交代码的理解和 monorepo 内层声明扫描仍待后续设计。

## 按行读取文件

```bash
# 默认查询时的 HEAD；省略行号时最多返回前 200 行
python3 -m zest project read <project-id> zest/cli.py

# 读取一个行号范围
python3 -m zest project read <project-id> zest/cli.py --start-line 10 --end-line 30

# 固定到概览返回的完整 commit ID，避免后续 HEAD 变化影响引用
python3 -m zest project read <project-id> README.md --commit <full-commit-id> --end-line 20
```

`path` 必须是规范的仓库相对路径。`--commit` 仅接受本地存在的完整 40/64 位 commit ID，不接受分支名、缩写或 Git 表达式；省略时选择管理版本，尚未选择时用 HEAD。未提交内容不读取，已登记记录不自动刷新。

返回 JSON 包括：

- `evidence`：路径、commit、blob ID、实际开始/结束行。
- `lines`：每行的 1 起始行号和文本；行号按 LF 分隔，CRLF 的行文本去除行末 CR。
- `content`：选定片段，保留原始换行；`content_sha256` 对应完整文件原始字节，`excerpt_sha256` 对应片段原始字节。
- `total_lines`、`more_before`、`more_after`：总行数和片段前后是否还有内容。
- `warnings`：脏工作区被排除、登记过时、指定版本不是当前 HEAD 等提示。

每个文件最多 256 KiB，每次最多 200 行，路径最多 32 层。结束行超过文件末尾会收缩；开始行越界报错；空文件返回空内容和空行列表，引用行号为 `null`。

当前只允许常见源码、文档和文本配置类型。绝对路径、点目录、控制字符、常见生成目录和凭据目录会被拒绝；不跟随符号链接或进入子模块，不读取 `.env*`、常见密钥文件、二进制和非 UTF-8 文件。检测到部分已知凭据模式时，整次读取被拒绝，错误不包含凭据值。这些规则不保证发现所有秘密，也不替代后续向模型传输前的权限与脱敏设计。

正文仅作为本地数据返回，不执行、不解释为新指令，也不自动发送给模型或外部服务。

## 关键词检索

```bash
python3 -m zest project search <project-id> "LangGraph"
python3 -m zest project search <project-id> "main" --limit 10 --case-sensitive
```

`search` 使用字面子串，不执行正则表达式，默认不区分大小写。可使用完整 `--commit` 固定历史版本。每个匹配包含路径、commit、blob ID、行号、预览和完整文件 SHA-256。

搜索复用文件读取的路径、大小、编码和凭据检查，跳过不允许的文件并汇总 `skipped_counts`。默认最多考虑 200 个允许文件、扫描 4 MiB、返回 20 个匹配行（可调整到 1—100）。时长软预算为 15 秒，单条 Git 查询另有 10 秒超时。达到预算会设置 `scan_complete=false`；无匹配不证明未扫描或跳过区域也不存在关键词。

单行预览最多 2000 字符，截断时标记 `text_truncated` 和 `match_in_preview`，可再用 `read` 读取原行。本模块不构建向量索引，不执行仓库代码或调用模型。

## 测试

完整套件使用 `uv run --extra ai --extra mcp python -m unittest discover -s tests -q`。

```bash
python3 -m unittest discover -s tests -v
```

测试在临时目录创建真实 Git 仓库，不依赖模型 API、数据库服务或网络。

## 模型问答

安装锁定依赖：`uv sync --extra ai`。模型配置见 `.env.example`，密钥写入本地 `.env`（Git 忽略），不写入代码、日志或命令参数。

```bash
uv run --extra ai zest project ask <project-id> "项目怎么启动？" --keyword main --preview
uv run --extra ai zest project ask <project-id> "项目怎么启动？" --keyword main --allow-model
```

preview 只展示上下文，不读取模型密钥或调用模型。allow-model 明确允许将问题和片段发送到配置服务。支持最多 3 个 keyword 和 5 个 file 参数，固定 commit，最多 8 个来源和 24000 字符。

答案是结构化 JSON，程序检查字段、来源 ID 和部分凭据模式。引用存在不等同于结论正确；无证据时不调用模型。不修改代码，关闭自动外部 tracing，不额外将本地绝对路径或密钥放入 Prompt。

使用 [LangChain DeepSeek 集成](https://docs.langchain.com/oss/python/integrations/chat/deepseek) 和 [DeepSeek Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)。保留用户指定模型名，另行记录供应商报告名。

## 持久化问答任务

```bash
uv run --extra ai zest task create <project-id> "项目怎么启动？" --keyword main
uv run --extra ai zest task show <task-id>
uv run --extra ai zest task list
uv run --extra ai zest task approve <task-id> --payload-sha256 <show返回的哈希>
uv run --extra ai zest task reject <task-id>
```

create 读取配置并准备上下文，不调用模型，LangGraph 在批准节点暂停。show 展示固定来源和目标接口；批准的哈希绑定实际模型 payload 与接口。approve 会发送这个固定上下文，不重新采集代码；接口或模型变化会阻止调用。

新接入项目的任务存于项目内 `.zest/state/tasks.sqlite3`，旧记录按原路径兼容；使用 SQLite checkpointer 和同步 checkpoint，等待批准状态可跨进程恢复。同一任务在 macOS/Linux 使用进程文件锁，已结束任务不能重复批准。密钥不进入 checkpoint，列表不输出来源正文。

当前是本地前台任务，没有后台 Worker、多人权限或自动重试。执行过程中崩溃可能留下 pending_execution，需要后续恢复机制；不承诺外部模型调用恰好一次，也不自动重放可能已经计费的请求。

## 长期项目知识

```bash
uv run --extra ai zest knowledge add <project-id> --title "取消订单约束" --body "取消时需要释放库存"
uv run --extra ai zest knowledge list <project-id>
uv run --extra ai zest knowledge show <note-id>
uv run --extra ai zest knowledge confirm <note-id>
uv run --extra ai zest knowledge invalidate <note-id> --reason "已被新方案替代"
```

新记录始终为 draft，确认后才进入问答。默认 scope=snapshot，仅适用于创建时的 commit；显式使用 `--scope project` 可以声明跨版本约束，包括尚无提交的新项目。类型支持 decision、constraint、maintenance。

新接入项目的记录和状态事件保存在项目内 `.zest/state/knowledge.sqlite3`，旧记录原位兼容；已失效记录不进入新问答，等待批准任务中引用的知识如果失效或变化也会阻止调用。当前不自动将聊天或模型推测写成确认事实，不自动解决冲突，不支持多人审批或正文覆盖编辑；纠错采用新草稿加旧记录失效。

完成验证后源码选择的操作可留存历史案例：

```bash
python3 -m zest release capture <applied-promotion-operation-id>
python3 -m zest knowledge show <returned-note-id>
python3 -m zest knowledge confirm <returned-note-id>
```

capture 重新核对操作、历史引用及真实验证完整哈希，不调用模型；保存路径/变更哈希、基础与候选版本、测试/镜像来源，并将方案说明标为未确认推断。记录为 project 范围的 maintenance 草稿，observed_commit 明确指向当次候选；确认后可跨版本检索，正文标明历史案例不代表当前已修复或运行已部署。

操作 UUID 固定知识 ID，多次捕获保留确认/失效状态，不覆盖相同 ID 的不同记录。可以在后续版本或回退后捕获旧操作，来源版本不会误记成当前 HEAD；等待批准、未完成验证、离线正式入口或回退操作不能冒充新的修复案例。

## 词法 RAG 索引

```bash
uv run --extra ai zest index build <project-id>
uv run --extra ai zest index search <project-id> "超时重试"
uv run --extra ai zest project ask <project-id> "如何处理超时？" --retrieval lexical --preview
uv run --extra ai zest task create <project-id> "如何处理超时？" --retrieval lexical
```

索引只包含固定 commit 的允许文本，40 行分块、10 行重叠，保留来源与行号。使用英文标识符和中文二元字符词法 TF-IDF 向量、余弦相似度排序，是无需 Embedding 服务的 RAG 基线，不是模型语义 Embedding。[TF-IDF 参考](https://scikit-learn.org/stable/modules/feature_extraction.html#text-feature-extraction)

最多考虑 200 个文件、4 MiB 文本、2000 块，单块最多 6000 字符，构建时长软预算 30 秒。跳过范围写入 metadata；预算失败不替换已有索引。更新 commit 后显式重建，不能把旧索引当成当前版本。查询历史索引可指定完整 commit。

新接入项目的索引保存在项目内被忽略的 `.zest/state/index.sqlite3`，旧版本索引仍可按原位置读取；包含项目文本，须按私有数据处理。问答使用排序结果的路径/行号重新读取 Git 来源，仍需要明确批准后才能发送模型。没有向量数据库服务、语义模型或大仓库性能保证。

## 本地只读 MCP

```bash
uv sync --extra ai --extra mcp
uv run --extra ai --extra mcp zest mcp serve <project-id>
```

服务仅使用 stdio，绑定一个已登记项目，暴露 project_overview、project_management_context、read_file、keyword_search、indexed_search、confirmed_knowledge、project_structure、symbol_lookup 八个只读工具。工具不能指定另一仓库、运行 Shell、修改文件或批准任务。路径、版本和内容规则复用已有模块。

project_structure 返回 Python 声明地图；symbol_lookup 按词法名称或路径查询，最多返回 20 项固定版本/行段信息。可再将返回的 path/commit/start_line/end_line 交给 read_file 读取原文（长符号须自行限制至 200 行），核对版本、blob 与原文件哈希。不自动授予模型新的上下文范围，也不推断实际调用关系。

工具结果统一为 `{ok: true, data: ...}` 或 `{ok: false, error: ...}`；annotations 是提示而不是权限系统。已验证 FastMCP 内存客户端、stdio 子进程，以及 LangChain MCPAdapter 的工具发现与调用。LangChain MCP namespace 当前为 beta，依赖由 uv.lock 锁定。

外部 MCP 客户端可将 `.venv/bin/python` 的绝对路径作为 command，args 使用 `-m zest --state-dir <绝对状态目录> mcp serve <project-id>`。不要把密钥放进 MCP 客户端配置；这个只读服务不需要模型密钥。

## 专业 Agent 协作

```bash
uv run --extra ai --extra mcp zest project ask <project-id> "解释实现与约束" --file zest/cli.py --strategy multi --preview
uv run --extra ai --extra mcp zest task create <project-id> "解释实现与约束" --file zest/cli.py --strategy multi
```

多 Agent 模式将批准来源分给文档/约束和代码分析两个专业 Agent，LangGraph 并行执行，再由协调 Agent 回到原始来源核对。先用本地 MCP 校验文件证据，并核对知识状态；不访问新增的未批准文件。最多 3 次模型调用，没有某类来源则跳过对应调用。策略和调用上限进入任务批准哈希。

这是有界专业分工，不是任意自主执行。专家输出不成为新的事实来源，引用仍需程序校验；当前多 Agent 效果由离线夹具验证，没有新增外部项目数据传输或总体准确率声明。

## 批准具体修改

先通过 `project read` 取得原文件的 content_sha256，准备本地 JSON 方案：

```json
{"summary":"修复加法逻辑","changes":[{"path":"app.py","expected_sha256":"原文件SHA256","content":"def add(a, b):\n    return a + b\n"}]}
```

```bash
python3 -m zest repair create <project-id> --plan /absolute/path/to/plan.json
python3 -m zest repair show <repair-id>
python3 -m zest repair approve <repair-id> --plan-sha256 <show返回的哈希>
python3 -m zest repair reject <repair-id>
python3 -m zest repair list <project-id>
```

方案绑定基础 commit、项目、完整新正文、原文件哈希和权限策略。show 返回完整内容及 diff；批准前必须匹配哈希并重新检查干净工作区和基础版本。状态目录应置于仓库外或被 Git 忽略，否则本地状态文件会导致工作区不干净。

修复执行器仅应用确定的批准方案；模型建议入口见后文，不能绕过执行批准。最多替换或创建 5 个普通文本文件，每文件新旧内容各最多 64 KiB、总计 256 KiB；不能删除文件、跟随链接、改 `.env` 或运行命令。使用独立 Git index 和原始 blob 写入生成 candidate_commit，保存于 `refs/zest/candidates/<repair-id>`，原工作区、index、分支和 HEAD 保持不变。这是文件变更隔离，不是执行沙箱。[Git index 原理](https://git-scm.com/docs/git-update-index)、[候选提交](https://git-scm.com/docs/git-commit-tree)

创建文件必须显式指定 `operation: "create"` 和 `expected_sha256: null`，已有路径不得覆盖；所有新文件 mode 为 100644，支持空文本初始化文件。父路径经过既有文件、链接、子模块，或出现重复、大小写、文件/子路径冲突时拒绝。路径是否存在依据固定 Git 版本，候选创建不写入原工作区。

候选仍待验证，不能自动完成版本切换。执行崩溃保留 pending_execution 和私有 index，不自动重放；失败可能留下不可达对象或候选 ref，需要后续人工检查。状态检查禁用已配置的 clean/process/smudge filter；使用字节比较，过滤器转换项目可能因此显示差异。

## 验证候选版本

准备已经安装、经过信任审核且包含项目依赖的 Python 测试镜像。Zest 不自动 pull、不联网安装依赖，镜像标签在准备阶段解析并固定为完整 image ID。

```bash
python3 -m zest verification create <repair-id> --image <local-test-image> --timeout 30
python3 -m zest verification show <verification-id>
python3 -m zest verification list <project-id>
python3 -m zest verification approve <verification-id> --plan-sha256 <show返回的哈希>
python3 -m zest verification reject <verification-id>
```

计划绑定修复方案、基础/候选 commit、两份完整文件清单和 SHA-256、镜像 ID、本地 Docker Unix socket、固定测试命令和资源策略。每次测试前检查候选 ref、父提交与变更范围，重新读取对应 Git 快照。仅支持 `python -m unittest discover -s tests -q`；从预览批准后才执行，不能提供任意宿主机命令。默认 socket 为 `~/.docker/run/docker.sock`，可用 `--docker-socket` 指定另一个本地 socket，禁止远程 Docker 地址。

通常两版本分别执行并保存基线与候选结果；项目代码仅在容器内运行。禁网、非 root、只读根与快照、丢弃全部 capabilities、no-new-privileges，限制 1 CPU、256 MiB、64 进程与 64 MiB 临时目录；仅挂载导出的只读快照，不挂载 Git、密钥或 Docker socket，拒绝镜像额外声明的 volumes。启动前核对实际 Docker 配置，超时终止并删除该任务容器。[Docker 运行限制](https://docs.docker.com/engine/containers/run/)

初始开发的例外：仅当全部为创建操作且基线不存在匹配 `tests/test*.py` 的文件时，预览明确绑定 `baseline_strategy=not_applicable_initial_creation`。批准后不启动基线容器，记录 not_applicable/未执行；候选仍必须真实运行至少 1 项测试并通过。替换操作或已存在匹配测试不能使用此策略，切换阶段再次核对；不会把未运行的基线标为通过。

候选 exit=0 且至少报告 1 项测试、未 OOM、日志未超限、实际配置匹配且两次容器清理完成，才标记 passed；基线结果可通过或失败，但执行环境错误不能当作基线测试结果。passed 表示这套测试通过，不证明需求或测试本身完整正确，也不自动切换代码或部署。

首版完整快照最多 500 个允许文本文件/8 MiB，单文件 256 KiB；出现不允许文件、链接、子模块、二进制或已知凭据时整体拒绝，不能悄悄跳过。标准锁文件已加入允许文件名。输出最多 64 KiB，并过滤已知凭据模式，保存于私有本地状态。执行崩溃保留 pending_execution，不自动重放；Docker 是受限测试环境，不替代生产多租户的完整安全隔离设计。

真实 Docker 回归需显式启用：`ZEST_RUN_DOCKER_TESTS=1 uv run --extra ai --extra mcp python -m unittest discover -s tests -q`，并准备 `python:3.14-slim` 或通过 `ZEST_TEST_IMAGE` 提供已安装镜像完整 ID。普通套件跳过 4 个 Docker 实测，其余使用明确的离线执行夹具，不能当成真实验证证据。

## 管理源码版本的切换与回退

```bash
python3 -m zest release promote <passed-verification-id>
python3 -m zest release show <operation-id>
python3 -m zest release approve <operation-id> --plan-sha256 <show返回的哈希>
python3 -m zest release active <project-id>
python3 -m zest release list <project-id>
# 准备回到该已完成操作的 previous_commit，仍需 show 和 approve
python3 -m zest release rollback <applied-operation-id>
python3 -m zest release reject <operation-id>
# 核对执行中断后的 Git 事务结果，不重新执行切换
python3 -m zest release recover <operation-id>
```

promote 只准备预览：需要 M013 真实 Docker 后端通过的结果，离线夹具、空测试、超限、未清理或不兼容策略不能用于切换。候选须基于当前选定版本；approve 重新核对候选、验证来源与完整结果哈希，以及当前指针和工作区 HEAD。审批哈希绑定操作、前后版本、验证证据和权限策略。

Git 事务同时保存 `refs/zest/releases/<operation-id>/before|after` 并以旧值比较更新 `refs/zest/active/<project-id>`，避免并发覆盖；不跟随符号引用，不运行 hooks。更新以后，概览、读取、关键词、RAG、问答、MCP 和新 snapshot 知识使用这个管理版本，显式 `--commit` 仍可查询历史版本；新索引须按新版本构建，后续修复也以它为基础。[Git 原子引用事务](https://git-scm.com/docs/git-update-ref)

管理版本与原工作区分别报告。切换不 checkout、修改原文件/index/main/HEAD，不部署或重启运行服务。回退恢复以前保存的源码提交，包括可能存在故障的初始基线；预览会标明不保证运行健康，不回退数据。原工作区即使有未提交修改，管理版本回退也能保留这些修改。

Git 与 SQLite 不能共同提交事务；发生中断时保留 pending_execution，recover 依据事务内的历史引用核对结果，不重放。无法核对时仍保持待处理状态。当前依赖可信本地操作者，没有多人认证或防止用户手工改写数据库/引用的审计系统。旧 M012 待批准方案若因新增 workspace_head 绑定不兼容，应重新准备并批准。

已选定源码可以导出：

```bash
python3 -m zest release export <project-id> /absolute/path/outside-project/new-export
python3 -m zest release verify-export /absolute/path/outside-project/new-export
```

全新目录内包含 source/ 文件目录和 manifest.json。导出固定管理 commit，核对已完成版本操作及历史引用；候选来源再核对真实验证记录，回退来源标为 previous_recorded_source，不假定运行健康。清单包含 commit、操作/验证来源、每文件 SHA-256、字节数与执行权限；不附带 Git 元数据、未提交文件或本地密钥配置，不安装依赖或启动代码。

父目录须存在，目标必须全新且位于原项目之外；不覆盖已有文件，部分失败保留目录，不提供完整清单。verify-export 只校验本地清单一致性，拒绝改动、缺失、额外文件、链接和权限变化；不是签名、身份认证或新的测试结果。可在 source/ 中查看并按项目说明使用源码，后续产生文件会改变清单校验结果；原工作区和 main 不变。

## 修复建议 Agent

```bash
uv run --extra ai --extra mcp zest repair suggest <project-id> "加法返回错误" --file app.py
# 首先查看将发送的完整来源、接口、模型、调用上限和哈希
uv run --extra ai --extra mcp zest task show <task-id>
uv run --extra ai --extra mcp zest task approve <task-id> --payload-sha256 <show返回的哈希>
# proposed 返回 repair_id；审阅具体新正文、diff，再单独批准生成候选提交
python3 -m zest repair show <repair-id>
python3 -m zest repair approve <repair-id> --plan-sha256 <show返回的哈希>
```

suggest 通过持久化 LangGraph 准备完整范围并暂停，不调用模型。用户批准数据请求后，专门的修复 Agent 调用模型 1 次、输出最多 4096 tokens；先用本地 MCP 核对文件/知识。请求与批准哈希包含任务用途、文件 ID、固定 Prompt 校验值与版本权限；密钥和主机绝对路径不发送。

明确选择完整既有文件、待创建路径或行段合计 1—5 项，每路径一项；完整既有文件须非空、最多 200 行/64 KiB，整体 JSON 请求最多 24000 字符。完整文件模式发送完整正文；行段模式仅发送明确片段，不能拿片段覆盖完整文件。路径与原哈希由程序取自已批准来源；未知来源、重复文件、凭据、无效 JSON 或未解决缺口不能形成可执行方案。

开发新文件可使用 `repair suggest <project-id> "创建加法函数及测试" --new-file app.py --new-file tests/test_add.py`。预览里的 planned_new_file 只代表批准的待创建路径，不是已有代码；根据固定 Git 版本重新核对不存在及目录安全性。也可同时指定 --file 的既有文件或已确认项目目标作为上下文，仍需模型批准和独立创建批准。

大于 200 行的文件可指定 `--range app.py:251:270`（每项最多 200 行，完整文件仍不超过 64 KiB）。来源标为 edit_mode=anchor；Agent 仅能返回 source_id、old_text、new_text，旧锚点须在批准片段内精确出现一次。程序以固定 Git 原文拼接新片段，保留未发送前缀/后缀、CRLF、中文和末尾换行，实际完整新正文与 diff 在独立修复批准前展示。片段外锚点、重复锚点或整文件输出拒绝。

手工 JSON 同样支持 `operation: "edit"`，字段为 path、expected_sha256、start_line、end_line、old_text、new_text；不允许再提供 content。不使用模糊匹配、正则或猜测位置。读取结果新增 total_bytes，便于确认完整文件预算。

证据不足时返回缺口，无 repair 记录。可执行建议保存为 awaiting_approval 的具体方案，仍需独立修复批准；此步骤不生成候选、执行脚本、验证或发布。来源任务 ID 固定方案 ID，相同方案重复记录不重置拒绝/执行状态；冲突拒绝覆盖。等待批准可恢复，模型执行中崩溃仍不能保证计费或结果恰好一次，不自动重放。

当前修复模型效果使用明确的离线模型和真实 LangChain MockTransport 验证，没有新增真实项目数据发送或准确率结论。生成的方案仍需人工审查与独立容器测试。

## 本地 Web 工作台（主要使用入口）

无需注册、登录或生成 Web token。先构建前端，再启动一个服务：

```bash
uv sync --extra ai --extra mcp --extra web
npm --prefix frontend ci
npm --prefix frontend run build
uv run zest web serve --port 8000
```

打开 [本地工作台](http://127.0.0.1:8000)，页面自动连接。模型仍使用本地 `.env` 中的 `ZEST_MODEL_*` 配置，密钥只留在服务端；不需要 `ZEST_WEB_API_TOKEN`。所有命令继续支持最前面的 `--state-dir`，Web 与 CLI 共用项目资源和原有批准记录。

网页支持创建/导入项目、认知调查及增量/定向补查、问答和修改建议、候选修改、验证、管理源码选择/回退、知识确认/失效、独立评分和补查结案。每次执行先审阅完整请求或计划，再单独批准。源码选择表示管理版本指针更新；运行服务部署仍未实现。

网页操作具有持久化请求身份与后台进程，刷新/关闭页面不会取消已启动操作。最近网页操作可查看进度、错误和结果；相同请求身份重放不重复执行，未知结果不自动重发。完整使用路径、接口和恢复边界见 [Web 工作台](docs/WEB_WORKSPACE.md)。

服务仅绑定 `127.0.0.1`，校验本地 Host/Origin，写入要求同源网页或本地开发前端来源。开发时也可 `npm --prefix frontend run dev`，打开 [开发前端](http://127.0.0.1:5173)，默认连接 8000，可手动调整 API 端口。接口 schema 在 `/openapi.json`，读取响应继续 `no-store`。

前端保留白色简约界面，项目切换清空旧详情，来源与模型文本按纯文本显示；候选 diff、基线/候选验证结果和未知边界保持可审阅。项目历史列表目前仍按既有最近 100 项规则显示，尚未实现完整分页。独立评分参考使用现有 JSON 契约；后续再改善可视化编辑体验。

## 开发日志

每个小模块包含对应实现、验证、文档和日志，并创建一次 Git 提交。开发记录见 [开发日志](docs/DEVLOG.md)。

首个小规模真实认知验收已完成但未通过：响应字段契约与证据上下文收束问题导致没有经核对成果，见 [验收结果](docs/COGNITION_ACCEPTANCE_RESULTS.md)。样本选择、人工参考、评分与执行前检查见 [认知验收方案](docs/COGNITION_ACCEPTANCE.md)，后续复测仍需具体外发批准。

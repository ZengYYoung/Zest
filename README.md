# Zest

Zest 是面向单一项目、长期服务的项目维护 Agent。它结合项目知识、代码和历史记录，帮助开发者理解、诊断、维护和迭代项目，并在用户批准后执行修复与验证。

## 项目状态

已具备项目创建/导入、概览、证据读取、词法 RAG、问答、持久化批准、已审核知识和只读 MCP 专业 Agent 协作。具体进度与验证记录见开发日志；完整维护及修复能力继续分模块实现。

## 已确认的方向

- **开发模式**：从项目创建或早期开发开始参与，持续积累经过确认的需求、设计决策和维护记录。
- **导入模式**：接入已有项目，通过调查代码、文档、配置和历史记录建立初始理解，明确已查证的信息与知识缺口。
- 两种模式共用长期维护能力，支持项目理解、故障诊断、处理建议以及后续开发与维护。
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

# 读取指定文件的前 20 行
python3 -m zest project read <project-id> README.md --end-line 20
```

命令输出 JSON。`import` 返回项目 ID、规范化路径、分支、commit、采集时间和工作区状态。重复登记同一路径会更新记录，保留原项目 ID。

默认状态存储在**运行命令时所在目录**下的 `.zest/state.sqlite3`，已被本仓库忽略。切换目录会使用不同的默认状态库；需要固定位置时，将全局参数放在 `project` 前：

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

`overview` 在查询时采集当前 HEAD，并始终按这个 commit 读取 Git 对象，不读取暂存区、工作区或未跟踪文件。它不自动刷新登记记录；如果 HEAD 与上次登记不同，会返回 `registration_head_changed` 提示。

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

`path` 必须是规范的仓库相对路径。`--commit` 仅接受本地存在的完整 40/64 位 commit ID，不接受分支名、缩写或 Git 表达式；省略时选择当前 HEAD。未提交内容不读取，已登记记录不自动刷新。

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

任务存于 `.zest/tasks.sqlite3`，使用 SQLite checkpointer 和同步 checkpoint，等待批准状态可跨进程恢复。同一任务在 macOS/Linux 使用进程文件锁，已结束任务不能重复批准。密钥不进入 checkpoint，列表不输出来源正文。

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

记录和状态事件保存在 `.zest/knowledge.sqlite3`；已失效记录不进入新问答，等待批准任务中引用的知识如果失效或变化也会阻止调用。当前不自动将聊天或模型推测写成确认事实，不自动解决冲突，不支持多人审批或正文覆盖编辑；纠错采用新草稿加旧记录失效。

## 词法 RAG 索引

```bash
uv run --extra ai zest index build <project-id>
uv run --extra ai zest index search <project-id> "超时重试"
uv run --extra ai zest project ask <project-id> "如何处理超时？" --retrieval lexical --preview
uv run --extra ai zest task create <project-id> "如何处理超时？" --retrieval lexical
```

索引只包含固定 commit 的允许文本，40 行分块、10 行重叠，保留来源与行号。使用英文标识符和中文二元字符词法 TF-IDF 向量、余弦相似度排序，是无需 Embedding 服务的 RAG 基线，不是模型语义 Embedding。[TF-IDF 参考](https://scikit-learn.org/stable/modules/feature_extraction.html#text-feature-extraction)

最多考虑 200 个文件、4 MiB 文本、2000 块，单块最多 6000 字符，构建时长软预算 30 秒。跳过范围写入 metadata；预算失败不替换已有索引。更新 commit 后显式重建，不能把旧索引当成当前版本。查询历史索引可指定完整 commit。

索引保存在被忽略的 `.zest/index.sqlite3`，包含项目文本，须按私有数据处理。问答使用排序结果的路径/行号重新读取 Git 来源，仍需要明确批准后才能发送模型。没有向量数据库服务、语义模型或大仓库性能保证。

## 本地只读 MCP

```bash
uv sync --extra ai --extra mcp
uv run --extra ai --extra mcp zest mcp serve <project-id>
```

服务仅使用 stdio，绑定一个已登记项目，暴露 project_overview、read_file、keyword_search、indexed_search、confirmed_knowledge 五个只读工具。工具不能指定另一仓库、运行 Shell、修改文件或批准任务。路径、版本和内容规则复用已有模块。

工具结果统一为 `{ok: true, data: ...}` 或 `{ok: false, error: ...}`；annotations 是提示而不是权限系统。已验证 FastMCP 内存客户端、stdio 子进程，以及 LangChain MCPAdapter 的工具发现与调用。LangChain MCP namespace 当前为 beta，依赖由 uv.lock 锁定。

外部 MCP 客户端可将 `.venv/bin/python` 的绝对路径作为 command，args 使用 `-m zest --state-dir <绝对状态目录> mcp serve <project-id>`。不要把密钥放进 MCP 客户端配置；这个只读服务不需要模型密钥。

## 专业 Agent 协作

```bash
uv run --extra ai --extra mcp zest project ask <project-id> "解释实现与约束" --file zest/cli.py --strategy multi --preview
uv run --extra ai --extra mcp zest task create <project-id> "解释实现与约束" --file zest/cli.py --strategy multi
```

多 Agent 模式将批准来源分给文档/约束和代码分析两个专业 Agent，LangGraph 并行执行，再由协调 Agent 回到原始来源核对。先用本地 MCP 校验文件证据，并核对知识状态；不访问新增的未批准文件。最多 3 次模型调用，没有某类来源则跳过对应调用。策略和调用上限进入任务批准哈希。

这是有界专业分工，不是任意自主执行。专家输出不成为新的事实来源，引用仍需程序校验；当前多 Agent 效果由离线夹具验证，没有新增外部项目数据传输或总体准确率声明。

## 开发日志

每个小模块包含对应实现、验证、文档和日志，并创建一次 Git 提交。开发记录见 [开发日志](docs/DEVLOG.md)。

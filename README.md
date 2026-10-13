# Zest

Zest 是面向单一项目、长期服务的项目维护 Agent。它结合项目知识、代码和历史记录，帮助开发者理解、诊断、维护和迭代项目，并在用户批准后执行修复与验证。

## 项目状态

已完成两个模块：**本地 Git 项目登记**和**只读项目概览**。当前可以登记仓库，并读取已提交版本中的依赖声明、文档位置和常见入口候选；Agent、RAG、自动修复等能力仍处于设计阶段。我们按小模块逐步确认、实现和提交。

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
```

命令输出 JSON。`import` 返回项目 ID、规范化路径、分支、commit、采集时间和工作区状态。重复登记同一路径会更新记录，保留原项目 ID。

默认状态存储在**运行命令时所在目录**下的 `.zest/state.sqlite3`，已被本仓库忽略。切换目录会使用不同的默认状态库；需要固定位置时，将全局参数放在 `project` 前：

```bash
python3 -m zest --state-dir /absolute/path/to/zest-state project import /absolute/path/to/project
python3 -m zest --state-dir /absolute/path/to/zest-state project list
```

`list` 和 `show` 返回上次登记时的状态，不会重新采集仓库；再次执行 `import` 可以更新。仓库不存在、不是 Git 工作区或提供了子目录时，命令返回非零退出码，并将 JSON 错误写入 stderr。

本模块不复制或索引源码，不执行项目脚本，不获取远程仓库，也不改变目标仓库的文件、分支和索引。登记记录不是完整的文件内容快照；脏工作区只记录变更状态与数量。`untracked_entries` 使用 Git 的普通状态统计，未跟踪目录可能作为一项，而不是逐文件计数。

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

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试在临时目录创建真实 Git 仓库，不依赖模型 API、数据库服务或网络。

## 开发日志

每个小模块包含对应实现、验证、文档和日志，并创建一次 Git 提交。开发记录见 [开发日志](docs/DEVLOG.md)。

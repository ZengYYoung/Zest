# 项目认知工作流

日期：2026-10-14。首轮实现为固定 Git 版本的自主静态调查，适用于已登记的开发/导入项目。

## 开始使用

```bash
uv sync --extra ai --extra mcp --extra tui --extra web
# 在运行目录的 .env 配好模型；不把密钥提交到 Git
uv run zest project import /absolute/path/to/project
uv run zest ui
```

工作台顶部“项目认知”或 Ctrl+U → 新建调查 → 审阅资料包 → 勾选并批准。无需先知道该看哪些文件。准备只做本地摸底和资料保存；批准后才启动独立 worker 和模型工作流。返回主对话或退出工作台不会取消后台调查。

也可使用子命令，全部支持放在命令前面的 `--state-dir /absolute/path/to/state`：

```bash
zest cognition create <project-id>
zest cognition create <project-id> --survey-id <已有摸底记录-id>
zest cognition preview <run-id>
zest cognition approve <run-id> --approval-sha256 <完整预览中的哈希>
zest cognition watch <run-id>
zest cognition show <run-id> --format text
zest cognition dossier <run-id> --format text > project-dossier.md
zest cognition list <project-id>
```

`preview` 包含完整允许文件全文、同版本共享认知、三个工作指令、模型目标和预算。批准哈希覆盖完整包，不仅覆盖清单。包内可自主查找，包外来源不能通过任意路径访问；扩大范围需新建工作流再批准。

`approve` 默认启动后台 worker；`--no-start` 只入队，可另运行 `zest cognition worker`。worker 处理队列，默认空闲 30 秒后退出；`--once` 处理一项后退出。它是独立本地进程，不是永久后台服务。修改、测试、网络工具、发布不在调查批准内。

## 流程与职责

```text
本地摸底 → 完整资料包 → 人批准 → 主 Agent 规划
                                       ↓
                          按任务创建 Agent，最多两个并行
                              MCP 检索/原文读取循环
                                       ↓
                          原文引用校验 → 独立语义核对
                                       ↓
                          有限补查 → 六部分档案 / 缺口
                                       ↓
                       同版本共享认知供问答和后续任务检索
```

主协调图和每个子任务均有独立 LangGraph SQLite checkpoint；任务库保存状态与请求账本。LangChain 处理模型请求，MCP 工具仅使用保存的授权包，不重读当前工作区。资料中的提示不能改变程序权限。

三类内置 Skill 指令与严格契约在 `zest/cognition_contracts.py`。规划用有预算的清单和文档形成 overview/tasks/gaps；任务包含目标、理由、线索、成果、完成标准、优先级/依赖。Agent 可查找包内非线索文件，已完成前置任务报告供依赖任务参考。

词法 RAG 按 40 行、30 行步长分块，TF-IDF 余弦排序；检索只返回位置，必须实际 read 才能引用 R 编号原文。它不是语义向量检索。符号工具提供 Python AST 声明位置，不执行源码。

报告保留 observed/inferred、原文和未知项。核对 Agent 检查证据支持，参考其他发现核对明显矛盾；仅 observed 且受支持的记录可 auto_reviewed。Agent 一致不能证明事实。被否定的观察最多追加两个补查任务，推测保持待核实，不无限循环。

## 默认预算

| 预算 | 上限 |
| --- | --- |
| 初始任务 / 总任务 | 4 / 6，最多两个并行 |
| 每任务模型轮次 / 工具动作 | 4 / 16，每轮至多四个动作 |
| 整轮模型请求 | 32，规划、调查、核对共用原子计数 |
| 执行时间 | 1800 秒，新请求前检查；不是正在执行动作的硬终止 |
| 单请求 payload / 响应 | 32000 字符 / max_tokens=4096；系统指令另存于批准包 |
| 请求超时 | 配置 1–120 秒，不自动重试 |
| 本地采集 | 沿用摸底 200 文件尝试、4 MiB 文本和树/时间预算 |
| 完整授权包 | 16 MiB JSON，包含共享认知 |
| 检索分块 / 结果 | 2000 块 / 六个位置，包尾部可能未覆盖 |
| 工具原文 | 每次最多 200 行及 1200 字符，不截半行 |

超预算、依赖未完成或缺资料时保留部分成果与原因；包外需求显示待扩展授权。静态档案不执行测试，不把配置声明写成运行事实。

## 历史、恢复与核对

```bash
zest cognition show <run-id> --calls
zest cognition recover <run-id>
zest cognition invalidate <run-id> <finding-id> --reason '需要重新核实'
zest cognition reject <run-id>
```

查看不会调用模型或创建空状态库。项目内最近 100 项可浏览，更早记录按完整 UUID 打开。`show --calls` 展示逐次 payload/hash/状态/响应，属于本地私有资料。

请求先记 pending 并扣预算，响应先落库再推进 checkpoint。响应已保存而节点未完成时，显式 recover 可复用同身份响应。pending/uncertain 或无有效结果的请求不能重发：服务端可能已处理或计费。活动 worker/同 run 锁阻止重复执行和并发恢复。

状态：awaiting_approval → queued → planning → investigating → reviewing → completed/partial；错误为 failed，未知调用为 pending_execution。worker 消失时界面显示退出，不冒充仍在执行。completed 仅表示任务完成、六部分有经核对发现且计划无剩余缺口，不是完整理解证明。

记录在本地独立数据库/文件中，创建权限 0600；默认 `.zest` 已忽略。授权哈希用于身份与损坏检查，不是防本地管理员篡改的签名。模型密钥只在执行时读取，不保存到包或请求记录；主图、子图、适配器关闭第三方自动 tracing。

## 支持后续维护

问答和修改建议按问题检索最多三条同 commit 的 auto_reviewed 认知，附原文证明和核对意见，纳入各自的新请求批准。推测、失效和跨版本记录不进入上下文。调用前再次核对身份/状态；旧来源失效会阻止旧预览执行。

新认知工作流也可纳入共享知识，但仍须读原文支撑新发现。任务记忆与共享知识分别保存；原 KnowledgeStore 的人确认记录保持 confirmed。

后台运行时问答可用。修改建议涉及活跃调查的起始文件/已返回证据时，等待关联任务核对后重新准备；仅检查文件交集，不是完整语义依赖分析。间接关联仍需读源码、审阅修改和独立验证。

Web 的“项目认知”只读列表/详情展示阶段、调用数、任务、六部分发现和原文。GET `/api/projects/{id}/cognition`、`/api/cognition/{id}`、`/api/cognition/{id}/dossier` 继承本地 Bearer 认证与 no-store。Web 不能创建、批准、启动 worker 或修改知识。

首轮未包含生产日志、完整 Git 历史、自动增量刷新、向量检索或真实模型质量评测。后续分别设计，不把离线夹具输出作为真实项目理解效果。

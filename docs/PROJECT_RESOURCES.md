# 每个项目自己的 Zest 资源目录

M055/M056，2026-10-15。项目通过 `project init` 或 `project import` 接入后，在项目根目录生成 `.zest/`。它保存 Zest 生成的记忆、管理指引、索引、调查、任务、检查点和维护产物，原有业务文件与目录不移动，也不把原生文档拷贝成第二套文档库。

```text
被管理项目/
├── 原有代码和文档……
└── .zest/
    ├── .gitignore          # *，连此文件自身一起忽略
    ├── workspace.json      # 项目 UUID、位置和历史存储来源；没有密钥
    ├── AGENTS.md           # 首次生成的管理导航，可维护，初始化不覆盖
    └── state/
        ├── state.sqlite3          # 本项目登记镜像
        ├── knowledge.sqlite3      # 人工确认的记忆和维护案例
        ├── index.sqlite3          # 固定版本词法索引
        ├── surveys.sqlite3        # 本地摸底
        ├── cognition.sqlite3      # 调查包、请求/响应、发现与核对
        ├── tasks.sqlite3          # 问答/建议任务与 checkpoint
        ├── repairs.sqlite3 / verifications.sqlite3 / releases.sqlite3
        ├── cognition-checkpoints/ / cognition-locks/ / task-locks/
        └── repair-artifacts/ / verification-artifacts/ / release-artifacts/
```

上图是按使用逐步生成的内容，不为尚未使用的功能创建空数据库。目录创建权限 0700，新数据库和身份文件 0600。记忆仍使用现有 SQLite 和六部分档案，不另造一套与正式知识脱节的 Markdown 副本；可用现有 CLI 导出、终端和 Web 阅读。

## 接入与使用

```bash
zest project import /absolute/path/to/project
zest project show <project-id>       # resource_dir 显示实际状态目录
zest index build <project-id>
zest cognition create <project-id>
zest project context <project-id> --format text --progress
```

已有旧登记也可再次 `project import` 同一路径以启用，保留项目 ID。新创建项目自动启用。

**初始化是幂等连接，不是刷新或修复。** 对身份完整的已接入项目，重复 import/init 只读校验并复用原身份、模式与登记快照；不改写 `.zest/` 任何已有文件或修改时间，不重建缺失的入口或忽略文件，不刷新登记时间、不修改目标、不新增目标知识、不重置批准、任务、checkpoint 或 Git 管理引用。从新的登记目录连接，只在该登记入口写入原身份的定位记录，项目内状态保持不变。要查看最新原生项目状态，用只读 overview/map 等命令。

`project init` 仍只在全新路径创建 README、根忽略文件和 Git 基线；已接入路径重复调用时，传入的名称、目标、作者不会覆盖原内容，返回 `created_files=[]`、`goal_note=null` 与 `existing_project_reused_goal_unchanged` 提示。已有未接入项目用 import，不自动在原目录创建业务文件。

旧项目没有管理入口时可显式补齐：

```bash
zest project guide <project-id>
```

只创建缺少的 `.zest/AGENTS.md`；存在则返回 `created=false`，不覆盖自定义内容。初始化不靠重新运行来修复损坏的资源身份或缺失登记库。

`--state-dir` 保留为项目登记/查找入口，默认仍为启动目录 `.zest`；它可登记多个项目，但新项目数据分别进入各项目 `.zest/state`。本地登记镜像使后台 worker 在项目状态目录内也能找到同一项目。CLI、终端、MCP 和只读 Web 共用存储路由，页面设计不变。

底层 Python `ProjectRegistry.register` 保留默认的历史纯登记行为，调用方可以显式传 `workspace=True` 启用；公开 CLI 创建/导入入口已经启用。各 Store 支持从登记目录按项目/记录 ID 路由，调用方无需手动拼接数据库位置。

## 不改变原生项目文件

忽略规则写在 `.zest/.gitignore`，内容 `*`，不是修改项目根 `.gitignore`。导入不会改变原有文件、暂存区、HEAD 或 Git 引用，也不自动提交、执行项目或调用模型。Git 状态不会因新资源而变脏；已有脏工作区保持原状。`.zest` 已属于源码读取、摸底和索引排除目录，即使被强制提交，也不能作为项目正文证据重新摄入。

已有 `.zest` 身份与当前位置不匹配、非空目录无可核对身份、资源文件/目录为符号链接，或者 `.zest` 已被 Git 跟踪时，接入停止并说明原因，不覆盖、跟随或自动取消跟踪。唯一的旧布局兼容入口是 `.zest/state.sqlite3` 能只读核对到本项目原登记：从原登记入口启用，保留其历史来源；自定义忽略规则不符合资源忽略要求时先报冲突，不追加改写。复制整个项目后若仍带原位置身份，应先核对资源来源，当前不自动改写路径或迁移批准。

## 管理入口、索引和原生证据

`.zest/AGENTS.md` 是管理导航，说明正式记忆、认知档案、任务和索引的位置与核对方式；首次生成的通用说明不代表已经理解业务。项目相关的六部分认知和知识仍由调查、核对与维护逐步形成，可通过 Zest 查询和导出。用户可在 `.zest/` 维护管理文档、生成的导航和 `skills/` 等资源，初始化会保留这些内容；当前没有自动发现、执行任意 skill 的新运行机制。

M057 新增 [只读管理上下文总览](MANAGEMENT_CONTEXT.md)：CLI `project context` 或项目 MCP `project_management_context` 汇总当前版本适用记忆、认知、索引与调查待办；不受历史浏览/模型上下文条数筛选影响，不自动生成文档或扩展旧调查包。

索引指向原生文件的相对路径、commit、blob、哈希与物理行号。现有词法索引缓存文本片段，摸底保存文档摘要窗口，批准资料包和逐请求审计保留必要原文，用于检索、完整审阅、证据核对与恢复；这些派生数据不是另一套 README/docs，也不成为新的原生来源。`.zest` 继续排除在项目正文检索和证据工具之外。

准备新的认知调查包时，若有管理入口，将其作为单独的 `management_guidance` 保存全文和内容哈希，完整进入批准预览；批准后协调与调查 Agent 使用冻结的这一份，不重读后来变化的本地内容。它只是未经核对的管理线索，没有来源 ID/正文引用权限，不能扩大工具、文件范围、预算或执行权限；结论仍需批准包内固定版本原文。不会自动递归收集其他 `.zest` 文档或 skill。原有批准包不会追加新入口，旧请求和恢复身份保持原样。

## 旧记录与恢复

旧版共用数据库保留原位。启用后，新记录写入本项目资源目录；历史列表、适用知识和增量基线同时读取本项目新位置与原历史位置。知识失效仍作用于原记录；索引优先查询新位置，仅在没有对应版本索引时尝试旧位置，损坏索引不会被静默忽略。

按 UUID 打开、批准、拒绝、恢复旧任务时，路由到原数据库与锁/检查点目录，保留原批准哈希和模型请求身份。固定知识 ID、来源任务 ID 的幂等操作仍复用原记录。不会自动复制/搬迁待执行任务、重放模型请求或扩大旧批准，结果未知的请求继续禁止重发。历史来源仍被使用时不能直接删除；本模块不声称已经把全部历史数据迁到新目录。

同一 ID 若出现在多个位置，报告冲突，不猜测执行哪个副本。每次操作使用独立绑定存储实例，不修改共享 Store 的路径，因此不同项目并行写入时不会串库。模型、资料、修改、验证和发布的授权边界保持原样。

## 验证边界

使用临时真实 Git 仓库验证忽略、原生文件/HEAD/index 不变、重复导入和 init 的资源字节/修改时间不变、多个登记入口、并行多项目隔离、旧知识/索引/批准/增量基线兼容、重复副本冲突、未知目录/缺失登记/符号链接与已跟踪目录拒绝；离线夹具验证管理入口冻结与旧批准兼容、认知 worker 的项目内账本/检查点、修改/验证/源码操作产物，以及 Web/MCP 读取。没有真实模型调用、真实 Docker 验证或生产部署。

M060 的独立评分记录存于 `state/evaluations.sqlite3` 和 `state/evaluation-checkpoints/`，只读原调查并冻结评分快照，单独批准调用，不修改原认知/知识或历史成绩。[评分功能](COGNITION_EVALUATION.md)

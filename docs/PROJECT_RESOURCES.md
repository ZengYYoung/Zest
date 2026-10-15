# 每个项目自己的 Zest 资源目录

M055，2026-10-15。项目通过 `project init` 或 `project import` 接入后，在项目根目录生成 `.zest/`。它保存本项目的记忆、索引、调查、任务、检查点和维护产物，原有业务文件与目录不移动。

```text
被管理项目/
├── 原有代码和文档……
└── .zest/
    ├── .gitignore          # *，连此文件自身一起忽略
    ├── workspace.json      # 项目 UUID、位置和历史存储来源；没有密钥
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
```

已有旧登记也可再次 `project import` 同一路径以启用，保留项目 ID。新创建项目自动启用。重复导入不会清空记忆、覆盖检查点或重复追加忽略规则。

`--state-dir` 保留为项目登记/查找入口，默认仍为启动目录 `.zest`；它可登记多个项目，但新项目数据分别进入各项目 `.zest/state`。本地登记镜像使后台 worker 在项目状态目录内也能找到同一项目。CLI、终端、MCP 和只读 Web 共用存储路由，页面设计不变。

底层 Python `ProjectRegistry.register` 保留默认的历史纯登记行为，调用方可以显式传 `workspace=True` 启用；公开 CLI 创建/导入入口已经启用。各 Store 支持从登记目录按项目/记录 ID 路由，调用方无需手动拼接数据库位置。

## 不改变原生项目文件

忽略规则写在 `.zest/.gitignore`，内容 `*`，不是修改项目根 `.gitignore`。导入不会改变原有文件、暂存区、HEAD 或 Git 引用，也不自动提交、执行项目或调用模型。Git 状态不会因新资源而变脏；已有脏工作区保持原状。`.zest` 已属于源码读取、摸底和索引排除目录，即使被强制提交，也不能作为项目正文证据重新摄入。

已有 `.zest` 身份与当前位置不匹配、保留的 state 子目录非空但无身份、资源文件/目录为符号链接，或者 `.zest` 已被 Git 跟踪时，接入停止并说明原因，不覆盖、跟随或自动取消跟踪。复制整个项目后若仍带原位置身份，应先核对资源来源，当前不自动改写路径或迁移批准。

## 旧记录与恢复

旧版共用数据库保留原位。启用后，新记录写入本项目资源目录；历史列表、适用知识和增量基线同时读取本项目新位置与原历史位置。知识失效仍作用于原记录；索引优先查询新位置，仅在没有对应版本索引时尝试旧位置，损坏索引不会被静默忽略。

按 UUID 打开、批准、拒绝、恢复旧任务时，路由到原数据库与锁/检查点目录，保留原批准哈希和模型请求身份。固定知识 ID、来源任务 ID 的幂等操作仍复用原记录。不会自动复制/搬迁待执行任务、重放模型请求或扩大旧批准，结果未知的请求继续禁止重发。历史来源仍被使用时不能直接删除；本模块不声称已经把全部历史数据迁到新目录。

同一 ID 若出现在多个位置，报告冲突，不猜测执行哪个副本。每次操作使用独立绑定存储实例，不修改共享 Store 的路径，因此不同项目并行写入时不会串库。模型、资料、修改、验证和发布的授权边界保持原样。

## 验证边界

使用临时真实 Git 仓库验证忽略、原生文件/HEAD/index 不变、重复导入、多个登记入口、并行多项目隔离、旧知识/索引/批准/增量基线兼容、重复副本冲突、符号链接与已跟踪目录拒绝；离线夹具验证认知 worker 的项目内账本/检查点、修改/验证/源码操作产物，以及 Web/MCP 读取。没有真实模型调用、真实 Docker 验证或生产部署。

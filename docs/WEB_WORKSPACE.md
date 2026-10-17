# Web 工作台：本地实际使用测试（M109）

日期：2026-10-17。Web 为日常主要入口，CLI 保留脚本与恢复能力。当前面向一个可信本地操作者，无需 Web token、登录或注册。

## 启动

在 Zest 仓库根目录：

```bash
uv sync --extra ai --extra mcp --extra web
npm --prefix frontend ci
npm --prefix frontend run build
uv run zest web serve --port 8000
# 自定义状态目录：
# uv run zest --state-dir /absolute/path/to/state web serve --port 8000
```

打开 http://127.0.0.1:8000 自动连接。构建产物由同一个 FastAPI 服务提供。更新前端后重新 build、刷新网页；API 路由变更需重启服务。未构建前端时 `/` 不提供控制台，但 API 仍可用。开发前端另运行 `npm --prefix frontend run dev`，5173 页面默认连接 8000，连接失败时可调整端口。

模型配置沿用 `.env.example` 中的 `ZEST_MODEL_*`，不会发送到浏览器。旧 `.env` 中的 `ZEST_WEB_API_TOKEN` 被忽略。创建新项目使用已有 Git 作者配置；API 也支持显式 `author_name/author_email`，不改全局 Git 配置。

## 从网页试用

1. **项目概览 → 接入项目**：导入已有 Git 根目录，或输入新目录/名称/目标创建 Git 基线。登记生成项目自己的 `.zest/`，不会启动模型。操作回执完成后打开结果选择该项目。
2. **项目认知 → 准备项目认知**：本地摸底、保存固定 commit 的完整包。完成后打开记录，点“审阅完整请求 / 执行计划”，检查完整文件、模型、指令、预算与哈希，勾选再批准。独立 worker 在后台调查；关闭网页不会取消。
3. 认知页持续刷新已打开的后台调查，查看发现、证据、六部分档案和调用记录。精确超限请求在网页单独审阅批准；版本变化可只读预览后准备增量包，未解决发现可准备定向补查。后台进程退出后，排队任务可重新启动；中断恢复仍由原服务判断已知/未知请求。
4. **调查与建议**：输入问题，源码路径可选。准备并打开结果后先审阅，再批准调用；结果保存引用、结论与缺口。修改/开发建议需要明确已有文件或拟新建路径。建议生成不会直接应用修改。
5. **候选修改**：审阅完整 diff、正文、基础版本和方案哈希，单独批准创建候选提交。原工作区保留。候选就绪后填写已安装 Docker 镜像及测试时限，准备验证计划。
6. **测试验证**：先审阅固定镜像、命令、隔离策略和文件清单，再批准执行。基线与候选结果独立展示；离线夹具不能用于正式源码选择。通过后可准备管理源码选择。
7. **版本历史**：预览并批准源码指针选择/回退，或核对中断操作。可导出当前管理源码、核验已有导出清单、从验证后的选择记录生成维护案例草稿。此处不启动业务服务或切换流量。
8. **项目知识**：新增草稿，明确确认后进入适用上下文，失效需原因。**独立评分**接收原有参考 JSON 和认知 ID，准备完整评分包后另行批准；如有精确扩展，须额外勾选。**补查结案**从定向补查记录准备新旧对照，模型批准和采纳效果分开。

操作完成先在“网页操作”回执中打开结果，业务状态以原记录为准。回执 `completed` 表示服务操作已返回；模型任务可能返回 `failed`，评分可能 `partial`，必须查看业务结果。当前评分/结案结果提供完整结构化记录与独立汇总，参考编辑仍使用 JSON，未新增可视化编辑器。

## 接口

读取保持 `{ "data": ... }`。错误返回 `{ "error": { "code": ..., "message": ... } }`，不回显原始校验输入。`GET /openapi.json` 提供路由/请求 schema。除查询记录外，新增：

| GET | 内容 |
| --- | --- |
| `/api/projects/{id}/context` | 只读管理总览 |
| `/api/projects/{id}/refresh-preview` | 只读版本变化 |
| `/api/cognition/{id}/preview` | 完整冻结包和批准哈希 |
| `/api/cognition/{id}/context-requests` | 精确扩展请求完整正文及身份 |
| `/api/cognition/{id}/calls`、`/dossier` | 请求收据、六部分档案 |
| `/api/tasks/{id}/preview` | 模型 target、完整 payload、批准哈希 |
| `/api/knowledge/{id}` | 知识记录 |
| `/api/projects/{id}/surveys`、`/api/surveys/{id}` | 摸底历史与详情 |
| `/api/projects/{id}/evaluations`、`/resolutions` | 当前项目评分/结案历史 |
| `/api/evaluations/{id}`、`/preview`、`/report` | 评分详情、完整包与汇总 |
| `/api/resolutions/{id}`、`/preview` | 结案详情与完整对照 |
| `/api/operations`、`/api/operations/{id}` | 最近 100 条轻量回执、完整操作结果 |

写入统一 `POST /api/operations`，返回 HTTP 202：

```json
{
  "request_id": "由客户端生成的完整 UUID",
  "action": "task.create",
  "parameters": {"project_id": "项目完整 UUID", "question": "这个项目如何启动？"}
}
```

`request_id` 是持久化幂等身份，不是访问 token。相同身份/内容返回原回执，不同内容拒绝。程序仅调度下列明确操作，没有任意 shell 接口。

| action | parameters |
| --- | --- |
| `project.import` | `path` |
| `project.init` | `path/name/goal`，可选 `author_name/author_email` |
| `task.create` | `project_id/question`；可选 `files/keywords/retrieval(keyword或lexical)/strategy(single或multi)/purpose(question或repair_proposal)/new_files/ranges` |
| `task.decide`、`repair.decide`、`verification.decide`、`release.decide`、`cognition.decide` | `id/approved`，批准还需 `sha256` |
| `cognition.create`、`cognition.refresh`、`survey.build`、`index.build` | `project_id` |
| `cognition.start`、`cognition.recover` | `id` |
| `cognition.context-decide` | `id/actor/sequence/approved/sha256`，绑定具体扩展请求 |
| `cognition.followup`、`cognition.invalidate` | `id/finding_id`；失效还需 `reason` |
| `verification.create` | 候选修改 `id/image`，可选 `timeout` 1–120 秒 |
| `release.promote` | 验证记录 `id` |
| `release.rollback`、`release.recover`、`release.capture` | 已有源码操作 `id` |
| `release.export`、`release.verify-export` | `project_id/destination`；核验使用 `path` |
| `knowledge.add` | `project_id/title/body`，可选 `kind(decision/constraint/maintenance)/scope(snapshot/project)` |
| `knowledge.confirm`、`knowledge.invalidate` | `id`；失效还需 `reason` |
| `evaluation.create` | 认知 `id/criteria`，可选 `max_calls` 1–128 |
| `resolution.create` | 定向补查 `id` |
| `evaluation.decide`、`resolution.decide` | `id/approved/sha256`，精确超限还需 `allow_extended_requests=true` |
| `evaluation.run/recover`、`resolution.run/recover` | `id`；run 仅执行已批准排队项，recover 只恢复原服务允许的已知状态 |
| `resolution.adopt` | `id/sha256`，绑定完整 `proposal_sha256` |

`criteria` 契约沿用 [独立评分](COGNITION_EVALUATION.md)。JSON 字段/版本/证据校验继续由原服务执行；Web 不生成参考答案、补造评分或放宽来源。

## 回执、恢复和本地边界

操作回执写入启动时指定状态目录的 `web-operations.sqlite3`，业务数据仍由原项目资源路由保存。提交先落账，独立进程加文件锁并原子领取一次，再调用原服务。多个 HTTP 重放/多个进程不能让同一请求重复执行。Web 服务重启后不会自动重放 `running` 或 `unknown` 项。

`queued` 表示尚未领取；若启动进程失败，可以 `POST /api/operations/{id}/start` 启动同一排队项。多个启动进程仍只有一个能领取。`running` 的进程锁消失后只读返回 `unknown`，保留原回执，不把它回退为队列。此时可能已经有业务副作用，须在原任务/候选/验证/源码记录中核对。文件系统/Git/模型与 SQLite 不能共享事务，回执不会声称跨系统 exactly-once。

浏览器在发送前仅保存待核对请求 UUID，输入正文和凭据不进入浏览器持久存储。HTTP 响应丢失时，同一页面可用原身份/原内容重交核对；刷新页面后只 GET 原回执，不自动再发送。确认未落账才允许重新准备。操作历史列表不载入资料包/正文；完整内容需按 ID 读取。

无账号体系，默认单本机操作者。服务固定绑定 `127.0.0.1`；Host 限定本地，API 对其他 Origin 拒绝读写，写入还拒绝缺失或 `null` Origin。允许同源控制台及固定的 5173 开发来源；CORS 仅 GET/POST、Content-Type，不用 cookie。响应 no-store/nosniff，操作体最多 1 MiB。该来源校验服务于浏览器跨站隔离，不用于区分本机进程或多人权限。

当前历史仍为最近 100 项；Web 问答默认单 Agent，可选检索/协作策略及行段修改参数在 API 中可用，页面先保留常用问题/明确文件/新路径输入。尚未新增运行部署、远程克隆或登录注册。

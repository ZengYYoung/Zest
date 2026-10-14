# Zest 本地维护闭环验收

本页按实际能力使用 Zest：项目接入 → 证据问答 → 修复/开发建议 → 独立批准 → 候选版本 → 容器验证 → 管理源码选择 → 导出 → 历史知识。

当前入口为本地 CLI，可信操作者单人使用。源码选择不代表服务发布、流量切换或数据库回退；这些需要另行设计运行部署适配。

## 1. 准备运行环境

```bash
uv sync --extra ai --extra mcp
uv run --extra ai --extra mcp zest doctor
uv run --extra ai --extra mcp zest doctor --docker --image <已安装镜像名或完整ID>
```

doctor 仅读取版本、依赖和配置状态，绝不调用模型、自动下载镜像或运行项目代码；输出不含模型密钥。未检查 Docker 时 available 为 null，不代表失败或成功。配置有效不证明实际模型请求成功，镜像可读不证明项目测试通过。

模型配置放在本地 `.env`，参考 `.env.example`。所有终端命令在 Zest 根目录执行；固定全局状态目录时，将 `--state-dir <目录>` 放在一级命令前，后续每一步使用同一个目录。状态目录位于被管理仓库内时必须被 Git 忽略。

## 2. 项目接入

已有项目：

```bash
zest project import /absolute/path/to/repository
zest project overview <project-id>
zest project read <project-id> README.md --end-line 30
zest index build <project-id>
```

新项目：

```bash
zest project init /absolute/path/to/new-directory --name "加法项目" --goal "实现加法及对应测试"
zest knowledge show <返回的goal-note-id>
zest knowledge confirm <goal-note-id>
```

init 只创建 README、忽略规则和 Git 初始提交。父目录须存在、目标全新；需要 Git 作者配置，或同时提供 `--author-name` 与 `--author-email`。不会创建远程 GitHub 仓库或推送。

## 3. 调查与建议的数据批准

问答：

```bash
zest task create <project-id> "项目如何启动？" --keyword main --strategy multi
zest task show <task-id>
```

修复小文件：

```bash
zest repair suggest <project-id> "加法计算结果错误" --file app.py
```

维护大文件的明确行段：

```bash
zest repair suggest <project-id> "修改这段逻辑" --range app.py:251:270
```

开发新源码：

```bash
zest repair suggest <project-id> "创建 add 函数和 unittest，验证 2+3=5" --new-file app.py --new-file tests/test_add.py
```

上述任务均停在 awaiting_approval，没有模型调用。查看 show 的目标服务、模型、用途、完整待发送来源、版本与预算，确认实际数据范围后：

```bash
zest task approve <task-id> --payload-sha256 <show中的哈希>
# 不同意则使用 zest task reject <task-id>
```

这一步允许把预览数据发送给配置的模型。任务密钥不进入 checkpoint；拒绝或哈希错误不调用模型。提议 Agent 只生成建议，不能自动修改项目；证据不足返回缺口，越界或无效输出失败，不产生可执行方案。

完整文件/行段/新路径合计最多 5 项；每个行段最多 200 行，完整变化文件 64 KiB，上下文 24000 字符。局部来源只允许精确一次锚点替换，未发送的字节由程序保留。模型建议的语义正确性仍需审查。

## 4. 独立批准具体修改

```bash
zest repair show <task结果中的repair-id>
zest repair approve <repair-id> --plan-sha256 <repair show中的哈希>
```

核对完整新正文、diff、路径、原哈希、基础版本和操作类型。生成的 candidate_commit 使用私有 Git index，不 checkout 或改写原工作区、main、HEAD。create 不覆盖既有 Git 路径，edit 不能匹配片段外位置；未提交工作或基础变化时停止。

也可以不用模型，手工创建 JSON 方案后 `repair create --plan <文件>`，仍经同样的独立批准规则。

## 5. 容器验证及管理源码选择

```bash
zest verification create <repair-id> --image <已安装且信任的Python测试镜像> --timeout 30
zest verification show <verification-id>
zest verification approve <verification-id> --plan-sha256 <show中的哈希>
```

测试镜像应预装所需依赖；不自动 pull 或联网装依赖。当前命令固定为 `python -m unittest discover -s tests -q`，只在本地受限 Docker 中执行源码。Docker socket 默认 `~/.docker/run/docker.sock`，可指定其他本地 Unix socket，不接受远程地址。

通常基础/候选均运行。纯创建且没有匹配的基线测试时，预览明确 baseline_strategy=not_applicable_initial_creation；基线没有运行，不能宣称基线通过或原故障复现。候选仍必须有实际测试，退出 0、非 OOM、输出完整且清理确认才通过。

读取 passed 后还需要单独选择管理源码版本：

```bash
zest release promote <verification-id>
zest release show <operation-id>
zest release approve <operation-id> --plan-sha256 <show中的哈希>
zest release active <project-id>
```

正式入口只接受真实 Docker 证据，离线夹具不合格。之后概览、问答、RAG、MCP 和新维护操作选择同一 commit；Git 工作区仍单独报告。新版本索引显式重建，不冒用旧索引。

## 6. 导出、回退与维护知识

```bash
zest release export <project-id> /absolute/path/outside-project/new-export
zest release verify-export /absolute/path/outside-project/new-export
zest release capture <已完成promote的operation-id>
zest knowledge show <capture返回的note-id>
zest knowledge confirm <note-id>
```

导出包含 source/ 与来源/文件清单，直接查看和按项目说明使用文件；Zest 不自动启动它。核对清单不是新的测试或身份签名。维护案例是草稿，确认后进入跨版本检索；历史源码与测试事实、未经确认的方案说明分别标记，不推断当前版本已修复。

```bash
zest release rollback <已完成的operation-id>
zest release show <rollback-operation-id>
zest release approve <rollback-operation-id> --plan-sha256 <show中的哈希>
```

回到原操作 previous_commit，包括可能有故障的初始基线；恢复的是管理源码引用，不覆盖工作区未提交内容，也不回退运行服务或数据。

## 7. 中断和验证边界

- 等待模型数据批准可跨进程恢复；同任务不能重复批准。
- 执行模型时崩溃可能留下 pending_execution，不自动重放计费未知的请求。
- Git 引用事务成功但日志写入中断时，使用 `release recover <operation-id>` 核对历史引用，不重放切换。
- 验证/导出部分失败保留私有 artifacts，检查具体记录；不会删除用户工作或自动发布。
- 无真实模型修复准确率、负载、多人授权、生产部署或零停机验证声明。

完整回归：

```bash
uv run --extra ai --extra mcp --extra web python -m unittest discover -s tests -q
ZEST_RUN_DOCKER_TESTS=1 ZEST_TEST_IMAGE=<本地完整镜像ID> uv run --extra ai --extra mcp --extra web python -m unittest discover -s tests -q
```

普通套件跳过 Docker 实测，模型结果来自明确的离线夹具/MockTransport。显式 opt-in 的 Docker 案例是真实临时项目，测试容器清理完成；不能用它们泛化所有项目和生产环境的效果。

## 8. 只读管理台

在本地 .env 设置独立 ZEST_WEB_API_TOKEN 后，以 `uv run --extra ai --extra mcp --extra web zest web serve --port 8000` 启动 API。另开终端进入 frontend，运行 `npm ci` 和 `npm run dev`，打开 http://127.0.0.1:5173，填写端口与 Web token；不能填写模型密钥。

检查概览的管理源码与工作区 HEAD 是否分别显示、README 原文是否携带实际 commit；调查列表仅显示当前项目的最近记录，详情可以审阅完整来源和批准哈希。候选、知识、版本状态与 CLI 记录保持一致；当前所有批准和执行仍通过 CLI。

通过 CLI 新增登记或状态后刷新，切换项目/页签时旧详情清空；断开或刷新页面后重新填写令牌。浏览器没有模型或修改入口。`npm test` 检查本地请求边界，`npm run build` 检查 TypeScript 和打包；依赖安装需要网络，实际使用/API 请求仅本机。

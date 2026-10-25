# 共享项目文档接口

本页记录 M112 新增接口。其他既有接口可从运行服务的 `/openapi.json` 查询。

## CLI

```bash
# 接入一个本地 Git 仓库；原登记可复用
uv run zest project import /absolute/path/to/project

# 显式建立共享文档，迁入选定提交的原资料；返回待检查的文件和冲突
uv run zest project documents <project-id> --init

# 只读列出当前选定 Git 版本中的共享文档
uv run zest project documents <project-id>

# 读取对应版本的原文；未提交内容不会混入
uv run zest project read <project-id> .zest/docs/index.md
```

`--state-dir` 仍放在 `project` 之前，表示本地登记入口。
未提交的共享文档可以在磁盘上检查；文档目录查询和模型来源使用已提交版本。

## Web

`GET /api/projects/{project_id}/documents` 返回 `data`，包含：

- `project_id`、`commit`：本地项目身份和选定的 Git 版本。
- `working_tree_changes_included=false`：未提交改动不计入本次目录。
- `documents`：允许的共享文档路径、commit、blob、来源类别。
- `warnings`：脏工作区、目录容量等提示；不能把截断目录当作完整结果。

该 GET 不初始化文档、不调用模型。

迁入通过既有操作队列：`POST /api/operations`。下面是结构示例，需将 `project_id`
替换为已接入项目的真实 ID，每次新操作使用新的 `request_id`：

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "action": "project.documents.init",
  "parameters": {"project_id": "00000000-0000-4000-8000-000000000002"}
}
```

本地同源要求和操作回执沿用既有 Web API。按 `GET /api/operations/{id}` 查询结果；
同一请求身份重用既有执行记录，未知状态不自动重放。

初始化结果列出 `created`、`updated`、`conflicts`、`skipped`、`source_commit` 和 `warnings`。
`requires_review=true`、`committed=false`、`source_documents_deleted=false` 表明需要检查，
没有自动提交，也没有删除原文档。来源变化时返回冲突，不覆盖已维护的团队文档。

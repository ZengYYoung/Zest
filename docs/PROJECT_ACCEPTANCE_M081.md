# M081：代表性项目首次调查在规划接收处停止

日期：2026-10-16。执行实现固定 `9b1afcdbda40dc3e1040d00918f0ca8b18d42ba5`，样本、完整范围和实用性准则见 [M080](PROJECT_ACCEPTANCE_M080.md)。

## 实际结果

用户批准冻结清单 `8f0e4115bbaeaa62736e143b9212cb75177a3778fcad6a79014491a4dbdbb303` 后，仅执行已批准的首次调查。run `4fc22bc8-d6b7-45d6-8067-f4cd7885a2ec` 最终 failed / cognition_response_invalid。

- 2次物理请求、1条逻辑请求，已知格式补试一次用完；没有未知或待确认请求。
- 两次均收到完整工具调用 JSON，finish_reason=tool_calls，没有输出截断。
- 第一次规划覆盖应用装配、领域模型、路由流程和依赖/测试，但第四个任务多出不参与执行的 `accepectance_placeholder`，被 extra_forbidden 拒绝。
- 第二次规划的第三、第四个任务缺少必需 sections，被拒绝；没有擅自补填。
- 0个调查任务、0条发现，没有可用的项目认知档案。问答及独立评分均未执行，因此尚不能判断整个项目认知和问答是否好用。

模型 deepseek-flash，官方 beta chat/completions 接口。输入7632、输出3903、合计11535 token，cache_read4992；含保存核查耗时约22.140秒。

## 根因与后续范围

本次阻塞发生在规划响应接收，尚未进入业务调查，不能据此判断项目理解质量。只在独立诊断副本删除第一次响应的额外字段后，具名 ProjectPlanResponse 和业务 validate_plan 均通过；副本没有采用、发布或写回原 run。

用户同意下一模块仅容忍普通项目规划中不参与执行的额外字段。必需字段缺失、类型、来源、任务依赖、六部分覆盖、权限和预算仍检查；保留原始安全收据。修复后先复用保存响应离线验证，再为继续整体验收冻结新具体包。旧失败、两次请求和原始响应保持原样，未用的预算不能自动成为新调用许可。

## 证据与保留

原始材料：`work/project-acceptance/realworld-m080-20261016/`。review/investigation_result.json 保存完整账本与安全收据；investigation_outcome.json、result_verification.json 记录计数、错误、用量及核查；planning_diagnostic_copy.local.json 明确仅为诊断副本。导出的 dossier 为空，不能作为可用档案成果。

22份冻结文件不变，45个原生Git文件及HEAD/index不变，478份历史材料字节及SHA256一致。另保存本轮项目管理状态/目录数据库快照和最终文件清单，不覆盖原始输入、响应、数据库或旧清单。无样本程序/测试执行、LangSmith、问答/评分调用、Web/API变更、推送、部署或新会话。

本模块仅记录真实失败结果，检查收据/具名Schema/业务诊断、请求身份/用量/计数、原文与历史哈希、文档链接和 diff；不重复完整回归。

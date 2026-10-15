# 模型结构化输出契约

日期：2026-10-14。M037 将所有固定格式模型输出统一到 LangChain 的 `with_structured_output`；真实项目认知质量与供应商 strict 接口效果仍需新的批准验收。

## 类型与调用方式

[model_outputs.py](../zest/model_outputs.py) 集中定义 Pydantic v2 模型。每个模型有说明，每个字段有 `Field(description=...)`；使用枚举、明确的嵌套对象和互斥变体。所有字段显式返回，禁止额外字段，并进行严格本地类型校验。

模型返回的是外部不可信数据，需要运行时校验并自动生成可审阅 schema，因此采用 Pydantic。内部 LangGraph 状态继续使用 TypedDict，模型配置继续使用 dataclass；它们并非模型输出，无需为统一格式替换。

| 用途 | 类型 | 覆盖路径 |
| --- | --- | --- |
| 证据问答 | QAResponse | 单 Agent、文档/代码专业 Agent、协调回答 |
| 修改建议 | RepairProposalResponse | 完整文件、新文件及锚点替换建议 |
| 初步认知与计划 | ProjectPlanResponse | 主 Agent 规划 |
| 自主调查 | ProjectResearchResponse | 工具动作与最终报告 |
| 保留报告轮 | ProjectReportResponse | 末轮/上下文预警只允许交付报告 |
| 原文核对 | ProjectReviewResponse | 每条发现的支持性判断 |

[models.py](../zest/models.py) 是唯一真实模型适配入口：

```python
model.with_structured_output(
    model_for(purpose),
    method="function_calling",
    strict=True,
    include_raw=True,
)
```

不保留 JSON Mode 或纯提示词格式控制的替代路径，不自动修补后重发。系统指令仍说明业务约束，Pydantic 负责数据形状；字段描述有助于生成，不保证事实正确。原文身份、来源权限、引用支持性、任务依赖、数量/长度预算和修改范围仍由原有业务校验器检查。

调查输出使用 `response` 包裹互斥变体：`kind=actions` 提供动作，`kind=report` 提供完整报告。适配器映射回已有内部 actions/report 协议。工具默认值显式表达；read 的 `end_line=0` 表示已有工具的默认读取窗口。这样避免含糊的可空字段，同时保持原工具权限和固定版本证据。

## 实际目标与批准

DeepSeek 的原生 strict 工具调用使用官方 `/beta` 接口。配置根地址 `https://api.deepseek.com` 时，在准备预览前就解析为 `https://api.deepseek.com/beta`；doctor 显示配置地址及实际目标。自定义地址保持原值，其 strict 兼容性需另行确认。[供应商 strict 契约](https://api-docs.deepseek.com/guides/tool_calls/)

完整输出 schema、用途、调用方式、strict/include_raw、schema 哈希和系统指令哈希进入批准内容：问答/建议的 execution_policy，以及认知包的 structured_outputs。调用前再核对实际目标和当前契约。旧预览缺少新契约或契约变化时拒绝执行，需要重新准备批准；已完成历史仍可只读查看，旧文件不被改写。

上述冻结身份检查适用于持久 task 和认知工作流。直接 `project ask --preview` 也展示问答 schema，但 `--allow-model` 仍是即时执行入口，会重新准备当次上下文，不能将独立的预览命令当成带哈希的持久批准；需要固定目标与资料身份时使用 task 入口。

schema 使用闭合对象、必填字段、enum 和 anyOf；导出展开引用，并将 const 表达为单值 enum。测试核对预览 schema 与 LangChain 实际发送的工具 schema，允许框架移除说明性 title 元数据，不允许业务属性变化。

## 响应记录与恢复

`include_raw=True` 同时保留解析结果与 SDK 原始消息；HTTP 响应钩子补充供应商原始工具参数，避免 SDK 解析后丢失不合法 JSON。记录 usage、请求模型名、供应商返回模型名和实际目标；费用及精确单请求耗时仍不推算。

已收到但无法解析、字段错误、类型错误或出现多个输出工具的响应记为已知无效，保留安全原始收据与稳定错误码；不回显可能带正文的解析异常。命中现有凭据规则或原始收据超过 256 KiB 时拒绝输出、舍弃原始正文，只保存安全状态。规则检测不是完整的敏感信息识别系统。

认知逐次请求的安全收据与 completed/invalid 分类在同一 SQLite 事务落库。响应落库后节点中断，显式恢复可复用同身份结果；已知 invalid、pending/uncertain 均不重发。事务提交前中断仍属于结果未知，保持原来的禁止重发边界。

问答和建议通过现有任务 checkpoint 保存结果或失败收据；多 Agent 问答额外保存各实际调用的 payload 身份与安全响应。它不是新增的逐次持久请求调度器：进程在 checkpoint 保存前退出时，仍按原 pending_execution 处理，不能从内存收据恢复或自动重试。

## 已验证与待完成

离线测试覆盖五类真实 LangChain/HTTP MockTransport schema、必填/额外/错误类型、原始参数、敏感输出、调用次数、批准变化与恢复事务。它们不证明 DeepSeek 在线 strict 的效果，也不替代项目理解质量评价。

M038 已补齐完整请求体计数、证据去重、保留报告轮和适度超限暂停批准，见 [认知上下文与单次扩展](COGNITION_CONTEXT.md)。新增的报告类型仍使用同一原生结构化输出路径；完整请求体和实际 URL 通过本地内存传输编译，发送前核对。

M039 的一次线上规划响应通过类型解析，但被额外来源数量限制拒绝；后续调查/报告/核对线上效果仍未知。M040 已修正这类数量限制、统一字段描述并扩大资料/报告容量，见 [容量与离线验证边界](COGNITION_CAPACITY.md)。契约变化需要新包和新的批准，不能沿用旧 run 的外发许可。

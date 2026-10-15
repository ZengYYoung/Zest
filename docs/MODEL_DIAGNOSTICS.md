# 单次结构化输出诊断与 LangSmith

M050 为真实模型验收增加显式、可选的追踪出口。它不改变模型请求、提示词、Pydantic 类型、接收规则或恢复授权；普通问答和后台调查默认不上传追踪。

M051 已完成一次具体批准下的模型请求、11 个追踪步骤上传与精确回读；本次格式/引用/资料分类通过，但不足以证明稳定性，见 [真实结果与修复方向](COGNITION_ACCEPTANCE_LANGSMITH.md)。

## 配置与调用

在本地 `.env` 配置 `LANGSMITH_API_KEY`；可选 `LANGSMITH_ENDPOINT`、`LANGSMITH_PROJECT`。默认接口为 `https://api.smith.langchain.com`，默认项目为 `zest-structured-diagnostics`。配置本身不启用追踪，不要提交 `.env` 或把密钥写入对话。

`zest/model_diagnostics.py` 的 `LocalModelTrace` 通过 LangChain 的 `RunCollectorCallbackHandler` 在内存收集真实 prompt、模型、解析器和 fallback 的父子关系与时间。把对象显式传给 `DeepSeekModel(..., diagnostics=trace)` 才会收集；同一对象不能调用两次。适配器仍在 `tracing_context(enabled=False)` 中执行，不继承全局云端 tracing 开关。

完成调用后先保存 `safe_model_receipt`，再用 `trace.export(request, receipt, secrets=...)` 生成并持久保存上传内容，最后单独调用 `publish_trace`。它通过官方 LangSmith SDK 同步导入已收集步骤，不再次调用模型。

## 上传内容与结果

- 根步骤包含固定 HTTP URL、完整请求体（包括项目原文、系统指令、工具 schema）与安全原始响应、结构化状态、用量和原生工具参数的 JSON 诊断；不包含请求头、模型配置对象或密钥。
- 子步骤保留真实 LangChain 名称、类型、父子关系和起止时间；正文集中在根步骤。子步骤不上传异常 repr、模型构造器序列化或运行环境。
- 原生 arguments 只进行 JSON 语法诊断，保存原字符串哈希、长度及零起始错误位置；不删除字符、修补 JSON 或生成新的有效报告。
- 即使 LangChain 吞下解析错误并返回 `parsed=None`，根步骤也按适配器结果标记 invalid，避免仪表盘把 HTTP 200 或链正常返回当作业务成功。内部 fallback 可能保留失败子步骤，需要同时看最终根状态。
- 导出及上传前都检查现有凭据模式及调用方传入的真实密钥；安全过滤已舍弃或过大的响应禁止导出。规则不是完整敏感信息识别系统，上传仍需批准具体资料和接收方。
- 上传采用显式 endpoint/project，不开放公共分享；遥测上传和读取可能涉及多条 HTTP 请求。模型请求仍为批准的一次、`max_retries=0`，不能把遥测重试或读取当作模型重跑授权。
- 上传失败保留本地响应/导出，以稳定错误码停止；不得重新执行模型来获取追踪。线上 trace ID/可见性需要独立核对，离线测试不证明云端接入成功。

该诊断只覆盖一次模型调用及其 LangChain 子步骤，不声称已经覆盖所有后台 worker/MCP 调查图。项目认知的语义质量、引用支持性、资料授权仍分别核对。

M052 的格式补试位于认知执行器，须在新审批包内占用总调用预算；本页的单次诊断入口及历史“一次具体外发”预算不变，不会因开启诊断自动增加模型调用。详见 [补试边界](COGNITION_RETRIES.md)。

## 验证

`tests/test_model_diagnostics.py` 覆盖真实 LangChain 本地步骤、请求逐字不变、无效 JSON 保留、单次调用保护、凭据和请求身份检查、上传失败不调用模型、真实 LangSmith SDK 的离线 HTTP 序列化。原有 transport/schema 测试继续检查实际 strict、工具名、目标和无重试。

官方参考：[LangChain 追踪](https://docs.langchain.com/langsmith/trace-with-langchain)、[输入输出与脱敏](https://docs.langchain.com/langsmith/mask-inputs-outputs)。

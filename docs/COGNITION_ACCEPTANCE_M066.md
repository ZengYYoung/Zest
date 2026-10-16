# M066：移除字符索引后的单次真实核对

日期：2026-10-16（Asia/Shanghai）。执行实现：`9ee6b0e1483ddde5985b3b3ec5fdf00c503bec48`。

**本次新协议处理验证通过，整体语义质量未评分。** 唯一一次真实请求返回四条判断，通过具名 Schema 和生产逐属性接收器，没有再因字符定位失败。模型将四条均判为支持，但响应仍有证据使用和支持边界疑点，不能把处理成功或四条 supported 当作四条正确。

## 固定输入与许可

- 公开样本：KaleoOreo/python-personal-finance-tracker，commit `3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09`。
- Probe：`e65ebac8-777e-48bb-abe1-9e56a98a5848`，与原调查分开保存。
- 输入原样复用 M064 首个失败核对批次：T1:F1、T1:F10、T1:F11、T1:F12，四份去重原文、40 条其他发现线索、37 条任务未知线索。线索只用于冲突检查，不作为支持证据，不提供评分参考答案。
- 只更换 M065 的核对指令与具名 Schema，不要求 start/end；通过 `model.with_structured_output` 调用 ProjectReviewResponse。
- 模型 `deepseek-flash`，接口 `https://api.deepseek.com/beta/chat/completions`，超时 60 秒，输出上限 4096 token。
- 完整 HTTP 正文 32,826 字符，SHA256 `6c539afcb4a4c8b74480e07c1162fb9d17b7c7c38e11c673e4c88e4b716d321e`。用户审阅具体冻结包后批准一次请求及其超默认值的 826 字符扩展。
- 最多一次物理请求、零格式补试；无效或结果未知均保存后停止。不恢复旧调查，不执行完整调查、独立评分或补查结案，不发布知识。

发送前自动审批审查曾因具体载荷与目的地授权证据不足拒绝创建进程，未产生模型请求。随后补充原文逐项匹配公开样本、冻结正文无 API 密钥、具体预览与用户批准记录的只读核查；同一发送动作通过复审后执行，没有改换路径绕过审查，也没有增加模型请求。

## 实际返回

| 项目 | 本次结果 |
| --- | --- |
| 物理请求 / 补试 | 1 / 0，许可已消费 |
| 响应 | 完整收到；reported_model=deepseek-flash；finish_reason=tool_calls |
| Schema / 生产接收器 | 均通过，四个 finding_id 完整 |
| 模型属性判断 | 共 10 项，均为 supported；四条 coverage_complete=true；未报告冲突 |
| 程序派生 supported | 4；只是接收规则的结果，不是独立正确性分数 |
| 供应商用量 | 输入 10,984，输出 1,526，总计 12,510 token；输入 cache_read=128 |
| 执行耗时 | 约 7.097 秒，含本地保存，不等于纯模型延迟 |
| 内容评分 / 知识发布 | 均未执行 |

该批输入的新核对响应没有字符索引，无须人工改字段即可处理。这验证了 M064 前置阻断在本批新协议请求上没有重现；不是原 M064 run 的恢复或成功改判，也没有给 M065 的评分、结案模型交互做真实验收。

## 响应中的待核验现象

以下是原响应和输入边界的对照记录，不是主协作 Agent 临时给出的语义分数。

1. **T1:F11 的证据范围。** 该发现获准支持证据只有 P3，对应 database.py 第 106—130 行。模型核对过滤与排序时，理由明写“P2 与 P3 衔接”，而结构化 evidence_ids 只填 P3。P3 包含 end_date、排序与还原，其他过滤条件位于前段。接收器能校验证据 ID 属于该发现，却不能从合法 ID 自动证明自然语言理由只依赖获准证据。此现象应交独立评分检查，不能因 Schema 通过而忽略。
2. **T1:F12 的清洗支持边界。** P4 止于测试中 `transaction = result.transactions[0]`。模型明确承认后续清洗断言不可见，仍认为“以该输入验证清洗”的陈述已被测试输入和意图支持，并将整条判为支持。原句既有具体测试内容，也有“测试证据印证清洗与行号追踪行为”的概括；是否完整支持该概括仍需独立审查，不能只凭测试名称或输入推定行为已验证。

与旧保存响应相比，这次模型不再保留清洗项的 insufficient_evidence。旧响应迁移夹具中的阻断测试通过，不能保证新模型每次都作出同样判断。部分 check 仍合并多个字段或属性，10 项只是本次返回数量，不能称为经去重的原子命题数。

未执行专职评分 Agent，不生成新的事实覆盖、核心流程覆盖、误收率或人工替代成绩。M058 的历史分数不变，M064 保持 failed 与全部 44 条 pending_review。后续若评估语义质量，应基于 M065 后的版本化 Skill 重新冻结独立评分包；M064 原未执行评分包不能直接沿用。

## 保存与验证

私有材料位于忽略目录 `work/cognition-acceptance/review-m065-20261016/`，不提交原文、请求、响应或数据库。

- `offline/`：明确标注的旧响应协议迁移夹具，零模型调用。
- `real-probe/input_spec.json`、`payload.json`、`instructions.md`、`contract.json`、`request.json`、`execution_request.md`、`freeze_manifest.json`：执行前冻结材料；预览中的“尚未批准”保留历史原文，批准另记。
- `real-probe/authorization.json`、`execution_attempt.json`：具体批准和禁止重复发送的独占预占标记。
- `real-probe/receipt.json`、`validated_response.json`、`outcome.json`、`execution.log`、`verification.json`：完整安全收据、生产接收结果和核查；不修补实际模型响应。
- 本模块结果提交后另存 `final_manifest.json`，覆盖新材料及报告版本，保留执行前冻结清单。

四份原文的 commit/blob/文件 SHA256 和物理行段再次匹配；冻结清单 8 份文件未变。M058 的 128 份、M064 的 101 份原始材料逐项核对字节数与 SHA256 均不变；旧 run 与调用完整记录相同，样本 HEAD 与工作区不变。

本模块只新增结果文档、当前状态链接与开发日志，检查证据、文档链接和 diff，不重复完整后端回归。M065 已完成后端 561 项、557 通过、4 Docker 跳过。无 LangSmith 上传、样本程序/测试执行、知识发布、推送、部署或新会话。

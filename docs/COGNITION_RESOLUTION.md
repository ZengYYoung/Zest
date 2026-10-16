# 补查结果审阅与问题结案

M063。针对 [M062 定向补查](COGNITION_FOLLOWUP.md) 的新结果，提出新旧结论关系和冲突处理建议，用户明确采纳后更新有效认知与后续行动。调查结束、模型生成建议、独立评分通过都不自动结案。

## 使用

```bash
# 已结束的定向补查；准备只读保存结果，不调用模型
zest resolution create <followup-run-id>
zest resolution preview <resolution-id>
# 审阅完整新旧对照包、原文、模型、指令、Schema、精确请求和预算后另行批准
zest resolution approve <resolution-id> --approval-sha256 <package-sha256> --no-run
zest resolution run <resolution-id>
zest resolution show <resolution-id> --format text
zest resolution preview <resolution-id>
# 核对完整逐项建议、冲突和 effects，再决定是否采纳；不调用模型
zest resolution adopt <resolution-id> --proposal-sha256 <proposal-sha256>
zest project context <project-id> --format text
```

`approve` 未加 `--no-run` 时执行已批准的对照请求。`reject` 拒绝尚未批准的请求；生成后的建议可以保持不采纳。`recover` 仅在显式操作后复用已保存成功响应；未知或无效请求不重发。无 Web/交互式终端写入入口，无自动 worker 调度。

## 冻结范围和权限

只接受已结束的 `completed/partial` 定向补查，版本必须与原发现、原冲突、新证据相同；准备与采纳时还须是项目当前选定 commit。普通调查和版本增量不冒充同版本结案。原问题已经有效结案时不重复准备。

独立包冻结原问题和明确关联的冲突链/未知项、补查完整发现/核对/任务结果与计划、双方批准包身份、模型目标和超时、对照指令、具名 Schema、真实 SDK 完整请求及哈希。保存原文重新与固定包的路径、blob、SHA256、物理行段和正文核对；旧核对意见只作为待核实线索。

模型输入包含每个旧断言、核对意见、明确冲突、已核对的新候选和其本次非空原文；新调查的未接收发现、任务状态、缺口、未知和资料需求也显式提供。完整任务状态保存在包快照，模型输入只去除工具记录和重复原文，不截断用于比较的证据。新引用使用本包 P 编号，候选身份与旧身份通过各自 run 区分。

首版精确预算为 **1 次请求、0 次格式补试、8192 输出 token**，发送前核对模型、Schema、指令及完整请求。默认 32000 字符；32000—48000 的精确请求必须预览后另加 `--allow-extended-requests` 明确同意，普通批准不能放行；超过 48000 停止，不静默丢失冲突或原文。完整本地包上限 16 MiB。所有许可独立于旧调查和评分，不包含修改项目、执行测试、发布或 LangSmith 上传。

## 逐项判断和结案门槛

`CognitionResolutionResponse` 通过 `model.with_structured_output` 绑定具名 `ResolutionJudgment / ResolutionCheck / ResolutionConflict`。

每个旧发现或定位未知必须恰好判断一次。M065 起，checks 通过 reference_id 关联完整旧结论，按对象、属性、条件和断言判断，不再返回字符位置；程序检查身份、结构和引用，语义拆分及完整性仍由模型判断。见 [职责与历史兼容](COGNITION_SEMANTIC_REVIEW.md)。

| 属性结果 | 含义 |
| --- | --- |
| confirmed | 原断言及限定条件完整成立，新候选和原文明确支持 |
| corrected | 新候选完整纠正错误或补足条件，同时保留其他正确属性；旧未知问题得到明确回答也用此值 |
| unresolved | 证据不足、只回答一部分或仍有矛盾 |

每个已处理属性必须关联明确回答它的新候选及候选实际引用的原文，不接受主题近似、旧 supported 或 run completed 作为依据。未知问句不能 confirmed。只有 observed、auto_reviewed 且通过 M059 整条接收条件的新候选能参与替代；不能由关系建议直接制造或发布新事实正文。

程序派生整条 outcome：全部已处理且 `coverage_complete=true` 才能确认或纠正，否则未解决。每条明确冲突必须单独说明两端和处理原因：指出哪方错误，或补齐不同成立条件。resolved 要求两端的属性判断完整、有新候选和证据；一端未解决不能宣称冲突消解。

每个 reference 单独计算 `closable`，要求其整条已处理、所有直接关联冲突 resolved，并且补查没有未完成任务、计划缺口、未知、待补读/包外资料需求、未接收发现或报告校验拒绝项。这是首版保守规则：即使模型说已回答，仍有本次未清工作也不关闭。`completed` 本身不充分，`partial` 本身也不直接决定单项语义结果。

冲突链不整体联动关闭。例如 A—B 已解释、B—C 仍未解决，可以关闭满足条件的 A，B/C 继续待处理；相关旧发现只有自身完整处理并明确列入可结案效果清单才会关闭。

## 采纳后的有效认知与历史

请求批准只允许生成建议。`adopt` 另行绑定完整建议哈希，包括逐项判断、冲突与效果清单；再次核对原问题、新结果、版本和重复处理情况后，追加一条带时间的采纳记录。相同采纳幂等；不同建议不能重复处理已有效关闭的相同旧断言。

- `cognition.show / dossier` 仍是原调查的历史快照，旧断言、状态、原核对、调用和批准记录不覆盖。
- 管理总览把已解决旧发现移入 `cognition.resolved_findings`，保留旧句、原状态、处理决定和新结论定位；不再混入当前 facts/pending 或 followup_actions。
- 已明确回答的关联未知保留原历史问题并标注 resolution，不顺带清除一般未知或计划缺口。
- 当前共享认知、问答、修改建议及新调查包跳过被处理的旧断言，使用新调查已有的核对成果；不是把旧记录伪装成人工 confirmed。执行前 `reviewed_finding` 复核也拒绝已替代的旧认知，要求重新准备，不能让旧批准继续消费旧错误。
- 不能结案的项继续留在后续行动中；采纳一份含未解决项的建议不会强制关闭它们。

采纳后每次有效查询仍对照冻结依赖。新证据或关联来源发生变化时，该处理决定变为依赖失效，问题重新进入待复核清单；此前被替代的旧错误不自动恢复为有效知识。仍符合 M062 原状态条件的问题可以重新准备补查；原始状态为 auto_reviewed 的关联旧结论仅提供处理决定检查入口，首版不绕过 M062 的来源条件。

状态读写位于项目 `.zest/state/resolutions.sqlite3`，独立 LangGraph/SQLite checkpoint 位于 `resolution-checkpoints/`。兼容历史来源原位读取，不搬迁原库；各库独立读快照，不声称跨库全局事务。项目级采纳锁避免本产品入口同时采纳相同问题；身份核对与当前视图阻止陈旧依赖继续生效。

## 验证边界

离线夹具验证完整纠正/确认、部分回答和语义覆盖不足、冲突两端/剩余冲突链、未知回答、有效知识与旧批准复核、身份和版本变化、依赖失效、重复采纳、项目资源/历史来源、预算和扩展、未知不重发、保存响应恢复，以及真实 LangChain 本地 MockTransport 的具名 strict Schema 和冻结请求一致性。

这些验证证明接收与状态机制，不证明模型能够稳定判断语义关系；真实模型质量、与人工判断一致性仍需完整新包预览和另行批准。本模块不运行 M058 旧脚本、不改历史评分，也不自动消费任何真实模型许可。

# M100：覆盖评分先定位候选表达，再比较原文

日期：2026-10-16。用户确认修复后实测。[M099](PROJECT_EVALUATION_M099.md)真实评分暴露两类问题：明有候选却误认为未指定目标，full/partial不给候选身份；把源码中的完整流程算成候选已经表达，且判定与理由矛盾。本模块只处理评分依据的关联和覆盖判断。

## 具体变化

内置[评分Skill](../zest/skills/cognition-evaluation/SKILL.md)明确区分：requirements是覆盖目标，candidates是供模型自主查找的交付内容，findings是另一类逐发现语义评分目标。候选不必预先指派给每项参考，也不必同时出现在findings中；requirements非空、findings为空时，照常完成coverage，仅judgments为空。

具名`CoverageJudgment`按“finding_ids → candidate_observation → evidence_ids → verdict/reason”形成输出。新增必填`candidate_observation`，先简述所选候选实际表达了什么及必要主干缺口，再以固定原文支持性与冻结要求比较。允许语义概括或多条共同覆盖，不要求逐字摘抄、字符位置或机械穷举源码细节。没有候选表达时说明查找结果，使用missing；full/partial仍须有真实候选与原文身份。

例如参考要求处理、保存与返回，候选只列入口、鉴权与调用，即使源码完整，也不能补算为产出已表达全部主干。Skill要求观察、判定、理由一致保留必要缺口；非关键措辞和细节继续按项目级实用性准则处理。

程序只检查观察字段存在、严格字符串、非空、符合既有文本1600字符保护，以及原有完整身份/引用/预算。不解析理由关键词、不按字符串匹配改分、不强制摘抄原句，也不自动判观察真实。判定和理由是否语义一致仍由评分Agent负责，新增字段不能保证不再幻觉，效果须真实实测。

## 兼容与历史

继续`model.with_structured_output(CognitionEvaluationResponse, strict=True, include_raw=True)`与M097的额外字段兼容，原始安全收据完整保留。评分的逐发现语义检查结构、另8种用途Schema保持，调查/核对/问答/结案行为不变。

Skill全文/哈希及新Schema进入批准包，旧包在新规则下调用前阻断。已保存结果按原冻结内容回读，不补候选观察、不迁移为新成绩；原D1/D2正式及无效意见都保留。后续完整实测使用统一新版本重评9批24项，不将旧分数混入新评价。

## 离线验证

新增5项控制测试，覆盖findings为空仍能使用candidates、full/partial/missing/reference_unsupported、必填/类型/文本保护、候选身份不能由观察代替、原文观察保留与不做关键词打分、Schema顺序与无字符索引。连同结构/评分/传输/观察回归，61项唯一用例通过；补充旧契约历史回读定向复核通过。

对M099两份保存响应只读检查，确认缺新字段的旧响应不会被偷偷补写或接收为新成绩；8种非评分Schema和评分judgments结构与修复前逐份相同。实际失败payload的61候选原样进入真实LangChain/HTTP MockTransport/生产接收器，以明确人工标注的协议响应演练候选关联和额外字段隔离。该夹具不代表模型已改进，也不产生新质量成绩。

原M099最终清单44份、M096的44份、M097的7份及全部更早材料、45原生文件、8问答任务与认知保持。离线证据在忽略目录`work/project-acceptance/coverage-candidates-m100-20261016/`。本模块0真实调用、无LangSmith、样本执行、补查、知识发布、Web/API、推送、部署或新会话；提交后另准备完整冻结实测包。

# 首轮认知验收样本：个人收支 CLI

- 日期：2026-10-14；模块：M035。
- 状态：用户已选定公开样本并授权本地准备，固定版本摸底、人工参考与完整预览已保存；尚未批准真实模型外发。
- 通用方法：[认知验收准备](COGNITION_ACCEPTANCE.md)；评分模板：[验收记录模板](templates/COGNITION_ACCEPTANCE_RECORD.md)。

## 样本身份与准备结果

样本为 [KaleoOreo/python-personal-finance-tracker](https://github.com/KaleoOreo/python-personal-finance-tracker)，固定 [完整 commit 3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09](https://github.com/KaleoOreo/python-personal-finance-tracker/tree/3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09)。它是公开的 Python 个人收支 CLI 项目，有文档、校验/导入/数据库/报表等模块及 pytest 测试源码，可检验静态跨模块流程与业务约束认知。

本地 checkout 为上述 commit 的 detached HEAD，工作区干净。没有安装样本依赖、导入其 Python 模块、执行其命令或测试。Zest 准备执行版本为 `b16af39`；真实执行时还需记录届时的 Zest 版本，文档提交不代表执行过模型。

| 准备项 | 本次结果 |
| --- | --- |
| 跟踪文件 / 总字节 | 21 / 33302 |
| 入包文件 / 文本字节 | 19 / 33028 |
| 采集遍历 | inventory_scan_finished=true，未触及文件/字节/时间预算 |
| 排除文件 | data/sample_transactions.csv、reports/.gitkeep，均为 unsupported_extension |
| 人工参考 | 10 条事实覆盖六部分，6 个静态导入流程环节，3 项待核实事项 |
| 原文引用核对 | 37 个范围，固定 commit、blob、完整文件 SHA-256 与源码行号 |
| 事实/流程证据入包 | 全部入包；人工答案本身不入包 |
| 冷启动共享认知 | shared_knowledge 为空，状态目录全新 |
| 调查状态 | awaiting_approval；0 子任务、0 请求账本、0 发现；worker 未启动 |
| 模型/测试结果 | 未执行，质量、费用及连通性未知 |

CSV 原文未被当前文件读取规则纳入资料包，因此本次不拿示例数值作为必答事实；原始样本保持不变，也不为本次验收扩大读取类型。包内测试中出现的示例文本仍按源码正文保留。涉及真实数据库数据的结论需要其他证据，示例数据不能证明运行状态。

这是小型单语言公开项目的第一基线，不能据此评价复杂架构、多语言、增量更新或生产维护能力。没有人为注入文档矛盾，也不声称涵盖该专项。

## 本地材料与隔离

材料保存于 Git 忽略目录 `work/cognition-acceptance/finance-20261014/`，源码、状态与人工评价分别置于 `source/`、`state/`、`review/`。顶层与状态/评价目录权限为 0700，评价文件和数据库为 0600。该目录不作为 Zest 开发源码提交。

`review/` 中包含：

- `record.md`：当前身份、准备结果和未批准状态。
- `reference.json`、`reference.md`：人工参考及逐项接受标准，保留原文、行号和完整身份。
- `source_manifest.json`：全部跟踪文件及入包情况。
- `survey.json`、`survey.txt`：实际摸底记录。
- `preview.json`：实际完整包、目标、指令、POLICY、批准哈希及空请求账本。
- `readiness.json`、`preparation_evidence.json`、`prepared_ids.json`：只读就绪状态、零调用/零任务证据与原记录 ID。
- `proposed_rules.json`：待共同确认的门槛提案。
- `freeze_manifest.json`：上述 12 个文件的 SHA-256，人工参考先于模型调用冻结；规则提案被冻结不等于已批准。

资料包按固定样本 Git 对象采集，未复制评价目录或人工答案；后续也不得将参考答案作为介绍、已确认知识或任务补充发送给被验收 Agent。对参考答案的纠正应另存修订记录，不覆盖已冻结文件。

## 下一项共同决定：评分门槛

以下只是提案，尚未成为通过规则或外发授权：

- 10 条预先冻结事实中，至少 8 条正确且有完整证据支持；三个预先标注的 critical 事实全部正确覆盖。
- 6 个核心流程环节中，至少 5 个正确且有证据支持。
- 自动核对没有重要事实错误或缺乏完整支持的关键结论；新增关键发现同样逐条检查，不能只检查参考清单。
- 不将静态资料无法证明的部署、实际数据库状态或历史选型理由写成已核对事实；“未涉及”单独统计，不强迫无提示逐项回答预设未知。
- 工程授权边界通过；零 observed 不算质量通过。可信率、误收率、六部分覆盖与未知保留仍完整报告，不用一个总分代替这些结果。

先共同确认门槛，再确认预览中实际模型目标和现有固定预算。预算仍是 32 请求、初始 4/总计 6 任务、最多 2 并行、每任务 4 模型轮次/16 工具动作、1800 秒新请求前检查、响应 max_tokens=4096；没有 CLI 调低参数，也没有费用或硬中止保证。

最后需在开发会话明确批准**这个 run 的完整资料包、具体目标、指令和预算**，才能使用产品的带哈希 approve。当前没有 approve、worker、recover 或真实模型请求；此文档与已批准的本地准备不能替代外发许可。

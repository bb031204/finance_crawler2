# 股票实体、行业与影响范围修改审查

> 此文记录修复前快照。2026-11-11已修复文中代码格式、局部配对、未选中内容回流及正文猜测代码漏检；旧属性纠正与验证结果见[当前实施状态](IMPLEMENTATION_STATUS.md)和[映射修复报告](logs/mapping_repair.json)。历史证券身份及StockMixer关联的边界仍保留。

审查日期：2026-11-11（Asia/Taipei）。依据financial-data-project2与stockmixer-llm技能，只读检查当前采集实现、全部正式JSON、迁移备份及审计，并核对目标StockMixer的读取接口和股票映射。没有修改采集逻辑、正式数据、配置或模型代码，没有启动采集、联网搜索、下载、真实Qwen推理或训练。

## 结论

新增10字段格式、保守时间标记、备份和副本一致性已生效，但实体语义校验仍有四处需要修复。不能把“219项已有测试通过”和“迁移哈希匹配”理解为全部股票关联正确。以下四项均有可复现证据，其中第一项已存在于正式数据，另外三项用离线合成反例验证，不声称已经污染全部历史记录。

## 1. [P1] 公司全名被当作股票代码迁入正式数据

位置：[record_quality.py](record_quality.py:51)、[analysis_metadata.py](analysis_metadata.py:96)。

`validate_securities`只要求ticker字符串出现在quote里，没有验证它是否为证券代码。旧审计的哈希匹配仅证明它与旧记录对应，不能证明其中每个属性正确；迁移再次用该校验器，保留了错误代码。

实际记录[data/GLOBAL/2021_06_17_Mining_Mining_bloomberg.json](data/GLOBAL/2021_06_17_Mining_Mining_bloomberg.json)中：

```json
{"名称":"Zijin Mining Group Co.","股票代码":"Zijin Mining Group Co.","交易所":null}
{"名称":"Serbia Zijin Copper","股票代码":"Serbia Zijin Copper","交易所":null}
```

这是一条唯一记录的两个错误属性，同时存在CN/US/GLOBAL三个副本中。新Qwen输出也复用同一校验器，存在同类风险。下游按代码连接时会失败；若再次交LLM识别，可能被误认为已验证证券标识。

建议：对证券代码增加适用市场的格式及明确代码声明依据检查，不能只判断字符串出现。恢复旧审计时执行相同规则；不能确认时保留名称、ticker置null。修复正式记录前仍须备份、同步副本、重算审计并更新该版特征可用时间。

## 2. [P1] 未使用紧邻括号声明时，跨公司配对仍会通过

位置：[record_quality.py](record_quality.py:53)。

仅在名称后紧邻括号声明存在时核对ticker归属；否则只检查名称和代码是否同在quote。如下反例被`grounded_securities`接受：

```text
Apple Inc. reported quarterly earnings. NVIDIA Corp. (NASDAQ: NVDA) announced new chips.
```

模型实体为`name="Apple Inc.", ticker="NVDA", exchange="NASDAQ"`，quote使用上述整句。由于Apple后面没有证券声明，配对限制未触发，错误关系直接保留。这会将公司消息分配到另一只股票，影响文本与股票轴的对应关系。

建议：名称与代码的配对必须有对应的局部证券声明、可核验表格行或明确关系证据；“同在一段”不足以确认。不能证明时分开保留名称与有依据的代码，不把它们合成同一实体。增加没有括号、跨句、多公司表格的反例。

## 3. [P1] 全文代码兜底会把未选中内容重新写回摘要

位置：[record_quality.py](record_quality.py:121)、[record_quality.py](record_quality.py:137)。

`preserve_securities`对整个`page.text`执行`retain_explicit_tickers`，随后把结果追加到关键词和正文，没有限定为Qwen选择的主文章内容或有效片段。

离线反例的有效摘要只讨论Example Bank，输入文本另有一段`Related content: Example Motors (NASDAQ: TSLA)...`。即使源笔记和最终笔记的securities均为空，程序仍输出TSLA实体，并追加`Securities explicitly mentioned in the source include TSLA.`。若后段是推荐新闻或论坛后续内容，还可能错误地沿用早期主文章的发布日期。

建议：将兜底限定于已确认属于主文章、且被选中的有用内容；保留片段位置或内部证据范围，不能在筛选后重新扫描所有页面文字。对普通网站推荐模块及论坛后续内容增加验证，不能仅依靠PR Newswire的专用清理规则。

## 4. [P2] 猜测代码只在关键词检查，正文仍可能泄漏

位置：[qwen_agent.py](qwen_agent.py:386)、[qwen_agent.py](qwen_agent.py:400)。

新增校验仅遍历keywords。原文只有`Apple Inc. reported quarterly earnings and revenue.`时，合成模型输出正文`Apple Inc. (AAPL) reported...`，keywords仅含Apple Inc.和earnings。即使模型实体中的AAPL被清空，正文中的AAPL仍然通过并被保留。

下游语言模型主要阅读主要内容，因此正式实体属性为null不代表猜测代码已从输入中移除。现有测试仅覆盖“错误代码同时位于关键词与正文”的情况。

建议：对模型声称的所有证券代码统一核验，同时核对正文中证券代码的语境；不要只遍历关键词。未通过的证券属性不能继续作为正文事实。增加“仅正文出现”“只有实体字段出现”等组合反例，避免误删合法金融缩写。

## 已通过的完整性检查

| 检查项 | 当前结果 |
| --- | --- |
| 正式唯一记录 / 物理JSON | 354 / 404 |
| 规范归属 | CN 27、US 302、GLOBAL 25；GLOBAL有三份视图 |
| 全部记录为10字段 | 通过 |
| 相同source_id的副本内容一致 | 通过 |
| 全部正式记录与内部审计哈希对应 | 通过 |
| 原六内容字段、原路径、已知发现/采集时间、来源精度保留 | 404份通过 |
| 迁移备份文件及清单核验 | 713份通过 |
| 已有离线回归测试 | 219项通过 |
| 股票实体非空 / 为空 | 21 / 333条 |
| 至少一个非空代码属性 | 16条，包含上文错误值，不代表16条均已语义核验 |
| 影响范围非空 | 21条 |
| 行业依据 | 354条均为legacy_metadata |

354条旧记录经过的是元数据恢复，不是对全部原文重新运行新提示词。[]表示未知或未恢复，不能当作“确认没有实体/影响”的负样本。legacy_metadata也不能直接当成已核验的历史行业标签。需要更完整的语义时，应另行重提炼可访问的原始来源；本轮没有启动该工作。

备份：[archive/record_schema_20261111_015308_814377.zip](archive/record_schema_20261111_015308_814377.zip)。完整性及反例机器报告：[logs/analysis_fields_review.json](logs/analysis_fields_review.json)。回归日志：[logs/analysis_fields_review_tests.txt](logs/analysis_fields_review_tests.txt)。可复现检查脚本：[.runtime/review_analysis_metadata_20261111.py](.runtime/review_analysis_metadata_20261111.py)。该脚本只读正式数据，在隔离临时目录使用替代模型函数，真实模型调用为0。

## 与StockMixer的实际衔接范围

只读核对目标`D:/finance/model/StockMixer/StockMixer`：当前根配置为NASDAQ、1026只、lookback_length=16、steps=1。`src/train.py`读取数值数组，`src/model.py:StockMixer.forward(inputs)`只接收数值输入；当前尚无文章JSON读取、文本编码、关联表或融合接口。这里是数据侧字段审查，不把尚未实现的文本模块算成本次修改失败，也没有对原始论文结果作性能比较。

已读取`docs/stock_identity_mapping.csv`和对应说明。后续应先用经过核验的股票代码/历史名称建立文章到证券的外部关联，再按dataset、array_index和交易日排列文本向量；不能把文章source_id、公司名称或行业名直接当股票数组行号。SP500是数据集名，不是交易所。行业主题也不等于带历史有效期的发行人行业分类。

以下按US可读视图、source_id去重，仅描述候选量；未把代码字面匹配当作证券身份已核实，也未完成预测截止时间对齐：

| 数据集 | 数值日期范围 | 来源日期在范围内的文章 | 至少一个代码字面命中映射 | 特征可用时间不晚于数据集末日 |
| --- | --- | ---: | ---: | ---: |
| NASDAQ | 2013-01-02至2017-12-08 | 134 | 3 | 0 |
| NYSE | 2013-01-02至2017-12-08 | 134 | 4 | 0 |
| SP500训练截取后 | 2016-01-04至2022-05-25 | 218 | 7 | 0 |

所有现有特征可用时间均为2026年迁移时刻，因此按严格的当时可用性口径，不能把它们直接作为2013—2022年已知的模型输入。保留该时间是正确的保守处理，不能为了回测把它倒填成发布日期。若研究允许对历史新闻进行事后文本编码，应明确这是回溯构造的实验假设，并另行核对历史网页版本、来源发布时间、模型知识时间及股票身份映射；不能据此声称已满足严格时点可用性。

建议先修复上述四项并增加失败反例，再对已受影响记录做有备份的纠正；之后单独执行历史来源重提炼与股票/交易日关联，最后才接文本编码和融合模块。本轮仅提交审查结果，未实施这些修复。

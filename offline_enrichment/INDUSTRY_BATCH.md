# 旧新闻行业字典补处理

本次处理 `D:\finance\model\StockMixer+llm\data_llm` 的 1,904 篇唯一新闻，不回写原始 JSON，也不重新抓取或改写摘要。原有 1,423 篇冻结数据之外新增 481 篇，其中 1 篇是中文摘要，保留原文并排除出英文 FinBERT 输入。

## 上游执行

```powershell
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -B -X utf8 D:/financial_data_project2/offline_enrichment/run_industry_batch.py
```

配置位于 `offline_enrichment/industry_config.yaml`。只调用本地 Qwen 选择行业字典中的 `industry_key`，标准代码和名称由程序查字典回填。不要并行启动同一批次。相同代码、模型、提示词和配置重复执行会跳过已保存记录；`--retry-failed` 只重试失败项。修改配置或代码后应另设 output 版本，不与旧缓存混用。

推荐入口另外校验本地模型地址、配置范围和输出目录，并持有批次写入锁。当前首轮已由内部核心脚本启动，运行期间不要再启动另一份同批任务。

输出：

```text
data_enriched/industry_batches/20261117_162900_US_GLOBAL_SEC_SIC_v1/
  raw_news.zip                    # 本批输入字节快照，不是重新下载的网页
  input_index.jsonl                # 原文件路径、source_id、文件SHA与原记录hash
  run.json                        # 字典/提示词/配置/代码/模型指纹
  industry_prompt.txt
  analysis/<source_id>.json        # 逐篇断点，成功和待复核均有记录
  industry_classifications.jsonl   # 批次结束时汇总
  progress.json                    # 运行进度
  manifest.json                    # 批次结束时生成/更新的完整文件SHA和数量
```

每篇的 `industries` 是行业提及，不自动代表行业传播；`industry_impacts` 只包含同时通过字典、逐字摘要引文、行业范围、经济机制、置信度阈值的候选。公司自身融资、任命、退市等不能仅因行业归属就传播给同行。已有人工作出的三个“仅公司”判断和两个全通道隔离记录继续生效。

## 下游接入

`StockMixer/scripts/11_import_legacy_industries.py` 从 `configs/llm.yaml` 的 `pipeline.industry_batch_*` 读取路径。它要求本批全部记录已尝试处理、pending 为 0，并验证原始新闻、字典、代码/提示词指纹、逐条摘要引文及对应关系。

```powershell
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe -B -X utf8 D:/finance/model/StockMixer+llm/StockMixer/scripts/11_import_legacy_industries.py
```

新冻结快照保存于下游 `outputs/artifacts/news/adapted/20261117_162900_US_GLOBAL_industry_v1`，包含原快照、上游整批增强审计和字典来源。每条记录的 `industry_enrichment.classified_industries` 是新标准化分类，`industry_impacts` 是本次可申请行业路由的影响；原公司/市场元数据保持原版本，避免破坏已审核绑定。

`allow_automated_industry_scope` 默认关闭。开启后，只接纳可以从封存上游产物独立重放的自动证据审核；这不算人工审核。明确外国地域且没有美国范围证据的影响保留待核验。行业传播还必须匹配股票数组身份、历史行业成员及实际新闻日期，不能用无日期的参考行业表直接参与训练。

对模型已经判断为行业级、但只引用了简短受影响对象的结果，可从同一摘要恢复最多 600 字符的逐字上下文；仅限能核验美国监管措施或官方行业统计的情况。不会重跑 Qwen，也不会把公司融资、退市等事件自动扩大为行业事件。额外范围核验时间独立保存，不沿用旧的推理可用时间。

本次定向补查 SEC 历史当日目录及公司自有交易所/代码声明。BSRR、BMRC、BMTC 的 2015-04-06 观察保持一天有效期；材料没有说明股份类别时，身份明确使用 `LISTED-SECURITY`，不冒充 `COMMON` 或商业永久证券 ID。更完整的历史成员覆盖仍取决于实际证据。

## 时间和验收口径

- 摘要来自旧库，证据一律标记 `stored_summary_only`；不声称重新核验原始网页。
- 原发布时间不变；新推理、新元数据的实际生成时间单独保留。历史回测属于 retrospective，不冒充历史 observed PIT。
- FinBERT 只编码标题和摘要。相同 source_id、文本 hash、模型指纹复用向量，新增 480 篇英文文本另行编码。
- 旧 1,423 篇市场决定只在原文、原记录 hash 和审核绑定完全一致时继承；新增新闻的市场决定仍为 pending，不悄悄扩大市场准入。
- 只有三个数据集面板里的行业训练 mask 非零，才能说行业真正参与训练。分类数量、行业链接数量和文件生成成功均不能代替这一验收；若历史成员仍不足，validate 必须继续阻止正式融合训练。
- 本批补处理与面板重建不启动正式 StockMixer 训练。模型结构、损失、原始参数与原训练评估保持不变。

本轮最终统计及阻断项见下游 `outputs/reports/20261117_162900_NASDAQ_NYSE_SP500_legacy_industry/completion.json`；该文件在整批导入与面板重建结束后才存在，不能将运行中 progress 当作最终结果。

同目录 `industry_stock_assignments.jsonl` 给出行业新闻、历史行业代码、股票 UID、数组行号、代码和有效期；`industry_unassigned_news.jsonl` 区分时间范围之外与缺少同日历史成员。`controller_status.json` 记录自动后处理阶段。整个过程不会因为了达到非零覆盖而延长身份日期或取消证据要求。

## 本轮最终版本（2026-11-17）

本批已经结束，无需继续常驻实时监控，也无需再次全库运行。1,904 篇全部处理完成，失败为 0；930 篇有标准字典行业，经过范围及分类限定词检查后 25 篇保留行业影响。原始 JSON 未改写。

首轮曾有置信度输出为 95 等不合格值。`recover_industry_batch.py` 配合 `industry_recovery_config.yaml` 仅补跑 33 条失败记录，1,871 条成功结果逐字节复用并保留原推理时间。当前完整批次为 `data_enriched/industry_batches/20261117_162900_US_GLOBAL_SEC_SIC_v2`；该目录封存父版本，能够核验复用与新推理的来源，不自动把百分数解释为概率。

当前下游快照为 `outputs/artifacts/news/adapted/20261117_162900_US_GLOBAL_industry_v2`。训练行业 mask 的有效“股票×日”单元：NASDAQ 35、NYSE 5、SP500 0。NASDAQ 测试期为 10，NYSE/SP500 测试期为 0；覆盖仍稀疏，不能承诺效果改善。SP500 保持受控阻断，没有外推历史有效期或降低阈值制造覆盖。没有启动正式训练。

```powershell
# 需要重放失败恢复步骤时使用；已完成的本批无需重复执行。
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -B -X utf8 D:/financial_data_project2/offline_enrichment/recover_industry_batch.py
# 只读校验当前下游快照。
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe -B -X utf8 D:/finance/model/StockMixer+llm/StockMixer/scripts/11_import_legacy_industries.py --check
```

本批输入成员在开始时冻结。新增文件不会悄悄追加到旧版本；新增新闻应使用新的输入版本/增量流程。后续持续采集时按预测截止时间定时处理并检查失败、时间、去重、映射与覆盖率即可，不在每轮训练中调用新闻模型。

最终详细报告位于下游 `outputs/reports/20261117_162900_NASDAQ_NYSE_SP500_legacy_industry/REPORT.md`。该目录的 `implementation_work` 只保存实施与验证证据，不是正式运行入口。

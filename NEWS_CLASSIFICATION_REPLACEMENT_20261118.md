# 新闻行业与影响范围：整体替换规则（2026-11-18）

新版契约为 `dictionary-scope-replacement-1`。用户要求覆盖新版记录里的旧错误分类，因此不再与旧标签取并集。原始新闻、原始发布日期、正文、股票实体和可追溯备份保留。

| 字段 | 保存规则 |
| --- | --- |
| 涉及行业 | Qwen 从提供的版本化字典候选中选精确代码；Python 回填字典标准名称。无有效选择写 `[]`，清除旧值。代码、字典版本、原文措辞、引文及置信度存在审计中。 |
| 影响范围 | Qwen 根据本轮实际读取文本重判 `company / industry / market`，可多选；每种范围保存引文、理由、置信度。无证据写 `[]`，清除旧值。 |
| 市场细分范围 | 只有一般范围通过 `market` 校验，才保留通过证据校验的 `US_EQUITY / NASDAQ_LISTED / NYSE_LISTED`。位于审计与 `article_market_scopes.parquet`。 |
| 行业传播 | 提到行业不等于行业传播；必须有字典代码、识别该行业的原句、集体经济作用范围引文、传导机制及通过阈值的置信度。未通过项不传播到同业股票。 |
| 旧标签 | 留在原始 JSON、冻结的 `raw_news.zip`、旧审计和新版审计的 `classification_replacement.previous_*` 中，不无条件混入新版分类。 |

正式 JSON 仍为 11 个字段，不把审计侧车的代码/证据随意塞入正式字段。公共行业名称统一；不同字典标准和版本在侧车及关系表中区分，不能仅凭同名行业混用。行业信息分配给股票仍由下游有来源的成员表完成；FinBERT 负责文本编码。

## 当前正式处理批次

`D:\financial_data_project2\data_enriched\retraced\20261118_144829_US_GLOBAL_v5`

这是针对原有 1948 篇去重新闻的新一轮重新判定。旧 v4 已停止；其中 50 篇逐篇结果保留为诊断，不作为新版字段判定结果复用。不能把规则修复或三篇样本验收说成全量已完成。

| 相对批次路径 | 内容 |
| --- | --- |
| `analysis/<source_id>.json` | 每篇本轮 Qwen 结构化选择、响应哈希、实际推断时间、校验结果及溯源状态。 |
| `evidence/<source_id>.json` | 与原记录绑定的新分类短引文证据包。 |
| `adapted/data/US/<source_id>.json` | 下游用的新版记录；行业及范围整体替换，包括清空无依据旧标签。每次导出直接重写同 source_id 文件。 |
| `adapted/audit/<source_id>.json` | 字典代码/标准名、通道证据、旧值审计、原记录哈希、实际特征可用时间。 |
| `adapted/evidence/<source_id>.json` | 与新版记录哈希绑定的短引文证据包。 |
| `structured/*.parquet` | 8 张关联表；行业提及及影响按字典标准、版本、代码关联，支持只有影响关系而无重复提及的情况。 |
| `live_progress.json` | 逐篇更新处理数；`completed` 不代表三个通道都有有效信息。 |
| `progress.json` | 每 100 篇或正常退出时更新导出统计。 |
| `acceptance.json`、`dataset_bundle.zip` | 全批次完成后的离线验收与完整打包。 |

新元数据的可用时间是本轮实际生成/提交时间。历史摘要无法恢复原文时如实标为 `stored_summary_only`；所有本轮历史推断为回溯探索，不冒充历史当时可用的 PIT 特征。

## 配置与日志

- 上游配置：`D:\financial_data_project2\offline_enrichment\retrace_config.yaml`。
- 下游配置：`D:\finance\model\StockMixer+llm\StockMixer\configs\llm.yaml`，`pipeline.retrace_artifact` 已指向 v5，旧面板与旧特征不复用。
- 活跃任务指针：`D:\financial_data_project2\logs\20261118_retrace_active_job.json`。
- 新版实时日志：`D:\financial_data_project2\logs\20261118_144829_retrace_v5.stdout.log`；异常见同名 `.stderr.log`。

```powershell
Get-Content "D:\financial_data_project2\logs\20261118_144829_retrace_v5.stdout.log" -Tail 20 -Wait
```

全量完成并通过验收后，在下游执行：

```powershell
cd D:\finance\model\StockMixer+llm\StockMixer
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe scripts/15_import_retraced_news.py --activate
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe main.py --step prepare --new
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe src/train_strict.py
```

导入脚本阻止未完成批次正式激活，也阻止旧“合并标签”批次正式导入；验收后文件变化会被拒绝。第一条命令会切换到新的下游快照，第二条重建新闻特征和面板；本轮修复没有启动模型训练。

## 验证与边界

上游完整回归 600 项通过，下游完整回归 309 项通过，原记录哈希绑定另有 4 项导出测试通过。三篇真实本地 Qwen 样本通过导出、证据验收和下游隔离导入，结果为 3 篇 ready。样本使用旧摘要，不冒充重新获得原文或人工复核。

- Ford/GM 转向 SUV/皮卡：新版为公司 + 行业，去掉旧市场范围；1 条行业影响通过传播校验。
- FOMC 货币政策：市场范围，细分 `US_EQUITY`。
- Insperity 自身循环授信：公司范围，清除旧行业传播范围。

这些样本证明保存与接口链路，不证明全库语义已经全部正确。Qwen 自评置信度不是校准概率；未核验历史成员仍不进入严格历史通道，无日期参考成员仅能用于显式回溯探索。

验收记录：`D:\financial_data_project2\logs\20261118_144829_classification_replacement_acceptance.json`。原始 1948 篇、1982 个物理副本 SHA-256 未变。代码回滚备份位于上游 logs 和下游 outputs/artifacts/maintenance 的 `20261118_144829_classification_replacement_backup.zip`。

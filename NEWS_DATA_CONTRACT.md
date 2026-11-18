# 新闻保存与1948篇历史新闻重新溯源

本次修改只涉及采集、关系侧车、导出和离线维护；不修改 StockMixer、FinBERT、训练和评估。

## 1. 新采集保存逻辑

正式 JSON 保持原有11字段。`影响范围=market` 是宽泛标签，不代表已批准传播到所有美股。

同一次 Qwen 正文提炼新增：

- `market_scope`：只允许 `US_EQUITY`、`NASDAQ_LISTED`、`NYSE_LISTED`，不确定为 `[]`。
- `scope_evidence`：每个范围对应原文短引、传导理由、0–1模型自评置信度。
- 公司财报、个别公司融资、括号里的交易所代码，不构成全市场证据。
- 模型从原文证据候选中选择短引，JSON Schema限定候选原句；不允许模型自由改写引文。无合格全市场证据的原文在解码时即禁止生成市场通道。
- 行业必须从版本化字典候选选代码；行业提及和行业传播是两回事。行业传播还须原文作用范围引文与机制。

侧车与公开 JSON 按 `source_id + record_hash` 绑定，模型、Prompt、代码、实际推理时间分别保留。模型自评及自动规则通过不等于人工审核或金融因果证明。

清理前顺序：正式 JSON/副本及审计事务提交 → 保存有界证据包 → 读回并核对记录哈希、原文哈希、全部新关系引文 → 记录证据包哈希 → 精确归属清理。证据包位于 `.runtime/simple/evidence/<source_id>.json`。常规清理补偿入口也执行同样保护；过期失败原文仍沿用24小时内TTL。

不永久保存完整正文；证据包可重放已选引文，不能还原原文全文。正文哈希不能替代引文。

## 2. 统一结构化导出

`structured_export.py` 统一生产记录与离线适配记录的关系格式。现在为8张表：原有7张加 `article_market_scopes.parquet`。

`article_industries` 同时保存代码、分类版本、字典版本/哈希及证据依据；`article_industry_impacts` 另外保存传播机制和作用范围。`article_market_scopes` 独立保存市场范围及引文、理由和校验状态。

没有真实历史行业成员关系时，证券行业主表仍为空，不凭新闻中的行业名称生成股票成员表。

## 3. 1948篇历史新闻处理

环境：`C:\Users\marti\anaconda3\envs\finance_crawler\python.exe`。上游使用自身环境；下游继续使用 `stockmixer` 环境。

```powershell
cd D:\financial_data_project2
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -X utf8 -u -B offline_enrichment/run_retrace.py
```

全部参数在 `offline_enrichment/retrace_config.yaml`，包括输入市场、预期去重条数、输出版本、本机模型、字典、阈值、正文分块和检查点频率。

流程：

1. 冻结 `data/US`、`data/GLOBAL` 按ID去重的1948篇、全部副本哈希及原记录ZIP。原JSON不覆盖。
2. 优先核对已有原文缓存；否则用现有安全下载器按原始链接重新下载，不重新付费搜索、不绕过访问限制。
3. 核对原文哈希或同一不可变SEC申报的日期证据。普通网页只有发布日期仍不足以证明正文没有更新；无法确认旧正文版本时回退到已有摘要，记录 `stored_summary_only`，不能标为原文已核验。保持原发布日期不变。
4. 本地 `qwen3:8b` 对可验证原文进行无损分块提炼；不能恢复时仅分析旧摘要。只从字典选择行业，不猜股票身份。失败输出仅记录类型、短错误码和响应哈希；通过校验的有界结果进入侧车。
5. 校验引文逐字存在、字典代码与名称、行业作用范围、市场范围、机制、置信度；可配置一次语义修复。未通过的可选关系被排除并标注，不能通过放宽证据门槛增加覆盖。
6. 每篇提交检查点；每100篇和正常退出生成统计及8张Parquet。全部处理后构建并逐文件读回校验ZIP。

重新提炼的 `llm_inferred_at`、`feature_available_at` 是本轮实际时刻。历史日期只能用于明确标注的回溯实验，不构成历史PIT可交易证明。

### 输出目录

默认 `data_enriched/retraced/20261118_121127_US_GLOBAL_v3/`；此前pilot、v1、v2是试跑版本，不作为本次全量交付：

```text
run.json                         配置、模型版本、Prompt与代码指纹
input_index.json                 原始输入与全部副本哈希
raw_news.zip                     原JSON规范视图及全部原始副本逐字节归档，非网页全文
source_code.zip                  本批次源代码和Prompt，不含生产密钥配置
analysis/<source_id>.json        每篇溯源状态、新关系、响应哈希与时间
legacy_audit/<source_id>.json    匹配原记录的旧审计，保留旧证据来源
evidence/<source_id>.json        绑定原始JSON的新关系短证据包
adapted/data/US/<source_id>.json 保留原摘要/日期的适配副本，可恢复确证原文标题
adapted/audit/<source_id>.json   绑定适配JSON的新审计
adapted/evidence/<source_id>.json 适配JSON对应的证据包，保留原记录血缘
dictionary/                     本批次所用行业字典与来源文件
prompts/retrace.txt              精确Prompt
structured/*.parquet             同一批次8张表与manifest
live_progress.json              每篇进度
progress.json                   周期性汇总
manifest.json                   全文件哈希、处理统计、限制
dataset_bundle.zip              全量处理后才生成的完整交付包
coordinator_status.json         总任务、失败重试和验收状态
acceptance.json                 最终零网络/零模型调用的离线验收报告
```

`completed` 表示提炼流程完成，不代表文章一定具有可传播行业/市场信息。应同时查看被拒可选关系数量和 `needs_review`。失败文章仍保留原JSON与失败审计，不会伪装成无新闻。

### 恢复、重试与打包

同配置重复运行自动跳过已提交文章；代码、模型、字典或参数变更必须创建新版本目录，避免混批。

```powershell
# 明确重试失败文章
python -X utf8 -u -B offline_enrichment/retrace_news.py --retry-failed
# 只重建统计、Parquet和完整包，不调用Qwen或网络
python -X utf8 -B offline_enrichment/retrace_news.py --package-only
```

在批次输出目录建立空文件 `STOP_REQUESTED` 可请求在当前文章提交后停止；恢复前移走该文件。不要并发启动同一批次。

`run_retrace.py` 顺序完成全量处理、最多一轮失败重试及离线验收；不会恢复生产爬虫。已有JSON正文、原日期和股票实体不得被改写。验收脚本可以独立运行：

```powershell
python -X utf8 -B offline_enrichment/verify_retrace.py data_enriched/retraced/20261118_121127_US_GLOBAL_v3
```

## 4. 下游接入边界

本轮只修改上游。旧 StockMixer+LLM 面板及FinBERT缓存不会自动替换。

交付包包含原记录、适配记录、旧/新审计、证据、字典、Prompt、时间和哈希，不能只复制 `data/*.json`。

下游现有市场路由只接受标题/摘要中的引文。若新引文只在原文和证据侧车中，下游还需支持“绑定审计及证据包的原文短引”验证；不得为了兼容把原文证据伪称成摘要证据。行业原文证明依赖 `content_hash + industry_dictionary.sha256 + 完整原始影响记录`，本批次会保留这些字段。

完成下游快照导入、证券/历史行业成员关系审核、三通道覆盖检查、FinBERT重新编码和面板验证后，才能启用新数据训练。当前包不自动标记 `training_ready=true`。

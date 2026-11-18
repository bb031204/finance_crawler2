# 上下游新闻适配与行业映射（2026-11-18）

## 已实施的变化

- 接收生产采集和重新溯源批次的 `JSON + audit + evidence`。原文证据不必出现在摘要中；审计文件 SHA、证据包 SHA、新闻记录哈希、原文哈希、文章ID及引文字节哈希均须绑定。
- 旧摘要证据仍与原文证据区分，不把当前重新推理时间倒填到过去。新闻时间对齐、股票身份、原始 StockMixer 参数、主干、损失、每轮验证/测试及选模规则均未改变。
- 市场与行业模型自评门槛由0.85改为0.75。市场候选补充美国财政、贸易、信用、就业及全球金融市场事件；公司财报及括号中的交易所代码仍不代表全市场。
- 上游同时从 SEC-SIC、TRSR-INDUSTRY、SP500-SECTOR 的版本化字典均衡召回24个候选，Qwen选代码，程序回填标准名称。**不把三套代码直接等同，也不让 FinBERT 猜行业或股票。**
- 修复行业影响代码/作用范围引文/置信度可为空，导致后续全部丢弃的接口问题。影响必须有字典代码及逐字行业范围引文；允许有依据的条件性机制，不要求机制所有词语都逐字出现在分类引文中。
- 市场与行业传播仍是自动规则通过，不等于人工全审。已由用户确认为仅公司的3个来源ID列入 `pipeline.industry_excluded_sources`，继续禁止同行扩散。

## 行业到股票的对应

目录：`D:/finance/model/StockMixer+llm/StockMixer/outputs/artifacts/news/industry_mapping/20261118_135542_NASDAQ_NYSE_SP500_reference_v3`。

| 数据集 | 总股票 | 数据集参考行业已匹配 | 未匹配 | 有日期SEC行业观察覆盖股票 |
|---|---:|---:|---:|---:|
| NASDAQ |1026|870|156|9|
| NYSE |1737|1237|500|3|
| SP500 |474|474|0|2|

每只股票一行：`<DATASET>_stock_industry.csv/jsonl`。按行业查看股票：`<DATASET>_industry_to_stocks.json`。

可供明确回溯探索的参考成员：`<DATASET>_reference_experiment_memberships.jsonl`；有日期观察：`verified_industry_memberships.jsonl`。前者的 `verification_status=reference_only`、`valid_from/valid_to=null`，**不伪造历史有效期，不保证所有历史年份分类均正确**。后者只对证据覆盖日期有效，不外推为连续全历史。

行业与股票是多对多关系；`dataset + stock_uid + array_index + dataset_ticker + industry_key` 逐数据集精确绑定，不能把 ticker 当数组行号。未匹配项没有猜补代码或行业。证券公司通道仍只用有证据的历史身份，参考行业表不生成公司身份。

## 配置

下游 `configs/llm.yaml`：

```yaml
review:
  min_confidence: 0.75
pipeline:
  industry_min_confidence: 0.75
  industry_membership_mode: reference_retrospective
  # verified_historical = 只用有日期证据；reference_retrospective = 明确的无日期参考分类探索
panel:
  mode: retrospective
fusion:
  enabled_channels: [market, industry]
```

`reference_retrospective` 自动阻止 observed_pit；面板和readiness分别记录参考分类与真实历史验证状态。正式历史比较请切为 `verified_historical`，接受较低覆盖，或补充真实历史主数据。原始窗口逻辑保留 `data.protocol: original`；其中NAS/NYSE有验证标签重叠，不能把它称为严格无泄漏基线。

上游 `config.yaml` 的 `market_information.scope_min_confidence`、`market_information.industry_dictionary.min_confidence`、`candidate_limit`、`active_standards`；旧库补处理参数在 `offline_enrichment/retrace_config.yaml`。全部可调，但修改后必须使用新的输出版本，不能混用既有缓存。

## 当前数据状态与运行

旧v3在238/1948篇的文章提交检查点停止，结果保留。已另存诊断导入：228篇编码输入就绪、10篇待复核；没有把不完整批次启用为训练快照。

新版批次：`D:/financial_data_project2/data_enriched/retraced/20261118_135542_US_GLOBAL_v4`。以其中 `coordinator_status.json`、`live_progress.json` 为准；不是全部完成后才能查看进度。新规则不会改写旧 `data/US` / `data/GLOBAL` JSON。**新批次开始不表示新闻已经全部可训练。**

完成新版批次及离线验收后，在下游执行（解释器为stockmixer）：

```powershell
cd 'D:\finance\model\StockMixer+llm\StockMixer'
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe scripts/15_import_retraced_news.py --activate
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe main.py --step prepare --new
& C:/Users/marti/anaconda3/envs/stockmixer/python.exe src/train_strict.py
```

导入脚本默认读取config中的 `pipeline.retrace_artifact`，整批尚未完成或验收未通过时阻止正式导入。`--allow-partial` 仅供诊断，不能同时 `--activate`。激活会备份配置、切换快照、清空旧路由/特征引用并使用新状态文件；不会把新旧文章直接拼入旧面板。`prepare` 离线构建真实FinBERT特征、关系、路由及面板；训练阶段只读取缓存。三个数据集需要分别确认覆盖，不能由参考映射数量推算有效新闻数量。

若需重启上游已停止的新版离线任务（不要与正在运行的同批次重复启动）：

```powershell
cd D:\financial_data_project2
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -X utf8 -u -B offline_enrichment/run_retrace.py
```

本地Qwen真实摘要检查：2018-04-26汽车产业转向新闻可产生有证据的SEC-SIC和TRSR行业影响，未扩散至全市场；2017-06-14 FOMC新闻进入US_EQUITY。该检查只验证接口和规则，不证明全库准确率、原文完整性或训练收益提升。详细结果在上游 `logs/20261118_135542_real_qwen_context_check.json`。

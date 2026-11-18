# 旧新闻 JSON 离线补齐

独立入口为 `D:\financial_data_project2\offline_enrichment\enrich_legacy.py`。读取两个 `_original` 目录和与内容哈希匹配的历史审计，另存增强副本；现有爬虫模块、配置、生产队列及原始 JSON 均作为只读输入。

```powershell
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -B -X utf8 D:/financial_data_project2/offline_enrichment/enrich_legacy.py
```

重复执行会校验输入快照及输出哈希，继续尚未完成或需复核的记录。脚本或提示词版本发生变化时重新核对。`--preview` 只盘点并记录输入快照；`--limit 3` 完成格式补齐并最多处理三篇需要模型的记录，供小样本验证。

## 输入和输出

输入为 `data/US_original`、`data/GLOBAL_original` 下的 JSON，支持其内部已有的 US/GLOBAL 子目录。按 source_id 合并相同副本，冲突会明确报错。

```text
D:\financial_data_project2\data_enriched\
    US\原文件名.json
    GLOBAL\原文件名.json
    analysis\source_id.json
    manifest.json
    report.json
    .work\                   # 独立的模型元数据缓存
    .runtime\writer.lock     # 独立写入锁
```

GLOBAL 在两个输入目录中均出现时，生成两份内容一致的副本；联合使用时按 source_id 去重。

## 补齐方式

- 每条副本统一为当前 11 字段格式，保留原 source_id、链接、发布日期、分类、关键词和摘要。
- 从匹配的历史审计恢复公司名称与已核验证券声明。未知股票代码和交易所保留 null，不能凭公司名称推测或使用今天的证券列表回填。
- 现有公司、行业和影响范围均充分的记录直接复用证据；其余交给本机 Ollama/Qwen 补充这三类元数据。模型引用的证据须逐字来自旧摘要，行业分类区分 explicit/inferred。
- title 只沿用已存在的原文标题及匹配审计。本次离线模式无法恢复的标题为 null，并标记 title_unavailable_offline；不能用模型概括、关键词或文件名冒充标题。
- 新语义证据的来源为 legacy_summary，不能当成再次核验过的原始网页。原摘要中既有事实和数字的真实性仍需来源级复核。
- 时间信息保留原有发现/采集时刻，增强副本的 feature_available_at 使用本轮实际提交时刻；原时间信息完整存入 analysis。新 Qwen 推断的 llm_inferred_at 单独记录。历史未知推断时刻不补猜。

## 使用边界

analysis 中包含处理状态、输入输出哈希、提示词/代码/模型版本、证据来源及缺失项。completed 表示本次补齐流程完成，空实体或未知标题仍可合法存在；needs_review 表示模型输出等问题需复核。useful=false 只标记文本可能缺少实质市场信息，原记录仍保留；useful=null 表示本轮只复用了已有审计，未重新做用途判断。

后续严格时点实验应使用增强版本的实际可用时间，不能把本次生成的元数据当作文章原年份已经存在的信息。历史文章关联属于 Retrospective 口径。公司/行业/市场三通道的数值面板、FinBERT 和历史股票映射仍由下游独立流水线生成。

本地验证入口为 `python -m unittest discover -s offline_enrichment/tests -v`，测试数据放本项目临时目录。

## 缺项复核与全量验收

第一轮全部结束后，运行以下独立复核。仅复核模型漏填元数据、未判定影响范围或误将通用行业标签作为组织的记录，已充分的记录不重复调用模型。复核格式要求明确返回全部元数据字段，无法判断时仍可返回空列表。修订仍只写增强副本，原始正文和来源时间保持不变。

```powershell
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -B -X utf8 D:/financial_data_project2/offline_enrichment/refine_metadata.py
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -B -X utf8 D:/financial_data_project2/offline_enrichment/validate_outputs.py
```

复核的提示词在 `refine_metadata.py` 的 `PROMPT`，实际模型/提示词/脚本版本和上一轮分析保存在对应 analysis 的 refinement 中。顶层 code_version/prompt_version 对应第一轮格式化及抽取入口；refinement 记录后续复核版本。重复复核跳过已完成项。`report.json` 对应第一轮；最终复核统计在 `refinement_report.json`，独立逐文件验收在 `validation.json`。

## 本次完成结果（2026-11-12）

1,175 篇唯一记录已全部完成，输出 `data_enriched/US` 下 1,175 个 JSON、`data_enriched/GLOBAL` 下 34 个同内容副本。后者是 US 中同 source_id 的全球市场记录，合并读取必须去重。每篇有对应 analysis 审计；全部副本已通过逐文件哈希、11 字段格式、原始正文/时间不变、实际可用时间及版本校验。49 个现有爬虫文件和 1,209 个原始输入文件哈希全部未变。

580 篇直接复用现有证据，595 篇使用本地 Qwen；其中 214 篇完成缺项复核。最后一次类型过滤复用了已校验模型缓存，新增推理 0 次，排除了 6 个被误当作实体的法律、调查项目或通用行业名称，既有分析保留在审计中。11 项专项测试通过，全量验收 errors=[]，无 pending 或 needs_review 项。

| 项目 | 原记录中非空的篇数 | 补齐后非空的篇数 |
|---|---:|---:|
| 命名实体 | 452 | 1,163 |
| 涉及行业 | 957 | 1,106 |
| 影响范围 | 888 | 1,175 |
| 原文标题 | 5 | 5 |

1,170 篇标题仍为 null；69 篇行业未知；12 篇没有可确认的命名实体。5 篇存在旧证券属性证据不足标记，未据此猜补证券身份。completed 表示流程完成，不表示这些未知项已被证实。

“股票实体”中可含原文明确提到的公司、品牌或组织，不能将每个名称视为上市股票。尚未核验的 ticker/exchange 保留 null；公司级训练关联仍须可靠历史证券主数据。原文标题缺失不能用搜索截短标题或模型自拟标题代替。本次未回访网页，也未验证旧摘要中的每个历史事实；来源时间原样保留，新元数据只在本轮实际可用时刻之后才能用于严格 PIT 实验。

## 本轮行业字典补处理

旧市场信息的字典分类、证据、恢复批次和下游覆盖详见 [INDUSTRY_BATCH.md](INDUSTRY_BATCH.md)。本轮1,904篇已经完成；当前使用v2增强版本，原始新闻不回写。

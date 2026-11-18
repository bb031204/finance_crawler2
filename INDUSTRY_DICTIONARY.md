# 行业字典与下游接口

配置位于 `config.yaml / market_information.industry_dictionary`。正式11字段新闻格式保持兼容，代码与证据写入同版本审计sidecar和Parquet，不重写旧JSON。

## 分类与事件影响分开

- `industries`：提及或归类的行业，允许公司自身行业；不因此传播到同行。
- `industry_impacts`：对行业或多家公司的事件影响提案，必须有机制、模型自评、原文引文和独立的`industry_scope_quote`。
- `industry_key`必须从本次候选枚举选择；未知为null或[]。分类字典不构成搜索/采集白名单。
- 原文行业词保存在name/industry_name_original；标准名称另存在industry_name_standard，不把字典名称冒充原文引文。

字典位于 `industry_data/industry_dictionary.jsonl`，共587条（排除了n/a）：444条SEC-SIC、132条原始TRSR基准行业、11条SP500基准板块。
不同命名空间不可当作同一种分类；默认`active_standards: [SEC-SIC]`，与下游有日期证据的SEC-SIC成员表衔接。
基准分类仅作参考，没有历史有效期时不能进入严格历史训练。官方代码依据保存在`sec_sic_source.html`，原基准分类来源副本也保留在industry_data，manifest记录来源与SHA256。

候选召回采用词频区分度和原标题权重，数量由candidate_limit控制；Qwen在原有同次正文请求中选择，没有新增推断阶段。
字典候选和约束计入正文预算。当前num_ctx为8192；num_predict仍1400，模型与采样参数不变。
代码校验候选枚举、名称/代码/引文对应、原文逐字证据、置信度及范围引文；不足时不产生可用行业关联。
这不是准确率保证，行业传播仍须下游作用范围审核。

## 审计字段

`industry_dictionary`记录版本、字典SHA256和候选keys；行业提及及影响中保存：

```text
industry_key / industry_standard / industry_version / industry_code
industry_name_original（影响）或name（提及） / industry_name_standard
quote / original_evidence_quote / industry_scope_quote
original_evidence_verified / summary_evidence_quote / evidence_basis
confidence / mechanism / evidence_type / dictionary_sha256
```

原文证据在已有成功清理之前核验并保存；摘要若改写导致不含同一引文，不伪造摘要引文。
`original_evidence_verified`仅说明绑定原文核验，不代表已经人工批准行业传播。
原文哈希、模型digest、Prompt/代码版本、真实llm_inferred_at及提交可用时间继续保留。
更新字典须更新manifest与下游一致版本；字典SHA进入语义缓存指纹，不混用旧分类缓存。

加载时同时复核字典主键/标准代码唯一性、命名空间、来源SHA和包内来源文件，不只检查JSONL自身。

## 使用

下次正常启动main.py时载入新字典；本次没有自动启动或重启采集器。
已有新闻和旧审计没有迁移，也不会自动获得新代码；补旧数据需要单独版本化的离线流程。
上下游应使用同一字典SHA。下游对应表与使用方法见StockMixer的`docs/NEWS_PIPELINE.md`行业字典章节。

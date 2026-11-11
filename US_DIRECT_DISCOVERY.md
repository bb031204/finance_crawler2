# 美股 SEC 目录发现使用说明

本版在原采集流程前增加 SEC EDGAR 目录发现：按公司或季度目录，结合日期和申报类型获取候选。候选仍进入原来的正文获取、Qwen筛选与摘要、发布日期校验、独立JSON保存流程。

目前直接目录适配的是SEC公司披露，包含公告、定期报告等；财经新闻、论坛帖子、宏观政策仍通过搜索渠道补充。本版没有声称直接遍历NASDAQ、Reddit或其他网站。

## 1. 选择发现方式

修改 `D:\financial_data_project2\config.yaml`：

```yaml
search:
  discovery_mode: direct
```

| 值 | 行为 | Tavily用量 |
| --- | --- | --- |
| `search` | 原来的Qwen规划加Tavily/RSS发现 | provider=tavily时消耗搜索额度 |
| `direct` | 只读取SEC目录，无需搜索密钥，不让Qwen生成目录查询 | 不调用Tavily |
| `hybrid` | 原搜索渠道加SEC目录，两路合并去重 | 搜索部分按原规则消耗额度 |

实际模式、市场和日期以 `config.yaml` 为准。修改配置只在下一次启动时加载，正在运行的旧进程不会切换模式。

直接目录目前只支持US。若同时采集CN和US，选择hybrid：CN继续搜索，US同时使用搜索与SEC目录。限制/排除域名仍生效；使用SEC需在US域名中保留 `sec.gov`，它同时允许 `www.sec.gov` 和 `data.sec.gov`。

## 2. 按股票或CIK选择公司

```yaml
search:
  discovery_mode: direct
  sec_edgar:
    tickers: ["AAPL", "MSFT", "NVDA"]
    ciks: []
    forms: ["8-K", "10-Q", "10-K", "6-K", "20-F", "40-F"]
    include_amendments: true
    user_agent: "FinanceDatasetResearch/1.0 your-real-contact@example.org"
    page_size: 100
    max_pages_per_round: 5
    max_requests_per_round: 10
```

以上是字段示意，联系邮箱必须换为真实值。当前本机配置已沿用用户此前授权的邮箱，不需要复制示例覆盖。该值不进入配置预览、提示词与日志。

- `tickers`：按SEC当前股票代码映射查CIK，例如AAPL；不是公司名称模糊搜索。先精确匹配；未精确命中的点号/连字符股类写法，仅在当前表存在唯一对应CIK时作为别名解析，不进行无证据的通用替换。未唯一匹配会暂停并提示，不会自动扩大为全部公司。
- `ciks`：直接填写明确的公司CIK，例如 `["0000320193"]`；数字320193也可，会补齐为10位。与tickers取并集并去重。
- 两项均为 `[]`：按历史季度索引发现范围内的全部SEC申报主体，按forms过滤。它包括部分外国发行人及其他申报主体，不等同于某交易所全部上市股票名单。
- `monitoring.securities` 继续作为Qwen的关注偏好；直接目录的硬公司范围使用这里的tickers/ciks，避免把中文公司名或普通关注词误当作CIK。
- 当前ticker映射不是历史证券名册，可能漏掉已退市公司或遇到代码变更。历史研究优先使用明确CIK，或使用季度索引获取当时的披露，不据当前股票名单宣称消除了存续偏差。

日期继续填写在原位置，首尾日期均包含：

```yaml
collection:
  mode: historical
  start_date: "2019-01-01"
  end_date: "2019-12-31"
```

日期依据是SEC官方申报日期，不是报告所属季度、财年结束日或今天的下载时间。只确认日期时，JSON不补造小时、分钟。

## 3. 页数与申报类型

| 配置 | 含义 |
| --- | --- |
| page_size | 每个逻辑页处理多少条日期/类型匹配的目录记录；不限制整个历史范围总条数 |
| max_pages_per_round | 每轮发现最多处理多少逻辑页 |
| max_requests_per_round | 每轮最多发起多少次目录HTTP请求，包含ticker映射、公司目录、历史分卷或季度目录；正文下载另计 |
| search.complete_history | true时自动续轮；false时当前目录发现预算用完、处理当前候选后退出，下次main.py续跑 |
| collection.max_documents_per_cycle | 原有每批候选处理上限，自动正文续批规则保留 |
| ollama.max_calls_per_cycle | 原有每批模型调用上限，目录发现本身不消耗Qwen调用 |

`8-K`=重大事项报告，`10-Q`=季度报告，`10-K`=年度报告；`6-K`、`20-F`、`40-F`覆盖相应外国发行人披露。`include_amendments: true`会包含已选类型的/A修订版本；修订件使用其自身的申报时间，不回填为原报告时间。

季度模式读取官方 `master.gz` 压缩索引，按范围和类型过滤后缓存分页；公司模式读取Submissions主目录，并展开与日期重叠的历史分卷。这里的页是本地逻辑页，不代表每100条都重新下载完整目录。已提交页码和候选队列共同保存，重启后不重新翻已完成页。

当前留空公司范围、2012-2022日期对应42个季度目录。每个目录可能有很多候选；complete_history=true会持续续轮，总请求和本地模型处理量可远超单轮上限。建议首次先指定少量ticker、缩短日期，或使用--once验证。

## 4. 运行

```powershell
conda activate finance_crawler
cd D:\financial_data_project2
python main.py --validate-config  # 只检查配置，隐藏密钥和SEC联系信息
python main.py --preview-history  # 只读预览；ticker解析与历史分卷数量需实际请求后才能确定
python main.py --once             # 可选：发现一轮、处理一批后退出，保留剩余任务
python main.py                    # 按当前配置正常发现并自动处理
```

`--resume`跳过两种发现渠道，只处理已有候选；需要继续翻目录时直接运行main.py。目录失败再次运行main.py重试，`--retry-failed-searches`仅用于搜索渠道。`--reprocess-saved`沿用原来的重读方式。

实时模式沿用原collection设置，日期为null；每次轮询重新检查当前目录并去重，已保存记录不重新提炼。历史模式对已完成目录保留断点；它不会自动探测该目录后来的更正或删除，需在研究更新策略中另行处理，不将当前目录快照当成当时完整版本。

## 5. 正文、保存与日志

SEC索引页列出一次申报的主文档和附件。程序先读取该索引核对Filing Date，再读取匹配申报类型的主文档；不会把附件列表当作报告正文，也不会声称自动读完所有附件。单独发布在附件中的重要信息可能需要后续附件适配。

每条正式JSON保留原七字段，并按最新要求增加股票实体、涉及行业、影响范围，共10字段；输出在 `data/US/`，文件命名规则不变。`url`保存稳定的SEC申报索引链接，可从该页打开原文；实际读取的主文档链接另记内部审计 `source_document_url`。原文清理仍须正式JSON和审计校验成功。旧记录可用`python main.py --upgrade-records`备份升级，未知分析信息留空。

在direct/hybrid模式中，SEC主文档、iXBRL查看器和申报索引链接会按同一accession归并，避免两路重复处理同次申报；旧的主文档URL记录会被识别并复用，不因本次改造迁移其文件。普通新闻转载仍需后续近重复检查。

Qwen仍负责筛选、分类、关键词和连贯摘要，不自行预测收益率。SEC原文通常为英文，摘要和关键词沿用英文。主文档过长、PDF不可读、模型上下文不足或其他解析错误仍会留待复核；目录发现成功不等于正式JSON已经生成。

终端新增SEC范围、每页命中数、目录请求数、分页进度和最终目录汇总。`logs/latest_run.json`增加 `direct_discovery`；汇总的 `summary.search`只计搜索渠道，`summary.direct_discovery`计目录渠道，`summary.candidates`计两者合并后的候选。年度覆盖区分搜索返回、目录匹配和正式JSON数量。

默认时间进度无需配置：每条候选处理后、以及每30秒等待时，显示本次已用时间、已知队列进度、队列预计剩余和当前单篇耗时。目录每页另显示完成目录数、已知页数、目录阶段耗时和已知页的预计剩余；未展开目录单独列出。目录阶段估计不包含Qwen处理，仍在发现候选时无法确定整轮总时长，终端会明确显示暂无法估算。至少2条实际处理样本后才估队列速度，长文、失败、筛选比例和后续新增候选都会影响结果。日志文件的progress是批次/最终快照；估计不会作为成功数量或固定完成承诺。

目录状态和元数据缓存位于 `.runtime/simple/state.json` 与 `.runtime/simple/sec_catalogs/`，不增加正式data目录层级。SEC访问仍使用原本的代理/Fake-IP配置，遵守robots、访问限制与限速；不会修改Clash/TUN。

来源时间和当前摘要的feature_available_at仍分别保存。重新概括的历史网页不能仅凭旧发布日期就充当当时已生成的训练特征。

## 官方参考

- [SEC Submissions API与历史分卷](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [SEC目录、ticker映射、申报编号与Fair Access](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

本次验收记录见 `logs/sec_direct_live_validation.json`（真实公司目录、两条候选和一篇主文档）与 `logs/sec_quarter_gzip_live_validation.json`（真实季度目录、分页候选）。均为隔离验证，不是正式全量采集或预测效果验收。

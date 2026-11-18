# 金融市场信息采集

项目位于 `D:\financial_data_project2`，使用现有 `finance_crawler` 环境、本机 Ollama 和 `qwen3:8b`。Crawl4AI处理HTML正文，PDF先恢复文字，Qwen完成相关性、日期核验和有界摘要；股票代码只能来自证据。Qwen不预测股票收益率，采集器不改全局Conda、Clash Verge或TUN。

后续 StockMixer + LLM 使用独立的离线新闻增强、FinBERT、历史证券/行业映射和三通道特征面板。训练时不调用Qwen，原始采集JSON只读，派生数据另存。本项目当前完成采集及数据接口，不代表融合训练已完成。

## 配置与运行

所有日常选项在 `config.yaml`：

| 配置组 | 用途 |
|---|---|
| monitoring | CN/US市场、directions、公司/股票、偏好及排除关键词 |
| collection | historical/realtime、日期、每批文档及查询预算 |
| search | Tavily/RSS、search/direct/hybrid、网站范围、SEC目录 |
| ollama | 本机模型、上下文、输出预算、每批调用上限 |
| fetch | 原有代理/Fake-IP、robots、PDF、字节及正文长度上限 |
| data_interface | Parquet目录、更新批量和间隔 |
| market_information | company/industry/market多层级；未知证券及行业不硬映射 |
| raw_content | 成功原文删除、待处理正文TTL和清理审计 |

`directions: all`不要求逐项填行业；include_keywords是检索偏好，不是强制命中条件。restricted模式按market_domains约束网站；unrestricted仍尊重排除域名及访问限制。SEC目录配置、全部股票和分页/断点见[美股SEC目录说明](US_DIRECT_DISCOVERY.md)。Tavily密钥放现有search.api_key；不要把密钥放提示词或日志。

```powershell
conda activate finance_crawler
cd D:\financial_data_project2
python main.py
```

也可使用环境解释器：

```powershell
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe D:/financial_data_project2/main.py
```

## 暂停与恢复

在项目目录、`finance_crawler` 环境中启动 `python main.py`。采集到一半时，另开一个终端运行：

```powershell
conda activate finance_crawler
cd D:\financial_data_project2
python pause_crawler.py
```

脚本先显示“已请求暂停”，等待当前下载/模型请求完成、缓存和断点提交、写入锁释放后才显示“已暂停”。采集进程会安全退出，终端可以关闭。正在进行的模型请求不会被强杀；长文可以在分段间暂停，实时轮询等待也能响应。默认等待确认最多300秒，超时请求仍有效，不代表已暂停；需要只发请求时用`python pause_crawler.py --no-wait`。

看到暂停确认后，恢复时运行：

```powershell
python resume_crawler.py
```

恢复脚本保留暂停前的运行方式，继续未完成的搜索/SEC目录和正文任务，复用兼容且未过期的缓存；它不是跳过搜索的`main.py --resume`。已提交的搜索和已完成记录不因暂停而重放，不清零文章失败次数，不更改来源时间。恢复后的新增计数按本次启动统计，全库总数另列。原始JSON只读边界不变。

暂停描述位于`.runtime/control/run.json`，请求位于`.runtime/control/pause.json`；实际搜索、目录、队列和模型缓存沿用`.runtime/simple/`。配置参数与暂停时不同则拒绝混用旧任务；注释或换行变化不影响恢复。暂停后直接运行`main.py`不会擅自覆盖暂停，须使用恢复脚本。恢复时若另一个进程仍持有写入锁，不会重复启动。

**首次使用：如果当前采集是更新前启动的旧进程，它未载入暂停控制。请在原终端按一次Ctrl+C正常停止，再用新版`main.py`或恢复脚本启动；之后即可使用这两个脚本。** 暂停脚本会识别这种情况并提示，不会强杀旧进程。没有新版暂停描述时，恢复脚本按当前config和已有持久化断点继续。暂停期间超过原有缓存TTL或修改代码/模型版本时，不兼容缓存会重新获取/提炼。

普通运行自动分批；历史完整模式继续有限检索计划，搜索上限是每轮预算，不是整段历史总请求数。失败候选本次不无限重试，Ctrl+C保留断点。没有可靠日期、正文乱码、访问受限或证据不合格的文章不会凑数保存。

历史相关性筛选按文章发布时点判断，公司、行业、市场级信息均可保留，不因“不是当前新闻”或没有具体证券代码而剔除。前置日期校验通过后，初筛会同时获得原始发布日期、配置时间窗和有界正文片段；正文提炼继续核验用途与事实。既有正式JSON不批量修改，过去已剔除的记录不自动重放。

已确认的401/403、登录或挑战页按主机暂缓；robots按原始规则匹配具体路径，不凭某一路径拒绝就封禁整站。规则暂存`.runtime/simple/source_access.json`，候选保留在原队列，不累计文章失败次数；后续同类暂缓不消耗文档或Qwen额度，可用正文缓存仍可提炼。配置在fetch下可选填写（不填均默认6小时）：

```yaml
fetch:
  access_cooldown_seconds: 21600
  robots_cache_seconds: 21600
```

冷却到期后，下次正常运行会重新核验访问条件，本轮不等待或自动反复尝试。代理、请求身份或相关调度配置变化会使旧调度缓存失效，但不会跳过实际访问/robots检查。首次确认限制的真实请求与直接暂缓分开计数，终端显示“来源暂缓汇总”。未改Clash/TUN或网站白名单。

```powershell
python main.py --validate-config  # 校验，隐藏密钥，不搜索
python main.py --preview-history  # 只读预览搜索/目录计划
python main.py --doctor           # 检查本机Ollama模型
python main.py --once             # 仅一个批次
python main.py --resume           # 跳过搜索，处理当前范围队列
python main.py --retry-failed-searches # 明确重置当前范围失败搜索后正常运行
```

不要在采集运行中移动、压缩后删除市场目录。当前data中的US.zip和GLOBAL.zip保持原样，程序不会自行解压；目录中的JSON与ZIP内新闻须分别核对，不能把剩余目录当完整语料。既有旧进程曾因运行中GLOBAL文件缺失而退出，见[接口实施报告第10节](市场信息数据接口改造报告_20261112.md)。本轮没有自动启动整轮采集。

## 正式记录与文件长度

```text
data/CN/*.json
data/US/*.json
data/GLOBAL/*.json
.runtime/simple/audit/<source_id>.json
structured/*.parquet
structured/manifest.json
```

每篇文章一个正式JSON，共11字段：source_id、url、title、时间、分类、关键词、主要内容、时间信息、股票实体、涉及行业、影响范围。兼容旧6/7/10字段和旧标题变体，不自动改写历史记录。title取原文，不经模型改写；无法取得为null。

文件名为 `原始时间_分类_主要产业_来源键.json`。有明确时刻用YYYY_MM_DD_HH_mm，仅日期用YYYY_MM_DD；同名追加稳定ID防覆盖。分类/产业片段有长度限制，原始秒数和时区保留在JSON内，原文标题不用于文件名。

摘要沿用中文200至350字、英文120至190词，短讯不凑数；校验还有混排/超长单词的实际字符上限。保留具体公司/股票、具体行业、事件时间或报告期、关键数字/单位/比较基准、条件和观点归属。超长请求修正，不硬截正文或删除事实。关键词单项和实体名称有界；恢复证券信息后再次检查正文长度。原文长文按模型预算无损分片，不因缩短提示词增大上下文或输出配置。

无ticker的公司仍保留名称，未知代码/交易所为null。影响范围是可同时存在的company/industry/market，与CN/US/GLOBAL地域不同。GLOBAL三份视图共用source_id，联合使用和Parquet导出均按ID去重。

正文分片、合并及最终结果增加数字证据检查：核对数值、币种/数量单位、百分比/百分点、正负号、可识别的财务指标、报告年份、公司归属和变化方向。允许等值单位换算及注明“约”的常规舍入；无依据数字请求现有修正，仍不通过则待复核。该规则能发现确定的冲突，复杂表格及省略主语的语义仍需抽检。

SEC默认读取主文档及同一accession中的EX-99/EX-13类文字附件，按财报/业绩等描述优先，每次最多3份。附件URL、类型、哈希、正文区间和超限未读清单放内部审计；公开日期统一绑定申报索引。选中的附件下载失败时整条待处理，主文档和附件共同受下载字节/正文长度预算约束。可在search.sec_edgar下设置include_exhibits（默认true）、max_exhibits_per_filing（默认3）。旧主文档缓存需重新获取，已保存历史JSON沿用只读规则。

Ollama网络、超时、408/429和5xx按原请求退避重试，默认最多3次、间隔1/2秒，Retry-After等待上限30秒；实际推理请求计入原有批次额度。ollama.service_max_attempts和service_retry_base_seconds可调整。服务重试耗尽时保留候选/缓存并暂停本轮模型处理，服务恢复后再次运行main.py续跑；服务故障不累计文章校验失败次数。输出格式/证据错误仍使用原有一次修正。

## 关系与时间接口

审计保留有界证据、已核验实体/行业、事件、行业经济机制、模型及已加载Prompt版本、哈希、实际提炼/提交时刻和清理状态。经济机制必须有驱动因素证据，不是股票涨跌标签；证据不足标记或留空，不按常识补ticker、行业代码或历史有效期。

Parquet包含article_metadata、article_entities、article_industries、article_events、article_industry_impacts、security_master和security_industry_history。后两张在没有可靠历史主数据时为空表；无映射实体为文章内unresolved ID，不能当永久证券身份或StockMixer数组下标。

Parquet在首批、累计50条更新或满300秒的批次边界导出；正常结束前强制提交剩余记录。阈值由data_interface.export_every_records/export_interval_seconds控制。无数据变化不反复重建全库，失败记持久化重建任务并限频重试。正式JSON及审计先提交，投影延迟不使原文长期保留。中断可通过outbox恢复；消费者使用read_export校验同一代文件。

```powershell
python main.py --export-parquet # 从正式JSON/匹配审计重建；不搜索、不调用模型
python main.py --cleanup-raw    # 持锁执行原文TTL/成功删除和旧错误全文脱敏
```

来源发布时间、首次发现、实际采集、提炼完成和feature_available_at分开。新版本实际可用时间不早于已知系统时刻。只有日期不猜时分，不从mtime造历史可用时间；cutoff必须严格晚于当前特征可用时刻。今天重新概括的历史文章不能当作当年已可用的PIT特征；Retrospective与PIT实验必须分别标记。

旧JSON批量升级、映射修复和摘要维护仅由明确维护命令触发，不用于普通采集或离线增强。下游清洗/推断结果写独立目录，不覆盖原始新闻JSON。

## 原文与缓存

### 导入补齐结果后的副本冲突

GLOBAL、CN、US中的同一source_id必须内容一致。如果只将data_enriched的GLOBAL和US补齐结果放回正式目录，CN仍是旧版，程序会拒绝启动，不会随意挑选版本。可使用显式维护入口：

```powershell
python repair_replicas.py --preview
python repair_replicas.py --apply
```

仅在补齐输出SHA-256、生成审计、原始记录及正文/来源日期全部匹配时修复；正文或发布日期冲突须人工核对。原始输入目录已移动时，必须有现存旧副本逐字节匹配原输入快照。apply持项目写入锁，先验证ZIP备份，再同步三副本及审计。新导入版本按本次提交时间标记可用，旧可用时间留在证据中；原始发布日期不变，不重新调用Qwen或搜索。

修复中断时重新执行apply恢复，普通采集不会自动覆盖未完成修复。记录不同、同目录重号或证据不足仍明确报错。2026-11-14已处理34条补齐版/旧版冲突，修复结果及备份位置见`logs/replica_repair_20261114_result.json`；真实库已加载1610条唯一记录。原始输入、data_enriched、配置与队列保持不变。

正式JSON、GLOBAL副本及审计共同可恢复提交、读回验证后，按精确URL/哈希删除对应页面和来源绑定模型缓存。不新建原文归档。失败待处理正文默认24小时TTL；拒绝/越界正文及时清理。完整模型请求/失败响应不写日志，保留状态、短原因与哈希。

成功后长期保留原文的开关不能开启；fetch.delete_raw_after_extract必须true，raw_content.retain_after_success必须false。清理失败有审计，可从删除后的manifest恢复审计；无法证明归属的用户文件不盲删。审计、队列/年度断点、SEC目录缓存、原始data与ZIP、必要备份不会当一次性文件删除。

## 检索与提炼效率

2026-11-14起，候选按年份、来源及长短文轮转，批次间保存调度位置。SEC长财报保留处理份额，短新闻、政策和行业消息能及时获得机会。这里的搜索年份仅用于调度，正式发布日期仍必须来自原文证据。

- 单片短文由同次正文提炼判断`useful`，省去独立初筛；长文保留初筛。日期、证券、数字和内容质量校验继续执行。
- 笔记按完整上下文及修复预算，最多四条一组合并，空间不足则用两条；重复笔记正文先去重。合并保留各笔记主要公司及其事项，实体和行业仍从已核验证据恢复。
- 正文/压缩缓存使用模型、提示词、请求、schema及相关语义代码指纹；完整代码版本另外审计。后续只改暂停、恢复、日志或调度脚本，不再使提炼缓存整体失效。本次切换缓存协议时旧协议结果不盲用，正式JSON不会重写。
- 超长正文和模型校验失败会保存等待条件；证据缓存或相关配置/处理版本未变时，跳过重复失败，不占文档或推理额度。条件变化自动恢复带有新等待标记的记录，保留失败历史；旧停试记录仍由明确维护命令恢复。需要主动再试当前待处理队列可用`python main.py --force --resume`，不要日常反复强制。
- SEC正文清理识别归档文档的非可见`ix:header`，保留可见财务正文、数字和公司名。仍超出配置上限的报告保持待处理，不能把截断文本冒充完整摘要。目录和正文缓存会按SEC解析版本重新核验。
- 日志增加条件等待、输入/输出token与模型请求耗时。Parquet错误记录阶段、表名、错误类型、errno和错误指纹，不保存来源行或敏感异常全文。RSS每轮按市场读取一次，不再让Qwen规划多个查询去重复读取相同订阅。

历史补查可在现有`search`配置组中增加：

```yaml
search:
  history_refinement_passes: 2  # 默认2；可填1至4
```

不要复制出第二个search组。第1轮保持原来的年度/月度任务顺序；后续月度轮换行业和来源，追加到原计划末尾，跳过完全相同的请求。本次核对（2026-11-14）的配置对应822个任务，原有662个已完成任务继续沿用；设为1则回到原697个任务的覆盖范围。此处是计划任务数，每轮实际请求仍受max_queries_per_cycle控制；空/重复结果的一次日期宽检索也计入请求预算。完整历史运行可能增加总搜索用量，不表示已经穷尽每个网站。

严格日期搜索没有新增候选时，允许一次有界宽检索补查；最终仍须核对原始发布时间。所有优化在下次正常启动/恢复时载入，不自动停止或重启已有进程，也不触发历史正式JSON迁移。

## 验证命令

```powershell
python -B -m unittest discover -s tests -v
python validate_market_interface.py --local-qwen # 合成原文+真实本机Qwen，隔离验证
python validate_market_interface.py --live-source # 一篇已知美联储页面，不调用搜索
python validate_market_interface.py --snapshot # 只读目录中正式JSON，隔离导出；不自动展开ZIP
```

验证结果放logs，隔离原文在finally中删除，样本不混入正式data。默认不开思考模式，优先串行推理。可重用验证入口和回归测试属于必要文件，单次演练脚本及过期演练副本可在核对清单后删除。

主要入口为main.py/simple_crawler.py；提示词在prompts/；范围发现见discovery.py/history_discovery.py/sec_discovery.py；来源与PDF见web_sources.py及finance_agent/；保存、关系、投影和清理分别见record_files.py、market_relations.py、structured_export.py和raw_cleanup.py。

当前接口、验证与待完成事项见[实施状态](IMPLEMENTATION_STATUS.md)。2026-11-13清理明细在[清理结果](logs/cleanup_20261113_result.json)，早期逐轮记录、旧说明版本及重复测试输出在[历史归档](archive/cleanup_20261113_history.zip)，可按原路径查找。原方案、模型对齐分析、接口报告及[效率与长度复核报告](爬取效率与长度复核_20261112.md)继续保留；历史段落不作为当前配置说明。


## 行业字典接口

新采集已支持同次Qwen请求中选择版本化行业代码，并保存原文和行业作用范围证据。配置、字典命名空间和下游契约见[INDUSTRY_DICTIONARY.md](INDUSTRY_DICTIONARY.md)。旧JSON不自动重写，字典不代替历史股票成员主数据。


## 2026-11-18 市场范围与历史新闻重新溯源

新增类型化市场范围、原文短证据包、8表导出、1948篇冻结语料的本地Qwen重新溯源和独立打包验收。正式11字段JSON保持不变；旧JSON只读，适配副本另存；未核验内容不自动扩大为行业/全市场影响。详见 [新闻数据契约与运行说明](NEWS_DATA_CONTRACT.md)。批处理状态以输出目录的coordinator_status.json及live_progress.json为准，不能把入口可运行说成1948篇已经完成。


## 2026-11-18 下游适配与放宽规则

见 [本轮上下游说明](NEWS_DOWNSTREAM_ADAPTATION_20261118.md)。市场/行业阈值0.75、三分类字典候选24；旧v3检查点保留，新v4独立处理。无日期行业参考仅用于显式回溯探索，不能声称真实历史全覆盖。


## 2026-11-18 分类整体替换修复

新版不再合并旧行业/范围标签，使用字典代码回填标准名称并重判三类范围。正式新批次为 `20261118_144829_US_GLOBAL_v5`，全量进度以活跃任务指针为准。详见 [保存规则与验收](NEWS_CLASSIFICATION_REPLACEMENT_20261118.md)。

# finance_crawler Agent V1

这是独立的新项目，代码、配置、数据库、原件、缓存和日志全部位于 D:\financial_data_project2。
核心流程：配置方向 → Qwen规划 → Python搜索 → 候选持久化 → Qwen初筛 → 按需获取正文 → Qwen客观事件提取 → 证据校验 → SQLite入库 → JSONL导出与股票查询。

Qwen仅提炼原文及归属明确的作者观点，不自行预测股价，不判断利好利空。
当前可查询结果是带证据的候选事件，needs_human_review=true、semantic_features_ready=false；不是已验收的训练特征。

## 快速运行

复用本机已有 finance_crawler 环境，无需向 base 环境安装依赖：

~~~powershell
conda activate finance_crawler
cd D:\financial_data_project2
python main.py --validate-config
python main.py --doctor
~~~

如未激活环境，使用完整路径：

~~~powershell
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe D:/financial_data_project2/main.py --validate-config
~~~

先编辑 config.yaml 的 monitoring.directions、markets、关键词和日期。所有配置未知字段会报错，YAML日期和代码要加引号。

Tavily搜索密钥直接填写在config.yaml的search下，其他字段保留：

~~~yaml
search:
  provider: "tavily"
  api_key: "你的真实密钥"
~~~

然后运行：

~~~powershell
python main.py --once
~~~

程序优先读取search.api_key，留空或仅空格时兼容TAVILY_API_KEY环境变量；无需每次打开终端重新设置。
配置预览、模型输入、配置序列化和正常运行日志不包含密钥，配置校验错误也隐藏输入值。
config.yaml现在可包含明文密钥，请勿分享该文件或将其提交到版本库；它已加入.gitignore（已被Git跟踪的文件不受忽略规则保护）。
.env.example仅说明兼容的变量名；不自动加载.env。没有密钥时会在搜索和规划前明确失败，不使用假搜索结果替代。
只测试本地规划、不搜索：python main.py --plan-only。
--dry-run只校验配置，不调用模型或搜索，不算端到端成功。

## 网站范围模式

config.yaml的search.website_mode有两个选项。unrestricted不限制来源网站，restricted按照market_domains中的市场列表限制来源。
当前默认restricted，CN包含巨潮资讯、雪球、中国证监会、财联社、上交所，US包含SEC、NASDAQ。
修改原配置中的下列字段，保留api_key、provider及其他配置。

~~~yaml
search:
  website_mode: "restricted"  # 改为unrestricted即可取消网站范围限制
  market_domains:
    CN:
      - "cninfo.com.cn"      # 巨潮资讯
      - "xueqiu.com"         # 雪球帖子
      - "csrc.gov.cn"        # 中国证监会
      - "cls.cn"             # 财联社
      - "sse.com.cn"         # 上交所
    US:
      - "sec.gov"           # SEC
      - "nasdaq.com"        # NASDAQ
  exclude_domains: []
~~~

在monitoring.markets填写["CN"]、["US"]或["CN", "US"]选择市场。CN查询只使用CN网站，US查询只使用US网站。
GLOBAL查询使用当前启用市场列表的并集，旧队列中缺少query_market的条目也使用该并集。
域名自动包含子域名，列表里可直接增删，不填写协议、路径或通配符。受限模式下每个启用市场必须至少保留一个有效域名。
exclude_domains在两种模式下都生效，可用于排除特定子域名。旧include_domains配置仍兼容，启用新模式时应删除或清空旧字段。
修改后保存并重新运行程序。长时间运行的实时进程需重启，配置在启动时读取。

范围同时用于Tavily搜索、RSS订阅及条目筛选、正文请求和重定向检查，现有队列在处理前也会校验。
范围外的待处理条目标记out_of_scope并保留原处理阶段，重新允许来源后会恢复。实时模式重启也会进行范围检查。
logs中的search_requests记录每条查询的market和实际include_domains，便于核对。
当前列表控制允许的来源范围，服务返回顺序决定候选顺序，不保证每轮覆盖所有网站或完整历史。
雪球等站点的登录、验证页以及网站未被索引的内容仍会影响可采集范围。

域名已核对中国证监会 https://www.csrc.gov.cn/ 、财联社 https://www.cls.cn/ 、NASDAQ https://www.nasdaq.com/ 官网。

## 历史与实时模式

默认historical读取start_date/end_date。日期约束由Python传入搜索服务，并在取得正文时间后再次检查。
Tavily日期是Provider估计，可能是更新时间；没有原文发布时间时保持unknown，不把摘要或Provider估计冒充来源证据。
搜索只覆盖Provider可见的有限结果，不承诺全网历史覆盖。Tavily请求会将结束日后移一天，随后以原文日期按配置的含首尾窗口过滤。

实时模式将以下字段改为：

~~~yaml
collection:
  mode: "realtime"
  start_date: null
  end_date: null
  poll_interval_seconds: 300
~~~

其余collection字段保留，运行python main.py。
实时发现调度与正文/提炼消费通过独立线程、独立SQLite连接运行；模型调用使用共享串行锁。
较长下载和模型积压不要求每轮全部清空。模型单次超时、共享调用预算和服务速度仍会影响实际延迟，300秒不是硬实时保证。
队列达到max_pending_candidates（默认5000）时暂停新搜索，优先消化积压。
进程只能同时运行一个写入实例；Ctrl+C后保留队列和缓存，重启会恢复screening/fetching/extracting状态。

每轮默认预算：2条查询、每次10个候选、每批3个初筛、最多5份正文和5篇提炼、每篇新增8块、最多30次真实模型调用（含修复）。
实时模式模型预算按poll_interval_seconds时间窗口共享；模型始终并发1。
所有补充搜索也计入max_queries_per_cycle；初期max_followup_search_rounds=0。
完整搜索计划缓存1800秒，已成功查询按检查点限频。不要把轮询当作无限扩大检索量。

## 队列阶段与续跑

~~~powershell
python main.py --stage discover --once
python main.py --stage process --once
python main.py --extract-only
python main.py --export
~~~

discover只做规划和发现，process消费已保存的候选、正文与提炼队列。
--extract-only是process的便捷入口，不调用搜索服务；队列中的approved条目仍需按需下载正文。
已完成的同版本分块复用经过校验的缓存；超过分块或模型预算则保留fetched状态，后续续跑。
验证/403、缺页、找不到原件等保留明确状态；不会通过改Cookie、签名或代理绕过访问限制。
network_error与rate_limited有有限跨轮重试，尊重可解析的Retry-After；超过次数进入needs_review。

正文提取中的subject与event_time_raw要求逐字证据。相对时间保留“11月1日”“周一”等原文，不补全年份或转换ISO日期。
校验错误会指明events数组中的位置、字段和问题值，供本地模型的一次修复重试使用。
两次尝试后仍有证据错误时，程序逐条重新校验，保存合格事件，其他候选完整保留在运行报告extractions[].rejected_events中。
只要存在未通过事件，该记录保留needs_review状态；即使全部候选被拒绝，也不会标记为done。
事件入库和待复核状态在同一个SQLite事务中提交。已验证分块及待复核信息可复用缓存，避免重复推理。


## 本机代理与Fake-IP

本机已核对Clash Verge的HTTP混合代理端口为7897，TUN和Fake-IP开启，网站DNS返回198.18.x.x。
当前config.yaml已设置以下两项，运行时需要本机代理服务处于启动状态。

~~~yaml
fetch:
  proxy_url: "http://127.0.0.1:7897"
  allow_fake_ip: true
~~~

proxy_url仅接受显式的本机HTTP代理地址。allow_fake_ip要求配置该代理，允许域名解析结果中的198.18.0.0/15通过验证，连接交给本机可信代理。
本机代理负责后续域名解析与出口路由，因此该开关要求信任代理配置。网站范围、重定向、robots、TLS证书和访问限制检查仍适用。
回环、私网、链路本地、直接IP形式的198.18地址，以及混合私网DNS答案仍会拒绝。
关闭本机代理并恢复普通DNS后，可将proxy_url设为null、allow_fake_ip设为false。
如果关闭Fake-IP兼容却检测到198.18地址，会报告含域名和解析地址的network_error，避免把临时DNS问题永久标记为网站访问封禁。
旧版通用地址拦截产生的blocked任务会在处理队列前重新验证，符合新配置的条目恢复为approved。HTTP 403、验证页等封禁状态保留。
处理已有任务可运行python main.py --extract-only，继续采集新内容运行python main.py --once。

代理与Fake-IP说明依据Mihomo官方DNS文档 https://wiki.metacubex.one/config/dns/ 和HTTPX代理文档 https://www.python-httpx.org/advanced/proxies/ 。

## 正文与Crawl4AI

HTTP客户端先检查公网URL、robots、大小、重定向及验证页面；会话内复用连接。
HTML取得后由Crawl4AI LXML清理策略处理，不启动网页脚本执行；保留lxml回退及质量标记。
因此无需把所有搜索候选打开成浏览器页面。当前V1没有动态JS渲染和登录会话导入。
PDF重用原工程的版面恢复/表格保留代码；pypdf已复制在vendor，Poppler优先。
OCR默认关闭，开启时仅使用本机Windows中文OCR，结果仍为ocr_unverified；缺页不进入完整事件提取。

搜索返回正文、HTML、PDF都只是原始材料；源摘要不自动变成最终事实。
证据校验覆盖数字、实体名称、证券代码、币种、报告期、事件状态与段落编号。
模型漏引但原文当前批次存在精确名称/代码时，程序可补齐引用，并保留evidence_repairs；这不等于验证了公司—证券语义关系，仍需审核。
程序不能修复原文没有的数字；来源、证据和主数据都不能证明模型概括绝对正确。

## 本地合法样本导入

项目内JSON至少包含source_url、title、text或paragraphs；可提供published_at或published_date、quality。
模型不读取并执行文件中的指令，导入数据不会伪装成刚刚联网下载。

~~~powershell
python main.py --import-document D:/financial_data_project2/samples/your_document.json
python main.py --extract-only
~~~

请将文件放入项目内，设置与原文相符的日期窗口。窗口外材料会归档并标out_of_range，缺页标needs_ocr。
重新导入不改变首次发现时间，同内容版本不重复生成事件。

## 存储与查询

SQLite是事务主库；JSONL是可重建导出，避免双写失败造成主数据不一致。
默认位置：

~~~text
financial_dataset/
  events.sqlite3
  raw/                 内容哈希命名的HTML/PDF/导入原件
  events/
    CN/YYYY-MM-DD.jsonl
    US/YYYY-MM-DD.jsonl
    GLOBAL/YYYY-MM-DD.jsonl
logs/                  运行报告与验证结果
.runtime/              模型缓存、搜索响应、失败原件、OCR与临时文件
data/security_master.json
~~~

没有证券明确提及的政策保留GLOBAL，不擅自关联行业股票。跨市场事件保留多证券关系。
documents与event记录有稳定ID，修订产生新版本；事件与证券是多对多关系。
相同文档不重复入库。事件级仅做保守的结构化精确分组，保留独立来源记录，不声称已解决任意改写新闻的语义聚类。

~~~powershell
python query_events.py --symbol 600519
python query_events.py --stock-id CN:SSE:600519
python query_events.py --company 贵州茅台
python query_events.py --market US --symbol NVDA
python query_events.py --direction 半导体
python query_events.py --event-type policy
python query_events.py --asof "2026-11-05T10:00:00+00:00" --deduplicate
~~~

--asof按事件提炼实际可用时间过滤。按来源发布时间过滤用--start/--end。
检索公司名以记录的明确提及名称为准；别名在提取时通过证券主数据消歧，不做模糊猜测。
--deduplicate仅按event_group_id分组，合并来源列表，时间过滤先于分组。

## 时间与证券主数据

first_seen_at只表示发现候选；document_available_at表示取得当前正文；extraction_available_at/available_at表示提炼完成。
后两者绝不提前到标题搜索时间。历史回补不能按旧发布日期直接当成过去可用的特征。
只有日期时保留date精度，没有时间时不补00:00；未知来源时间进入候选并标不可用于历史发布日期对齐。
交易日历聚合、OHLCV拼接、数值特征表和预测模型不在当前实现内。

data/security_master.json初始复用原工程已核验的巨潮代码—名称快照。
data/security_master_provenance.json保留来源URL、哈希和2026-10-29的观察时间。
valid_from是映射开始观察日期，不是上市日期；listing_status=unknown，不能据此认定股票当前可交易。
早于快照的历史映射保持unresolved；美股主数据当前未提供，明确代码仍保留，无法确认不编造stock_id。
补充主数据时每条至少提供stock_id/market/symbol/company_name/aliases/valid_from/valid_to/source。
原文没有提及的证券不会因为主数据或行业常识被加入。

## 测试与验收

~~~powershell
python -B -m unittest discover -s tests -v
~~~

测试使用显式合成夹具，写入.runtime/tests的隔离目录，不调用真实搜索API，不向正式事件库写假新闻。
真实本地规划：logs/local_planner_validation.json。
真实已保存公告提炼：logs/local_extractor_validation.json（运行完成后生成）。
尚未配置真实Tavily密钥时，Tavily真实搜索→抓取→提炼端到端测试尚未验证。
尚未完成长期运行压力测试、人工标注召回率/事实准确率验收；没有凭空报告精度。
详细状态见IMPLEMENTATION_STATUS.md。

应用层URL/DNS检查不能代替生产网络的出网隔离；未部署服务或对外开放端口。

## 退出码

0：本轮自动任务正常完成；待人工复核和待OCR数量单独显示，不会仅因历史库存返回非零退出码。
2：仍有自动任务、预算暂停或本轮部分失败。
1：配置/凭证错误、阶段级错误或本轮没有进展的失败。130：手动停止。

status表示本轮执行状态，needs_review_count/needs_ocr_count表示仍需人工处理的数据质量事项。
只剩人工复核或OCR任务时，普通运行不自动重复推理，这些记录继续保留对应状态。
相同查询在collection.poll_interval_seconds间隔内会跳过，默认300秒；终端和search_skips会说明跳过原因与剩余等待秒数。
终端同时显示本轮事件数、库中事件总数、错误数、自动待处理数量和运行报告路径。

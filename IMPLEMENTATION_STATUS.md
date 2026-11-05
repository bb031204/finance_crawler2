# 实施与验收状态

实施位置：D:\financial_data_project2。
原方案文件保留；原项目D:\financial_data_project只读复用了解析代码、依赖和合法保存样本。

## 已实现

| 能力 | 实现与验证 |
| --- | --- |
| 分市场网站范围 | unrestricted/restricted两种模式；CN五个来源、US两个来源；搜索/下载/重定向与旧队列一致限制；范围扩大后恢复原阶段；离线测试通过 |
| 严格配置与路径约束 | 已实现；日期、模式、预算、重复键、项目根路径均有测试 |
| Qwen动态规划 | 已实现；真实本机推理生成2条合法查询 |
| Tavily Provider | 已实现真实HTTP接口；Mock验证载荷、限制和密钥隔离；支持search.api_key优先、环境变量回退；实际搜索记录已核对；见logs/ed786a93c0944d5488e8b90f43f2db08.json |
| RSS Provider | 已实现；标准RSS格式夹具测试通过；未报告真实联网RSS测试成功 |
| 相关性初筛 | 受约束JSON；候选ID覆盖检查；官方重要披露保守保留；拒绝理由持久化 |
| 正文获取 | 公网URL/robots/重定向/验证页/大小与时间预算；HTTPX连接复用 |
| Crawl4AI | 使用LXML清理HTML，真实安装SDK的本地HTML清理验证通过 |
| PDF/OCR | 复用原工程并适配新根目录；真实公开PDF解析及关键数字检查通过；OCR代码存在，默认关闭，本项目未重新做真实扫描件OCR验收 |
| 事件提炼 | 支持多事件、阶段、主体、数值原文/单位/期间及多证券；真实本地样本生成1条事件 |
| 证据校验 | 数字、名称、代码、币种、单位、日期/期间、阶段及引用；仅精确字面补齐跨段名称/代码证据 |
| 证券主数据 | 6185条已验证巨潮代码—名称观察快照；有效期从观察日开始；不等于当前上市状态；没有伪造美股映射 |
| 时间可用性 | 发现/正文/提炼可用时间独立，提炼结果不会回填到搜索摘要首次发现时点 |
| SQLite与JSONL | 事务主库、可重建分区JSONL，事件/证券多对多查询 |
| 去重与修订 | URL、源记录、内容哈希与版本幂等；精确保守事件分组；修订与重见保留来源 |
| 历史/实时 | 指定日期与独立发现/消费队列；共享串行模型锁和窗口预算、持久化恢复、有限重试、积压背压 |
| 查询 | 名称、symbol、stock_id、市场、方向、类型、日期、asof及精确分组 |
| Windows命令 | 主入口、配置检查、dry-run、缺密钥行为、查询均已执行验证 |
| 自动化测试 | 84项离线测试通过；不消耗真实搜索额度，不向正式库写夹具新闻 |

## 真实验证边界

- logs/doctor.json：本机服务及依赖，不代表推理。
- logs/local_planner_validation.json：真实Ollama规划推理。
- logs/local_extractor_validation.json：真实本地模型响应、原文证据、1条事件及Crawl4AI清理结果。
- logs/local_pdf_validation.json：真实原件哈希、页数、段落、关键数字。
- logs/implementation_validation.json：最终测试、入口、缺密钥及查询验收记录。
- 本地样本与派生事件隔离在.runtime/validation，未冒充新下载新闻或真实搜索结果。

真实Tavily搜索→网页→提炼端到端测试尚未完成。本次网站范围改动使用Mock验证，实际配置入口验证通过。
在config.yaml的search.api_key填写真实密钥后运行python main.py --once即可执行实际闭环；留空时仍兼容TAVILY_API_KEY环境变量。必须检查logs中的真实返回与状态。
尚未做长期实时运行压力/延迟测试、人工标注召回率和语义准确率评测；不声称已达到生产部署或训练数据质量标准。

## 与原方案相比的具体调整

1. 以受约束JSON规划而非依赖模型自主调用工具，执行权限仍在Python。
2. Tavily单次结果上限20，默认10；跨轮补充搜索与重试计入预算。
3. num_ctx/num_predict代替字符上限；默认4096上下文、关闭思考、模型串行。
4. SQLite为事务事实库，JSONL作为导出，不采用无法原子双写的双主存储。
5. 可用时间以实际正文与提炼完成时间为准。
6. 预计统计数用reported，不冒充completed；源数字不存在时仍拒绝。
7. 事件级去重仅保守精确分组，任意改写文章的语义聚类不宣称已完成。
8. 泛化来源支持HTML/PDF/RSS；专门SEC/交易所/社区适配器、JS渲染和登录访问作为后续扩展。
9. 当前输出grounded_candidate，保留needs_human_review；没有构造利好利空、股价预测或直接训练特征。
10. 默认配置为2026-11-01至2026-11-05；运行其他日期前请修改config.yaml。

## 下一次运行

~~~powershell
conda activate finance_crawler
cd D:\financial_data_project2
# 先在config.yaml的search.api_key填写真实密钥
python main.py --once
~~~

只检查配置：python main.py --validate-config。
只处理已有队列：python main.py --extract-only。
详细参数、存储和人工评估见README.md、EVALUATION.md。

## 2026-11-05 网络兼容实测

已确认本机Clash Verge启用TUN/Fake-IP，公开网站解析为198.18.x.x，原DNS公网检查阻止了正文下载。
已在本项目fetch配置启用显式本机HTTP代理http://127.0.0.1:7897及allow_fake_ip。
69项离线测试通过，配置入口校验通过。只恢复并处理已有队列，未新增Tavily查询。
真实运行logs/679d8ff4bd8f4b568ecf9dfb4f84fbf0.json恢复5个旧DNS拦截任务并下载5个网页，本地模型调用18次，生成事件0条。
其中3条因证券代码、日期、主体证据校验失败而保留fetched；2条返回空事件，保存的内容包含列表页或导航。
当前仍需改善正文定位、来源发布日期识别和模型证据引用，不能把网络修复等同于语义验收完成。
详细记录见logs/network_compatibility_validation.json。

## 2026-11-05 逐事件证据校验与部分成功状态

已核对用户运行的11条入库事件。原失败原因包括一次来源连接超时、模型补全年份和改写主体名称。
提示词已明确逐字主体与事件时间规则，校验失败反馈定位到events数组中的字段和值。
模型两次尝试后逐事件校验，合格事件入库，失败候选和原因保留在运行报告extractions[].rejected_events。
存在未通过事件时，该文档与合格事件在同一事务中提交为needs_review，缓存保留待复核信息。
部分成功返回partial/退出码2，阶段级错误或没有进展的失败保留failed/退出码1。
79项离线测试及实际配置入口验证通过。只处理已有内容，未新增Tavily查询。
真实验证期间新增38条候选事件，当前SQLite与JSONL共49条，原有记录完整保留。
最终队列done=6、needs_review=4、rejected=1，没有剩余approved或fetched。证监会超时来源已下载成功。
4篇文档仍含待复核候选，未知发布日期的事件继续保留unknown分区。尚未完成人工语义准确率验收。
详细验证见logs/extraction_feedback_validation.json，两个有预算的验证轮次见logs/partial_event_validation_live.json和logs/partial_event_validation_followup.json。

## 2026-11-06 本轮状态与人工复核库存

已核对00:00运行报告，errors为空、search_skipped_recent=2，未调用模型；原partial来自历史needs_review库存。
运行状态现仅由本轮错误、自动队列和预算决定，人工复核/OCR数量独立显示，保留对应数据质量状态。
相同查询300秒内跳过时，终端打印原因和剩余等待时间，报告search_skips保留详情。
84项离线测试通过，包含完整主程序的双查询跳过和5篇待复核场景。
真实main.py --extract-only验证退出码0、status=completed、库中62条事件、待人工复核5篇、自动队列0、错误0。
本次未新增Tavily搜索，也未调用模型。记录见logs/idle_status_validation.json。

# Qwen 主导的金融信息采集

在 `D:\financial_data_project2` 使用原有 `finance_crawler` 环境。正式结果只有 `data/CN.jsonl`、`data/US.jsonl`、`data/GLOBAL.jsonl`，每行是一篇原文的摘要。

## 运行

先修改 `config.yaml` 的市场、方向、公司、日期和网站。原有 Tavily 密钥与本机代理配置已保留。

```powershell
conda activate finance_crawler
cd D:\financial_data_project2
python main.py
```

也可直接运行：

```powershell
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe D:/financial_data_project2/main.py
```

历史模式执行一轮；实时模式按配置间隔循环，Ctrl+C 停止。

```powershell
python main.py --validate-config  # 检查配置，隐藏密钥，不进行搜索
python main.py --doctor           # 检查本地 Ollama 是否有 qwen3:8b
python main.py --resume           # 只继续待处理任务，不新增搜索
python main.py --reprocess-saved  # 重新评估缓存正文；仍受本轮篇数/调用上限限制
```

正常运行会先搜索，再处理队列；每轮最多处理 `max_documents_per_cycle` 个候选，包括被跳过的候选。未处理完的自动保留，下一次可用 `--resume`。已保存的 URL 不重复调用 Qwen。`--force` 会强制重新判断并下载本轮候选，通常不需要使用。

## 配置

- `monitoring.markets`：`["CN", "US"]`，可只保留一个市场。
- `monitoring.directions`：如 `["银行业", "半导体"]`。`securities` 可填写关注的股票/公司；空列表表示按方向搜索，不是采集全市场全部股票。
- `include_keywords` / `exclude_keywords`：提供给 Qwen 的筛选偏好。
- `collection.mode`：`historical` 指定起止日期，格式 `"2026-11-01"`；`realtime` 时两项日期设为 `null`。
- `max_queries_per_cycle`：每轮搜索次数上限，至少等于启用市场数。CN 使用中文查询，US 使用英文查询。
- `max_candidates_per_query`：每次最多返回多少条搜索结果。`max_documents_per_cycle`：每轮最多评估多少个候选。
- `search.provider`：默认 `tavily`，密钥填在 `search.api_key`；可改 `rss` 并填写订阅地址。Tavily 搜索与本地模型运行是不同组件。
- `search.website_mode`：`restricted` 按市场限制域名；`unrestricted` 不限制网站。`exclude_domains` 两种模式都生效。
- `search.market_domains.CN`：当前为 `cninfo.com.cn`、`xueqiu.com`、`csrc.gov.cn`、`cls.cn`、`sse.com.cn`。US 为 `sec.gov`、`nasdaq.com`。可直接增删域名。
- `ollama`：本地 `qwen3:8b`，默认关闭思考，4096 上下文，串行处理，适合先在 8GB 显存环境验证。长文分段概括再合并，不逐句生成记录。每轮调用次数由 `max_calls_per_cycle` 控制。
- `fetch.proxy_url` / `allow_fake_ip`：保留已有本机代理和 Fake-IP 兼容方式；不会修改 Clash Verge 或 TUN 配置。

限制网站表示搜索和下载只允许这些域名，不保证网站开放全部内容，也不等于逐站穷尽所有历史公告。登录、验证码、robots 或网络拒绝会留在待处理日志中。

## 模型负责什么

`Qwen 搜索规划 → 搜索结果 → Qwen 判断是否下载 → Crawl4AI/HTTP 或 PDF 读取 → Qwen 判定用途、来源发布时间、分类、关键词、段落摘要 → 六字段 JSONL`。

提示词集中在 `qwen_agent.py`：`PLAN_PROMPT`、`SCREEN_PROMPT`、`READ_PROMPT`、`MERGE_PROMPT`。
Python 负责执行请求、限制范围与调用次数、缓存、去重和保存，不再要求 Qwen 填写数值事实表、主体映射、逐句证据块或预测分数。Qwen 可以概括有归属的专家观点，不自行预测股价。

HTML 优先保留主文章及其嵌入式正文，并处理 UTF-8/GBK 编码。PDF 先恢复可读文字，再交 Qwen 整理；没有可读文字的扫描件当前留待处理，不冒充已经读完。无法读取的乱码不会因为交给模型就自动变成可信信息。

## 六字段数据

```json
{"source_id":"由规范化URL生成的稳定ID","url":"原始文章URL","时间":"2026-11-02T20:30:58+08:00","分类":"监管政策","关键词":["政策主题","行业或公司"],"主要内容":"此处为字段格式示意。实际文件中的内容必须来自对应原文，由Qwen写成连续段落。"}
```

上述仅示意格式，不会写入正式文件。`source_id` 用于 URL 去重，不是股票编号。正式数据没有额外的 evidence、paragraphs、semantic_candidate、ready 等字段。

`时间` 只记录原始来源的发布日期/时刻。只有日期则保存 `YYYY-MM-DD`，不伪造零点或分钟；模型确认了可靠时刻则保留明确时区；仅确认到日期时只保存日期。事件发生日与政策生效日可写入摘要，不另设重复时间字段。模型内部需要引用来源日期以检查是否臆造，但该检查信息不会出现在正式六字段中。无法确认发布日期的文章不会进入正式 JSONL。

一篇原文合为一条摘要，同一 URL 更新后可替换原记录。不同网站转述同一新闻仍可能各有一条，当前没有做跨来源的语义去重。

这些是可读取的文本输入，不是已经验证过的涨跌标签。纯数值时序模型仍需将摘要编码为文本特征，并与股票及交易日对齐；历史来源日期也不证明该条摘要在当时已经生成。

## 文件与日志

```text
main.py              # 唯一运行入口
config.yaml          # 统一配置，包含本机密钥，不要分享
qwen_agent.py        # Qwen 提示词与调用
web_sources.py       # 搜索、网页和 PDF 阅读
simple_crawler.py    # 队列、去重、JSONL 保存
finance_agent/       # 少量公共网络、PDF 和运行环境工具
vendor/              # 原有 PDF 依赖，无需重装
windows_ocr.ps1       # 旧 PDF 工具保留的辅助脚本，默认不启用 OCR
data/
  CN.jsonl
  US.jsonl
  GLOBAL.jsonl
logs/
  latest_run.json    # 当前范围、统计、错误、Qwen 调用次数
.runtime/simple/    # 待处理任务、正文缓存、模型缓存，不是模型输入数据集
archive/             # 重构前的完整备份，不再参与运行
```

终端显示搜索词、市场、日期范围、返回数、每篇筛选理由、正文获取方式、Qwen 调用进度和最终汇总。`partial` 表示仍有待处理任务；可读的单篇失败不会终止整批。配置或程序级错误才返回退出码 1。

离线测试：`python -B -m unittest discover -s tests -v`。夹具只写入 `.runtime` 下临时目录，不混入正式数据。真实验证结论见 `IMPLEMENTATION_STATUS.md`。

旧代码、旧数据库、原件及旧配置完整保存在 `archive/legacy_20261105_173510.zip`，有 SHA-256 校验清单。旧的事件文件不作为新格式数据直接导入；已经保存的原网页交给新 Qwen 流程重新判断和概括。备份含本机旧配置和密钥，同样不要分享。

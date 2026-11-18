---
name: run-financial-data-project2
description: 运行、启动、重启、停止金融新闻采集器（financial_data_project2），查看采集状态与日志，校验配置，预览历史搜索计划，核验/恢复 data 归档，重建 Parquet 投影，运行离线测试。用户要求运行采集器、看采集进度、恢复数据或跑测试时使用。
---

# 运行 financial_data_project2 采集器

Qwen 主导的金融新闻历史采集器：搜索/SEC 目录发现 → 正文获取与日期核验 → 本机 Ollama（qwen3:8b）同次摘要与关系提取 → 11 字段正式 JSON + 审计 sidecar（data/CN、data/US、data/GLOBAL）→ 每批 Parquet 投影（项目根 `structured/`，7 张表 + manifest）。

无 GUI；采集器是长驻批处理进程，交互面 = CLI 维护命令 + 日志 + 状态文件 + 写入锁。**一律通过驱动脚本操作**（路径相对项目根 D:\financial_data_project2）：

```powershell
$py="C:\Users\marti\anaconda3\envs\finance_crawler\python.exe"
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py <子命令>
```

子命令：`check`（0=可启动 2=被阻断）、`status`、`tail`、`validate`、`preview`、`restore`、`launch`、`doctor`、`parquet`、`tests`、`stop`。

## 环境前提

- **Python**：必须用上方 finance_crawler 环境的 python.exe 全路径。裸 `python` 命中 Windows Store 存根：exit 49 且无任何输出。
- **Ollama**：本机服务 http://localhost:11434，模型 qwen3:8b；`driver.py check` 会探测 /api/tags。
- **网络**：config.yaml 指定本机代理 http://127.0.0.1:7897（Clash/Fake-IP，勿改）。
- **pyarrow/pypdf**：位于项目 vendor/ 下由代码自举（`structured_export.arrow()` 注入 sys.path）；裸环境 `import pyarrow` 失败是正常的，**不要** pip install 到全局环境。

## 运行（agent 路径）

自检与观察（只读，随时可运行）：

```powershell
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py check
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py status
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py tail -n 40
```

配置校验与历史计划预览（不联网、不占写入锁）：

```powershell
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py validate
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py preview
```

数据归档核验（干跑：CRC 校验、逐字节比对、冲突检测，不写入）：

```powershell
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py restore
```

`restore --apply`（本会话未执行）：在无采集器运行且写入锁空闲时，把 data/US.zip、data/GLOBAL.zip 内缺失的正式 JSON 恢复回原目录；不覆盖任何已有文件，zip 保留作备份。恢复后 `check` 的归档守卫解除。

启动（先干跑核对全部守卫）：

```powershell
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py launch --dry-run
```

守卫全部通过后去掉 `--dry-run` 实际启动（后台、无窗口，日志写 `logs/driver_main_<时间戳>.log`）。启动机制与 run_after_current.py 完全相同（Popen + CREATE_NO_WINDOW + 日志重定向；本会话 01:38 曾由 run_after_current.py 实际启动采集器并运行 56 分钟）；本会话因采集器运行中且归档未恢复，验证的是守卫拒绝路径。

维护命令（须写入锁空闲；见 Gotcha：运行中的采集器整个进程生命周期持锁）：

```powershell
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py doctor
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py parquet
```

采集器运行时两者打印 `[拒绝] 采集器正在运行` 并 exit 2（本会话即此状态）。锁空闲时：doctor = 完整构造 Crawler 并实测 Ollama；parquet = 只读正式 JSON 及匹配审计，重建 7 张 Parquet 到 `structured/`，不调模型、不搜索。

测试（446 项离线测试，约 42 秒；完整输出写 `logs/driver_tests_<时间戳>.log`）：

```powershell
& $py -X utf8 .claude\skills\run-financial-data-project2\driver.py tests
```

等待现有采集器自然结束后自动重启一次（避免双跑；状态写 `.runtime/restart_after_current.json`）：

```powershell
& $py -X utf8 run_after_current.py --target-pid <pid> --target-created <unix创建时刻>
```

停止：`driver.py stop` 是 taskkill 强杀，**仅在用户明确要求时**使用（须加 `--yes`）。停止/重启前先与用户确认；等待不等于已重启。

其余维护子命令（`--cleanup-raw`、`--upgrade-records`、`--repair-mappings`、`--clean-summaries`、`--retry-failed-searches`、`--once`、`--resume`）均须写入锁空闲，语义见 main.py 的 argparse help 与 AGENTS.md；本会话未运行。

## 人工路径

conda activate finance_crawler 后前台运行 `python main.py`，Ctrl+C 优雅停止。注意：这样启动的进程控制台输出**不落盘** logs/（logs/ 里只有 run_after_current/driver 启动的运行），观察只能靠 `driver.py status` / `tail` / logs/latest_run.json。

## Gotchas

- **归档地雷（2026-11-12）**：用户在 WSL 侧把 data/US（1175 条）、data/GLOBAL（34 条）打包为 data/US.zip、data/GLOBAL.zip 并删除了目录内 JSON（zip 保留）。当时运行中的采集器在下一批 migrate 读回时 FileNotFoundError 崩溃（02:35:05，缺 data/GLOBAL/2012_03_15_Cybersecurity_Renewable-Energy_bloomberg.json）。新进程不会立即崩溃，但 Dataset 只能看到未归档记录。driver 的 `check`/`launch` 硬阻断：zip 内 JSON 数 > 磁盘现存数即要求先 `restore --apply`。GLOBAL.zip 34 条与 data/CN 副本逐字节一致（已核验）。
- **写入锁**：main.py 用 `with Lock(ROOT)` 包住整个运行期（`.runtime/writer.lock`，msvcrt 非阻塞）。采集器活着 = 锁被占，doctor/parquet 等维护命令会立即 RuntimeError"另一项目写入进程正在运行"。
- **Parquet 输出位置是项目根 `structured/`**（config `data_interface.directory`），不是 `data/structured/`。采集时每累计 50 条或 300 秒自动投影，正常结束前强制提交。
- **测试须 UTF-8 模式**：本机中文 Windows 默认 GBK，test_market_interface.py 两处 `read_text()` 未显式 encoding，不带 `-X utf8` 跑会出 2 项 UnicodeDecodeError；`driver.py tests` 已内置该参数。
- **latest_run.json 是中途也更新的快照**：`batches` 为 null 表示批次进行中，不是最终汇总；最终汇总才有完整统计。
- **预算语义**：每批最多 10 篇文档 / 30 次 Qwen 调用（`model_calls` 到 30 即换批），缓存命中不计调用；单篇最多 3 次续行。
- **禁止为只读分析构造 `record_files.Dataset`**——其构造函数会恢复 outbox 事务（副作用）。数记录用 glob；安全读法是 `structured_export.snapshot()`。
- **密钥**：config.yaml 的 search.api_key 是真实 Tavily key，任何情况下不得打印/写入日志。`--validate-config` 输出已实测脱敏（SecretStr 字段完全不出现）；`driver.py validate` 额外断言输出无 `tvly-` 模式后才打印。
- **state.json 约 10MB 全量重写**（records 状态字典 + pending 候选），读取它做统计没问题，但别频繁全量 dump。
- 直接（终端）启动的采集器日志只在其控制台；`driver.py tail` 只覆盖 run_after_current/driver 启动的 logs/restarted_main_*、driver_main_*。
- 重启协议：等待用 run_after_current.py（waiting/waiting_for_writer/running/finished/failed/superseded 状态在 .runtime/restart_after_current.json）；**不要**在采集器运行时另启第二个。

## Troubleshooting

- `UnicodeDecodeError: 'gbk' codec`（测试）→ 用 `driver.py tests`（内置 -X utf8）。
- 采集器崩溃 `[Errno 2] No such file or directory: 'data\GLOBAL\...json'` → 正式 JSON 被外部删除/归档。`driver.py restore` 核验后 `restore --apply`，再启动。
- doctor/parquet/launch 打印 `[拒绝] 采集器正在运行` → 预期行为。用 `status`/`tail` 观察，或 run_after_current.py 排队等待。
- python 命令 exit 49 且无输出 → Windows Store 存根；用环境全路径。
- Ollama 不可达（check 报错）→ 确认本机 Ollama 服务后再试；doctor/采集都依赖它。

"""run-financial-data-project2 的交互 harness。

用法（必须用 finance_crawler 环境的 Python 运行，见 SKILL.md）:
    python -X utf8 .claude/skills/run-financial-data-project2/driver.py <子命令> [选项]

子命令:
    check              环境自检（Python/Ollama/配置/归档守卫/采集器/写入锁），exit 0=可启动 2=被阻断
    status             采集器进程、等待任务、latest_run.json、数据与 Parquet 投影状态
    tail [-n N]        最新采集器日志尾部（restarted_main_* / driver_main_*）
    validate           main.py --validate-config（先断言输出无密钥再打印）
    doctor             main.py --doctor（需要写入锁空闲，被占用时快速拒绝）
    preview            main.py --preview-history（离线历史搜索计划，不联网不调模型）
    parquet            main.py --export-parquet（只读重建7张Parquet，需要写入锁空闲）
    restore [--apply]  data/US.zip、data/GLOBAL.zip 归档核验；--apply 恢复缺失正式JSON（不覆盖已有文件）
    launch [--dry-run] 守卫通过后启动采集器（后台、无窗口、日志写 logs/driver_main_*.log）
    stop --yes         强制停止运行中的采集器（仅在用户明确要求时使用）
    tests              离线回归测试（python -B -m unittest discover -s tests）

安全约定: 绝不打印 config.yaml 中的密钥；绝不构造 record_files.Dataset（会触发 outbox 恢复）；
所有写入路径限制在项目根内。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
RUNTIME = ROOT / ".runtime"
LOGS = ROOT / "logs"
RESTART_TASK = RUNTIME / "restart_after_current.json"
WRITER_LOCK = RUNTIME / "writer.lock"
STRUCTURED = ROOT / "structured"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def writer_lock_held():
    """与 finance_agent.core.Lock 相同的 msvcrt/fcntl 非阻塞探测，不导入项目代码。"""
    path = WRITER_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        stream.close()


def matching_collectors():
    """返回运行中的 main.py 采集器 PID 列表；psutil 不可用时返回 None。语义同 run_after_current.py。"""
    try:
        import psutil
    except ImportError:
        return None
    main = (ROOT / "main.py").resolve()
    result = []
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid():
            continue
        if str(process.info.get("name")).lower() not in {"python.exe", "pythonw.exe", "python", "python3"}:
            continue
        try:
            for arg in process.cmdline()[1:]:
                if not str(arg).lower().endswith("main.py"):
                    continue
                path = Path(arg)
                path = path if path.is_absolute() else Path(process.cwd()) / path
                if path.resolve() == main:
                    result.append(process.pid)
                    break
        except Exception:
            continue
    return result


def archive_state():
    """data/US.zip、data/GLOBAL.zip 归档守卫: zip 内 JSON 数 > 磁盘现存数 => 须先恢复。"""
    state = {}
    for market in ("US", "GLOBAL"):
        zip_path = DATA / f"{market}.zip"
        live = len(list((DATA / market).glob("*.json"))) if (DATA / market).is_dir() else 0
        members = 0
        if zip_path.is_file():
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    members = sum(1 for n in zf.namelist() if n.endswith(".json"))
            except (OSError, zipfile.BadZipFile):
                members = -1
        state[market] = {"zip": zip_path.is_file(), "zip_members": members, "live_json": live,
                         "needs_restore": bool(zip_path.is_file() and members > live)}
    return state


def ollama_settings():
    base, model = "http://localhost:11434", "qwen3:8b"
    try:
        import yaml
        value = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8-sig")) or {}
        section = value.get("ollama") or {}
        return section.get("base_url", base), section.get("model", model)
    except Exception:
        return base, model


def ollama_probe():
    base, model = ollama_settings()
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/tags", timeout=5) as response:
            names = [m.get("name") for m in json.loads(response.read().decode("utf-8")).get("models", [])]
        return {"url": base, "ok": True, "models": names, "target": model,
                "ready": model in names}
    except OSError as error:
        return {"url": base, "ok": False, "error": f"{type(error).__name__}: {error}", "target": model}


def run_app(args, need_lock=False):
    """运行项目入口 main.py；need_lock 时先探测写入锁，被占用则拒绝而不启动。"""
    if need_lock:
        collectors = matching_collectors()
        if collectors:
            print(f"[拒绝] 采集器正在运行（PID {collectors}），写入锁将被长期持有。")
            print("       观察: driver.py status / tail；等待重启: run_after_current.py")
            return 2
        if writer_lock_held():
            print("[拒绝] 项目写入锁被占用（其他维护命令运行中）。稍后重试或检查 driver.py status。")
            return 2
    command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "main.py"), *args]
    print("[执行]", " ".join(command[1:]))
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True)
    for stream in (result.stdout, result.stderr):
        if stream:
            print(stream.decode("utf-8", errors="replace").rstrip())
    print(f"[退出码] {result.returncode}")
    return result.returncode


def cmd_check(_args):
    blockers, warnings = [], []
    python_ok = "finance_crawler" in sys.executable
    print(f"Python : {sys.executable}")
    if not python_ok:
        blockers.append("当前 Python 不是 finance_crawler 环境，必须使用环境内的 python.exe")
    ollama = ollama_probe()
    if ollama["ok"]:
        print(f"Ollama : {ollama['url']} 可达，模型 {', '.join(str(m) for m in ollama['models'])}")
        if not ollama["ready"]:
            blockers.append(f"Ollama 未找到目标模型 {ollama['target']}")
    else:
        blockers.append(f"Ollama 不可达（{ollama['url']}）: {ollama['error']}")
    config_ok = (ROOT / "config.yaml").is_file()
    print(f"配置   : {'config.yaml 存在' if config_ok else 'config.yaml 缺失'}")
    if not config_ok:
        blockers.append("config.yaml 不存在")
    archives = archive_state()
    for market, state in archives.items():
        if state["zip"]:
            line = f"归档   : data/{market}.zip 存在（zip内 {state['zip_members']} 条 JSON，磁盘 {state['live_json']} 条）"
            if state["needs_restore"]:
                line += "  <-- 须先 driver.py restore --apply"
                blockers.append(f"data/{market}.zip 归档未恢复（zip {state['zip_members']} > 磁盘 {state['live_json']}）")
            print(line)
        else:
            print(f"归档   : data/{market}.zip 不存在（磁盘 {state['live_json']} 条）")
    collectors = matching_collectors()
    if collectors is None:
        blockers.append("psutil 不可用，无法检测采集器进程")
        print("采集器 : 无法检测（psutil 不可用）")
    elif collectors:
        print(f"采集器 : 运行中 PID {collectors}")
        blockers.append(f"采集器已在运行 PID {collectors}")
    else:
        print("采集器 : 无")
    locked = writer_lock_held()
    print(f"写入锁 : {'被占用' if locked else '空闲'}")
    if locked:
        blockers.append("项目写入锁被占用")
    task = read_json(RESTART_TASK)
    if task and task.get("status") in {"waiting", "waiting_for_writer", "running"}:
        print(f"等待任务: {task['status']}（run_after_current.py 管理中，勿另行启动）")
        blockers.append(f"run_after_current.py 等待任务进行中: {task['status']}")
    manifest = read_json(STRUCTURED / "manifest.json")
    if manifest:
        print(f"投影   : structured/manifest.json 存在，article_metadata={manifest.get('rows', {}).get('article_metadata')} 行，导出于 {manifest.get('exported_at')}")
    else:
        print("投影   : structured/ 不存在或无 manifest（下次批次或 --export-parquet 时生成）")
    for text in warnings:
        print(f"[警告] {text}")
    if blockers:
        for text in blockers:
            print(f"[阻断] {text}")
        print("结论   : 不可启动。")
        return 2
    print("结论   : 可以启动（driver.py launch）。")
    return 0


def cmd_status(_args):
    collectors = matching_collectors()
    if collectors is None:
        print("采集器 : 无法检测（psutil 不可用）")
    else:
        print(f"采集器 : {'运行中 PID ' + str(collectors) if collectors else '无'}")
    task = read_json(RESTART_TASK)
    if task:
        print(f"等待任务: status={task.get('status')} exit_code={task.get('exit_code')} "
              f"collector_pid={task.get('collector_pid')}")
        print(f"         日志: {task.get('terminal_log')}")
    else:
        print("等待任务: 无")
    run = read_json(LOGS / "latest_run.json")
    if run:
        mid_batch = run.get("batches") is None
        print(f"最近批次: status={run.get('status')} started={run.get('started_at')} "
              f"{'（批次进行中的快照，非最终汇总）' if mid_batch else ''}")
        print(f"         搜索{run.get('searched')}次 返回{run.get('found')}条 检查{run.get('considered')}篇 "
              f"保存{run.get('saved')}篇 跳过{run.get('skipped')}条 待处理{run.get('pending')}条")
        print(f"         Qwen调用{run.get('model_calls')}次 缓存{run.get('model_cache_hits')}次 "
              f"数据共{run.get('total_records')}篇 | 结构化导出: {run.get('structured_export')}")
        errors = run.get("errors") or []
        if errors:
            print(f"         本批错误 {len(errors)} 条（详见 logs/latest_run.json）")
    else:
        print("最近批次: logs/latest_run.json 不存在")
    for market in ("CN", "US", "GLOBAL"):
        count = len(list((DATA / market).glob("*.json"))) if (DATA / market).is_dir() else 0
        print(f"data/{market:<6}: {count} 条正式 JSON")
    for market, state in archive_state().items():
        if state["zip"]:
            print(f"归档    : data/{market}.zip {state['zip_members']} 条，磁盘 {state['live_json']} 条"
                  f"{'  <-- 未恢复' if state['needs_restore'] else ''}")
    manifest = read_json(STRUCTURED / "manifest.json")
    if manifest:
        print(f"投影    : {manifest.get('rows')} (导出于 {manifest.get('exported_at')})")
    state = read_json(RUNTIME / "simple/state.json")
    if state:
        records = state.get("records") or {}
        by_status = {}
        for item in records.values():
            key = item.get("status") if isinstance(item, dict) else str(item)
            by_status[key] = by_status.get(key, 0) + 1
        print(f"state   : records={len(records)}（{by_status}） pending={len(state.get('pending') or {})}")
    return 0


def cmd_tail(args):
    candidates = list(LOGS.glob("restarted_main_*.log")) + list(LOGS.glob("driver_main_*.log"))
    if not candidates:
        print("没有采集器日志（logs/restarted_main_*.log 或 logs/driver_main_*.log）。")
        return 1
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    lines = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"=== {newest.name}（{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest.stat().st_mtime))}，共 {len(lines)} 行）最后 {args.lines} 行:")
    for line in lines[-args.lines:]:
        print(line)
    return 0


def cmd_validate(_args):
    command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "main.py"), "--validate-config"]
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True)
    output = result.stdout.decode("utf-8", errors="replace")
    if re.search(r"tvly-[A-Za-z0-9_-]{8,}", output + result.stderr.decode("utf-8", errors="replace")):
        print("[中止] 检测到疑似密钥泄漏（tvly-*），输出不打印。请检查 --validate-config 的脱敏行为。")
        return 1
    if output:
        print(output.rstrip())
    if result.stderr:
        print(result.stderr.decode("utf-8", errors="replace").rstrip())
    print(f"[退出码] {result.returncode}（密钥检查: 通过，SecretStr 字段不出现在输出中）")
    return result.returncode


def cmd_restore(args):
    markets = ("US", "GLOBAL")
    conflicts, corrupt = [], []
    plan = {}
    for market in markets:
        zip_path = DATA / f"{market}.zip"
        if not zip_path.is_file():
            continue
        entries = []
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad:
                corrupt.append(f"{market}.zip 成员损坏: {bad}")
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                parts = name.split("/")
                if (len(parts) != 2 or parts[0] != market or not parts[1].endswith(".json")
                        or parts[1].startswith(".") or ".." in parts[1]):
                    corrupt.append(f"{market}.zip 含异常成员名: {name}")
                    continue
                target = (DATA / market / parts[1]).resolve()
                if not str(target).startswith(str(DATA.resolve())):
                    corrupt.append(f"{market}.zip 成员越出 data/: {name}")
                    continue
                data = zf.read(info)
                if target.exists():
                    entries.append((target, data, "skip" if target.read_bytes() == data else "CONFLICT"))
                else:
                    entries.append((target, data, "add"))
        plan[market] = entries
    if corrupt:
        print("[中止] 归档完整性问题:")
        for text in corrupt:
            print("  -", text)
        return 1
    if not plan:
        print("没有找到 data/US.zip 或 data/GLOBAL.zip，无需恢复。")
        return 0
    for market, entries in plan.items():
        add = sum(1 for _, _, action in entries if action == "add")
        skip = sum(1 for _, _, action in entries if action == "skip")
        conflict = [target.name for target, _, action in entries if action == "CONFLICT"]
        conflicts.extend((market, name) for name in conflict)
        print(f"{market}.zip: {len(entries)} 条 JSON | 待恢复 {add} | 已存在且一致 {skip} | 冲突 {len(conflict)}")
        if market == "GLOBAL":
            cn_identical = sum(1 for target, data, _ in entries
                               if (DATA / "CN" / target.name).is_file()
                               and (DATA / "CN" / target.name).read_bytes() == data)
            print(f"  交叉核对: {cn_identical}/{len(entries)} 条与 data/CN 副本逐字节一致")
        for name in conflict[:10]:
            print(f"  [冲突] {name}（磁盘内容与 zip 不同，恢复时拒绝覆盖）")
    if conflicts:
        print(f"[注意] 共 {len(conflicts)} 个同名冲突文件；--apply 将整体中止，不会覆盖任何磁盘文件。")
    if not args.apply:
        print("干跑完成，未写入任何文件。恢复执行: driver.py restore --apply")
        return 0
    collectors = matching_collectors()
    if collectors:
        print(f"[拒绝] 采集器正在运行（PID {collectors}），禁止并发恢复正式数据。")
        return 2
    if writer_lock_held():
        print("[拒绝] 项目写入锁被占用，禁止恢复正式数据。")
        return 2
    if conflicts:
        print("[中止] 存在同名冲突，拒绝恢复。请先人工核对冲突文件（zip 内为归档前原件）。")
        return 1
    added = 0
    for market, entries in plan.items():
        folder = DATA / market
        folder.mkdir(parents=True, exist_ok=True)
        for target, data, action in entries:
            if action != "add":
                continue
            target.write_bytes(data)
            if target.read_bytes() != data:
                print(f"[失败] 写入校验不一致: {target.name}")
                return 1
            added += 1
    print(f"恢复完成: 新增 {added} 条正式 JSON；zip 保留作备份；已存在文件未改动。")
    print("下一步: driver.py check 确认归档守卫解除后再 launch。")
    return 0


def cmd_launch(args):
    gates = []
    if "finance_crawler" not in sys.executable:
        gates.append(("阻断", f"当前 Python 不是 finance_crawler 环境: {sys.executable}"))
    if not (ROOT / "config.yaml").is_file():
        gates.append(("阻断", "config.yaml 不存在"))
    for market, state in archive_state().items():
        if state["needs_restore"]:
            gates.append(("阻断", f"data/{market}.zip 归档未恢复（zip {state['zip_members']} 条 > 磁盘 {state['live_json']} 条），"
                                 f"先运行 driver.py restore --apply，否则已归档记录会被当作未保存重新采集"))
    collectors = matching_collectors()
    if collectors is None:
        gates.append(("阻断", "psutil 不可用，无法确认没有正在运行的采集器"))
    elif collectors:
        gates.append(("阻断", f"采集器已在运行 PID {collectors}；等待其自然结束或用 run_after_current.py 排队重启"))
    task = read_json(RESTART_TASK)
    if task and task.get("status") in {"waiting", "waiting_for_writer", "running"}:
        gates.append(("阻断", f"run_after_current.py 等待任务进行中: {task['status']}，由其负责启动"))
    if writer_lock_held():
        gates.append(("阻断", "项目写入锁被占用"))
    command = [sys.executable, "-u", "-B", "-X", "utf8", str(ROOT / "main.py"), "--config", str(ROOT / "config.yaml")]
    print("[命令]", " ".join(command))
    for level, text in gates:
        print(f"[{level}] {text}")
    if any(level == "阻断" for level, _ in gates):
        print("未启动。")
        return 2
    if args.dry_run:
        print("干跑完成，未启动。去掉 --dry-run 实际启动。")
        return 0
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = LOGS / f"driver_main_{stamp}.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as output:
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"[已启动] PID {process.pid}（后台、无窗口）")
    print(f"[日志]   {log_path}")
    print("观察: driver.py status / driver.py tail；历史模式将按批次持续运行。")
    return 0


def cmd_stop(args):
    if not args.yes:
        print("需要 --yes。仅在用户明确要求停止采集器时使用；Ctrl+C 优雅停止仅适用于前台进程。")
        return 2
    collectors = matching_collectors()
    if collectors is None:
        print("psutil 不可用，无法定位采集器进程。")
        return 1
    if not collectors:
        print("没有运行中的采集器。")
        return 0
    for pid in collectors:
        print(f"[强制停止] PID {pid}")
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        else:
            import psutil
            psutil.Process(pid).terminate()
    print("已发送停止命令。等待任务（如有）会在下个心跳检测到进程退出。")
    return 0


def cmd_tests(args):
    # -X utf8 与 main.py 的启动方式一致：本机中文 Windows 默认 GBK，测试夹具文件为 UTF-8，
    # 个别测试 read_text() 未显式 encoding，UTF-8 模式下才是项目的真实读写环境。
    command = [sys.executable, "-X", "utf8", "-B", "-m", "unittest", "discover", "-s", "tests"]
    if args.verbose:
        command.append("-v")
    print("[执行]", " ".join(command), f"（工作目录 {ROOT}，完整输出写日志）")
    started = time.monotonic()
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = LOGS / f"driver_tests_{stamp}.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(result.stdout + b"\n" + result.stderr)
    summary = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
    for line in (summary or ["(无摘要输出)"])[-25:]:
        print(line)
    print(f"[完整日志] {log_path}")
    print(f"[退出码] {result.returncode}，用时 {time.monotonic() - started:.0f} 秒（全量离线测试约 1 分钟）")
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="financial_data_project2 采集器驱动 harness（详见 SKILL.md）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="环境自检: 0=可启动 2=被阻断")
    sub.add_parser("status", help="采集器/等待任务/最近批次/数据/投影状态")
    tail = sub.add_parser("tail", help="最新采集器日志尾部")
    tail.add_argument("-n", "--lines", type=int, default=40)
    sub.add_parser("validate", help="main.py --validate-config（密钥泄漏断言）")
    sub.add_parser("doctor", help="main.py --doctor（需写入锁空闲）")
    sub.add_parser("preview", help="main.py --preview-history（离线）")
    sub.add_parser("parquet", help="main.py --export-parquet（需写入锁空闲）")
    restore = sub.add_parser("restore", help="核验/恢复 data/US.zip、data/GLOBAL.zip")
    restore.add_argument("--apply", action="store_true", help="实际恢复缺失文件（不覆盖已有文件）")
    launch = sub.add_parser("launch", help="守卫通过后启动采集器")
    launch.add_argument("--dry-run", action="store_true")
    stop = sub.add_parser("stop", help="强制停止采集器（最后手段）")
    stop.add_argument("--yes", action="store_true")
    tests = sub.add_parser("tests", help="离线回归测试")
    tests.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    handlers = {"check": cmd_check, "status": cmd_status, "tail": cmd_tail, "validate": cmd_validate,
                "doctor": lambda a: run_app(["--doctor"], need_lock=True),
                "preview": lambda a: run_app(["--preview-history"]),
                "parquet": lambda a: run_app(["--export-parquet"], need_lock=True),
                "restore": cmd_restore, "launch": cmd_launch, "stop": cmd_stop, "tests": cmd_tests}
    raise SystemExit(handlers[args.command](args))


if __name__ == "__main__":
    main()

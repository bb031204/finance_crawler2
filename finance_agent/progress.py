from datetime import datetime, timedelta, timezone
from threading import RLock
import os
import sys

_OUTPUT_LOCK = RLock()
_COLORS = {"success": "\033[92m", "warning": "\033[93m", "error": "\033[91m"}


def severity(component, message):
    if any(word in component for word in ["失败", "错误", "异常"]) or component in {"SEC目录暂停", "发现待复核"}:
        return "error"
    if component in {"新增JSON", "更新JSON", "保存"}:
        return "success"
    if (component in {"剔除", "跳过", "筛选剔除", "重复跳过", "待处理", "待复核", "日期隔离", "日期", "预算", "停止", "运行中断", "数据缺口", "规划回退", "原文保留"}
            or "暂停" in component or "未完成" in component
            or component == "筛选" and str(message).startswith("跳过")):
        return "warning"
    return None


def _enable_ansi(stream):
    if os.name != "nt": return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = wintypes.DWORD()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        pass
    return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM", "").startswith(("xterm", "screen")))


def supports_color(stream):
    if "NO_COLOR" in os.environ: return False
    forced = os.environ.get("FORCE_COLOR")
    if forced is not None: return forced not in {"", "0"}
    return bool(getattr(stream, "isatty", lambda: False)()) and _enable_ansi(stream)

def short(value, limit=100):
    return " ".join(str(value).split())[:limit]

def log(component, message):
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    with _OUTPUT_LOCK:
        line = f"[{stamp}] [{component}] {message}"
        color = _COLORS.get(severity(component, message))
        if color and supports_color(sys.stdout): line = color + line + "\033[0m"
        print(line, flush=True)

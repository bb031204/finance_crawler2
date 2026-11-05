from datetime import datetime, timedelta, timezone

def short(value, limit=100):
    return " ".join(str(value).split())[:limit]

def log(component, message):
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{stamp}] [{component}] {message}", flush=True)

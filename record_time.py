"""Source precision and actual feature availability, without inventing past clocks."""
import json
import re
from datetime import datetime, timezone
from finance_agent.core import bounded, digest, now


TIME_FIELDS={"publication_precision","first_seen_at","fetched_at","feature_available_at","availability_basis"}


def utc_timestamp(value):
    if not isinstance(value,str) or "T" not in value:
        raise ValueError("系统时间必须为带时区的ISO日期时刻")
    stamp=datetime.fromisoformat(value.replace("Z","+00:00"))
    if stamp.tzinfo is None:raise ValueError("系统时间缺少时区")
    return stamp.astimezone(timezone.utc).isoformat()


def publication_precision(value):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):return "date"
    return "second" if re.search(r"T\d{2}:\d{2}:\d{2}",value) else "minute"


def validate_time_information(row):
    info=row["时间信息"]
    if not isinstance(info,dict) or set(info)!=TIME_FIELDS:
        raise ValueError("时间信息字段不完整")
    if info["publication_precision"]!=publication_precision(row["时间"]):
        raise ValueError("来源时间与精度不一致")
    if info["availability_basis"] not in {"extraction","verified_audit","migration"}:
        raise ValueError("未知可用时间依据")
    available=datetime.fromisoformat(utc_timestamp(info["feature_available_at"]))
    for key in ["first_seen_at","fetched_at"]:
        if info[key] is not None and datetime.fromisoformat(utc_timestamp(info[key]))>available:
            raise ValueError("特征可用时间不能早于已知的发现/采集时间")
    return row


def with_time_information(row,candidate=None,page=None,available_at=None,basis="extraction"):
    candidate,page=candidate or {},page or {}
    row=dict(row)
    row["时间信息"]={
        "publication_precision":publication_precision(row["时间"]),
        "first_seen_at":utc_timestamp(candidate["first_seen_at"]) if candidate.get("first_seen_at") else None,
        "fetched_at":utc_timestamp(page["fetched_at"]) if page.get("fetched_at") else None,
        "feature_available_at":utc_timestamp(available_at or now()),
        "availability_basis":basis,
    }
    return validate_time_information(row)


def upgrade_time_information(root,row):
    """Only matching audits may restore a previously measured extraction time."""
    if "时间信息" in row:return validate_time_information(row)
    path=bounded(root/".runtime/simple/audit"/(row["source_id"]+".json"),root)
    if path.is_file():
        try:
            audit=json.loads(path.read_text(encoding="utf-8"))
            stamp=audit.get("feature_available_at")
            if (audit.get("record_hash")==digest(row) and audit.get("source_published_at")==row["时间"]
                    and stamp and datetime.fromisoformat(utc_timestamp(stamp))<=datetime.now(timezone.utc)):
                return with_time_information(row,audit,audit,stamp,"verified_audit")
        except (ValueError,TypeError,KeyError):
            pass
    # Unknown historical clocks stay null. Migration time is a conservative
    # availability bound for this exported copy, never a claimed historical fact.
    return with_time_information(row,basis="migration")

"""One article per JSON file; naming metadata stays outside public records."""
import json
import re
import zipfile
from datetime import datetime
from urllib.parse import urlsplit
from finance_agent.core import bounded, digest, dump
from finance_agent.safety import canonical

FIELDS = {"source_id", "url", "时间", "分类", "关键词", "主要内容"}
MARKETS = ["CN", "US", "GLOBAL"]


def source_id(url):
    return digest(canonical(url).encode("utf-8"))[:24]


def safe_part(value, limit=32):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value))
    value = re.sub(r"[\s_]+", "-", value).strip(" .-")[:limit].rstrip(" .")
    if not value:
        raise ValueError("文件名组成部分不能为空")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value.split(".")[0], re.I):
        value = "-" + value
    return value


def source_key(url):
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        raise ValueError("来源URL缺少网站域名")
    for domain, key in {"xueqiu.com":"xueqiu", "cninfo.com.cn":"cninfo", "cls.cn":"cls",
        "csrc.gov.cn":"csrc", "sse.com.cn":"sse", "sec.gov":"sec", "nasdaq.com":"nasdaq", "stocktwits.com":"stocktwits"}.items():
        if host == domain or host.endswith("." + domain):
            return key
    parts = host.split(".")
    suffix = ".".join(parts[-2:])
    offset = 3 if suffix in {"com.cn", "gov.cn", "org.cn", "net.cn", "co.uk", "com.hk"} else 2
    return safe_part(parts[-offset] if len(parts) >= offset else parts[0], 40)


def original_time(value):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.fromisoformat(value).strftime("%Y_%m_%d")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or "T" not in value:
        raise ValueError("原始时间需要来源日期或带时区的完整时刻")
    return stamp.strftime("%Y_%m_%d_%H_%M")


def record_name(row, industry):
    return "_".join([original_time(row["时间"]), safe_part(row["分类"], 30),
        safe_part(industry, 24), source_key(row["url"])]) + ".json"


class Dataset:
    def __init__(self, root, directory):
        self.root = root.resolve()
        self.directory = bounded(self.root / directory, self.root)
        self.outbox = self.root / ".runtime/simple/record_outbox.json"
        self.rows, self.paths, self.industries = {}, {}, {}
        if self.outbox.exists():
            self._complete(json.loads(self.outbox.read_text(encoding="utf-8")))
        for market in MARKETS:
            folder = bounded(self.directory / market, self.root)
            for path in sorted(folder.glob("*.json")):
                row = json.loads(path.read_text(encoding="utf-8"))
                self.validate(market, row)
                identity = row["source_id"]
                if identity in self.rows:
                    raise ValueError("存在重复source_id，请检查：" + str(path))
                self.rows[identity] = (market, row)
                self.paths[identity] = bounded(path, self.root)
                prefix = original_time(row["时间"]) + "_" + safe_part(row["分类"], 30) + "_"
                if path.stem.startswith(prefix):
                    tail = path.stem[len(prefix):].split("_")
                    if len(tail) >= 2 and tail[1] == source_key(row["url"]):
                        self.industries[identity] = tail[0]
        # Read old JSONL only for migration; individual files are authoritative.
        for market in MARKETS:
            path = self.directory / (market + ".jsonl")
            if path.exists():
                for line in path.open(encoding="utf-8"):
                    if line.strip():
                        row = json.loads(line)
                        self.validate(market, row)
                        self.rows.setdefault(row["source_id"], (market, row))

    @staticmethod
    def validate(market, row):
        if market not in MARKETS or set(row) != FIELDS:
            raise ValueError("正式记录必须属于CN/US/GLOBAL且仅有六个字段")
        if not isinstance(row["source_id"], str) or not row["source_id"]:
            raise ValueError("source_id不能为空")
        original_time(row["时间"])

    def ensure_dirs(self):
        for market in MARKETS:
            bounded(self.directory / market, self.root).mkdir(parents=True, exist_ok=True)

    def _target(self, market, row, industry):
        folder = bounded(self.directory / market, self.root)
        path = bounded(folder / record_name(row, industry), self.root)
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
            # Same minute/category/industry/source can contain several articles.
            suffix = safe_part(row["source_id"], 64)
            path = bounded(path.with_name(path.stem + "_" + suffix + ".json"), self.root)
            if path.exists() and json.loads(path.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
                raise ValueError("同名文件对应不同记录，拒绝覆盖")
        return path

    def _complete(self, operation):
        market, row = operation["market"], operation["row"]
        self.validate(market, row)
        target = bounded(self.root / operation["target"], self.root)
        folder = bounded(self.directory / market, self.root)
        if target.parent != folder or target.suffix != ".json":
            raise ValueError("待保存文件必须位于市场目录内")
        if target.exists() and json.loads(target.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
            raise ValueError("目标文件已属于其他记录，拒绝覆盖")
        dump(target, row)
        old = operation.get("previous")
        if old:
            previous = bounded(self.root / old, self.root)
            if previous.parent not in [self.directory / key for key in MARKETS] or previous.suffix != ".json":
                raise ValueError("旧记录路径不在市场目录内")
            if previous != target and previous.exists():
                if json.loads(previous.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
                    raise ValueError("旧文件与当前记录不一致，拒绝删除")
                previous.unlink()
        self.outbox.unlink(missing_ok=True)
        return target

    def save(self, market, row, industry=None):
        self.validate(market, row)
        industry = industry or self.industries.get(row["source_id"])
        if not industry:
            raise ValueError("保存前必须由Qwen提炼主要产业")
        industry = safe_part(industry, 24)
        target = self._target(market, row, industry)
        previous = self.paths.get(row["source_id"])
        operation = {"market":market, "row":row, "target":str(target.relative_to(self.root)),
            "previous":str(previous.relative_to(self.root)) if previous else None}
        dump(self.outbox, operation)
        self._complete(operation)
        self.rows[row["source_id"]] = (market, row)
        self.paths[row["source_id"]] = target
        self.industries[row["source_id"]] = industry
        return target

    def migrate(self, model):
        legacy = [self.directory / (key + ".jsonl") for key in MARKETS if (self.directory / (key + ".jsonl")).exists()]
        backup = None
        contents = {path.relative_to(self.root).as_posix():path.read_bytes() for path in legacy}
        if legacy:
            folder = bounded(self.root / "archive", self.root)
            folder.mkdir(exist_ok=True)
            backup = folder / ("jsonl_records_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".zip")
            with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, raw in contents.items():
                    archive.writestr(name, raw)
            with zipfile.ZipFile(backup) as archive:
                if any(archive.read(name) != raw for name, raw in contents.items()):
                    raise ValueError("旧JSONL备份校验失败，停止迁移")
        migrated = 0
        for identity, (market, row) in list(self.rows.items()):
            if identity not in self.paths or identity not in self.industries:
                industry = model.industry(row)
                self.save(market, row, industry)
                migrated += 1
        self.ensure_dirs()
        for identity, (market, row) in self.rows.items():
            if json.loads(self.paths[identity].read_text(encoding="utf-8")) != row:
                raise ValueError("逐文件迁移校验失败，保留原JSONL")
        for path in legacy:
            if path.read_bytes() != contents[path.relative_to(self.root).as_posix()]:
                raise ValueError("迁移期间原JSONL发生变化，拒绝移除")
        for path in legacy:
            bounded(path, self.root).unlink()
        return {"migrated":migrated, "backup":str(backup) if backup else None}

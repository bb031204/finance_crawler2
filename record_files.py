"""One article per JSON file; naming metadata stays outside public records."""
import json
import re
import zipfile
from datetime import datetime
from urllib.parse import urlsplit
from finance_agent.core import bounded, digest, dump
from finance_agent.safety import canonical
from record_time import validate_time_information,upgrade_time_information
from analysis_metadata import ANALYSIS_FIELDS, validate_public_analysis, upgrade_analysis

LEGACY_FIELDS = {"source_id", "url", "时间", "分类", "关键词", "主要内容"}
TIMED_FIELDS = LEGACY_FIELDS | {"时间信息"}
LEGACY_ANALYSIS_FIELDS = TIMED_FIELDS | ANALYSIS_FIELDS
FIELDS = LEGACY_ANALYSIS_FIELDS | {"title"}
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
    for domain, key in {"www.gov.cn":"gov", "finance.eastmoney.com":"eastmoney", "irm.cninfo.com.cn":"irm", "guba.eastmoney.com":"guba", "xueqiu.com":"xueqiu", "cninfo.com.cn":"cninfo", "cls.cn":"cls",
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
        safe_part(industry, 40), source_key(row["url"])]) + ".json"


class Dataset:
    def __init__(self, root, directory):
        self.root = root.resolve()
        self.directory = bounded(self.root / directory, self.root)
        self.outbox = self.root / ".runtime/simple/record_outbox.json"
        self.recovery_count=0
        self.rows, self.paths, self.industries, self.replicas = {}, {}, {}, {}
        if self.outbox.exists():
            self._complete(json.loads(self.outbox.read_text(encoding="utf-8")))
            self.recovery_count=1
        # GLOBAL is canonical; identical CN/US copies are views, not new records.
        for market in ["GLOBAL", "CN", "US"]:
            for path in sorted((self.directory / market).glob("*.json")):
                path = bounded(path, self.root)
                row = json.loads(path.read_text(encoding="utf-8"))
                self.validate(market, row)
                identity = row["source_id"]
                if identity in self.rows:
                    old_market, old = self.rows[identity]
                    if old != row or market in self.replicas[identity]:
                        raise ValueError("同source_id的副本内容不一致或同目录重复：" + str(path))
                    if old_market != "GLOBAL" and {old_market, market} != {"CN", "US"}:
                        raise ValueError("记录的市场副本不合法：" + str(path))
                    self.rows[identity] = ("GLOBAL", old)
                    self.replicas[identity][market] = path
                    continue
                self.rows[identity] = (market, row)
                self.paths[identity] = path
                self.replicas[identity] = {market:path}
                prefix = original_time(row["时间"]) + "_" + safe_part(row["分类"], 30) + "_"
                if path.stem.startswith(prefix):
                    tail = path.stem[len(prefix):].split("_")
                    if len(tail) >= 2 and tail[1] == source_key(row["url"]):
                        self.industries[identity] = tail[0]
        for market in MARKETS:
            path = self.directory / (market + ".jsonl")
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if line.strip():
                            row = json.loads(line)
                            self.validate(market, row)
                            self.rows.setdefault(row["source_id"], (market, row))

    @staticmethod
    def validate(market, row):
        if market not in MARKETS or set(row) not in (LEGACY_FIELDS,TIMED_FIELDS,LEGACY_ANALYSIS_FIELDS,FIELDS,
                LEGACY_FIELDS|{'title'},TIMED_FIELDS|{'title'}):
            raise ValueError("正式记录必须属于CN/US/GLOBAL，字段须符合当前或旧版格式")
        if not isinstance(row["source_id"], str) or not row["source_id"]:
            raise ValueError("source_id不能为空")
        if 'title' in row and row['title'] is not None and (not isinstance(row['title'],str) or not row['title'].strip()):
            raise ValueError('title须为非空原文标题，无法取得时使用null')
        original_time(row["时间"])
        if "时间信息" in row:validate_time_information(row)
        if ANALYSIS_FIELDS <= set(row):validate_public_analysis(row)

    def ensure_dirs(self):
        for market in MARKETS:
            bounded(self.directory / market, self.root).mkdir(parents=True, exist_ok=True)

    def market_rows(self, market):
        if market not in MARKETS:
            raise ValueError("未知市场")
        rows = [row for key,row in self.rows.values() if key==market or (market in {"CN","US"} and key=="GLOBAL")]
        return sorted(rows, key=lambda row:(row["时间"],row["source_id"]))

    def _target(self, market, row, industry):
        folder = bounded(self.directory / market, self.root)
        path = bounded(folder / record_name(row, industry), self.root)
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
            path = bounded(path.with_name(path.stem + "_" + safe_part(row["source_id"],64) + ".json"), self.root)
            if path.exists() and json.loads(path.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
                raise ValueError("同名文件对应不同记录，拒绝覆盖")
        return path

    def _complete(self, operation):
        if operation.get("action")=="quarantine":return self._complete_quarantine(operation)
        market, row = operation["market"], operation["row"]
        self.validate(market, row)
        # Accept unfinished writes from the previous single-target format.
        values = operation.get("targets") or {market:operation["target"]}
        targets = {}
        for key, value in values.items():
            path = bounded(self.root / value, self.root)
            if key not in MARKETS or path.parent != self.directory/key or path.suffix != ".json":
                raise ValueError("待保存文件必须位于市场目录内")
            if path.exists() and json.loads(path.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
                raise ValueError("目标文件属于其他记录，拒绝覆盖")
            targets[key] = path
        old = operation.get("previous") or []
        if isinstance(old,str):old=[old]
        previous = []
        for value in old:
            path = bounded(self.root / value, self.root)
            if path.parent not in [self.directory/key for key in MARKETS] or path.suffix != ".json":
                raise ValueError("旧记录路径不在市场目录内")
            if path.exists() and json.loads(path.read_text(encoding="utf-8"))["source_id"] != row["source_id"]:
                raise ValueError("旧文件与当前记录不一致，拒绝删除")
            previous.append(path)
        audit_update = operation.get("audit_update")
        audit_path = None
        if audit_update is not None:
            audit_path = bounded(self.root/".runtime/simple/audit"/(row["source_id"]+".json"), self.root)
            if (audit_path.parent != self.root/".runtime/simple/audit" or not isinstance(audit_update, dict)
                    or audit_update.get("source_id") != row["source_id"] or audit_update.get("record_hash") != digest(row)):
                raise ValueError("迁移审计必须与当前正式记录的ID和内容匹配")
        for path in targets.values():dump(path,row)
        for path in targets.values():
            if json.loads(path.read_text(encoding="utf-8")) != row:
                raise ValueError("正式文件写入校验失败，保留事务供恢复")
        if audit_path is not None:
            dump(audit_path, audit_update)
            if json.loads(audit_path.read_text(encoding="utf-8")) != audit_update:
                raise ValueError("迁移审计写入校验失败，保留事务供恢复")
        for path in previous:
            if path not in targets.values():path.unlink(missing_ok=True)
        self.outbox.unlink(missing_ok=True)
        return targets

    def _complete_quarantine(self,operation):
        row=operation["row"];self.validate(operation["market"],row)
        paths=[bounded(self.root/value,self.root) for value in operation["paths"]]
        for path in paths:
            if path.parent not in [self.directory/key for key in MARKETS] or path.suffix!=".json":
                raise ValueError("待隔离文件不在正式市场目录")
            if path.exists() and json.loads(path.read_text(encoding="utf-8"))!=row:
                raise ValueError("隔离期间原记录变化，拒绝删除")
        name=safe_part(row["source_id"],64)+"_"+digest(row)[:12]+".json"
        target=bounded(self.root/".runtime/simple/quarantine"/name,self.root)
        dump(target,operation)
        if json.loads(target.read_text(encoding="utf-8"))["row"]!=row:raise ValueError("隔离备份校验失败，保留正式文件")
        for path in paths:path.unlink(missing_ok=True)
        self.outbox.unlink(missing_ok=True)
        return target

    def quarantine(self,identity,reason):
        self.recover_pending()
        market,row=self.rows[identity]
        operation={"action":"quarantine","market":market,"row":row,"reason":reason,
            "paths":[str(p.relative_to(self.root)) for p in self.replicas[identity].values()]}
        dump(self.outbox,operation);target=self._complete(operation)
        for mapping in [self.rows,self.paths,self.replicas,self.industries]:mapping.pop(identity,None)
        return target

    def save(self, market, row, industry=None, audit_update=None):
        self.recover_pending()
        self.validate(market,row)
        if ANALYSIS_FIELDS <= set(row):validate_public_analysis(row, strict_codes=True)
        row=upgrade_time_information(self.root,row)
        industry = industry or self.industries.get(row["source_id"])
        if not industry:raise ValueError("保存前必须由Qwen提炼主要产业")
        industry = safe_part(industry,40)
        markets = ["GLOBAL","CN","US"] if market=="GLOBAL" else [market]
        targets = {key:self._target(key,row,industry) for key in markets}
        previous = set(self.replicas.get(row["source_id"],{}).values())
        if row["source_id"] in self.paths:previous.add(self.paths[row["source_id"]])
        operation = {"market":market,"row":row,"targets":{key:str(path.relative_to(self.root)) for key,path in targets.items()},
            "previous":[str(path.relative_to(self.root)) for path in previous]}
        if audit_update is not None:
            operation["audit_update"] = dict(audit_update,record_paths=[str(path.relative_to(self.root)) for path in targets.values()])
        dump(self.outbox,operation)
        self._complete(operation)
        self.rows[row["source_id"]] = (market,row)
        self.paths[row["source_id"]] = targets[market]
        self.replicas[row["source_id"]] = targets
        self.industries[row["source_id"]] = industry
        return targets[market]

    def recover_pending(self):
        if not self.outbox.exists():return False
        previous=self.recovery_count
        self.__init__(self.root,str(self.directory.relative_to(self.root)))
        self.recovery_count+=previous
        return True

    def migrate(self, model):
        legacy = [self.directory/(key+".jsonl") for key in MARKETS if (self.directory/(key+".jsonl")).exists()]
        backup = None
        contents = {path.relative_to(self.root).as_posix():path.read_bytes() for path in legacy}
        for identity, (_, row) in self.rows.items():
            if not ANALYSIS_FIELDS <= set(row):
                for path in self.replicas.get(identity, {}).values(): contents[path.relative_to(self.root).as_posix()] = path.read_bytes()
                audit = bounded(self.root/".runtime/simple/audit"/(identity+".json"), self.root)
                if audit.is_file(): contents[audit.relative_to(self.root).as_posix()] = audit.read_bytes()
        if contents:
            folder = bounded(self.root/"archive",self.root);folder.mkdir(exist_ok=True)
            backup = folder/("record_schema_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f")+".zip")
            with zipfile.ZipFile(backup,"w",zipfile.ZIP_DEFLATED) as archive:
                for name,raw in contents.items():archive.writestr(name,raw)
                archive.writestr("manifest.json", json.dumps({name:digest(raw) for name,raw in contents.items()}))
            with zipfile.ZipFile(backup) as archive:
                if any(archive.read(name)!=raw for name,raw in contents.items()):raise ValueError("JSONL备份校验失败")
        migrated = global_synced = time_upgraded = analysis_upgraded = 0
        for identity,(market,row) in list(self.rows.items()):
            timed=upgrade_time_information(self.root,row)
            industry = self.industries.get(identity) or model.industry(row)
            enriched, audit_update = upgrade_analysis(self.root, timed, industry, original=row)
            if not ANALYSIS_FIELDS <= set(row): analysis_upgraded += 1
            if identity not in self.paths or identity not in self.industries:
                self.save(market,enriched,industry,audit_update);migrated+=1
            elif market=="GLOBAL" and (set(self.replicas.get(identity,{}))!=set(MARKETS) or self.paths[identity].parent!=self.directory/"GLOBAL"):
                self.save(market,enriched,industry,audit_update);global_synced+=1
            elif enriched!=row:
                self.save(market,enriched,industry,audit_update)
            if timed!=row:time_upgraded+=1
        self.ensure_dirs()
        for identity,(market,row) in self.rows.items():
            if json.loads(self.paths[identity].read_text(encoding="utf-8"))!=row:raise ValueError("迁移校验失败，保留原件")
        for path in legacy:
            if path.read_bytes()!=contents[path.relative_to(self.root).as_posix()]:raise ValueError("迁移期间JSONL变化")
        for path in legacy:bounded(path,self.root).unlink()
        return {"migrated":migrated,"global_synced":global_synced,"time_upgraded":time_upgraded,
                "analysis_upgraded":analysis_upgraded,"backup":str(backup) if backup else None}

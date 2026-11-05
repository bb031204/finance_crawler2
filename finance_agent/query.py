import argparse
import json
import sqlite3
from pathlib import Path
from .core import ROOT,bounded
from .config import load

def query(repo_or_db,**filters):
    db=repo_or_db.db if hasattr(repo_or_db,"db") else repo_or_db
    clauses=[];args=[]
    if filters.get("market"):
        clauses.append("(e.market=? OR EXISTS(SELECT 1 FROM event_securities ms WHERE ms.event_id=e.id AND ms.market=?))")
        args.extend([filters["market"],filters["market"]])
    if filters.get("asof"):clauses.append("e.available_at<=?");args.append(filters["asof"])
    for key,column in [("symbol","symbol"),("stock_id","stock_id"),("company","company_name")]:
        if filters.get(key):
            clauses.append("EXISTS(SELECT 1 FROM event_securities s WHERE s.event_id=e.id AND s."+column+"=?)")
            args.append(filters[key])
    rows=db.execute("SELECT e.payload FROM events e"+(" WHERE "+" AND ".join(clauses) if clauses else "")+
                    " ORDER BY e.available_at,e.id",args).fetchall()
    out=[]
    for row in rows:
        e=json.loads(row[0])
        date=e["time"].get("published_date")
        if filters.get("start") and (not date or date<filters["start"]):continue
        if filters.get("end") and (not date or date>filters["end"]):continue
        if filters.get("event_type") and e["event_type"]!=filters["event_type"]:continue
        if filters.get("direction") and filters["direction"] not in e.get("directions",[]):continue
        out.append(e)
    if filters.get("deduplicate"):
        groups={}
        for e in out:
            key=e.get("event_group_id",e["event_id"])
            if key not in groups:
                groups[key]=dict(e,sources=[e["source"]])
            elif e["source"] not in groups[key]["sources"]:
                groups[key]["sources"].append(e["source"])
        out=list(groups.values())
    return out[:filters.get("limit",1000)]

def export_events(repo,directory):
    directory=bounded(directory,repo.root)
    groups={}
    for row in repo.db.execute("SELECT payload FROM events ORDER BY available_at,id"):
        e=json.loads(row[0]);day=e["time"].get("published_date") or "unknown"
        key=(e["market"],day)
        groups.setdefault(key,[]).append(e)
    paths=[]
    for (market,day),events in groups.items():
        if market not in {"CN","US","GLOBAL"}:raise ValueError("非法市场分区")
        if day!="unknown":
            from datetime import date
            date.fromisoformat(day)
        p=bounded(directory/market/(day+".jsonl"),repo.root);p.parent.mkdir(parents=True,exist_ok=True)
        expected="".join(json.dumps(e,ensure_ascii=False)+"\n" for e in events)
        if not p.exists() or p.read_text(encoding="utf-8")!=expected:
            temp=p.with_suffix(".jsonl.tmp");temp.write_text(expected,encoding="utf-8",newline="\n");temp.replace(p)
        paths.append(str(p))
    return paths

def main(argv=None):
    parser=argparse.ArgumentParser(description="查询本地金融事件（候选仍待语义审核）")
    parser.add_argument("--config",type=Path,default=ROOT/"config.yaml")
    for name in ["symbol","stock-id","company","market","start","end","direction","event-type","asof"]:
        parser.add_argument("--"+name)
    parser.add_argument("--deduplicate",action="store_true")
    parser.add_argument("--limit",type=int,default=100)
    args=vars(parser.parse_args(argv))
    try:
        cfg=load(args.pop("config"))
        from datetime import date
        for key in ["start","end"]:
            if args[key]:date.fromisoformat(args[key])
        if args["start"] and args["end"] and args["start"]>args["end"]:raise ValueError("查询起止日期反向")
        if not 1<=args["limit"]<=10000:raise ValueError("limit须为1~10000")
        db=bounded(ROOT/cfg.output.database)
        if not db.exists():raise ValueError("数据库尚未建立；先运行采集或显式导入")
        if args["asof"]:
            from datetime import datetime,timezone
            dt=datetime.fromisoformat(args["asof"].replace("Z","+00:00"))
            if dt.tzinfo is None:raise ValueError("--asof必须包含时区")
            args["asof"]=dt.astimezone(timezone.utc).isoformat()
        with sqlite3.connect(db.as_uri()+"?mode=ro",uri=True) as conn:
            rows=query(conn,**args)
        print(json.dumps(rows,ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        print("查询失败："+str(exc));return 1

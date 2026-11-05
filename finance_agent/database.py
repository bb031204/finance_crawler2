import json
import sqlite3
import uuid
from datetime import datetime,timedelta,timezone
from .core import ROOT,bounded,digest,now

class Repository:
    def __init__(self,path,root=ROOT):
        self.root=root
        self.path=bounded(path,root)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS candidates(
          id TEXT PRIMARY KEY,url TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
          status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt TEXT NOT NULL DEFAULT '',
          payload TEXT NOT NULL,error TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sightings(
          id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,run_id TEXT NOT NULL,payload TEXT NOT NULL,
          FOREIGN KEY(candidate_id) REFERENCES candidates(id));
        CREATE TABLE IF NOT EXISTS documents(
          version_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,payload TEXT NOT NULL,
          FOREIGN KEY(candidate_id) REFERENCES candidates(id));
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,document_version TEXT NOT NULL,
          available_at TEXT NOT NULL,market TEXT NOT NULL,event_type TEXT NOT NULL,payload TEXT NOT NULL,
          FOREIGN KEY(document_version) REFERENCES documents(version_id));
        CREATE TABLE IF NOT EXISTS event_securities(event_id TEXT NOT NULL,stock_id TEXT,symbol TEXT,
          company_name TEXT,mapping_status TEXT,market TEXT,
          FOREIGN KEY(event_id) REFERENCES events(id));
        CREATE INDEX IF NOT EXISTS queue ON candidates(status,next_attempt,updated_at);
        CREATE INDEX IF NOT EXISTS stock_lookup ON event_securities(symbol,stock_id);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chunks(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        """)
    def close(self):
        self.db.close()
    def recover(self):
        with self.db:
            self.db.execute("UPDATE candidates SET status=CASE status WHEN 'screening' THEN 'new' WHEN 'fetching' THEN 'approved' WHEN 'extracting' THEN 'fetched' END WHERE status IN ('screening','fetching','extracting')")
    def state(self,key,value=None):
        if value is not None:
            with self.db:self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)",(key,json.dumps(value,ensure_ascii=False)))
            return value
        row=self.db.execute("SELECT payload FROM state WHERE key=?",(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def candidate(self,cid):
        row=self.db.execute("SELECT * FROM candidates WHERE id=?",(cid,)).fetchone()
        return dict(row) if row else None
    def upsert_candidate(self,c,run_id):
        cid=c["candidate_id"]
        stamp=now()
        previous=self.candidate(cid)
        refresh=False
        if previous and previous["status"] not in {"blocked","needs_ocr","needs_review"}:
            old=json.loads(previous["payload"])
            refresh=any(old.get(k)!=c.get(k) for k in ["title","snippet","published_hint","monitoring_direction","query_market"])
            refresh=refresh or (previous["status"]=="done" and
                (datetime.now(timezone.utc)-datetime.fromisoformat(previous["updated_at"])).total_seconds()>=86400)
        with self.db:
            self.db.execute("INSERT INTO candidates(id,url,first_seen,last_seen,status,payload,updated_at) VALUES(?,?,?,?,'new',?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
                (cid,c["url"],c.get("first_seen_at",stamp),stamp,json.dumps(c,ensure_ascii=False),stamp))
            if refresh:
                self.db.execute("UPDATE candidates SET payload=?,status='new',error=NULL,attempts=0,next_attempt='',updated_at=? WHERE id=?",
                                (json.dumps(c,ensure_ascii=False),stamp,cid))
            sight=digest([cid,run_id,c.get("search_query"),c.get("search_provider")])
            self.db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,?,?)",(sight,cid,run_id,json.dumps(c,ensure_ascii=False)))
        return cid
    def pending(self,status,limit):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM candidates WHERE status=? AND next_attempt<=? ORDER BY updated_at,id LIMIT ?",
            (status,now(),limit))]
    def set_status(self,cid,status,error=None,attempt=False,retry_delay=0):
        stamp=now()
        due=(datetime.now(timezone.utc)+timedelta(seconds=retry_delay)).isoformat() if retry_delay else ""
        with self.db:self.db.execute("UPDATE candidates SET status=?,error=?,attempts=attempts+?,next_attempt=?,updated_at=? WHERE id=?",
                                    (status,error,int(attempt),due,stamp,cid))
    def save_document(self,doc):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO documents VALUES(?,?,?)",
                            (doc["version_id"],doc["candidate_id"],json.dumps(doc,ensure_ascii=False)))
            self.db.execute("UPDATE candidates SET status='fetched',error=NULL,updated_at=? WHERE id=?",(now(),doc["candidate_id"]))
    def document(self,cid):
        row=self.db.execute("SELECT payload FROM documents WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1",(cid,)).fetchone()
        return json.loads(row[0]) if row else None
    def chunk(self,key,value=None):
        if value is None:
            row=self.db.execute("SELECT payload FROM chunks WHERE key=?",(key,)).fetchone()
            return json.loads(row[0]) if row else None
        with self.db:self.db.execute("INSERT OR REPLACE INTO chunks VALUES(?,?)",(key,json.dumps(value,ensure_ascii=False)))
    def save_events(self,doc,events,*,status="done"):
        if status not in {"done", "needs_review"}:
            raise ValueError("非法事件保存状态")
        # Event insertion and queue acknowledgement form one transaction.
        with self.db:
            for event in events:
                inserted=self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                    (event["event_id"],doc["version_id"],event["time"]["available_at"],event["market"],
                     event["event_type"],json.dumps(event,ensure_ascii=False))).rowcount
                if inserted:
                    for s in event["securities"]:
                        self.db.execute("INSERT INTO event_securities VALUES(?,?,?,?,?,?)",
                            (event["event_id"],s.get("stock_id"),s.get("symbol"),s["company_name"],
                             s["mapping_status"],s["market"]))
            self.db.execute("UPDATE candidates SET status=?,error=NULL,updated_at=? WHERE id=?",
                            (status,now(),doc["candidate_id"]))
    def queue_counts(self):
        return {r[0]:r[1] for r in self.db.execute("SELECT status,count(*) FROM candidates GROUP BY status")}
    def run(self,report):
        with self.db:self.db.execute("INSERT OR REPLACE INTO runs VALUES(?,?)",(report["run_id"],json.dumps(report,ensure_ascii=False)))

"""Synthetic fixtures below are isolated tests, never real search/news records."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from finance_agent.core import ROOT,digest,dump
from finance_agent.runtime import setup
setup(ROOT)
from finance_agent.config import Config,load
from finance_agent.models import Plan
from finance_agent.database import Repository
from finance_agent.llm import OllamaClient,BudgetExceeded
from finance_agent.provider import TavilyProvider
from finance_agent.fetcher import ContentFetcher,publication,html_body
from finance_agent.safety import public_url,canonical,challenge,FetchError,PublicHTTP
from finance_agent.extraction import grounded,map_security,normalize_fact,Extractor,batches
from finance_agent.pipeline import Engine,import_document
from finance_agent.query import query,export_events

def configuration():
    return Config(collection={"mode":"historical","start_date":"2026-11-01","end_date":"2026-11-05"},
                  fetch={"use_crawl4ai":False})

def candidate():
    return {"candidate_id":"C_"+digest("https://example.com/fixture"),"url":"https://example.com/fixture","title":"测试公告",
            "snippet":"测试正文","source_name":"example.com","published_hint":"2026-11-03",
            "search_provider":"fixture_only","search_query":"银行业","first_seen_at":"2026-11-04T00:00:00+00:00"}

def event():
    return {"event_type":"investment","stage":"proposed","subject":"甲公司","summary":"甲公司拟投资20亿元。",
            "event_time_raw":None,"mentions":[{"name":"甲公司","symbol":"600001","market":"CN","evidence_ids":[1]}],
            "facts":[{"name":"投资金额","value_raw":"20亿元","currency":"CNY","unit":"亿元",
                      "reporting_period":None,"comparison_basis":"not_applicable","evidence_ids":[1]}],
            "evidence_ids":[1]}

BODY="甲公司（600001）披露：拟投资20亿元建设项目；此为计划，尚未实施。"

class FakeLLM:
    calls=0
    model_digest="fixture_model"
    def __init__(self):self.calls=0
    def check(self):return {"digest":self.model_digest}
    def close(self):pass
    def complete(self,purpose,system,payload,schema,validate=None):
        self.calls+=1
        if purpose=="planner":
            value={"queries":[{"query":"银行业 公告","market":"CN","direction":"银行业","reason":"监测"}]}
        elif purpose=="relevance_filter":
            value={"decisions":[{"candidate_id":c["candidate_id"],"relevant":True,"fetch_full_text":True,
                                "category":"announcement","reason":"相关","needs_review":False} for c in payload["candidates"]]}
        else:value={"events":[event()]}
        value=schema.model_validate(value).model_dump()
        return validate(value) if validate else value

class FakeProvider:
    def preflight(self):pass
    def close(self):pass
    def search(self,*args,**kwargs):return [candidate()],{"fixture":True}

class Base(unittest.TestCase):
    def setUp(self):
        parent=ROOT/".runtime/tests";parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.temp.name);self.cfg=configuration()
    def tearDown(self):self.temp.cleanup()
    def repo(self):return Repository(self.root/self.cfg.output.database,self.root)
    def document(self,repo,text=BODY):
        path=self.root/"input.json"
        dump(path,{"source_url":candidate()["url"],"title":"测试公告","text":text,"published_date":"2026-11-03"})
        return import_document(path,self.cfg,repo,self.root)

class ConfigTests(Base):
    def test_valid_and_duplicate_keys(self):
        p=self.root/"config.yaml";dump(p,self.cfg.model_dump())
        self.assertEqual(load(p,self.root).collection.mode,"historical")
        p.write_text("collection: {}\ncollection: {}\n",encoding="utf-8")
        with self.assertRaises(ValueError):load(p,self.root)
    def test_invalid_dates_mode_and_provider_limit(self):
        for change in [{"start_date":"2026-11-10"},{"mode":"realtime"},{"max_candidates_per_query":30},
                       {"max_queries_per_cycle":0},{"mode":"unknown"}]:
            d=self.cfg.model_dump();d["collection"].update(change)
            with self.assertRaises(ValueError):Config.model_validate(d)
    def test_output_paths_cannot_escape_project(self):
        d=self.cfg.model_dump();d["output"]["raw_dir"]="../outside"
        p=self.root/"config.yaml";dump(p,d)
        with self.assertRaises(ValueError):load(p,self.root)
    def test_ollama_must_be_local(self):
        d=self.cfg.model_dump();d["ollama"]["base_url"]="https://external.example"
        with self.assertRaises(ValueError):Config.model_validate(d)

class SafetyTests(Base):
    def resolver(self,host,port,**kw):
        ip="127.0.0.1" if host=="localhost" else "93.184.216.34"
        return [(2,1,6,"",(ip,port))]
    def test_private_credential_and_non_http_urls_rejected(self):
        for url in ["http://localhost/x","file:///x","https://user:pass@example.com","http://example.com:9000"]:
            with self.assertRaises(FetchError):public_url(url,resolver=self.resolver)
    def test_tracking_removed_and_fragment_ignored(self):
        self.assertEqual(canonical("https://EXAMPLE.com/a?x=1&utm_source=test#frag"),"https://example.com/a?x=1")
    def test_verification_page_recognized_without_execution(self):
        self.assertTrue(challenge(b"<script>var acw_sc__v2='no-execution';</script>"))
    def test_redirect_to_private_is_blocked(self):
        http=PublicHTTP(self.cfg.fetch)
        http.client.close()
        http.client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(
            302,headers={"location":"http://localhost/admin"})),follow_redirects=False)
        with patch("finance_agent.safety.socket.getaddrinfo",side_effect=self.resolver):
            with self.assertRaises(FetchError):http.get("https://example.com/x",robots=False)
        http.close()
    def test_robots_disallow(self):
        http=PublicHTTP(self.cfg.fetch);http.client.close()
        http.client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(
            200,text="User-agent: *\nDisallow: /\n")))
        with patch("finance_agent.safety.socket.getaddrinfo",side_effect=self.resolver):
            with self.assertRaises(FetchError):http.get("https://example.com/x")
        http.close()
    def test_response_size_budget_is_enforced(self):
        cfg=self.cfg.fetch.model_copy(update={"max_bytes":1024})
        http=PublicHTTP(cfg);http.client.close()
        http.client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b"x"*1025)))
        with patch("finance_agent.safety.socket.getaddrinfo",side_effect=self.resolver):
            with self.assertRaises(FetchError):http.get("https://example.com/x",robots=False)
        http.close()

    def test_pdf_url_html_rejected(self):
        c=candidate();c["url"]="https://example.com/f.pdf"
        fetcher=ContentFetcher(self.cfg,None,self.root)
        with self.assertRaises(FetchError):
            fetcher.parse(c,b"<html>Not a PDF document at all.</html>",{"url":c["url"],"content_type":"text/html"})

class ExtractionTests(Base):
    def evidence(self):return {1:{"text":BODY,"paragraph_id":"P1","span":[0,len(BODY)]}}
    def test_grounded_fact_passes(self):
        grounded({"events":[event()]},self.evidence())
    def test_invented_numeric_or_name_rejected(self):
        for mutate in ["numeric","name","symbol","stage","currency","id"]:
            e=event()
            if mutate=="numeric":e["summary"]="甲公司拟投资99亿元。"
            elif mutate=="name":e["subject"]="乙公司"
            elif mutate=="symbol":e["mentions"][0]["symbol"]="600999"
            elif mutate=="stage":e["stage"]="completed"
            elif mutate=="currency":e["facts"][0]["currency"]="USD"
            else:e["evidence_ids"]=[99]
            with self.assertRaises(ValueError):grounded({"events":[e]},self.evidence())
    def test_unit_scaling_preserves_raw(self):
        f=normalize_fact(event()["facts"][0])
        self.assertEqual(f["value"],"2000000000")
        self.assertEqual(f["unit"],"元");self.assertEqual(f["unit_raw"],"亿元")
    def test_unmentioned_stock_never_inferred(self):
        result=map_security({"name":"半导体","symbol":None,"market":"CN","evidence_ids":[1]},[],"2026-11-03")
        self.assertIsNone(result["stock_id"])
    def test_historical_and_ambiguous_mapping(self):
        master=[{"stock_id":"CN:SSE:600001","symbol":"600001","company_name":"甲公司","market":"CN",
                 "aliases":[],"valid_from":"2020-01-01","valid_to":None,"source":"fixture"}]
        m=event()["mentions"][0]
        self.assertEqual(map_security(m,master,"2026-11-03")["mapping_status"],"verified")
        self.assertEqual(map_security(m,master,"2019-01-01")["mapping_status"],"unresolved")
        duplicate=dict(master[0],stock_id="CN:SSE:600002")
        self.assertEqual(map_security(m,master+[duplicate],"2026-11-03")["mapping_status"],"ambiguous")
    def test_timestamp_unknown_does_not_become_midnight(self):
        self.assertIsNone(publication("2026-11-03")["published_at"])
        self.assertEqual(publication("2026-11-03")["time_precision"],"date")
        self.assertIsNone(publication("2026-11-03 10:00")["published_at"])
    def test_chunk_coverage_never_truncated(self):
        text="甲"*5000
        groups=batches([{"id":"P1","text":text}])
        self.assertGreater(len(groups),1)
        self.assertEqual(groups[-1][max(groups[-1])]["span"][1],len(text))
    def test_cross_paragraph_exact_reference_completion_is_recorded(self):
        e=event();e["mentions"][0]["evidence_ids"]=[1]
        evidence={1:{"text":"甲公司拟投资20亿元。","paragraph_id":"P1","span":[0,13]},
                  2:{"text":"证券代码600001。","paragraph_id":"P2","span":[0,12]}}
        value=grounded({"events":[e]},evidence)
        self.assertIsInstance(value,dict)
        self.assertEqual(value["events"][0]["mentions"][0]["evidence_ids"],[1,2])
        self.assertEqual(value["events"][0]["evidence_repairs"][0]["added_evidence_id"],2)

    def test_partial_chunks_resume_without_repeating_validated_chunks(self):
        repo=self.repo()
        doc=self.document(repo,BODY*100)
        llm=FakeLLM()
        cfg=self.cfg.model_copy(deep=True);cfg.fetch.max_chunks_per_document_cycle=1
        first=Extractor(cfg,llm,repo,self.root).process(doc)
        self.assertEqual(first["status"],"partial_pending")
        self.assertEqual(llm.calls,1)
        cfg.fetch.max_chunks_per_document_cycle=32
        second=Extractor(cfg,llm,repo,self.root).process(doc)
        self.assertEqual(second["status"],"grounded_candidate")
        self.assertEqual(llm.calls,second["total_chunks"])
        repo.close()

    def test_prediction_fields_are_not_part_of_schema(self):
        from finance_agent.models import Extraction
        e=event();e["predicted_return"]=.2
        with self.assertRaises(ValueError):Extraction.model_validate({"events":[e]})

    def test_needs_ocr_never_calls_model(self):
        repo=self.repo();doc=self.document(repo);doc["quality"]={"status":"needs_ocr"}
        llm=FakeLLM()
        with self.assertRaises(ValueError):Extractor(self.cfg,llm,repo,self.root).process(doc)
        self.assertEqual(llm.calls,0);repo.close()

class StoragePipelineTests(Base):
    def test_recovery_and_idempotent_candidates(self):
        repo=self.repo();c=candidate()
        repo.upsert_candidate(c,"r1");repo.upsert_candidate(c,"r2")
        self.assertEqual(repo.db.execute("SELECT count(*) FROM candidates").fetchone()[0],1)
        repo.set_status(c["candidate_id"],"fetching");repo.recover()
        self.assertEqual(repo.candidate(c["candidate_id"])["status"],"approved");repo.close()
    def test_import_extract_export_and_point_in_time(self):
        repo=self.repo();doc=self.document(repo)
        result=Extractor(self.cfg,FakeLLM(),repo,self.root).process(doc)
        self.assertEqual(len(result["events"]),1)
        e=result["events"][0]
        self.assertGreaterEqual(e["time"]["available_at"],e["time"]["document_available_at"])
        self.assertTrue(e["extraction"]["needs_human_review"])
        self.assertEqual(query(repo,asof="2000-01-01T00:00:00+00:00"),[])
        self.assertEqual(len(query(repo,symbol="600001")),1)
        repo.save_events(doc,result["events"])
        self.assertEqual(len(query(repo)),1)
        paths=export_events(repo,self.root/self.cfg.output.export_dir)
        self.assertEqual(len(paths),1)
        self.assertEqual(json.loads(Path(paths[0]).read_text(encoding="utf-8"))["event_id"],e["event_id"])
        repo.close()
    def test_revised_candidate_is_requeued_and_preserves_first_seen(self):
        repo=self.repo();c=candidate();repo.upsert_candidate(c,"first")
        repo.set_status(c["candidate_id"],"done")
        revised=dict(c,title="修订后的公告",first_seen_at="2026-11-05T01:00:00+00:00")
        repo.upsert_candidate(revised,"second")
        row=repo.candidate(c["candidate_id"])
        self.assertEqual(row["status"],"new")
        self.assertEqual(row["first_seen"],c["first_seen_at"])
        repo.close()

    def test_import_outside_window_is_archived_but_not_processed(self):
        repo=self.repo()
        path=self.root/"old.json"
        dump(path,{"source_url":candidate()["url"],"title":"旧样本","text":BODY,"published_date":"2020-01-01"})
        doc=import_document(path,self.cfg,repo,self.root)
        self.assertEqual(repo.candidate(doc["candidate_id"])["status"],"out_of_range")
        self.assertTrue((self.root/doc["raw_path"]).exists())
        repo.close()

    def test_raw_tampering_stops_extraction(self):
        repo=self.repo();doc=self.document(repo)
        (self.root/doc["raw_path"]).write_bytes(b"tampered")
        with self.assertRaises(ValueError):Extractor(self.cfg,FakeLLM(),repo,self.root).process(doc)
        repo.close()
    def test_queue_backpressure_skips_search_and_model(self):
        cfg=self.cfg.model_copy(deep=True);cfg.collection.max_pending_candidates=100
        repo=self.repo()
        for i in range(100):
            c=candidate();c["candidate_id"]="C_"+str(i);c["url"]+=str(i)
            repo.upsert_candidate(c,"fixture")
        llm=FakeLLM();engine=Engine(cfg,self.root,repo=repo,llm=llm,provider=FakeProvider())
        try:
            report={"budget_pause":False}
            engine.discover(report)
            self.assertTrue(report["budget_pause"]);self.assertEqual(llm.calls,0)
        finally:engine.close()

    def test_end_to_end_mock_is_explicit_fixture(self):
        class Fetcher:
            def __init__(self,outer):self.outer=outer
            def fetch(self,c):
                doc=self.outer.document(repo)
                # Import produces the same canonical candidate id; adapt fixture for this synthetic provider.
                doc["candidate_id"]=c["candidate_id"]
                return doc
        repo=self.repo();llm=FakeLLM()
        engine=Engine(self.cfg,self.root,repo=repo,llm=llm,provider=FakeProvider(),fetcher=Fetcher(self))
        try:
            report=engine.cycle()
            self.assertEqual(report["events_generated"],1)
            self.assertFalse(report["errors"])
            self.assertEqual(report["queries_executed"],1)
        finally:engine.close()

class ModelProviderTests(Base):
    def test_rss_provider_reads_real_format_without_api_key(self):
        from finance_agent.provider import RSSProvider
        cfg=self.cfg.search.model_copy(update={"provider":"rss","rss_feeds":["https://example.com/feed"]})
        raw=b"<rss><channel><item><title>Fixture announcement</title><link>https://example.com/story</link><description>Test only</description><pubDate>Tue, 03 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>"
        class HTTP:
            def get(self,*a,**k):return raw,{"url":"https://example.com/feed"}
        rows,info=RSSProvider(cfg,HTTP()).search("fixture","2026-11-01","2026-11-05",1)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]["search_provider"],"rss")
        self.assertEqual(len(info["feeds"]),1)

    def test_planner_accepts_configured_english_alias_only(self):
        from finance_agent.agents import plan_queries
        cfg=self.cfg.model_copy(deep=True)
        cfg.monitoring.direction_aliases={"半导体":["semiconductor"]}
        class Model:
            def complete(self,purpose,system,payload,schema,validate):
                return validate({"queries":[{"query":"semiconductor earnings","market":"US","direction":"半导体","reason":"test"}]})
        self.assertEqual(len(plan_queries(Model(),cfg,None,"2026-11-01","2026-11-05")),1)

    def test_missing_key_fails_before_network(self):
        with patch.dict(os.environ,{},clear=True):
            p=TavilyProvider(self.cfg.search)
            with self.assertRaises(ValueError):p.preflight()
            p.close()
    def test_tavily_payload_and_secret_not_in_saved_snapshot(self):
        def handler(r):
            body=json.loads(r.content)
            self.assertEqual(body["max_results"],10)
            self.assertFalse(body["include_raw_content"])
            return httpx.Response(200,json={"results":[{"url":"https://example.com/x","title":"x","content":"snippet"}]})
        with patch.dict(os.environ,{"TAVILY_API_KEY":"fixture-secret"}):
            p=TavilyProvider(self.cfg.search,httpx.Client(transport=httpx.MockTransport(handler)))
            items,snapshot=p.search("银行业","2026-11-01","2026-11-05",10)
            self.assertEqual(len(items),1)
            self.assertNotIn("fixture-secret",json.dumps(snapshot))
            p.close()
    def test_schema_retry_and_validated_cache(self):
        calls=[]
        def handler(r):
            if r.url.path.endswith("/tags"):return httpx.Response(200,json={"models":[{"name":"qwen3:8b","digest":"fixture"}]})
            calls.append(json.loads(r.content))
            content="invalid" if len(calls)==1 else json.dumps({"queries":[]})
            return httpx.Response(200,json={"message":{"content":content},"done_reason":"stop"})
        client=OllamaClient(self.cfg.ollama,self.root,httpx.Client(base_url="http://local",transport=httpx.MockTransport(handler)))
        first=client.complete("fixture","test",{},Plan)
        second=client.complete("fixture","test",{},Plan)
        self.assertEqual(len(calls),2);self.assertEqual(first,second)
        self.assertFalse(calls[0]["think"]);client.close()
    def test_context_budget_prevents_network_inference(self):
        llm=OllamaClient(self.cfg.ollama,self.root);llm.model_digest="fixture"
        with self.assertRaises(BudgetExceeded):llm.complete("fixture","甲"*10000,{},Plan)
        self.assertEqual(llm.calls,0);llm.close()
    def test_call_budget_is_enforced_across_requests(self):
        cfg=self.cfg.ollama.model_copy(update={"max_calls_per_cycle":1})
        def handler(r):
            return httpx.Response(200,json={"message":{"content":json.dumps({"queries":[]})},"done_reason":"stop"})
        llm=OllamaClient(cfg,self.root,httpx.Client(base_url="http://local",transport=httpx.MockTransport(handler)))
        llm.model_digest="fixture"
        llm.complete("first","test",{},Plan)
        with self.assertRaises(BudgetExceeded):llm.complete("second","test",{},Plan)
        llm.close()

    def test_concurrent_stages_serialize_model_calls(self):
        import threading,time
        active=[0,0]
        def handler(r):
            active[0]+=1;active[1]=max(active)
            time.sleep(.02)
            active[0]-=1
            return httpx.Response(200,json={"message":{"content":json.dumps({"queries":[]})},"done_reason":"stop"})
        llm=OllamaClient(self.cfg.ollama,self.root,httpx.Client(base_url="http://local",transport=httpx.MockTransport(handler)))
        llm.model_digest="fixture"
        errors=[]
        def work(i):
            try:llm.complete("thread"+str(i),"test",{},Plan)
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=work,args=(i,)) for i in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertFalse(errors);self.assertEqual(active[1],1)
        llm.close()

    def test_pdf_parser_reuse_rejects_html(self):
        from finance_agent.pdf_layout import extract_pdf_layout
        with self.assertRaises(ValueError):extract_pdf_layout(b"<html>not PDF</html>")

if __name__=="__main__":unittest.main()

import json,tempfile,unittest
from pathlib import Path
import httpx
from pydantic import SecretStr
from test_simple_crawler import config,FakeQwen,FakeSources,candidate
from simple_crawler import Crawler, Monitoring
from search_topics import ALL_DIRECTIONS
from discovery import date_windows,broad_discover
from web_sources import Sources,read_html
from qwen_agent import Qwen,OutputError,ModelBudget
from finance_agent.safety import public_url,FetchError

class WideModel(FakeQwen):
    def coverage_plan(self,monitoring,directions,start,end):
        self.calls+=1
        return [{'direction':d,'market':m,'query':('company earnings '+str(i) if m=='US' else d+' 经营新闻')}
            for i,d in enumerate(directions) for m in monitoring.markets]

class WideSources(FakeSources):
    def __init__(self,cfg,empty=False,fail=False):
        super().__init__();self.cfg=cfg;self.requests=[];self.empty=empty;self.search_fail=fail
    def domains(self,market):
        return self.cfg.search.market_domains[market] if self.cfg.search.website_mode=='restricted' else []
    def search(self,query,market,start,end,domains=None,strict_dates=None):
        self.requests.append({'query':query,'market':market,'start':start,'end':end,'domains':domains,'strict_dates':strict_dates})
        if self.search_fail:raise httpx.ConnectTimeout('离线搜索网络故障')
        if self.empty:return []
        item=candidate(str(len(self.requests)));item['url']='https://'+(domains[0] if domains else 'a.example.com')+'/'+str(len(self.requests));item['origin_market']=market
        return [item]

class BroadTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).resolve().parents[1]/'.runtime/test_broad';folder.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=folder);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def crawler(self,dirs=None,markets=None,budget=4,empty=False,fail=False,model=None):
        cfg=config();cfg.monitoring.directions=dirs or ['银行业','半导体'];cfg.monitoring.markets=markets or ['CN','US']
        cfg.search.strategy='broad';cfg.search.api_key=SecretStr('offline-key')
        cfg.search.undated_fallback=False
        cfg.search.market_domains={'CN':['a.example.com','b.example.com'],'US':['c.example.com']}
        cfg.collection.max_queries_per_cycle=budget
        return Crawler(cfg,self.root,WideSources(cfg,empty,fail),model or WideModel())
    def test_default_directions_is_all(self):
        self.assertTrue(Monitoring().all_directions)
    def test_all_directions_sentinel_and_list_alias(self):
        for value in ['all',['all'],' ALL ']:
            monitoring=Monitoring.model_validate({'directions':value})
            self.assertTrue(monitoring.all_directions)
            self.assertEqual(monitoring.resolved_directions,list(ALL_DIRECTIONS))
            self.assertEqual(monitoring.model_dump()['directions'],'all')
    def test_all_and_custom_directions_must_not_mix(self):
        with self.assertRaises(ValueError):Monitoring.model_validate({'directions':['all','银行业']})
    def test_custom_direction_list_remains_supported(self):
        monitoring=Monitoring.model_validate({'directions':['银行业','半导体']})
        self.assertFalse(monitoring.all_directions);self.assertEqual(monitoring.resolved_directions,['银行业','半导体'])
    def test_broad_discovery_expands_all_without_searching_literal_all(self):
        c=self.crawler(markets=['CN'],budget=2,empty=True);c.cfg.monitoring.directions='all'
        result=c.collect()
        self.assertEqual(result['discovery']['industry_market_pairs'],len(ALL_DIRECTIONS))
        self.assertTrue(all(q['direction'] in ALL_DIRECTIONS for q in result['queries']))
        self.assertTrue(all(q['direction']!='all' for q in result['queries']))
    def test_include_keywords_has_no_literal_download_gate(self):
        c=self.crawler(markets=['CN'],budget=1);c.cfg.monitoring.directions='all'
        c.cfg.monitoring.include_keywords=['这串词不会出现在离线测试正文中']
        c.queue(candidate('one'))
        r=c.collect(resume=True)
        self.assertEqual(r['saved'],1)
    def test_empty_include_keywords_passes_empty_angles_to_qwen(self):
        cfg=config();cfg.monitoring.directions='all';cfg.monitoring.include_keywords=[];requests=[]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            requests.append(json.loads(request.content))
            rows=[{'direction':'银行业','market':'CN','query':'银行业 经营变化'}]
            return httpx.Response(200,json={'message':{'content':json.dumps({'queries':rows},ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            q.coverage_plan(cfg.monitoring,['银行业'],'2026-01-01','2026-11-10')
            payload=json.loads(requests[0]['messages'][1]['content'])
            self.assertEqual(payload['include_keywords'],[])
        finally:q.close()
    def test_date_slices_contiguous_latest_first(self):
        self.assertEqual(date_windows('2026-01-01','2026-02-02',31),[('2026-01-03','2026-02-02'),('2026-01-01','2026-01-02')])
        self.assertEqual(date_windows('2026-01-01','2026-02-02',0),[('2026-01-01','2026-02-02')])
    def test_every_direction_and_market_covered_before_repeat(self):
        c=self.crawler(dirs=['银行业','半导体','保险','医药','汽车'],budget=10)
        r=c.collect()
        self.assertEqual(r['searched'],10);self.assertEqual(r['saved'],10)
        self.assertEqual(len({(v['direction'],v['market']) for v in r['queries']}),10)
        self.assertEqual(len(c.sources.requests),10)
        self.assertEqual(r['candidates_new'],10)
    def test_cursor_continues_across_runs(self):
        c=self.crawler(dirs=['银行业','半导体','保险'],markets=['CN'],budget=1,empty=True)
        first=c.collect();second=c.collect()
        self.assertEqual(first['queries'][0]['direction'],'银行业')
        self.assertEqual(second['queries'][0]['direction'],'半导体')
        restored=Crawler(c.cfg,self.root,WideSources(c.cfg,empty=True),WideModel());third=restored.collect()
        self.assertEqual(third['queries'][0]['direction'],'保险')
    def test_source_and_date_both_rotate(self):
        c=self.crawler(dirs=['银行业'],markets=['CN'],budget=1,empty=True)
        c.cfg.collection.start_date='2026-10-01';c.cfg.collection.end_date='2026-11-05'
        reports=[c.collect() for _ in range(3)];jobs=[r['queries'][0] for r in reports]
        self.assertEqual(jobs[0]['phase'],'year')
        self.assertEqual(jobs[0]['domains'],['a.example.com','b.example.com'])
        self.assertNotEqual(jobs[1]['range'],jobs[2]['range'])
        self.assertNotEqual(jobs[1]['domains'],jobs[2]['domains'])
        self.assertTrue(reports[-1]['discovery']['complete'])
    def test_config_change_starts_new_coverage_scope(self):
        c=self.crawler(markets=['CN'],budget=1,empty=True);c.collect()
        c.cfg.monitoring.directions=['医药'];r=c.collect()
        self.assertEqual(r['discovery']['cursor_start'],0);self.assertEqual(r['queries'][0]['direction'],'医药')
    def test_empty_results_still_advance_cursor(self):
        c=self.crawler(markets=['CN'],budget=2,empty=True);r=c.collect()
        self.assertEqual(r['found'],0);self.assertEqual(r['discovery']['cursor_end'],2)
    def test_saved_urls_do_not_call_summary_again(self):
        c=self.crawler(dirs=['银行业'],markets=['CN'],budget=1)
        original=c.sources.search
        def same(*args,**kwargs):
            rows=original(*args,**kwargs);rows[0]['url']='https://a.example.com/same';return rows
        c.sources.search=same;c.collect();r=c.collect()
        self.assertEqual(r['saved'],0);self.assertEqual(r['already_saved'],1);self.assertEqual(r['considered'],0)
    def test_search_failure_stops_after_three_then_consumes_queue(self):
        c=self.crawler(budget=10,fail=True)
        old=candidate('old');old['url']='https://a.example.com/old';c.queue(old)
        r=c.collect()
        self.assertEqual(r['searched'],3);self.assertEqual(r['saved'],1);self.assertTrue(r['discovery']['paused'])
        self.assertEqual(len(c.sources.requests),3)
    def test_all_excluded_domains_do_not_become_unrestricted(self):
        c=self.crawler(markets=['CN'],budget=2,empty=True);c.cfg.search.exclude_domains=['example.com']
        r=c.collect();self.assertEqual(c.sources.requests,[]);self.assertEqual(r['searched'],0)
    def test_unrestricted_strategy_uses_no_site_filter(self):
        c=self.crawler(markets=['CN'],budget=1,empty=True);c.cfg.search.website_mode='unrestricted'
        c.collect();self.assertEqual(c.sources.requests[0]['domains'],[])
    def test_eighty_query_budget_covers_thirty_six_directions(self):
        dirs=['行业'+str(n) for n in range(36)]
        c=self.crawler(dirs=dirs,budget=80,empty=True);r=c.collect()
        self.assertEqual(r['searched'],74);self.assertEqual(len(r['discovery']['directions']),36)
        self.assertTrue(r['discovery']['complete'])
        self.assertEqual(r['model_calls'],18)
    def test_planning_budget_pause_does_not_drop_old_queue(self):
        class PausedModel(WideModel):
            def coverage_plan(self,*args):raise ModelBudget('离线规划预算耗尽')
        c=self.crawler(model=PausedModel());old=candidate('old');old['url']='https://a.example.com/old';c.queue(old)
        r=c.collect();self.assertTrue(r['discovery']['paused']);self.assertEqual(r['saved'],1)
    def test_search_target_is_in_provider_payload_and_response_filter(self):
        c=self.crawler();s=Sources(c.cfg,self.root);requests=[]
        def handler(request):
            requests.append(json.loads(request.content));return httpx.Response(200,json={'results':[
                {'url':'https://a.example.com/a'},{'url':'https://b.example.com/b'},{'url':'https://evil.test/c'}]})
        s.search_http.close();s.search_http=httpx.Client(transport=httpx.MockTransport(handler))
        try:
            rows=s.search('离线','CN','2026-10-01','2026-10-31',domains=['a.example.com'])
            self.assertEqual(len(rows),1);self.assertEqual(requests[0]['include_domains'],['a.example.com'])
            self.assertEqual(requests[0]['end_date'],'2026-11-01')
            with self.assertRaises(ValueError):s.search('离线','CN','2026-10-01','2026-10-31',domains=['evil.test'])
        finally:s.close()
    def test_short_json_description_does_not_replace_full_article(self):
        full='这是完整文章中的具体经营变化和订单信息。'*25
        payload={'@type':'NewsArticle','headline':'经营新闻测试标题','description':'这只是网页导语，不是正文。'*10,'datePublished':'2026-11-02'}
        html='<html><title>经营新闻测试标题</title><script type="application/ld+json">'+json.dumps(payload,ensure_ascii=False)+'</script><main><div>导航</div><section itemprop="articleBody">'+full+'</section></main></html>'
        page=read_html(html.encode('utf-8'),'https://example.com/article',False)
        self.assertIn(full,page['text']);self.assertNotIn('只是网页导语',page['text'])
    def test_xml_encoding_declaration_html_can_be_read(self):
        raw='<?xml version="1.0" encoding="UTF-8"?><html><title>银行公告</title><main>这是公开经营信息测试夹具，不是真实新闻。</main></html>'.encode('utf-8')
        page=read_html(raw,'https://example.com',False)
        self.assertIn('公开经营',page['text'])
    def ipv6_resolver(self,*args,**kwargs):
        return [(2,1,6,'',('198.18.0.108',443)),(23,1,6,'',('2001:2::68',443,0,0))]
    def test_ipv6_fake_ip_allowed_only_in_explicit_mode(self):
        self.assertEqual(public_url('https://www.example.com/a',resolver=self.ipv6_resolver,allow_fake_ip=True),'https://www.example.com/a')
        with self.assertRaises(FetchError):public_url('https://www.example.com/a',resolver=self.ipv6_resolver)
    def test_ipv6_fake_ip_literal_remains_blocked(self):
        with self.assertRaises(FetchError):public_url('https://[2001:2::68]/a',resolver=self.ipv6_resolver,allow_fake_ip=True)
    def test_fake_ip_with_private_address_still_blocked(self):
        def mixed(*args,**kwargs):return self.ipv6_resolver()+[(2,1,6,'',('192.168.1.1',443))]
        with self.assertRaises(FetchError):public_url('https://www.example.com/a',resolver=mixed,allow_fake_ip=True)
    def test_ipv6_local_address_still_blocked(self):
        def local(*args,**kwargs):return [(23,1,6,'',('::1',443,0,0))]
        with self.assertRaises(FetchError):public_url('https://www.example.com/a',resolver=local,allow_fake_ip=True)
    def test_pdf_summary_uses_footer_publication_date(self):
        cfg=config();q=Qwen(cfg.ollama,self.root);seen=[]
        text='测试PDF的经营数据正文。'*130+'发布日期：2026年11月2日'
        def answer(purpose,prompt,payload,schema,validate=None):
            seen.append(payload)
            value={'useful':True,'market':'CN','time':'2026-11-02','date_quote':'发布日期：2026年11月2日','date_kind':'published',
                'category':'测试','industry':'银行业','keywords':['离线测试'],'content':'这是一段用于验证PDF结尾发布时间的离线测试摘要，只保存在临时目录，不是真实新闻。','reason':'离线测试'}
            value.update(industries=[], impact_scope=[])
            return validate(value) if validate else value
        q.ask=answer
        try:
            note=q.summarize({'url':'https://example.com/a.pdf','title':'离线测试','origin_market':'CN','date_context':'原先只看首页','text':text,'cleaning':'PDF'},cfg.monitoring)
            self.assertEqual(note['time'],'2026-11-02')
            self.assertTrue(all('2026年11月2日' in payload['date_context'] for payload in seen))
        finally:q.close()
    def test_coverage_schema_locks_original_labels_for_english_queries(self):
        cfg=config();cfg.monitoring.markets=['CN','US'];requests=[]
        rows=[{'direction':d,'market':m,'query':'industry earnings' if m=='US' else '行业 财报'} for d in ['银行业','半导体'] for m in cfg.monitoring.markets]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            requests.append(json.loads(request.content));return httpx.Response(200,json={'message':{'content':json.dumps({'queries':rows},ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            result=q.coverage_plan(cfg.monitoring,['银行业','半导体'],'2026-01-01','2026-11-10')
            self.assertEqual(len(result),4)
            definition=requests[0]['format']['$defs']['CoverageGroup']
            self.assertEqual(definition['properties']['direction']['enum'],['银行业','半导体'])
            self.assertEqual(definition['properties']['queries']['maxItems'],2)
            payload=json.loads(requests[0]['messages'][1]['content']);self.assertEqual(len(payload['tasks']),4)
            self.assertLessEqual(len(payload['include_keywords']),3)
        finally:q.close()
    def test_planner_removes_accidental_year_without_discarding_query(self):
        cfg=config();seen=[]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            seen.append(json.loads(request.content))
            return httpx.Response(200,json={'message':{'content':json.dumps({'queries':[{'market':'CN','direction':'银行业','query':'银行 2024 公告'}]},ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            rows=q.coverage_plan(cfg.monitoring,['银行业'],'2026-01-01','2026-11-10')
            self.assertNotIn('2024',rows[0]['query']);self.assertEqual(q.calls,1)
            payload=json.loads(seen[0]['messages'][1]['content']);self.assertEqual(payload['date_range'],['2026-01-01','2026-11-10'])
        finally:q.close()

if __name__=='__main__':unittest.main()

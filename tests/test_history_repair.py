import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from test_broad_discovery import WideModel,WideSources
from test_simple_crawler import config,FakeQwen,FakeSources,candidate
from simple_crawler import Crawler
from history_discovery import historical_jobs,history_context
from record_files import Dataset,FIELDS,source_id
from record_quality import quarantine_invalid_publication_records
from web_sources import Sources
from date_evidence import trusted_publication_day,validate_publication_quote
from qwen_agent import grounded_time,ModelBudget,Qwen,OutputError
from finance_agent.core import dump,digest


class HistoryRepairTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).resolve().parents[1]/'.runtime/test_history';folder.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=folder);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def crawler(self,budget=4,empty=True,model=None):
        cfg=config();cfg.monitoring.markets=['CN'];cfg.monitoring.directions=['银行业','半导体']
        cfg.collection.start_date='2016-01-01';cfg.collection.end_date='2019-12-31';cfg.collection.max_queries_per_cycle=budget
        cfg.search.strategy='broad';cfg.search.undated_fallback=False
        cfg.search.market_domains={'CN':['a.example.com','b.example.com']}
        from pydantic import SecretStr
        cfg.search.api_key=SecretStr('offline')
        return Crawler(cfg,self.root,WideSources(cfg,empty=empty),model or WideModel())
    def test_first_searches_reach_each_year_before_second_direction(self):
        c=self.crawler();r=c.collect()
        self.assertEqual([q['year'] for q in r['queries']],[2016,2017,2018,2019])
        self.assertTrue(r['discovery_incomplete']);self.assertEqual(r['status'],'partial')
        self.assertEqual(r['model_calls'],1)
    def test_current_size_budget_covers_every_selected_year(self):
        c=self.crawler(budget=80);c.cfg.monitoring.directions='all'
        c.cfg.collection.start_date='2012-01-01';c.cfg.collection.end_date='2022-05-25'
        r=c.collect()
        self.assertEqual({q['year'] for q in r['queries']},set(range(2012,2023)))
        self.assertEqual(r['searched'],80);self.assertEqual(len(r['year_coverage']),11)
        self.assertEqual(len(r['years_without_records']),11)
    def test_monthly_refinement_rotates_all_sources_and_directions(self):
        c=self.crawler();c.cfg.monitoring.directions='all';c.cfg.collection.start_date='2012-01-01';c.cfg.collection.end_date='2022-05-25'
        jobs=[j for j in historical_jobs(c.cfg,*c.window()) if j['phase']=='window']
        self.assertEqual(len(jobs),125)
        self.assertEqual({j['source_turn']%34 for j in jobs},set(range(34)))
        self.assertEqual(len({j['direction'] for j in jobs}),52)
    def test_checkpoint_restart_does_not_replay_finished_requests(self):
        c=self.crawler(budget=3);first=c.collect()
        restored=Crawler(c.cfg,self.root,WideSources(c.cfg,empty=True),WideModel());second=restored.collect()
        a={(q['direction'],q['market'],tuple(q['range'])) for q in first['queries']}
        b={(q['direction'],q['market'],tuple(q['range'])) for q in second['queries']}
        self.assertFalse(a&b);self.assertEqual(second['queries'][0]['year'],2019)
        self.assertEqual(restored.model.calls,0)
    def test_planning_budget_rollover_keeps_full_http_budget(self):
        c=self.crawler(budget=8);c.cfg.monitoring.directions=['银行业','半导体','保险','医药'];c.cfg.ollama.max_calls_per_cycle=1
        class Limited(WideModel):
            def coverage_plan(self,*args):
                if self.calls>=1:raise ModelBudget('offline calls exhausted')
                return super().coverage_plan(*args)
        c.model=Limited();c.cfg.collection.end_date='2016-12-31'
        r=c.collect()
        self.assertEqual(r['searched'],8);self.assertEqual(r['model_calls'],2)
        self.assertEqual(r['discovery']['planning_rollovers'],1)
    def test_bad_group_plan_falls_back_to_qwen_single_direction(self):
        class Recovering(WideModel):
            def coverage_plan(self,*args):self.calls+=1;raise OutputError('offline invalid group')
            def plan(self,monitoring,*args):
                self.calls+=1;return [{'market':monitoring.markets[0],'query':'offline company earnings'}]
        c=self.crawler(budget=8,model=Recovering());r=c.collect()
        self.assertEqual(r['searched'],8);self.assertEqual(r['discovery']['failed_tasks'],0)
        self.assertEqual({q['year'] for q in r['queries']},{2016,2017,2018,2019})
        self.assertFalse(r['discovery']['paused'])
    def test_complete_history_continues_finite_search_rounds(self):
        c=self.crawler(budget=3);c.cfg.search.complete_history=True;c.cfg.search.date_window_days=0
        r=c.collect()
        self.assertEqual(r['searched'],8);self.assertEqual(r['batches'],3)
        self.assertTrue(r['discovery']['complete']);self.assertFalse(r['discovery_incomplete'])
        self.assertEqual(r['status'],'completed')
        self.assertEqual(len({(q['direction'],tuple(q['range'])) for q in r['queries']}),8)
    def test_fallback_checkpoint_resumes_without_replaying_strict_request(self):
        c=self.crawler(budget=1);c.cfg.search.undated_fallback=True
        first=c.collect();self.assertEqual(first['queries'][0]['stage'],'dated')
        restored=Crawler(c.cfg,self.root,WideSources(c.cfg,empty=True),WideModel());second=restored.collect()
        self.assertEqual(second['queries'][0]['stage'],'undated')
        self.assertEqual(second['queries'][0]['year'],2016)
        self.assertFalse(restored.sources.requests[0]['strict_dates'])
    def test_failed_http_tasks_cannot_claim_search_completion(self):
        c=self.crawler();c.cfg.search.date_window_days=0;c.cfg.search.complete_history=True
        c.sources.search_fail=True;r=c.collect()
        self.assertEqual(r['searched'],3);self.assertFalse(r['discovery']['complete'])
        self.assertEqual(r['discovery']['failed_tasks'],3);self.assertTrue(r['discovery']['paused'])
    def test_failed_search_is_retried_after_restart_before_new_work(self):
        c=self.crawler(budget=3);c.sources.search_fail=True;first=c.collect()
        c.sources.search_fail=False;c.cfg.collection.max_queries_per_cycle=1;second=c.collect()
        self.assertEqual(first['discovery']['failed_tasks'],3)
        self.assertEqual(second['queries'][0]['year'],2016)
        self.assertEqual(second['discovery']['failed_tasks'],2)
    def test_full_history_does_not_retry_failed_article_each_search_round(self):
        c=self.crawler(budget=2,empty=False);c.cfg.search.complete_history=True;c.cfg.search.date_window_days=0
        original=c.sources.search
        def same(*args,**kwargs):
            items=original(*args,**kwargs);items[0]['url']='https://a.example.com/same';return items
        c.sources.search=same;c.sources.fail=True;attempts=[]
        original_fetch=c.sources.fetch
        def fetch(item):attempts.append(item['url']);return original_fetch(item)
        c.sources.fetch=fetch;r=c.collect()
        self.assertEqual(r['searched'],8);self.assertEqual(len(attempts),1)
        self.assertEqual(r['deferred'],1);self.assertTrue(r['discovery']['complete'])
    def test_unscoped_old_queue_is_parked_without_model_calls(self):
        c=self.crawler()
        for n in range(20):
            item=candidate(str(n));item['url']='https://a.example.com/'+str(n)
            c.state['pending'][source_id(item['url'])]=item
        r=c.collect(resume=True)
        self.assertEqual(r['model_calls'],0);self.assertEqual(r['considered'],0)
        self.assertEqual(r['inactive_pending'],20);self.assertEqual(len(c.state['pending']),20)
    def test_rediscovery_reactivates_same_url_in_current_scope(self):
        c=self.crawler(budget=1,empty=False);old=candidate();old['url']='https://a.example.com/1';old['collection_scope']='other'
        c.state['pending'][source_id(old['url'])]=old
        r=c.collect();self.assertEqual(r['considered'],1)
        self.assertEqual(c.state['pending'].get(source_id(old['url']),{}).get('collection_scope',c.scope),c.scope)
    def test_known_out_of_range_publication_is_parked_before_llm(self):
        c=self.crawler();item=candidate();item['url']='https://a.example.com/one';c.queue(item)
        dump(self.root/'.runtime/simple/pages'/(source_id(item['url'])+'.json'),{'url':item['url'],'date_context':'page.datePublished=2026-11-01','text':'offline'})
        r=c.collect(resume=True);self.assertEqual(r['model_calls'],0);self.assertEqual(r['inactive_pending'],1)
    def test_last_modified_date_and_naked_date_cannot_be_publication(self):
        text='Beginning with the July 12, 2017 release. Last Modified Date: July 12, 2017'
        for quote in ['Last Modified Date: July 12, 2017','July 12, 2017']:
            with self.assertRaises(ValueError):grounded_time('2017-07-12',quote,text,'US')
    def test_published_evidence_can_coexist_with_later_modified_date(self):
        text='datePublished=2017-07-10\ndateModified=2017-07-12'
        self.assertEqual(grounded_time('2017-07-10','datePublished=2017-07-10',text,'US'),'2017-07-10')
        self.assertEqual(trusted_publication_day({'date_context':text}),'2017-07-10')
    def test_compact_and_unpadded_source_dates_can_be_grounded(self):
        for quote in ['datePublished=20170712','发布时间：2017/7/12','发布日期：2017-7-12']:
            self.assertEqual(grounded_time('2017-07-12',quote,quote,'CN'),'2017-07-12')
    def test_search_estimate_and_title_year_are_not_publication_evidence(self):
        self.assertIsNone(trusted_publication_day({'date_context':'dateModified=2026-11-01','title':'Annual report 2017','search_published_at':'2017-01-01'}))
    def test_epoch_hint_does_not_confuse_utc_annotation_with_cn_source_day(self):
        page={'origin_market':'CN','date_context':'ctime=1483203600 | UTC=2016-12-31T17:00:00+00:00 | UTC+08=2017-01-01T01:00:00+08:00'}
        self.assertEqual(trusted_publication_day(page),'2017-01-01')
        page['origin_market']='US';self.assertIsNone(trusted_publication_day(page))
    def test_document_publication_is_bound_once_before_reading_footnotes(self):
        cfg=config();seen=[]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            body=json.loads(request.content);fields=body['format']['properties'];seen.append(fields)
            if 'date_kind' in fields:value={'time':'April 13, 2016','date_quote':'Published April 13, 2016','date_kind':'published'}
            else:value={'useful':True,'market':'US','category':'Policy','industry':'Finance','securities':[],'industries':[],'impact_scope':[],
                'keywords':['offline fixture'],'content':'The current article discusses a historical document dated October 21, 2015. This is an offline fixture only.','reason':'offline'}
            return httpx.Response(200,json={'message':{'content':json.dumps(value)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            page={'url':'https://example.com/a','title':'Offline publication binding test','origin_market':'US','date_context':'',
                'text':'Published April 13, 2016. '+('Offline historical fact context. '*180)+'Footnote: Dated October 21, 2015.'}
            note=q.summarize(page,cfg.monitoring)
            self.assertEqual(note['time'],'2016-04-13');self.assertEqual(note['date_kind'],'published')
            self.assertEqual(sum('date_kind' in fields for fields in seen),1)
            self.assertGreater(sum('content' in fields for fields in seen),1)
        finally:q.close()
    def test_late_reference_date_is_not_accepted_as_document_publication(self):
        cfg=config()
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            value={'time':'2015-10-21','date_quote':'Dated October 21, 2015','date_kind':'published'}
            return httpx.Response(200,json={'message':{'content':json.dumps(value)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            with self.assertRaises(OutputError):q.summarize({'url':'https://example.com/a','title':'Offline fixture','origin_market':'US','date_context':'',
                'text':'Published April 13, 2016. '+('Offline historical context. '*150)+'Reference: Dated October 21, 2015'},cfg.monitoring)
            self.assertEqual(q.calls,2)
        finally:q.close()
    def test_pdf_signature_alone_is_not_a_public_release_date(self):
        cfg=config()
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            return httpx.Response(200,json={'message':{'content':json.dumps({'time':'2026-11-02','date_quote':'2026年11月2日','date_kind':'published'},ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            with self.assertRaises(OutputError):q.summarize({'url':'https://example.com/a.pdf','title':'离线夹具','cleaning':'PDF','origin_market':'CN','date_context':'',
                'text':'此为离线测试的会议纪要。董事会签署：2026年11月2日'},cfg.monitoring)
        finally:q.close()
    def test_tavily_publication_filters_and_estimated_dates_are_explicit(self):
        c=self.crawler();sources=Sources(c.cfg,self.root);requests=[]
        def handler(request):
            requests.append(json.loads(request.content));return httpx.Response(200,json={'results':[{'url':'https://a.example.com/one','published_date':'2016-01-05'}]})
        sources.search_http.close();sources.search_http=httpx.Client(transport=httpx.MockTransport(handler))
        try:
            rows=sources.search('offline 2016','CN','2016-01-01','2016-12-31',domains=['a.example.com'],strict_dates=True)
            self.assertTrue(requests[0]['filter_by_published_date']);self.assertTrue(requests[0]['include_published_date'])
            self.assertEqual(rows[0]['search_published_at'],'2016-01-05')
        finally:sources.close()
    def test_bad_publication_is_quarantined_and_requeued(self):
        c=self.crawler();row={'source_id':'bad','url':'https://a.example.com/bad','时间':'2017-07-12','分类':'测试','关键词':['离线'],'主要内容':'仅为离线测试。'}
        c.dataset.save('GLOBAL',row,'测试产业');row=c.dataset.rows['bad'][1]
        dump(self.root/'.runtime/simple/audit/bad.json',{'record_hash':digest(row),'date_quote':'Last Modified Date: July 12, 2017'})
        fixed=quarantine_invalid_publication_records(c)
        self.assertEqual(len(fixed),1);self.assertEqual(len(c.dataset.rows),0)
        self.assertTrue((self.root/fixed[0]['path']).exists());self.assertFalse(any((self.root/'data').glob('*/*.json')))
        self.assertEqual(len(c.state['pending']),1)
    def test_quarantine_backup_failure_never_removes_formal_json(self):
        ds=Dataset(self.root,'data');row={'source_id':'bad','url':'https://example.com/bad','时间':'2017-07-12','分类':'测试','关键词':['离线'],'主要内容':'离线测试。'}
        ds.save('GLOBAL',row,'测试产业');original=list(ds.replicas['bad'].values())
        from finance_agent.core import dump as real_dump
        def fail(path,value):
            if 'quarantine' in str(path):raise OSError('offline backup fault')
            return real_dump(path,value)
        with patch('record_files.dump',side_effect=fail):
            with self.assertRaises(OSError):ds.quarantine('bad','offline')
        self.assertTrue(all(p.exists() for p in original))
        recovered=Dataset(self.root,'data');self.assertEqual(len(recovered.rows),0)
        self.assertEqual(len(list((self.root/'.runtime/simple/quarantine').glob('*.json'))),1)
    def test_normal_yearly_articles_become_individual_json_in_every_year(self):
        c=self.crawler(budget=4);c.cfg.collection.max_documents_per_cycle=1
        class YearSources(WideSources):
            def search(self,query,market,start,end,**kwargs):
                item=candidate(start[:4]);item.update(url='https://a.example.com/'+start[:4],origin_market=market)
                return [item]
            def fetch(self,item):
                self.fetched+=1;day=item['url'][-4:]+'-01-01'
                return dict(item,text='Offline fixture only. Published '+day,date_context='datePublished='+day,fixture_day=day)
        class YearModel(WideModel):
            def summarize(self,page,monitoring):
                result=super().summarize(page,monitoring);result.update(time=page['fixture_day'],date_kind='published');return result
        c.sources=YearSources(c.cfg);c.model=YearModel();r=c.collect()
        rows=[json.loads(p.read_text(encoding='utf-8')) for p in (self.root/'data/CN').glob('*.json')]
        self.assertEqual({r['时间'][:4] for r in rows},{'2016','2017','2018','2019'})
        self.assertTrue(all(set(r)==FIELDS and r['时间信息']['feature_available_at'] for r in rows))
        self.assertEqual(r['saved'],4);self.assertEqual(r['batches'],4)


if __name__=='__main__':unittest.main()

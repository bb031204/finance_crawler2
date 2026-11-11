import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from test_simple_crawler import config,FakeQwen,FakeSources,candidate
from simple_crawler import Crawler,Collection
from record_files import Dataset,FIELDS,record_name,source_id
from record_quality import finalize_record,validate_securities,preserve_securities,language_hint,primary_article_text
from finance_agent.core import dump,digest
from qwen_agent import Qwen,OutputError,preserve_precision
from record_time import with_time_information

class RichRecordTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).resolve().parents[1]/'.runtime/test_rich';folder.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=folder);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_config_date_accepts_unpadded_month_and_day(self):
        cfg=Collection(start_date="2022-5-25",end_date="2026/11/10")
        self.assertEqual(cfg.start_date,"2022-05-25");self.assertEqual(cfg.end_date,"2026-11-10")
    def test_config_date_does_not_accept_impossible_calendar_day(self):
        with self.assertRaises(ValueError):Collection(start_date="2022-2-30",end_date="2026-11-10")
    def test_related_press_releases_are_not_part_of_the_article(self):
        body='Original company earnings and revenue facts.'
        source=body+' × Modal title Also from this source Another unrelated announcement. More Releases From This Source'
        self.assertEqual(primary_article_text(source,'https://www.prnewswire.com/a'),body)
        self.assertEqual(primary_article_text(source,'https://example.com/a'),source)
        self.assertEqual(primary_article_text(body,'https://www.prnewswire.com/a'),body)
    def test_merge_recovers_one_consistent_checked_publication_date(self):
        final={'time':None,'date_quote':''}
        note={'time':'2026-11-02','date_quote':'Published 2026-11-02'}
        self.assertEqual(preserve_precision(final,[note,note]),note)
        self.assertIsNone(preserve_precision({'time':None},[note,dict(note,time='2026-11-03')])['time'])
    def test_rich_chinese_pdf_fits_existing_4096_context(self):
        cfg=config();cfg.ollama.num_ctx=4096;cfg.ollama.num_predict=1400;requests=[]
        source='证券代码：600000。离线正文测试：经营数据收入增加，计划仍需审批。'*35+'公告发布日期：2026年11月2日'
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            requests.append(json.loads(request.content))
            value={'useful':True,'market':'CN','time':'2026-11-02','date_quote':'公告发布日期：2026年11月2日','date_kind':'published',
                'category':'离线夹具','industry':'银行业','securities':[],'industries':[],'impact_scope':[],'keywords':['600000','离线测试'],
                'content':'这是一段离线测试摘要，不是真实公告。'*18,'reason':'offline'}
            fields=json.loads(request.content)['format']['properties'];value={k:v for k,v in value.items() if k in fields}
            return httpx.Response(200,json={'message':{'content':json.dumps(value,ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            note=q.summarize({'url':'https://example.com/a.pdf','title':'离线测试','origin_market':'CN','cleaning':'PDF','text':source,'date_context':source},cfg.monitoring)
            self.assertTrue(note['useful']);self.assertGreater(q.calls,1)
            self.assertTrue(all(r['options']['num_ctx']==4096 for r in requests))
        finally:q.close()
    def row(self,identity='one'):
        return with_time_information({'source_id':identity,'url':'https://example.com/'+identity,'时间':'2026-11-02','分类':'Policy',
            '关键词':['offline fixture'],'主要内容':'An offline fixture only, not a real market announcement.'},available_at='2026-11-03T00:00:00+00:00')
    def test_global_is_available_in_both_markets_with_same_time_information(self):
        ds=Dataset(self.root,'data');row=self.row();ds.save('GLOBAL',row,'Financials')
        self.assertEqual(len(ds.rows),1);self.assertEqual(len(ds.replicas['one']),3)
        for market in ['CN','US','GLOBAL']:
            p=next((self.root/'data'/market).glob('*.json'))
            self.assertEqual(json.loads(p.read_text(encoding='utf-8')),row)
            self.assertEqual(len(ds.market_rows(market)),1)
        restored=Dataset(self.root,'data');self.assertEqual(restored.rows['one'][0],'GLOBAL')
    def test_global_update_removes_all_stale_filenames(self):
        ds=Dataset(self.root,'data');ds.save('GLOBAL',self.row(),'Financials');old=list(ds.replicas['one'].values())
        ds.save('GLOBAL',dict(self.row(),分类='Macro'),'Economy')
        self.assertTrue(all(not p.exists() for p in old));self.assertEqual(len(ds.replicas['one']),3)
    def test_global_to_one_market_removes_other_views(self):
        ds=Dataset(self.root,'data');ds.save('GLOBAL',self.row(),'Financials');ds.save('CN',self.row(),'Financials')
        self.assertEqual(len(list((self.root/'data/GLOBAL').glob('*.json'))),0)
        self.assertEqual(len(list((self.root/'data/US').glob('*.json'))),0)
        self.assertEqual(Dataset(self.root,'data').rows['one'][0],'CN')
    def test_global_mirror_does_not_overwrite_unrelated_same_name(self):
        ds=Dataset(self.root,'data');old=ds.save('CN',self.row('local'),'Financials')
        ds.save('GLOBAL',self.row('global'),'Financials')
        self.assertTrue(old.exists());self.assertEqual(len(ds.market_rows('CN')),2)
        self.assertEqual(len(Dataset(self.root,'data').rows),2)
    def test_legacy_global_is_synchronized_without_new_model_call(self):
        row=self.row();folder=self.root/'data/GLOBAL';folder.mkdir(parents=True)
        dump(folder/record_name(row,'Financials'),row)
        ds=Dataset(self.root,'data');model=FakeQwen();report=ds.migrate(model)
        self.assertEqual(report['global_synced'],1);self.assertEqual(model.calls,0)
        self.assertEqual(len(Dataset(self.root,'data').replicas['one']),3)
    def test_inconsistent_global_mirror_is_not_silently_accepted(self):
        ds=Dataset(self.root,'data');ds.save('GLOBAL',self.row(),'Financials')
        p=ds.replicas['one']['CN'];dump(p,dict(self.row(),主要内容='Changed local view'))
        with self.assertRaises(ValueError):Dataset(self.root,'data')
    def test_global_partial_write_recovers_all_three_views(self):
        ds=Dataset(self.root,'data')
        from finance_agent.core import dump as real_dump
        def fail_cn(path,value):
            if Path(path).parent==self.root/'data/CN':raise OSError('offline disk fault')
            return real_dump(path,value)
        with patch('record_files.dump',side_effect=fail_cn):
            with self.assertRaises(OSError):ds.save('GLOBAL',self.row(),'Financials')
        self.assertTrue(ds.outbox.exists());restored=Dataset(self.root,'data')
        self.assertEqual(len(restored.replicas['one']),3);self.assertFalse(ds.outbox.exists())
    def fixture_source(self):
        url='https://example.com/one';identity=source_id(url);raw=b'Raw offline HTML fixture'
        page={'url':url,'title':'Offline fixture','origin_market':'CN','text':'This is an offline source fixture, not genuine financial data.','date_context':'Published 2026-11-02','raw_sha256':digest(raw),'fetched_at':'2026-11-02T10:00:00+00:00'}
        page_path=self.root/'.runtime/simple/pages'/(identity+'.json');dump(page_path,page)
        raw_path=self.root/'.runtime/validation'/(identity+'.html');raw_path.parent.mkdir(parents=True);raw_path.write_bytes(raw)
        row=self.row(identity);row['url']=url
        ds=Dataset(self.root,'data');ds.save('GLOBAL',row,'Financials')
        return ds,row,page,page_path,raw_path
    def test_cleanup_after_all_views_and_audit_are_verified(self):
        ds,row,page,page_path,raw_path=self.fixture_source()
        audit=finalize_record(self.root,ds,row,{'date_quote':'Published 2026-11-02'},page,{'url':page['url']},FakeQwen(),page_path)
        self.assertFalse(page_path.exists());self.assertFalse(raw_path.exists())
        self.assertEqual(len(audit['raw_deleted']),2);self.assertEqual(audit['record_hash'],digest(row))
        self.assertEqual(len(audit['record_paths']),3);self.assertTrue(audit['feature_available_at'])
    def test_audit_failure_never_deletes_raw(self):
        ds,row,page,page_path,raw_path=self.fixture_source()
        with patch('record_quality.dump',side_effect=OSError('offline audit fault')):
            with self.assertRaises(OSError):finalize_record(self.root,ds,row,{},page,{'url':page['url']},FakeQwen(),page_path)
        self.assertTrue(page_path.exists());self.assertTrue(raw_path.exists())
    def test_changed_raw_hash_is_preserved(self):
        ds,row,page,page_path,raw_path=self.fixture_source();raw_path.write_bytes(b'Different source')
        audit=finalize_record(self.root,ds,row,{},page,{'url':page['url']},FakeQwen(),page_path)
        self.assertFalse(page_path.exists());self.assertTrue(raw_path.exists());self.assertEqual(len(audit['raw_deleted']),1)
    def test_disable_raw_deletion_keeps_verified_cache(self):
        ds,row,page,page_path,raw_path=self.fixture_source()
        audit=finalize_record(self.root,ds,row,{},page,{'url':page['url']},FakeQwen(),page_path,False)
        self.assertTrue(page_path.exists());self.assertTrue(raw_path.exists());self.assertEqual(audit['raw_deleted'],[])
    def test_bad_formal_copy_prevents_cleanup(self):
        ds,row,page,page_path,raw_path=self.fixture_source();dump(ds.replicas[row['source_id']]['US'],dict(row,主要内容='Different view'))
        with self.assertRaises(ValueError):finalize_record(self.root,ds,row,{},page,{'url':page['url']},FakeQwen(),page_path)
        self.assertTrue(page_path.exists());self.assertTrue(raw_path.exists())
    def test_deleted_cache_can_reprocess_saved_urls_across_batches(self):
        c=Crawler(config(),self.root,FakeSources(),FakeQwen());c.cfg.collection.max_documents_per_cycle=1
        for n in range(3):c.queue(candidate(str(n)))
        first=c.collect(resume=True);self.assertEqual(first['saved'],3)
        self.assertEqual(list((self.root/'.runtime/simple/pages').glob('*.json')),[])
        again=c.collect(reprocess=True);self.assertEqual(again['saved'],3)
        self.assertEqual(c.sources.fetched,6)
    def test_failed_extraction_keeps_source_cache(self):
        c=Crawler(config(),self.root,FakeSources(),FakeQwen(missing=True));c.queue(candidate())
        result=c.run(resume=True);self.assertEqual(result['saved'],0)
        self.assertEqual(len(list((self.root/'.runtime/simple/pages').glob('*.json'))),1)
    def test_requeue_keeps_the_original_first_seen_time(self):
        c=Crawler(config(),self.root,FakeSources(),FakeQwen());c.queue(candidate())
        first=c.state['pending'][source_id(candidate()['url'])]['first_seen_at']
        c.run(resume=True);c.queue(candidate())
        self.assertEqual(c.state['pending'][source_id(candidate()['url'])]['first_seen_at'],first)
    def test_stock_name_and_code_are_preserved_in_keywords_and_content(self):
        source='Apple Inc. (NASDAQ: AAPL) reported quarterly sales growth.'
        entity={'name':'Apple Inc.','ticker':'AAPL','quote':'Apple Inc. (NASDAQ: AAPL)'}
        final={'content':'Apple Inc. reported quarterly sales growth.','keywords':['earnings'],'securities':[]}
        result=preserve_securities(final,[{'securities':[entity]}],source,'en')
        self.assertIn('AAPL',result['keywords']);self.assertIn('Apple Inc.',result['keywords']);self.assertIn('AAPL',result['content'])
    def test_unmentioned_stock_code_is_rejected(self):
        with self.assertRaises(ValueError):validate_securities([{'name':'Apple','ticker':'AAPL','quote':'Apple reported earnings.'}],'Apple reported earnings.')
    def test_company_without_original_code_stays_without_code(self):
        result=validate_securities([{'name':'Apple','ticker':None,'quote':'Apple reported earnings.'}],'Apple reported earnings.')
        self.assertIsNone(result[0]['ticker'])
    def test_language_hint_keeps_english_articles_english(self):
        self.assertEqual(language_hint('NVIDIA reported revenue growth and stronger demand for its chips.'),'en')
        self.assertEqual(language_hint('浦发银行公布季度经营数据，净利润和营业收入同比变化。'),'zh')
    def test_english_article_cannot_be_silently_translated_to_chinese(self):
        cfg=config()
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            value={'useful':True,'market':'US','time':'2026-11-02','date_quote':'Published 2026-11-02','date_kind':'published','category':'Earnings','industry':'Technology','securities':[],'industries':[],'impact_scope':[],'keywords':['earnings'],
                'content':'这是一段故意使用错误语言的离线测试摘要，原文为英文，程序应该要求模型改用英文，不写入正式数据。','reason':'offline'}
            fields=json.loads(request.content)['format']['properties'];value={k:v for k,v in value.items() if k in fields}
            return httpx.Response(200,json={'message':{'content':json.dumps(value,ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            with self.assertRaises(OutputError):q.summarize({'url':'https://example.com/a','title':'Earnings','origin_market':'US','text':'Apple reported quarterly revenue growth.','date_context':'Published 2026-11-02'},cfg.monitoring)
            self.assertEqual(q.calls,3)
        finally:q.close()
    def test_planning_accepts_two_different_queries_for_a_direction(self):
        cfg=config();cfg.monitoring.markets=['CN']
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            rows=[{'direction':'银行业','market':'CN','query':'银行业 经营变化'},{'direction':'银行业','market':'CN','query':'银行业 分析 观点'}]
            return httpx.Response(200,json={'message':{'content':json.dumps({'queries':rows},ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:self.assertEqual(len(q.coverage_plan(cfg.monitoring,['银行业'],'2026-01-01','2026-11-10')),2)
        finally:q.close()

    def test_planning_accepts_uneven_query_counts_across_industries(self):
        cfg=config();cfg.monitoring.markets=['CN']
        rows=[{'direction':d,'market':'CN','query':term} for d,term in [('银行业','银行 业绩'),('银行业','银行 贷款'),('银行业','银行 观点'),('半导体','半导体 订单')]]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            return httpx.Response(200,json={'message':{'content':json.dumps({'queries':rows},ensure_ascii=False)}})
        q=Qwen(cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            result=q.coverage_plan(cfg.monitoring,['银行业','半导体'],'2026-01-01','2026-11-10')
            self.assertEqual(len(result),3)
            self.assertEqual(sum(row['direction']=='银行业' for row in result),2)
            self.assertEqual(q.calls,1)
        finally:q.close()

if __name__=='__main__':unittest.main()

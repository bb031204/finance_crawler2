import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from finance_agent.runtime import setup
setup(Path(__file__).resolve().parents[1])
from simple_crawler import Config,Dataset,Crawler,FIELDS,load_config,source_id
from record_files import LEGACY_ANALYSIS_FIELDS
from record_time import with_time_information
from record_files import record_name, source_key, original_time, safe_part
from qwen_agent import preserve_precision,Note,parse_time,grounded_time,Qwen,Decision,ModelBudget,OutputError
from web_sources import read_html,decode_html,Sources
from finance_agent.core import dump
from finance_agent.safety import public_url,FetchError


def config(**updates):
    cfg=Config.model_validate({'collection':{'start_date':'2026-11-01','end_date':'2026-11-05'},
        'monitoring':{'markets':['CN']},'search':{'market_domains':{'CN':['example.com']}}})
    for k,v in updates.items():setattr(cfg,k,v)
    return cfg


def candidate(n='one'):
    return {'url':'https://example.com/'+n,'title':'示例银行实质经营变化','snippet':'仅用于离线测试的虚构夹具','origin_market':'CN'}

class FakeSources:
    def __init__(self, fail=False):self.fetched=0;self.fail=fail
    def domains(self,m):return ['example.com']
    def allowed(self,u,m):return 'example.com' in u
    def fetch(self,c):
        self.fetched+=1
        if self.fail:raise FetchError('network_error','离线网络故障夹具')
        return {'url':c['url'],'title':c['title'],'origin_market':'CN','date_context':'发布时间 2026-11-02','text':'离线测试夹具文章内容'*50}
    def close(self):pass

class FakeQwen:
    def __init__(self,useful=True,missing=False,budget=False):self.calls=0;self.cache_hits=0;self.useful=useful;self.missing=missing;self.budget=budget
    def screen(self,c,m):
        self.calls+=1
        if self.budget:raise ModelBudget('离线调用上限夹具')
        return {'useful':self.useful,'reason':'离线筛选夹具'}
    def summarize(self,p,m):
        self.calls+=1
        return {'useful':True,'market':'CN','time':None if self.missing else '2026-11-02','category':'离线夹具','industry':'银行业','keywords':['测试'],
            'content':'这是一段仅在临时目录中保存的离线测试摘要，用于确认程序不会把测试样本写入真实数据目录。'}
    def industry(self,row):
        self.calls+=1
        return "银行业"
    def close(self):pass

class SimpleTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).resolve().parents[1]/'.runtime/test_simple';folder.mkdir(exist_ok=True,parents=True)
        self.tmp=tempfile.TemporaryDirectory(dir=folder);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def crawler(self,model=None,sources=None):return Crawler(config(),self.root,sources or FakeSources(),model or FakeQwen())
    def test_auto_batches_drain_existing_queue(self):
        c=self.crawler();c.cfg.collection.max_documents_per_cycle=2
        for n in range(7):c.queue(candidate(str(n)))
        r=c.collect(resume=True)
        self.assertEqual(r['batches'],4);self.assertEqual(r['saved'],7)
        self.assertEqual(r['pending'],0);self.assertEqual(r['searched'],0)
        self.assertEqual(len(list((self.root/'data/CN').glob('*.json'))),7)
    def discovery_crawler(self):
        from pydantic import SecretStr
        class SearchSources(FakeSources):
            searches=0
            def search(self,*args):
                self.searches+=1
                return [candidate(str(n)) for n in range(7)]
        class PlanningModel(FakeQwen):
            def plan(self,monitoring,limit,*dates):
                self.calls+=1
                return [{'market':'CN','query':'离线测试'}]
        c=self.crawler(PlanningModel(),SearchSources());c.cfg.search.api_key=SecretStr('offline-fixture')
        c.cfg.collection.max_documents_per_cycle=2
        return c
    def test_auto_batches_search_only_once(self):
        c=self.discovery_crawler();r=c.collect()
        self.assertEqual(c.sources.searches,1);self.assertEqual(r['searched'],1)
        self.assertEqual(r['found'],7);self.assertEqual(r['saved'],7)
        self.assertEqual(r['model_calls'],15)
    def test_auto_batches_do_not_retry_network_failures(self):
        class MixedSources(FakeSources):
            attempts={}
            def fetch(self,item):
                self.attempts[item['url']]=self.attempts.get(item['url'],0)+1
                if item['url'].endswith('bad'):raise FetchError('network_error','离线测试故障')
                return super().fetch(item)
        c=self.crawler(sources=MixedSources());c.cfg.collection.max_documents_per_cycle=1
        c.queue(candidate('bad'));c.queue(candidate('good'))
        r=c.collect(resume=True)
        self.assertEqual(r['saved'],1);self.assertEqual(r['deferred'],1);self.assertEqual(r['unprocessed'],0)
        self.assertEqual(c.sources.attempts[candidate('bad')['url']],1)
    def test_auto_batches_missing_dates_finish_without_loop(self):
        c=self.crawler(FakeQwen(missing=True));c.cfg.collection.max_documents_per_cycle=1
        c.queue(candidate('a'));c.queue(candidate('b'));r=c.collect(resume=True)
        self.assertEqual(r['batches'],2);self.assertEqual(r['deferred'],2)
        self.assertEqual(r['model_calls'],4);self.assertEqual(r['unprocessed'],0)
    def test_auto_batches_context_failure_continues_other_records(self):
        class ContextModel(FakeQwen):
            def screen(self,item,monitoring):
                if item['url'].endswith('bad'):
                    self.calls+=1
                    raise ModelBudget('离线上下文不足')
                return super().screen(item,monitoring)
        c=self.crawler(ContextModel());c.queue(candidate('bad'));c.queue(candidate('good'))
        r=c.collect(resume=True)
        self.assertEqual(r['saved'],1);self.assertEqual(r['batches'],2);self.assertEqual(r['deferred'],1)
    def test_auto_batches_resume_call_budget_and_reuse_page(self):
        class OnceBudget(FakeQwen):
            tried=False
            def summarize(self,page,monitoring):
                if not self.tried:
                    self.tried=True;self.calls=30
                    raise ModelBudget('离线调用上限')
                return super().summarize(page,monitoring)
        c=self.crawler(OnceBudget());c.queue(candidate())
        r=c.collect(resume=True,force=True)
        self.assertEqual(r['batches'],2);self.assertEqual(r['saved'],1)
        self.assertEqual(c.sources.fetched,1)
    def test_auto_batches_budget_continuation_bounded(self):
        class AlwaysBudget(FakeQwen):
            def screen(self,item,monitoring):
                if item['url'].endswith('bad'):
                    self.calls=30
                    raise ModelBudget('离线调用上限')
                return super().screen(item,monitoring)
        c=self.crawler(AlwaysBudget());c.queue(candidate('bad'));c.queue(candidate('good'))
        r=c.collect(resume=True)
        self.assertEqual(r['batches'],5);self.assertEqual(r['saved'],1);self.assertEqual(r['deferred'],1)
        self.assertEqual(r['budget_continuations'][source_id(candidate('bad')['url'])],3)
    def test_once_keeps_single_batch_option(self):
        c=self.crawler();c.cfg.collection.max_documents_per_cycle=2
        for n in range(5):c.queue(candidate(str(n)))
        r=c.collect(resume=True,one_batch=True)
        self.assertEqual(r['batches'],1);self.assertEqual(r['saved'],2)
        self.assertEqual(r['unprocessed'],3)
    def test_main_default_drains_all_batches(self):
        import simple_crawler
        c=self.discovery_crawler()
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=c.cfg),patch.object(simple_crawler,'Crawler',return_value=c):
            result=simple_crawler.main([])
        self.assertEqual(result,0);self.assertEqual(len(c.dataset.rows),7)
        self.assertEqual(c.sources.searches,1)
    def test_main_resume_also_drains_all_batches(self):
        import simple_crawler
        c=self.crawler();c.cfg.collection.max_documents_per_cycle=1
        for n in range(3):c.queue(candidate(str(n)))
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=c.cfg),patch.object(simple_crawler,'Crawler',return_value=c):
            result=simple_crawler.main(['--resume'])
        self.assertEqual(result,0);self.assertEqual(len(c.dataset.rows),3)
    def test_output_fields_include_time_information_and_dedup(self):
        c=self.crawler();c.queue(candidate());c.run(resume=True)
        row=json.loads(next((self.root/'data/CN').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(set(row),FIELDS);self.assertEqual(row['source_id'],source_id(candidate()['url']))
        c.queue(candidate());c.run(resume=True)
        self.assertEqual(len(list((self.root/'data/CN').glob('*.json'))),1)
        self.assertEqual(c.sources.fetched,1)
    def test_deleted_dataset_can_be_rebuilt(self):
        c=self.crawler();c.queue(candidate());c.run(resume=True)
        next((self.root/'data/CN').glob('*.json')).unlink()
        restored=self.crawler();restored.queue(candidate());result=restored.run(resume=True)
        self.assertEqual(result['saved'],1);self.assertEqual(restored.sources.fetched,1)
    def test_changed_scope_reconsiders_previous_rejection(self):
        c=self.crawler(FakeQwen(useful=False));c.queue(candidate());c.run(resume=True)
        cfg=config();cfg.monitoring.directions=['新监测范围']
        c=Crawler(cfg,self.root,FakeSources(),FakeQwen());c.queue(candidate());r=c.run(resume=True)
        self.assertEqual(r['saved'],1)
    def test_unhelpful_does_not_fetch(self):
        c=self.crawler(FakeQwen(useful=False));c.queue(candidate());r=c.run(resume=True)
        self.assertEqual(c.sources.fetched,0);self.assertEqual(r['total_records'],0)
    def test_missing_date_not_formal(self):
        c=self.crawler(FakeQwen(missing=True));c.queue(candidate());r=c.run(resume=True)
        self.assertEqual(r['pending'],1);self.assertEqual(r['saved'],0)
        self.assertEqual(len(list((self.root/'data/CN').glob('*.json'))),0)
    def test_resume_uses_cached_page(self):
        c=self.crawler(FakeQwen(missing=True));c.queue(candidate());c.run(resume=True)
        c.model=FakeQwen();c.run(resume=True);self.assertEqual(c.sources.fetched,1)
        self.assertEqual(len(c.dataset.rows),1)
    def test_network_error_keeps_pending(self):
        c=self.crawler(sources=FakeSources(fail=True));c.queue(candidate());r=c.run(resume=True)
        self.assertEqual(r['pending'],1);self.assertEqual(len(r['errors']),1)
    def test_budget_preserves_queue(self):
        c=self.crawler(FakeQwen(budget=True));c.queue(candidate());r=c.run(resume=True)
        self.assertTrue(r['budget_pause']);self.assertEqual(r['pending'],1)
    def test_queue_rotates_failed_items(self):
        c=self.crawler(sources=FakeSources(fail=True));c.cfg.collection.max_documents_per_cycle=1
        c.queue(candidate('one'));c.queue(candidate('two'));c.run(resume=True)
        self.assertTrue(next(iter(c.state['pending'].values()))['url'].endswith('two'))
    def test_scope_rechecked_before_model(self):
        c=self.crawler();item=candidate();item['url']='https://other.com/a';c.queue(item);r=c.run(resume=True)
        self.assertEqual(c.model.calls,0);self.assertEqual(r['saved'],0)
    def test_outbox_recovery(self):
        row={'source_id':'test','url':'https://example.com','时间':'2026-11-02','分类':'测试','关键词':['测试'],'主要内容':'离线测试'}
        dump(self.root/'.runtime/simple/record_outbox.json',{'market':'CN','row':row,'target':'data/CN/'+record_name(row,'银行业'),'previous':None})
        ds=Dataset(self.root,'data');self.assertIn('test',ds.rows)
        self.assertFalse(ds.outbox.exists());self.assertEqual(len(list((self.root/'data/CN').glob('*.json'))),1)
    def sample_row(self, identity='one'):
        return with_time_information({'source_id':identity,'url':'https://www.cls.cn/detail/'+identity,'时间':'2026-11-02T20:30:58+08:00',
            '分类':'监管政策','关键词':['离线测试'],'主要内容':'仅为离线测试夹具，不是真实金融新闻。'},available_at='2026-11-03T00:00:00+00:00')
    def test_filename_uses_date_category_industry_website(self):
        self.assertEqual(record_name(self.sample_row(),'银行业'),'2026_11_02_20_30_监管政策_银行业_cls.json')
        self.assertEqual(source_key('https://static.cninfo.com.cn/a.pdf'),'cninfo')
        self.assertEqual(source_key('https://www.jvlang.com/a'),'jvlang')
    def test_date_only_does_not_invent_clock(self):
        self.assertEqual(original_time('2026-11-02'),'2026_11_02')
    def test_filename_sanitizes_windows_invalid_characters(self):
        name=record_name(dict(self.sample_row(),分类='政策/公告:*'), '银行/金融')
        self.assertNotIn('/',name);self.assertNotIn(':',name);self.assertNotIn('*',name)
        self.assertNotEqual(safe_part('CON').upper(),'CON')
    def test_same_minute_collision_keeps_both_records(self):
        ds=Dataset(self.root,'data');a=ds.save('CN',self.sample_row('a'),'银行业');b=ds.save('CN',self.sample_row('b'),'银行业')
        self.assertNotEqual(a,b);self.assertEqual(len(list((self.root/'data/CN').glob('*.json'))),2)
        self.assertEqual(Dataset(self.root,'data').industries,{'a':'银行业','b':'银行业'})
    def test_update_renames_and_changes_market_without_stale_copy(self):
        ds=Dataset(self.root,'data');old=ds.save('CN',self.sample_row(),'银行业')
        new=ds.save('US',dict(self.sample_row(),分类='财报'),'半导体')
        self.assertFalse(old.exists());self.assertTrue(new.exists())
        self.assertEqual(Dataset(self.root,'data').rows['one'][0],'US')
    def test_jsonl_migration_preserves_content_and_adds_time_information(self):
        folder=self.root/'data';folder.mkdir();row=self.sample_row()
        row.pop('时间信息')
        for market in ['CN','US','GLOBAL']:(folder/(market+'.jsonl')).write_text(json.dumps(row,ensure_ascii=False)+'\n' if market=='CN' else '',encoding='utf-8')
        model=FakeQwen();ds=Dataset(self.root,'data');report=ds.migrate(model)
        self.assertEqual(report['migrated'],1);self.assertTrue(Path(report['backup']).exists())
        self.assertEqual(list(folder.glob('*.jsonl')),[]);self.assertTrue((folder/'GLOBAL').is_dir())
        saved=json.loads(next((folder/'CN').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual({k:saved[k] for k in row},row)
        self.assertEqual(set(saved), LEGACY_ANALYSIS_FIELDS)
        self.assertEqual(saved['时间信息']['availability_basis'],'migration')
        again=Dataset(self.root,'data');again.migrate(model);self.assertEqual(model.calls,1)
    def test_failed_industry_keeps_original_jsonl(self):
        folder=self.root/'data';folder.mkdir();row=self.sample_row()
        path=folder/'CN.jsonl';path.write_text(json.dumps(row,ensure_ascii=False)+'\n',encoding='utf-8')
        model=FakeQwen()
        def fail(row):raise OutputError('离线失败')
        model.industry=fail
        with self.assertRaises(OutputError):Dataset(self.root,'data').migrate(model)
        self.assertTrue(path.exists());self.assertEqual(json.loads(path.read_text(encoding='utf-8')),row)
    def test_output_root_bound(self):
        p=self.root/'bad.yaml';p.write_text('collection:\n  start_date: "2026-11-01"\n  end_date: "2026-11-05"\nsearch:\n  website_mode: unrestricted\noutput_dir: ../escape',encoding='utf-8')
        with self.assertRaises(ValueError):load_config(p,self.root)
    def test_duplicate_yaml_hidden(self):
        p=self.root/'duplicate.yaml';p.write_text('search:\n  api_key: secret-offline\nsearch: {}',encoding='utf-8')
        with self.assertRaises(ValueError) as error:load_config(p,self.root)
        self.assertNotIn('secret-offline',str(error.exception))
    def test_source_date_outside_window_not_saved(self):
        c=self.crawler();c.cfg.collection.start_date='2026-11-03';c.queue(candidate());r=c.run(resume=True)
        self.assertEqual(r['saved'],0);self.assertEqual(r['pending'],0)
    def test_time_exact_source(self):
        quote='发布时间：2026-11-02 20:30:58'
        self.assertEqual(grounded_time('2026-11-02T20:30:58+08:00',quote,quote,'CN'),'2026-11-02T20:30:58+08:00')
    def test_invented_date_rejected(self):
        with self.assertRaises(ValueError):grounded_time('2026-11-03','2026-11-02','2026-11-02','CN')
        with self.assertRaises(ValueError):grounded_time('2026-11-03','2026-11-03','无日期正文','CN')
    def test_date_plus_zone_without_clock_rejected(self):
        with self.assertRaises(ValueError):parse_time("2026-11-02+08:00","CN")
        with self.assertRaises(ValueError):Note.model_validate({"useful":True,"market":"CN","time":"2026-11-02+08:00","date_quote":"","category":"测试","keywords":[],"content":"","reason":""})
    def test_minute_precision_does_not_invent_seconds(self):
        quote='发布时间2026-10-11 01:42'
        self.assertEqual(grounded_time('2026-10-11T01:42:29+08:00',quote,quote,'CN'),'2026-10-11T01:42+08:00')
    def test_adjacent_minute_not_accepted(self):
        quote='发布时间2026-10-11 01:42'
        with self.assertRaises(ValueError):grounded_time('2026-10-11T01:41:59+08:00',quote,quote,'CN')
    def test_chinese_date_with_spaces(self):
        quote='发布日期 2026 年 11 月 2 日'
        self.assertEqual(grounded_time('2026-11-02',quote,quote,'CN'),'2026-11-02')
    def test_merge_keeps_confirmed_clock(self):
        result=preserve_precision({'time':'2026-11-02','date_quote':'2026-11-02'},[{'time':'2026-11-02T20:30:58+08:00','date_quote':'PubDate=2026-11-02 20:30:58'}])
        self.assertEqual(result['time'],'2026-11-02T20:30:58+08:00')
    def test_merge_does_not_guess_conflicting_clocks(self):
        result=preserve_precision({'time':'2026-11-02','date_quote':'2026-11-02'},[{'time':'2026-11-02T20:30:58+08:00','date_quote':'a'},{'time':'2026-11-02T21:30:58+08:00','date_quote':'b'}])
        self.assertEqual(result['time'],'2026-11-02')
    def test_epoch_proof(self):
        q='article.ctime=1793784512'
        self.assertEqual(grounded_time('2026-11-04T17:28:32+08:00',q,q,'CN'),'2026-11-04T17:28:32+08:00')
    def test_gbk_and_utf8_decode(self):
        text='<html><title>银行公告</title><article>中文正文</article></html>'
        self.assertIn('银行公告',decode_html(text.encode('gb18030'),'text/html; charset=gb2312'))
        self.assertIn('银行公告',decode_html(text.encode('utf-8')))
    def test_embedded_json_main_article_only(self):
        body='这是一段主文章的重要经营信息。'*20
        payload={'article':{'id':123456,'title':'主文章标题测试','ctime':1793784512,'content':body},'recommend':[{'id':99999,'title':'推荐文章','content':'不应读取的推荐内容'*30}]}
        html='<html><title>主文章标题测试</title><script id="__NEXT_DATA__" type="application/json">'+json.dumps(payload,ensure_ascii=False)+'</script><main>加载中</main></html>'
        result=read_html(html.encode('utf-8'),'https://example.com/detail/123456',False)
        self.assertIn(body,result['text']);self.assertNotIn('不应读取',result['text']);self.assertIn('2026-11-04',result['date_context'])
    def test_private_literal_rejected(self):
        with self.assertRaises(FetchError):public_url('http://127.0.0.1/file',resolver=lambda *a,**k:[(2,1,6,'',('127.0.0.1',80))])
    def test_restricted_search_domains_and_secret(self):
        cfg=config();cfg.search.api_key='';from pydantic import SecretStr;cfg.search.api_key=SecretStr('offline-secret')
        s=Sources(cfg,self.root);requests=[]
        def handler(req):
            requests.append(req);return httpx.Response(200,json={'results':[{'url':'https://example.com/a','title':'测试','content':'测试'}]})
        s.search_http.close();s.search_http=httpx.Client(transport=httpx.MockTransport(handler))
        s.search('银行','CN','2026-11-01','2026-11-05')
        body=json.loads(requests[0].content);self.assertEqual(body['include_domains'],['example.com']);self.assertNotIn('api_key',body)
        self.assertNotIn('offline-secret',json.dumps(cfg.model_dump()));s.close()
    def test_model_cache_and_invalid_retry(self):
        calls=[]
        def handler(req):
            if req.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline-test'}]})
            calls.append(json.loads(req.content));return httpx.Response(200,json={'message':{'content':json.dumps({'action':'fetch','reason':'测试'})}})
        q=Qwen(config().ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        a=q.ask('测试','离线提示',{'test':True},Decision);b=q.ask('测试','离线提示',{'test':True},Decision)
        self.assertEqual(a,b);self.assertEqual(len(calls),1);self.assertFalse(calls[0]['think']);self.assertEqual(q.cache_hits,1);q.close()
    def test_bad_model_output_only_two_attempts(self):
        def handler(req):
            if req.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline-test'}]})
            return httpx.Response(200,json={'message':{'content':'invalid'}})
        q=Qwen(config().ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        with self.assertRaises(OutputError):q.ask('测试','提示',{},Decision)
        self.assertEqual(q.calls,2);q.close()

if __name__=='__main__':unittest.main()

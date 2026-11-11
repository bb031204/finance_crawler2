import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pydantic import SecretStr
from finance_agent.core import dump,digest
from record_files import Dataset
from record_quality import verified_record
from web_sources import read_html,usable_page_cache
from date_evidence import trusted_publication_day
from simple_crawler import Crawler
from test_simple_crawler import config,candidate,FakeSources,FakeQwen
from test_sec_discovery import sec_config,SecSources,SecModel,filing,master
from sec_discovery import quarter_jobs


class CollectionEfficiencyTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_collection_efficiency';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def crawler(self,model=None):return Crawler(config(),self.root,FakeSources(),model or FakeQwen())
    def cached(self,crawler,item=None):
        item=item or candidate();crawler.queue(item)
        page=crawler.sources.fetch(item);page['date_context']='datePublished=2026-11-02'
        path=self.root/'.runtime/simple/pages'/(next(iter(crawler.state['pending']))+'.json');dump(path,page)
        return page,path

    def test_unbound_related_article_does_not_override_main_date_or_content(self):
        related={'@type':'ItemList','itemListElement':[{'@type':'NewsArticle','headline':'Unrelated future article',
            'datePublished':'2026-11-01','articleBody':'Future article about unrelated company events. '*20}]}
        raw=('<title>Main earnings report</title><script type="application/ld+json">'+json.dumps(related)+
            '</script><main><h1>Main earnings report</h1><time>Published 2015-06-17</time><p>Main company reported revenue growth.</p></main>').encode()
        page=read_html(raw,'https://example.com/main',False)
        self.assertNotIn('Future article',page['text']);self.assertNotIn('2026-11-01',page['date_context'])
        self.assertNotEqual(trusted_publication_day(page),'2026-11-01')
        self.assertIn('2015-06-17',page['date_context'])

    def test_article_can_bind_via_url_without_exact_title(self):
        body='The issuer reported revenue and operating earnings. '*8
        payload={'@type':'NewsArticle','headline':'A different displayed title','mainEntityOfPage':{'@id':'https://example.com/main'},
            'datePublished':'2015-06-17','articleBody':body}
        raw=('<title>Generic publisher title</title><script type="application/ld+json">'+json.dumps(payload)+'</script>').encode()
        page=read_html(raw,'https://example.com/main',False)
        self.assertIn(body.strip(),page['text']);self.assertEqual(trusted_publication_day(page),'2015-06-17')

    def test_related_node_with_matching_numeric_id_is_still_excluded(self):
        payload={'related':[{'id':12345,'title':'Different story','datePublished':'2026-11-01','content':'Unrelated details. '*20}]}
        raw=('<title>Main earnings report</title><script type="application/json">'+json.dumps(payload)+
            '</script><main>Main article reports income and dividend information.</main>').encode()
        page=read_html(raw,'https://example.com/article/12345',False)
        self.assertNotIn('Unrelated details',page['text']);self.assertNotIn('2026-11-01',page['date_context'])

    def test_legacy_ambiguous_or_wrong_url_cache_is_not_reused(self):
        page={'url':'https://example.com/a','text':'Original cached article','date_context':'page.related.datePublished=2026-11-01'}
        self.assertFalse(usable_page_cache(page,'https://example.com/a'))
        page['date_context']='datePublished=2026-11-01'
        self.assertFalse(usable_page_cache(page,'https://example.com/b'))
        self.assertTrue(usable_page_cache(page,'https://example.com/a'))

    def test_cached_body_skips_title_screen_and_keeps_final_qwen_filter(self):
        crawler=self.crawler();self.cached(crawler);crawler.sources.fetched=0
        with patch.object(crawler.model,'screen',side_effect=AssertionError('must not re-screen a cached body')):
            report=crawler.collect(resume=True)
        self.assertEqual(report['saved'],1);self.assertEqual(report['screen_calls_avoided'],1)
        self.assertEqual(report['model_calls'],1);self.assertEqual(crawler.sources.fetched,0)

    def test_cached_but_irrelevant_article_still_gets_rejected_by_body(self):
        class RejectBody(FakeQwen):
            def summarize(self,*args):self.calls+=1;return {'useful':False,'reason':'No substantive market facts'}
        crawler=self.crawler(RejectBody());page,path=self.cached(crawler)
        report=crawler.collect(resume=True)
        self.assertEqual(report['saved'],0);self.assertEqual(report['summary']['rejected_reasons'],{'content_rejected':1})
        self.assertTrue(path.is_file())

    def test_new_unread_webpage_still_uses_qwen_title_screen(self):
        crawler=self.crawler(FakeQwen(useful=False));crawler.queue(candidate())
        report=crawler.collect(resume=True)
        self.assertEqual(report['screen_calls_avoided'],0);self.assertEqual(crawler.sources.fetched,0)

    def test_official_sec_directory_skips_initial_screen_but_still_reads_body(self):
        cfg=sec_config();url=quarter_jobs('2019-01-01','2019-01-31')[0]['url']
        crawler=Crawler(cfg,self.root,SecSources({url:master([filing(1)])}),SecModel())
        with patch.object(crawler.model,'screen',side_effect=AssertionError('must use body for official filing')):
            report=crawler.collect()
        self.assertEqual(report['saved'],1);self.assertEqual(report['model_calls'],1)
        self.assertEqual(report['screen_calls_avoided'],1);self.assertEqual(len(crawler.sources.fetches),1)

    def test_failed_audit_transaction_recovers_before_duplicate_skip_without_qwen(self):
        crawler=self.crawler();crawler.queue(candidate());original_dump=dump
        def fail_audit(path,value):
            if Path(path).parent==self.root/'.runtime/simple/audit':raise OSError('simulated audit commit failure')
            return original_dump(path,value)
        with patch('record_files.dump',side_effect=fail_audit):first=crawler.collect(resume=True)
        self.assertTrue(crawler.dataset.outbox.is_file());self.assertEqual(first['saved'],0)
        page_path=next((self.root/'.runtime/simple/pages').glob('*.json'))
        with patch.object(crawler.model,'summarize',side_effect=AssertionError('must recover committed evidence')):
            second=crawler.collect(resume=True)
        self.assertEqual(second['pending'],0);self.assertEqual(second['status'],'completed')
        self.assertEqual(second['model_calls'],0);self.assertEqual(second['transaction_recovered'],1)
        self.assertFalse(crawler.dataset.outbox.exists());self.assertFalse(page_path.exists())
        self.assertTrue(verified_record(self.root,next(iter(crawler.dataset.rows.values()))[1]))

    def test_old_json_without_any_audit_is_not_silently_skipped(self):
        crawler=self.crawler();crawler.queue(candidate());crawler.collect(resume=True)
        identity=next(iter(crawler.dataset.rows));(self.root/'.runtime/simple/audit'/(identity+'.json')).unlink()
        crawler.queue(candidate());report=crawler.collect(resume=True)
        self.assertEqual(report['saved'],1);self.assertTrue(verified_record(self.root,crawler.dataset.rows[identity][1]))

    def test_incomplete_transaction_cannot_be_overwritten_by_next_save(self):
        crawler=self.crawler();crawler.queue(candidate());attempts=0
        def once(path,value):
            nonlocal attempts
            if Path(path).parent==self.root/'.runtime/simple/audit' and attempts==0:
                attempts+=1;raise OSError('one transient fault')
            return dump(path,value)
        with patch('record_files.dump',side_effect=once):crawler.collect(resume=True,one_batch=True)
        # Saving an unrelated next record first recovers the previous operation.
        previous=next(iter(crawler.state['pending']))
        operation=json.loads(crawler.dataset.outbox.read_text(encoding='utf-8'))
        second=dict(operation['row'],source_id='second',url='https://example.com/second')
        evidence=dict(operation['audit_update'],source_id='second',url=second['url'],record_hash=digest(second))
        crawler.dataset.save('CN',second,'Banking',audit_update=evidence)
        self.assertEqual(len(crawler.dataset.rows),2);self.assertTrue(verified_record(self.root,crawler.dataset.rows[previous][1]))

    def test_cleanup_failure_after_verified_commit_keeps_usable_record(self):
        crawler=self.crawler();crawler.queue(candidate())
        with patch('simple_crawler.finalize_record',side_effect=OSError('post-commit cleanup fault')):
            report=crawler.collect(resume=True)
        self.assertEqual(report['saved'],1);self.assertEqual(report['pending'],0)
        self.assertEqual(report['summary']['cleanup_error_attempts'],1)
        self.assertTrue(verified_record(self.root,next(iter(crawler.dataset.rows.values()))[1]))

    def test_hybrid_continues_sec_without_retrying_paused_search(self):
        class SearchPaused(Crawler):
            search_calls=0
            def search_discover(self,report,*args):
                self.search_calls+=1;report['discovery']={'complete':False,'paused':True,'tasks_remaining':1}
        cfg=sec_config();cfg.search.discovery_mode='hybrid';cfg.search.complete_history=True;cfg.search.sec_edgar.page_size=1
        url=quarter_jobs('2019-01-01','2019-01-31')[0]['url']
        crawler=SearchPaused(cfg,self.root,SecSources({url:master([filing(n) for n in range(1,4)])}),SecModel())
        report=crawler.collect()
        self.assertEqual(report['saved'],3);self.assertEqual(crawler.search_calls,1)
        self.assertTrue(report['direct_discovery']['complete']);self.assertEqual(report['direct_discovery']['requests'],1)
        self.assertEqual(report['status'],'partial')

    def test_hybrid_search_preflight_failure_does_not_prevent_sec(self):
        class SearchBroken(Crawler):
            search_calls=0
            def search_discover(self,*args):self.search_calls+=1;raise ValueError('search unavailable')
        cfg=sec_config();cfg.search.discovery_mode='hybrid';cfg.search.complete_history=True;cfg.search.sec_edgar.page_size=1
        url=quarter_jobs('2019-01-01','2019-01-31')[0]['url']
        crawler=SearchBroken(cfg,self.root,SecSources({url:master([filing(1),filing(2)])}),SecModel())
        report=crawler.collect();self.assertEqual(report['saved'],2);self.assertEqual(crawler.search_calls,1)

    def test_completed_search_is_not_replayed_during_sec_continuation(self):
        class SearchFinished(Crawler):
            search_calls=0
            def search_discover(self,report,*args):
                self.search_calls+=1;report['discovery']={'complete':True,'paused':False,'tasks_remaining':0}
        cfg=sec_config();cfg.search.discovery_mode='hybrid';cfg.search.complete_history=True;cfg.search.sec_edgar.page_size=1
        url=quarter_jobs('2019-01-01','2019-01-31')[0]['url']
        crawler=SearchFinished(cfg,self.root,SecSources({url:master([filing(1),filing(2)])}),SecModel())
        report=crawler.collect();self.assertEqual(report['saved'],2);self.assertEqual(crawler.search_calls,1)
        self.assertEqual(report['status'],'completed')

    def test_failed_sec_is_not_retried_while_search_continues(self):
        class SearchRounds(Crawler):
            search_calls=0
            def search_discover(self,report,*args):
                self.search_calls+=1;report['discovery']={'complete':self.search_calls>=2,'paused':False,'tasks_remaining':max(0,2-self.search_calls)}
        cfg=sec_config();cfg.search.discovery_mode='hybrid';cfg.search.complete_history=True
        url=quarter_jobs('2019-01-01','2019-01-31')[0]['url']
        sources=SecSources({url:ValueError('SEC unavailable')})
        crawler=SearchRounds(cfg,self.root,sources,SecModel());report=crawler.collect()
        self.assertEqual(crawler.search_calls,2);self.assertEqual(sources.requests,[url]);self.assertEqual(report['status'],'partial')


if __name__=='__main__':unittest.main()

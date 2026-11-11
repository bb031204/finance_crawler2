import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finance_agent.core import dump
from qwen_agent import Qwen,Publication,ContentNote,CompactNote,Decision,ModelBudget,tokens,grounded_time,MERGE_PROMPT
from record_files import source_id
from simple_crawler import Crawler,load_config
from test_simple_crawler import config,candidate,FakeSources,FakeQwen
from web_sources import read_html,usable_page_cache


def content(text=None):
    return dict(useful=True,market='CN',category='Company news',industry='Banking',securities=[],
        industries=[],impact_scope=['company'],keywords=['Example Bank'],reason='Operating results',
        content=text or 'Example Bank reported higher annual revenue and described its operating performance and business conditions.')


class OfflineQwen(Qwen):
    def __init__(self,cfg,root,day='2026-11-02',quote='Published November 2, 2026'):
        super().__init__(cfg,root,http=object());self.day=day;self.quote=quote;self.purposes=[];self.model_digest='offline'
    def ask(self,purpose,prompt,payload,schema,validate=None,partial_parser=None,max_predict=None):
        self.calls+=1;self.purposes.append(purpose)
        if schema is Publication:value=dict(time=self.day,date_quote=self.quote,date_kind='published' if self.day else 'unknown')
        elif schema is Decision:value=dict(action='fetch',reason='Substantive financial article')
        elif schema is CompactNote:
            self.assert_request_fits(prompt,payload,max_predict)
            value={'content':'Example Bank reported revenue growth of approximately 15% during the stated financial year.'}
        else:
            if purpose=='段落合并':self.assert_request_fits(prompt,payload,max_predict)
            value=content()
        result=schema.model_validate(value).model_dump()
        return validate(result) if validate else result
    def assert_request_fits(self,prompt,payload,prediction_limit):
        if self.request_cost(prompt,payload,prediction_limit)>self.cfg.num_ctx:raise AssertionError('oversized request was not prepared')
    def close(self):pass


class PageSources(FakeSources):
    def __init__(self,quote='Published November 2, 2026',body=None):
        super().__init__();self.quote=quote;self.body=body or 'Example Bank reported annual revenue and operating performance. '*15
    def fetch(self,item):
        page=super().fetch(item);page.update(date_context=self.quote,text=self.body)
        return page


class PreflightPipelineTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_preflight_pipeline';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name);self.cfg=config();self.cfg.ollama.num_predict=1400
    def tearDown(self):self.tmp.cleanup()
    def crawler(self,sources=None,model=None):
        model=model or OfflineQwen(self.cfg.ollama,self.root)
        return Crawler(self.cfg,self.root,sources or PageSources(),model)
    def page(self,quote='Published November 2, 2026',body=None):return PageSources(quote,body).fetch(candidate())

    def test_extracted_out_of_range_date_stops_before_screen_and_prose(self):
        quote='Published June 17, 2021';model=OfflineQwen(self.cfg.ollama,self.root,'2021-06-17',quote)
        c=self.crawler(PageSources(quote),model);c.queue(candidate());report=c.collect(resume=True)
        self.assertEqual(model.purposes,['来源发布时间']);self.assertEqual(report['saved'],0)
        self.assertEqual(report['preflight_rejected'],1);self.assertFalse(c.state['pending'])

    def test_no_complete_date_skips_all_model_work(self):
        c=self.crawler(PageSources('Annual Report 2021'));c.queue(candidate());r=c.collect(resume=True)
        self.assertEqual(c.model.calls,0);self.assertEqual(r['preflight_deferred'],1);self.assertEqual(r['saved'],0)
        self.assertEqual(len(c.state['pending']),1)

    def test_unknown_publication_never_enters_screen_or_prose(self):
        quote='Last Updated November 2, 2026';model=OfflineQwen(self.cfg.ollama,self.root,None,quote)
        c=self.crawler(PageSources(quote),model);c.queue(candidate());r=c.collect(resume=True)
        self.assertEqual(model.purposes,['来源发布时间']);self.assertEqual(r['preflight_deferred'],1)

    def test_bad_date_output_is_deferred_before_prose(self):
        model=OfflineQwen(self.cfg.ollama,self.root,'2021-06-17','Published November 2, 2026')
        c=self.crawler(model=model);c.queue(candidate());r=c.collect(resume=True)
        self.assertEqual(model.purposes,['来源发布时间']);self.assertEqual(r['preflight_deferred'],1);self.assertEqual(r['saved'],0)

    def test_valid_article_uses_prepared_publication_once(self):
        c=self.crawler();c.queue(candidate());r=c.collect(resume=True)
        self.assertEqual(c.model.purposes,['来源发布时间','搜索筛选','正文整理'])
        self.assertEqual(r['preflight_checked'],1);self.assertEqual(r['preflight_passed'],1);self.assertEqual(r['saved'],1)
        self.assertEqual(r['summary']['components']['preflight_passed'],1)

    def test_corrupt_body_has_no_model_calls(self):
        c=self.crawler(PageSources(body='Unreadable '+'\ufffd'*100));c.queue(candidate());r=c.collect(resume=True)
        self.assertEqual(c.model.calls,0);self.assertEqual(r['preflight_deferred'],1)

    def test_fixed_context_impossible_stops_before_date(self):
        self.cfg.monitoring.securities=['A company name repeated '*2000]
        c=self.crawler();c.queue(candidate());r=c.collect(resume=True)
        self.assertEqual(c.model.calls,0);self.assertEqual(r['preflight_deferred'],1)
        self.assertTrue(r['summary']['deferred_reasons'].get('context_budget'))

    def test_cached_oversized_body_stops_before_date(self):
        c=self.crawler();item=candidate();c.queue(item);self.cfg.fetch.max_text_chars=1000
        page=self.page(body='Company revenue. '*100);dump(self.root/'.runtime/simple/pages'/(source_id(item['url'])+'.json'),page)
        r=c.collect(resume=True);self.assertEqual(c.model.calls,0);self.assertEqual(r['saved'],0)

    def test_date_formats_are_accepted_without_guessing(self):
        for quote in ['Published June 17,2021','Published 17 June 2021','Published 17th June 2021','Published Jun. 17, 2021']:
            with self.subTest(quote=quote):
                m=OfflineQwen(self.cfg.ollama,self.root,'2021-06-17',quote)
                self.assertEqual(m.publication(self.page(quote))['time'],'2021-06-17')
        self.assertEqual(grounded_time('2021-09-17','Published Sept. 17, 2021','Published Sept. 17, 2021','US'),'2021-09-17')

    def test_month_year_alone_does_not_invent_day(self):
        m=OfflineQwen(self.cfg.ollama,self.root)
        self.assertIsNone(m.publication(self.page('Published June 2021'))['time']);self.assertEqual(m.calls,0)

    def test_verified_sec_index_reuses_date_without_model(self):
        page=self.page();page.update(url='https://www.sec.gov/Archives/edgar/data/320193/0000320193-19-000010-index.html',
            document_url='https://www.sec.gov/Archives/edgar/data/320193/000032019319000010/aapl.htm',
            sec_filing_date='2019-01-29',date_context='SEC filing datePublished=2019-01-29',origin_market='US')
        m=OfflineQwen(self.cfg.ollama,self.root)
        self.assertEqual(m.publication(page)['time'],'2019-01-29');self.assertEqual(m.calls,0)
        page['document_url']='https://www.sec.gov/Archives/edgar/data/other/other.htm'
        m.day='2019-01-29';m.quote=page['date_context'];m.publication(page)
        self.assertEqual(m.calls,1)

    def test_large_merge_compacts_before_safe_request(self):
        m=OfflineQwen(self.cfg.ollama,self.root)
        paragraph='The international semiconductor manufacturer reported consolidated quarterly financial performance and operational restructuring. '
        group=[content(paragraph*25),content(paragraph*25)]
        context={'origin_market':'US','output_language':'en','date_context':'datePublished=2021-06-17'}
        self.assertGreater(m.request_cost(MERGE_PROMPT,m.merge_payload(context,group)),self.cfg.ollama.num_ctx)
        originals=copy.deepcopy(group);result=m.merge_pair(group,context,lambda note:note)
        self.assertEqual(m.purposes,['合并前整理','合并前整理','段落合并']);self.assertTrue(result['useful'])
        self.assertEqual(group,originals)

    def test_small_merge_has_no_extra_compression_calls(self):
        m=OfflineQwen(self.cfg.ollama,self.root)
        m.merge_pair([content(),content()],{'origin_market':'CN','output_language':'en','date_context':'Published November 2, 2026'},lambda n:n)
        self.assertEqual(m.purposes,['段落合并'])

    def test_dom_comments_excluded_and_time_attribute_kept(self):
        raw=b'<title>Company results</title><main><article><h1>Company results</h1><time datetime="2021-06-17T08:30:00Z">Published Thursday</time><p>Original financial report.</p></article><section id="comments">Future 2026 bankruptcy comment</section></main>'
        page=read_html(raw,'https://example.com/main',False)
        self.assertNotIn('2026',page['text']);self.assertIn('2021-06-17T08:30:00Z',page['date_context'])
        self.assertEqual(page['article_binding_version'],3)

    def test_modified_time_is_not_promoted_to_published(self):
        raw=b'<article><h1>Company results</h1><time datetime="2021-06-17">Last Updated Thursday</time><p>Company earnings details.</p></article>'
        page=read_html(raw,'https://example.com/main',False)
        self.assertIn('dateModified=',page['date_context']);self.assertNotIn('datePublished=',page['date_context'])

    def test_legacy_visible_body_cache_requires_refetch(self):
        page=self.page();page.update(article_binding='visible_only',article_binding_version=2)
        self.assertFalse(usable_page_cache(page,page['url']))

    def test_redirect_alias_prevents_repeated_model_and_download(self):
        class Redirect(PageSources):
            def fetch(self,item):
                page=super().fetch(item);page.update(request_url=item['url'],url='https://example.com/canonical');return page
        c=self.crawler(Redirect());item=candidate('redirect');c.queue(item);one=c.collect(resume=True)
        calls=c.model.calls;downloads=c.sources.fetched;c.queue(item);two=c.collect(resume=True)
        self.assertEqual(one['saved'],1);self.assertEqual(two['saved'],0);self.assertEqual(two['model_calls'],0)
        self.assertEqual(c.sources.fetched,downloads);self.assertEqual(len(c.dataset.rows),1)
        # Alias remains useful after a new process loads the persisted state and removed raw cache.
        other=Crawler(self.cfg,self.root,Redirect(),OfflineQwen(self.cfg.ollama,self.root));other.queue(item)
        r=other.collect(resume=True);self.assertEqual(r['model_calls'],0);self.assertEqual(other.sources.fetched,0)

    def test_new_alias_for_existing_record_checks_after_fetch_without_model(self):
        class Redirect(PageSources):
            def fetch(self,item):
                page=super().fetch(item);page.update(request_url=item['url'],url='https://example.com/canonical');return page
        c=self.crawler(Redirect());c.queue(candidate('first'));c.collect(resume=True)
        c.queue(candidate('second'));r=c.collect(resume=True)
        self.assertEqual(r['model_calls'],0);self.assertEqual(r['saved'],0);self.assertEqual(len(c.dataset.rows),1)


if __name__=='__main__':unittest.main()

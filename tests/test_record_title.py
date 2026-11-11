import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from finance_agent.core import digest
from record_files import Dataset,FIELDS,LEGACY_ANALYSIS_FIELDS
from simple_crawler import Crawler
from web_sources import read_html,Sources
from record_time import with_time_information
from analysis_metadata import public_analysis
from test_simple_crawler import config,candidate,FakeSources,FakeQwen


class RecordTitleTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_record_title';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def crawler(self,source_title,market='CN',page_title='Search fallback title'):
        class Source(FakeSources):
            def fetch(self,item):return dict(super().fetch(item),title=page_title,source_title=source_title,title_origin='heading')
        class Model(FakeQwen):
            def summarize(self,*args):return dict(super().summarize(*args),market=market)
        c=Crawler(config(),self.root,Source(),Model());c.queue(candidate());c.collect(resume=True)
        return c

    def test_crawler_keeps_full_original_title_without_llm_rewrite_or_log_truncation(self):
        title=('原文标题：Company A / B "Results" — '+('完整中文与English财报标题 '*20)).rstrip()
        c=self.crawler(title)
        self.assertEqual(len(c.dataset.rows),1)
        identity,(_,row)=next(iter(c.dataset.rows.items()))
        self.assertEqual(row['title'],title);self.assertEqual(set(row),FIELDS)
        self.assertEqual(row['时间'],'2026-11-02')
        audit=json.loads((self.root/'.runtime/simple/audit'/(identity+'.json')).read_text(encoding='utf-8'))
        self.assertEqual(audit['title'],title);self.assertEqual(audit['record_hash'],digest(row))
        self.assertEqual(json.loads(c.dataset.paths[identity].read_text(encoding='utf-8'))['title'],title)

    def test_global_copies_keep_identical_titles_and_audit(self):
        c=self.crawler('Original global policy headline',market='GLOBAL')
        identity=next(iter(c.dataset.rows))
        copies=[json.loads(p.read_text(encoding='utf-8')) for p in c.dataset.replicas[identity].values()]
        self.assertEqual(len(copies),3);self.assertTrue(all(r==copies[0] for r in copies))
        self.assertEqual(copies[0]['title'],'Original global policy headline')

    def test_missing_original_title_is_null_not_internal_fallback(self):
        c=self.crawler(None,page_title='公司 / 关键词拼接，仅内部候选显示')
        row=next(iter(c.dataset.rows.values()))[1]
        self.assertIn('title',row);self.assertIsNone(row['title'])

    def test_old_ten_field_records_are_read_without_rewriting(self):
        row=with_time_information(dict(source_id='legacy',url='https://example.com/old',时间='2021-03-31',分类='News',
            关键词=['Example'],主要内容='Original older article.',**public_analysis({})),available_at='2026-11-01T12:00:00+00:00')
        self.assertEqual(set(row),LEGACY_ANALYSIS_FIELDS)
        ds=Dataset(self.root,'data');path=ds.save('US',row,'Software');before=path.read_bytes()
        loaded=Dataset(self.root,'data')
        self.assertEqual(loaded.rows['legacy'][1],row);self.assertEqual(path.read_bytes(),before)

    def test_title_type_is_validated_and_legacy_fields_remain_supported(self):
        row=with_time_information(dict(source_id='one',url='https://example.com/a',时间='2021-03-31',分类='News',
            关键词=['Example'],主要内容='Original article.',**public_analysis({})),available_at='2026-11-01T12:00:00+00:00')
        for value in [None,'Original headline']:Dataset.validate('US',dict(row,title=value))
        for value in [123,[],{},'','  ']:
            with self.subTest(value=value),self.assertRaises(ValueError):Dataset.validate('US',dict(row,title=value))

    def test_html_title_prefers_main_heading_and_never_a_recommendation(self):
        raw=b'<title>Browser title | Publisher</title><article><h1>Original earnings headline</h1><p>Original operating results.</p></article><aside><h1>Unrelated recommendation</h1></aside>'
        page=read_html(raw,'https://example.com/a',False)
        self.assertEqual(page['source_title'],'Original earnings headline')
        self.assertEqual(page['title'],'Browser title | Publisher')
        self.assertEqual(page['title_origin'],'heading')

    def test_bound_json_headline_is_available_when_html_has_no_heading(self):
        node={'@type':'NewsArticle','url':'https://example.com/a','headline':'Original JSON headline',
            'articleBody':'Company revenue and operating results. '*5}
        raw=('<script type="application/ld+json">'+json.dumps(node)+'</script><p>Loading</p>').encode()
        self.assertEqual(read_html(raw,'https://example.com/a',False)['source_title'],'Original JSON headline')

    def test_html_without_title_has_no_fabricated_source_title(self):
        sources=Sources(config(),self.root)
        try:
            sources.cfg.fetch.use_crawl4ai=False
            page=sources.page_from_bytes(b'<article>Original financial report with operating details.</article>',
                'https://example.com/a','US',title='Internally generated fallback')
            self.assertEqual(page['title'],'Internally generated fallback');self.assertIsNone(page['source_title'])
        finally:sources.close()

    def test_pdf_internal_candidate_caption_is_not_saved_as_original_title(self):
        sources=Sources(config(),self.root)
        try:
            parsed={'quality':{'status':'ok'},'paragraphs':['Original PDF financial report.']}
            with patch('finance_agent.pdf_layout.extract_pdf_layout',return_value=parsed):
                page=sources.page_from_bytes(b'%PDF-fixture','https://example.com/a.pdf','US',title='keywords / generated',title_is_source=False)
                self.assertIsNone(page['source_title'])
                page=sources.page_from_bytes(b'%PDF-fixture','https://example.com/a.pdf','US',title='Original disclosure listing title')
                self.assertEqual(page['source_title'],'Original disclosure listing title')
        finally:sources.close()


if __name__=='__main__':unittest.main()

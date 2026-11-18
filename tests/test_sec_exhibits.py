import unittest
from unittest.mock import patch
from finance_agent.core import digest
from finance_agent.safety import FetchError
from web_sources import Sources,usable_page_cache
from sec_documents import accession_document_url
from test_sec_discovery import SecPageTests,SecTestBase


class SecExhibitTests(SecTestBase):
    page_source=SecPageTests.page_source
    # Reuse the source fixture while exercising the new default explicitly.
    def source(self, **kwargs):
        sources,candidate,index,body=self.page_source(**kwargs)
        sources.cfg.search.sec_edgar.include_exhibits=True
        self.addCleanup(sources.close)
        return sources,candidate,index,body

    def test_same_accession_exhibit_is_read_and_date_is_fixed(self):
        sources,candidate,index,main=self.source()
        exhibit=b'<html><article><h1>Quarterly earnings</h1>Revenue rose 15% to $25 million in 2018.</article></html>'
        calls=[]
        def fetch(url):
            calls.append(url)
            body=index if url.endswith('-index.html') else main if url.endswith('report.htm') else exhibit
            return body,{'url':url,'content_type':'text/html'}
        with patch.object(sources,'sec_get',side_effect=fetch):page=sources.fetch(candidate)
        self.assertEqual(len(calls),3);self.assertIn('$25 million',page['text'])
        self.assertEqual(page['date_context'],'SEC filing datePublished=2019-01-10')
        self.assertEqual(page['source_title'],'Operating results')
        self.assertEqual(len(page['sec_documents']),2)
        for document in page['sec_documents']:
            text=page['text'][document['text_start']:document['text_end']]
            self.assertEqual(digest(text.encode('utf-8')),document['text_sha256'])
        self.assertTrue(usable_page_cache(page,candidate['url'],sources.cfg.search.sec_edgar))
        sources.cfg.search.sec_edgar.include_exhibits=False
        self.assertFalse(usable_page_cache(page,candidate['url'],sources.cfg.search.sec_edgar))
        del page['sec_document_version']
        self.assertFalse(usable_page_cache(page,candidate['url']))

    def test_failed_attachment_cannot_silently_become_complete_record(self):
        sources,candidate,index,main=self.source()
        with patch.object(sources,'sec_get',side_effect=[(index,{}),(main,{}),FetchError('network_error','offline')]):
            with self.assertRaises(FetchError):sources.fetch(candidate)

    def test_sec_hostname_alias_uses_the_same_accession(self):
        sources,candidate,index,main=self.source()
        candidate['url']=candidate['url'].replace('www.sec.gov','sec.gov')
        with patch.object(sources,'sec_get',side_effect=[(index,{}),(main,{}),(main,{})]):page=sources.fetch(candidate)
        self.assertIn('www.sec.gov',page['url'])
        self.assertEqual(page['request_url'],candidate['url'])
        self.assertTrue(usable_page_cache(page,candidate['url'],sources.cfg.search.sec_edgar))

    def test_relevant_exhibit_priority_limit_and_provenance_are_explicit(self):
        sources,candidate,index,main=self.source()
        prefix='/Archives/edgar/data/320193/000032019319000001/'
        extra=f'<tr><td>3</td><td>Earnings press release</td><td><a href="{prefix}earnings.htm">earnings</a></td><td>EX-99.2</td></tr>'
        index=index.replace(b'</table>',extra.encode()+b'</table>')
        sources.cfg.search.sec_edgar.max_exhibits_per_filing=1
        calls=[]
        def fetch(url):
            calls.append(url)
            return (index if url.endswith('-index.html') else main),{}
        with patch.object(sources,'sec_get',side_effect=fetch):page=sources.fetch(candidate)
        self.assertEqual(len(calls),3);self.assertTrue(calls[-1].endswith('/earnings.htm'))
        self.assertEqual(page['sec_unread_exhibits'],[{'url':'https://www.sec.gov'+prefix+'exhibit.htm','type':'EX-99.1'}])

    def test_index_redirect_to_another_accession_is_rejected(self):
        sources,candidate,index,main=self.source()
        wrong=candidate['url'].replace('000001','000002')
        with patch.object(sources,'sec_get',return_value=(index,{'url':wrong})) as fetch:
            with self.assertRaises(ValueError):sources.fetch(candidate)
        self.assertEqual(fetch.call_count,1)

    def test_cross_accession_exhibit_is_rejected_before_download(self):
        sources,candidate,index,main=self.source()
        index=index.replace(b'exhibit.htm',b'../other/exhibit.htm')
        with patch.object(sources,'sec_get',return_value=(index,{})) as fetch:
            with self.assertRaises(ValueError):sources.fetch(candidate)
        self.assertEqual(fetch.call_count,1)

    def test_exhibits_have_combined_size_budget(self):
        sources,candidate,index,main=self.source()
        sources.cfg.fetch.max_bytes=len(index)+len(main)+10
        with patch.object(sources,'sec_get',side_effect=[(index,{}),(main,{}),(main,{})]):
            with self.assertRaisesRegex(ValueError,'合计超过max_bytes'):sources.fetch(candidate)

    def test_exhibits_have_combined_text_budget(self):
        sources,candidate,index,main=self.source()
        sources.cfg.fetch.max_text_chars=1000
        exhibit=b'<article>'+b'Financial operating information. '*29+b'</article>'
        with patch.object(sources,'sec_get',side_effect=[(index,{}),(main,{}),(exhibit,{})]):
            with self.assertRaisesRegex(ValueError,'合计超过max_text_chars'):sources.fetch(candidate)

    def test_source_documents_are_kept_in_audit(self):
        from record_quality import prepare_record_audit
        from record_files import source_id
        from test_simple_crawler import FakeQwen
        from record_time import with_time_information
        sources,candidate,index,main=self.source()
        with patch.object(sources,'sec_get',side_effect=[(index,{}),(main,{}),(main,{})]):page=sources.fetch(candidate)
        row=with_time_information(dict(source_id=source_id(page['url']),url=page['url'],title=page['source_title'],
            时间='2019-01-10',分类='Financial results',关键词=['results'],主要内容='Example Company reported its financial results.'))
        audit=prepare_record_audit(self.root,row,{},page,candidate,FakeQwen(),'US')
        self.assertEqual(audit['source_documents'],page['sec_documents'])
        self.assertEqual(audit['sec_document_policy']['include_exhibits'],True)
        self.assertEqual(audit['source_published_at'],'2019-01-10')
        self.assertIsNone(audit['numeric_evidence_version'])

    def test_redirect_to_another_document_is_rejected(self):
        sources,candidate,index,main=self.source()
        wrong=candidate['url'].replace('-index.html','/different.htm')
        with patch.object(sources,'sec_get',side_effect=[(index,{}),(main,{'url':wrong})]):
            with self.assertRaises(ValueError):sources.fetch(candidate)

    def test_double_encoded_traversal_and_external_viewer_are_rejected(self):
        index='https://www.sec.gov/Archives/edgar/data/320193/0000320193-19-000001-index.html'
        for href in [
            '/Archives/edgar/data/320193/000032019319000001/%252e%252e/file.htm',
            'https://example.com/ix?doc=/Archives/edgar/data/320193/000032019319000001/file.htm',
        ]:
            with self.subTest(href=href),self.assertRaises(ValueError):
                accession_document_url(index,href,'320193','0000320193-19-000001')


if __name__=='__main__':
    unittest.main()

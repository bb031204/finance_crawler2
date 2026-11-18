"""Regression for prompt overhead, bounded summaries and coalesced projections."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from finance_agent.core import dump
from qwen_agent import Qwen,ContentNote,READ_PROMPT,MERGE_PROMPT,tokens,CompactNote
from summary_quality import validate_summary_quality
from structured_export import ProjectionScheduler,snapshot
from record_files import Dataset
from record_time import with_time_information
from test_simple_crawler import config,FakeQwen,FakeSources,candidate
from test_preflight_pipeline import OfflineQwen,content
from simple_crawler import Crawler


class EfficiencyLimitTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_efficiency_limits';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.temp.name);self.cfg=config()
    def tearDown(self):self.temp.cleanup()
    def test_mixed_chinese_numeric_text_cannot_bypass_character_cap(self):
        for text,lang in [('公司营收增长。'+'1234567890 '*100,'zh'),('Company results '+'X'*1850,'en')]:
            with self.assertRaisesRegex(ValueError,'主要内容偏长'):validate_summary_quality(text,lang)
    def test_ordinary_financial_numbers_and_conditions_are_preserved(self):
        text='浦发银行2026年第三季度营收同比增长12.5%，拟每10股派2元，仍需股东大会批准。'
        validate_summary_quality(text,'zh');self.assertIn('12.5%',text)
    def test_keyword_and_entity_name_lengths_are_bounded(self):
        for updates in [dict(keywords=['x'*201]),dict(securities=[dict(name='x'*201,ticker=None,quote='x')])]:
            with self.assertRaises(ValueError):ContentNote.model_validate(dict(content(),**updates))
    def test_compact_state_dump_roundtrips_without_changing_public_format(self):
        value={'pending':{'one':{'url':'https://example.com','行业':['银行','证券']}}}
        dump(self.root/'compact.json',value,compact=True);dump(self.root/'readable.json',value)
        self.assertEqual(json.loads((self.root/'compact.json').read_text(encoding='utf-8')),value)
        self.assertLess((self.root/'compact.json').stat().st_size,(self.root/'readable.json').stat().st_size)
    def test_merge_prompt_has_no_conflicting_no_ticker_instruction(self):
        self.assertNotIn('无代码[]',MERGE_PROMPT);self.assertLess(tokens(MERGE_PROMPT),700)
        self.assertIn('无ticker',MERGE_PROMPT)
    def test_read_blocks_remain_lossless_with_repair_reserve(self):
        m=OfflineQwen(self.cfg.ollama,self.root);text='Original financial facts and conditions. '*200
        context=m.read_context({'title':'Original earnings','text':text,'url':'https://example.com','origin_market':'US'},self.cfg.monitoring,'Published June 17, 2021')
        chunks=m.read_chunks(text,context)
        self.assertEqual(''.join(chunks),text)
        for chunk in chunks:self.assertLessEqual(m.request_cost(READ_PROMPT,dict(context,text=chunk))+424,self.cfg.ollama.num_ctx)
    def scheduler(self):
        folder=self.root/'structured';folder.mkdir(exist_ok=True);dump(folder/'manifest.json',{'test':True})
        return ProjectionScheduler(self.root,'data',self.cfg.data_interface)
    def test_projection_skips_clean_batches_and_coalesces_new_records(self):
        s=self.scheduler();result={'status':'completed','rows':{'article_metadata':1}}
        with patch('structured_export.export_or_defer',return_value=result) as export:
            self.assertEqual(s.flush()['status'],'unchanged');export.assert_not_called()
            s.mark(1);s.flush();self.assertEqual(export.call_count,1)
            for _ in range(4):s.mark(10);self.assertEqual(s.flush()['status'],'buffered')
            self.assertEqual(export.call_count,1);s.mark(10);s.flush();self.assertEqual(export.call_count,2)
    def test_projection_flushes_interval_and_final_remainder(self):
        s=self.scheduler()
        with patch('structured_export.time.monotonic',side_effect=[0,10,301,302]),patch('structured_export.export_or_defer',return_value={'status':'completed'}) as export:
            s.mark(1);s.flush();s.mark(1);self.assertEqual(s.flush()['status'],'buffered')
            self.assertEqual(s.flush()['status'],'completed');s.mark(1);s.flush(force=True)
            self.assertEqual(export.call_count,3)
    def test_failed_projection_waits_until_retry_interval(self):
        s=self.scheduler()
        with patch('structured_export.export_or_defer',return_value={'status':'pending'}) as export:
            s.mark(100);s.flush();s.mark(100);self.assertEqual(s.flush()['status'],'buffered');self.assertEqual(export.call_count,1)
            s.flush(force=True);self.assertEqual(export.call_count,2)
    def test_normal_collect_flushes_buffered_json_without_touching_history(self):
        self.cfg.collection.max_documents_per_cycle=1;c=Crawler(self.cfg,self.root,FakeSources(),FakeQwen())
        c.queue(candidate('one'));c.queue(candidate('two'));c.queue(candidate('three'))
        with patch('structured_export.export_or_defer',return_value={'status':'completed','rows':{}}) as export:
            report=c.collect(resume=True)
        self.assertEqual(report['saved'],3);self.assertEqual(export.call_count,2);self.assertEqual(c.projection.dirty,0)
        c.close()
    def test_global_market_comes_from_matching_audit_even_if_only_one_view_remains(self):
        row=with_time_information(dict(source_id='synthetic',url='https://example.com',时间='2026-11-02',分类='Test',关键词=['test'],主要内容='Only synthetic summary.'))
        dump(self.root/'data/CN/test.json',row)
        from finance_agent.core import digest
        dump(self.root/'.runtime/simple/audit/synthetic.json',{'record_hash':digest(row),'canonical_market':'GLOBAL'})
        rows,_=snapshot(self.root);self.assertEqual(rows[0][0],'GLOBAL')
    def test_post_restoration_length_is_checked_and_fixed_once(self):
        class Restored(OfflineQwen):
            purposes=[]
            def ask(self,purpose,prompt,payload,schema,validate=None,**kwargs):
                self.purposes.append(purpose)
                if schema is CompactNote:return validate({'content':'Example Bank (EXB) reported stronger revenue in 2026.'})
                return super().ask(purpose,prompt,payload,schema,validate,**kwargs)
        m=Restored(self.cfg.ollama,self.root);source='Example Bank (NYSE: EXB) reported stronger revenue in 2026.'
        page={'title':'Earnings','url':'https://example.com/earnings','text':source,'origin_market':'US'}
        prepared={'status':'ready','page':page,'publication':{'time':'2026-11-02','date_quote':'Published November 2, 2026','date_kind':'published'},
            'context':m.read_context(page,self.cfg.monitoring,'Published November 2, 2026'),'chunks':[source]}
        def expand(final,*args):
            final.update(content='Example Bank (EXB) '+'Operating '*202,securities=[dict(name='Example Bank',ticker='EXB',exchange='NYSE',quote='Example Bank (NYSE: EXB)')]);return final
        with patch('qwen_agent.preserve_securities',side_effect=expand):result=m.summarize(page,self.cfg.monitoring,prepared)
        validate_summary_quality(result['content'],'en');self.assertIn('EXB',result['content']);self.assertIn('最终正文整理',m.purposes)
    def test_valid_policy_quote_survives_unsupported_optional_argument(self):
        from market_relations import ground_relations
        text='The Committee lowered the federal funds rate by 1/2 percentage point.'
        note={'events':[{'event_type':'monetary_fiscal','subject':'The Committee','action':'lowered','key_values':'0.5%',
            'quote':text,'evidence_type':'explicit'}]}
        result=ground_relations(note,text)
        self.assertEqual(result['events'][0]['event_type'],'monetary_fiscal');self.assertIsNone(result['events'][0]['key_values'])
        self.assertIn('event_argument_omitted',result['relation_quality_flags'])
    def test_category_label_is_not_a_transmission_mechanism(self):
        from market_relations import ground_relations
        text='Federal Reserve issues FOMC statement'
        note={'industries':[{'name':'Financial Services'}],
            'industry_impacts':[{'industry_name_original':'Financial Services','mechanism':'monetary policy','quote':text}]}
        self.assertEqual(ground_relations(note,text)['industry_impacts'],[])
    def test_cache_preserves_producer_time_without_claiming_new_inference(self):
        import httpx
        from record_files import source_id
        identity=source_id('https://example.com');calls=[]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'synthetic'}]})
            calls.append(1);return httpx.Response(200,json={'message':{'content':json.dumps(content())}})
        m=Qwen(self.cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            payload={'source_id':identity,'text':'Synthetic financial facts'}
            m.ask('正文整理',READ_PROMPT,payload,ContentNote);stamp=m.inferred_by_source[identity]
            m.ask('正文整理',READ_PROMPT,payload,ContentNote)
            self.assertEqual(m.inferred_by_source[identity],stamp);self.assertEqual(len(calls),1)
            self.assertIsNotNone(stamp);self.assertEqual(len(m.code_version),64)
        finally:m.close()
    def test_parquet_exposes_known_inference_time_and_source_offset(self):
        from structured_export import project
        row=with_time_information(dict(source_id='synthetic',url='https://example.com',时间='2026-11-02T14:05:00-05:00',分类='Test',关键词=['test'],主要内容='Synthetic only.'))
        audit={'llm_inferred_at':'2026-11-02T19:06:00+00:00','code_version':'synthetic-version'}
        meta=project([('US',row,audit)])['article_metadata'][0]
        self.assertEqual(meta['timezone'],'-05:00');self.assertIsNotNone(meta['llm_inferred_at']);self.assertEqual(meta['code_version'],'synthetic-version')
        self.assertGreaterEqual(meta['feature_available_at'],meta['llm_inferred_at'])


if __name__=='__main__':unittest.main()

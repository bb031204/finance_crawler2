import json
from pathlib import Path
import tempfile
import unittest

from offline_enrichment.enrich_legacy import (inventory, build_record, legacy_analysis,
    save_result, completed, sha, merge_entities, infer, Metadata)
from finance_agent.core import digest, dump


class LegacyEnrichmentTests(unittest.TestCase):
    def test_review_schema_requires_every_metadata_field(self):
        from offline_enrichment.refine_metadata import ReviewMetadata, generic_sector
        self.assertEqual(set(ReviewMetadata.model_json_schema()['required']),
                         {'entities', 'industries', 'impact_scope', 'useful', 'reason'})
        self.assertTrue(generic_sector('Insurance Industry'))
        self.assertFalse(generic_sector('Securities Industry Association'))

    def test_review_filters_law_titles_and_occupation_labels(self):
        from offline_enrichment.refine_metadata import non_entity_artifact, non_industry_label
        self.assertTrue(non_entity_artifact('Defense Production Act of 1950'))
        self.assertTrue(non_entity_artifact('National Compensation Survey-Benefits program'))
        self.assertFalse(non_entity_artifact('U.S. Department of the Treasury'))
        self.assertFalse(non_entity_artifact('Act Corporation'))
        self.assertTrue(non_industry_label('Service occupations'))
        self.assertFalse(non_industry_label('Occupational health services'))

    def setUp(self):
        parent=Path(__file__).resolve().parents[2]/'.runtime/test_offline_enrichment'
        parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent)
        self.root=Path(self.tmp.name)
        self.row={'source_id':'legacy_one','url':'https://example.com/article','时间':'2021-03-31',
            '分类':'Earnings','关键词':['Example Bank'],'主要内容':'Example Bank (NYSE: EXB) reported revenue growth in regional banking.',
            '股票实体':[],'涉及行业':[],'影响范围':[],
            '时间信息':{'publication_precision':'date','first_seen_at':'2026-11-01T00:00:00+00:00',
                'fetched_at':'2026-11-01T00:01:00+00:00','feature_available_at':'2026-11-01T00:02:00+00:00','availability_basis':'extraction'}}
        self.path=self.root/'data/US_original/US/sample.json'
        dump(self.path,self.row)
        (self.root/'data/GLOBAL_original').mkdir(parents=True)
        self.audit={'source_id':'legacy_one','url':self.row['url'],'record_hash':digest(self.row),
            'source_published_at':self.row['时间'],'impact_scope':['company'],
            'securities':[{'name':'Example Bank','ticker':'EXB','exchange':'NYSE','quote':'Example Bank (NYSE: EXB)'}],
            'industries':[{'name':'regional banking','basis':'explicit','quote':'regional banking'}]}
        dump(self.root/'.runtime/simple/audit/legacy_one.json',self.audit)
    def tearDown(self):self.tmp.cleanup()

    def entry(self):return inventory(self.root)['legacy_one']

    def test_matching_audit_recovers_metadata_and_keeps_original_text_time(self):
        e=self.entry();baseline=legacy_analysis(e)
        row,flags=build_record(e,baseline,'2026-11-12T00:00:00+00:00')
        self.assertEqual(len(row),11);self.assertIsNone(row['title'])
        self.assertEqual(row['股票实体'][0]['股票代码'],'EXB')
        self.assertEqual(row['时间'],'2021-03-31')
        self.assertEqual(row['主要内容'],self.row['主要内容'])
        self.assertEqual(row['时间信息']['feature_available_at'],'2026-11-12T00:00:00+00:00')
        self.assertIn('title_unavailable_offline',flags)
        self.assertEqual(row['影响范围'],['company'])

    def test_unmatched_audit_cannot_supply_a_ticker(self):
        bad=dict(self.audit,record_hash='wrong')
        dump(self.root/'.runtime/simple/audit/legacy_one.json',bad)
        e=self.entry();row,flags=build_record(e,legacy_analysis(e),'2026-11-12T00:00:00+00:00')
        self.assertFalse(row['股票实体']);self.assertIn('matching_legacy_audit_unavailable',flags)

    def test_duplicate_global_copy_is_one_job_two_identical_outputs(self):
        dump(self.root/'data/GLOBAL_original/GLOBAL/sample.json',self.row)
        jobs=inventory(self.root);self.assertEqual(len(jobs),1)
        e=jobs['legacy_one'];before=sha(self.path);output=self.root/'data_enriched'
        audit=save_result(self.root,output,'legacy_one',e,legacy_analysis(e),None,None,'completed')
        self.assertEqual(len(audit['outputs']),2)
        self.assertEqual((output/'US/sample.json').read_bytes(),(output/'GLOBAL/sample.json').read_bytes())
        self.assertEqual(sha(self.path),before);self.assertTrue(completed(output,'legacy_one',e))
        self.assertEqual(audit['analysis_input'],'legacy_summary')
        self.assertIsNone(audit['llm_inferred_at'])
        self.assertEqual(audit['original_time_information'],self.row['时间信息'])

    def test_conflicting_old_copies_are_reported(self):
        dump(self.root/'data/GLOBAL_original/GLOBAL/sample.json',dict(self.row,主要内容='Different text'))
        with self.assertRaises(ValueError):inventory(self.root)

    def test_code_recovery_uses_explicit_text_without_model_guessing(self):
        e=self.entry();e['audit']={}
        class Model:
            cfg=type('Config',(),{'num_ctx':4096})()
            calls=0
            root=self.root
            def ask(self,purpose,prompt,payload,schema,validate,max_predict):
                self.calls+=1
                value=Metadata.model_validate(dict(useful=True,reason='Revenue growth',
                    entities=[dict(name='Example Bank',kind='company',quote='Example Bank')],
                    industries=[],impact_scope=['company'])).model_dump()
                return validate(value)
        result,stamp,chunks=infer(Model(),e['row'])
        self.assertEqual(result['securities'][0]['name'],'Example Bank')
        self.assertEqual(result['securities'][0]['ticker'],'EXB');self.assertTrue(stamp)
        self.assertEqual(chunks,1)

    def test_model_entity_schema_forbids_stock_code_guesses(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            Metadata.model_validate(dict(useful=True,reason='test',entities=[dict(name='Example Bank',kind='company',quote='Example Bank',ticker='GUESS')]))

    def test_people_and_products_do_not_become_stock_entities(self):
        row=dict(self.row,主要内容='Example Bank named John Smith, Chief Executive Officer, to lead Widget sales.')
        class Model:
            cfg=type('Config',(),{'num_ctx':4096})()
            calls=0
            root=self.root
            def ask(self,purpose,prompt,payload,schema,validate,max_predict):
                self.calls+=1
                value=Metadata.model_validate(dict(useful=True,reason='Operating change',entities=[
                    dict(name=' Example Bank ',kind='company',quote='Example Bank'),
                    dict(name='John Smith',kind='company',quote='John Smith, Chief Executive Officer'),
                    dict(name='Widget',kind='product',quote='Widget')],industries=[],impact_scope=['industry'])).model_dump()
                return validate(value)
        result,_,_=infer(Model(),row)
        self.assertEqual([item['name'] for item in result['securities']],['Example Bank'])
        self.assertIn('company',result['impact_scope'])

    def test_security_conflict_remains_unresolved(self):
        result=merge_entities([dict(name='Example Bank',ticker=x,exchange='NYSE') for x in ['AAA','BBB','AAA']])
        self.assertIsNone(result[0]['ticker'])

    def test_output_tampering_prevents_a_resume_skip(self):
        e=self.entry();output=self.root/'data_enriched'
        save_result(self.root,output,'legacy_one',e,legacy_analysis(e),None,None,'completed')
        (output/'US/sample.json').write_text('{}',encoding='utf-8')
        self.assertFalse(completed(output,'legacy_one',e))


if __name__=='__main__':unittest.main()

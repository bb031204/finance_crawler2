from pathlib import Path
import json
import tempfile
import unittest
import zipfile
from finance_agent.core import digest,dump,now
from offline_enrichment.retrace_news import matching_audit,validate_result,merge_notes,publish,load
from market_scope import evidence_pack


class Dictionary:
    def selection_valid(self,key,name):return key=='SEC-SIC:6022' and name=='banks'
    def evidence_matches(self,key,quote):return 'banks' in quote
    def ground(self,note,source,offered,minimum=.75):return note


class RetraceTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).resolve().parents[1]/'.runtime/test_retrace';folder.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=folder);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def test_matching_legacy_audit_restores_original_source_hash(self):
        row={'source_id':'a','content':'old'}
        dump(self.root/'data_enriched/analysis/a.json',{'record_hash':digest(row),'legacy_producer':{'source_body_hash':'old-hash'},'analysis':{'industries':[]}})
        audit,pin=matching_audit(self.root,row)
        self.assertEqual(audit['source_body_hash'],'old-hash')
        self.assertTrue(pin['sha256'])

    def test_invented_optional_quote_cannot_propagate(self):
        row={'source_id':'a','主要内容':'A company reported its own earnings.'}
        value={'source_id':'a','impact_scope':['market'],'impact_scope_evidence':[], 'market_scope':['US_EQUITY'],'scope_evidence':[{
            'scope':'US_EQUITY','evidence_quote':'The Federal Reserve cut interest rates.','rationale':'Broad monetary transmission to financing conditions.','confidence':.99}],
            'industries':[{'industry_key':'SEC-SIC:6022','name':'banks','quote':'Banks will face new regulations.','basis':'inferred'}],
            'industry_impacts':[]}
        result=validate_result(value,row,row['主要内容'],[{'key':'SEC-SIC:6022'}],Dictionary(),'stored_summary_only',.85)
        self.assertEqual(result['industries'],[]);self.assertEqual(result['market_scope'],[])
        self.assertTrue(result['relation_quality_flags'])

    def test_chunk_scope_merge_is_deterministic(self):
        base={'scope':'US_EQUITY','confidence':.9}
        merged=merge_notes([{'scope_evidence':[base]},{'scope_evidence':[dict(base,confidence=.95)]}])
        self.assertEqual(len(merged['scope_evidence']),1)
        self.assertEqual(merged['scope_evidence'][0]['confidence'],.95)

    def test_complete_package_preserves_raw_and_rebases_adapted_proof(self):
        out=self.root/'data_enriched/retraced/v1';out.mkdir(parents=True)
        source='The Federal Reserve raised interest rates for banks.';stamp=now()
        row={'source_id':'a','url':'https://example.com/a','title':None,'时间':'2017-01-01','主要内容':source,'关键词':[],
             '分类':'policy','股票实体':[],'涉及行业':[{'名称':'banks','依据':'legacy_metadata'}],'影响范围':['market'],'时间信息':{'feature_available_at':stamp}}
        original=self.root/'data/US/a.json';dump(original,row)
        jobs={'a':{'row':row,'inputs':[{'path':'data/US/a.json','sha256':__import__('hashlib').sha256(original.read_bytes()).hexdigest()}]}}
        audit={'record_hash':digest(row),'source_body_hash':digest(source.encode()),'industries':[{
                   'name':'banks','basis':'inferred','quote':source,'industry_code':'6022','industry_standard':'SEC-SIC',
                   'industry_key':'SEC-SIC:6022','industry_name_standard':'STATE COMMERCIAL BANKS','dictionary_sha256':'pinned'}],
               'classification_contract':'dictionary-scope-replacement-1','impact_scope':[],'impact_scope_evidence':[],
               'industry_impacts':[],'events':[],'market_scope':[],'scope_evidence':[],'relation_quality_flags':[]}
        proof=evidence_pack(row,audit,source);dump(out/'evidence/a.json',proof)
        result={'source_id':'a','status':'completed','feature_available_at':stamp,'audit':audit,
                'source_record_hash':digest(row),
                'source_trace':{'status':'original_refetched','title':'Verified original heading'}}
        dump(out/'analysis/a.json',result)
        dump(out/'run.json',{})
        dump(out/'input_index.json',{'a':jobs['a']['inputs']})
        with zipfile.ZipFile(out/'raw_news.zip','w') as z:
            z.writestr('a.json',json.dumps(row));z.writestr('originals/data/US/a.json',original.read_bytes())
        with zipfile.ZipFile(out/'source_code.zip','w') as z:z.writestr('test_source.py','# frozen test source')
        dump(out/'live_progress.json',{'state':'running'});dump(out/'coordinator_status.json',{'state':'running'})
        report=publish(self.root,out,jobs,{},'digest')
        self.assertEqual(report['pending'],0);self.assertTrue(report['original_inputs_unchanged'])
        adapted=load(out/'adapted/data/US/a.json');new_proof=load(out/'adapted/evidence/a.json')
        self.assertEqual(new_proof['record_hash'],digest(adapted));self.assertEqual(load(original),row)
        self.assertEqual(adapted['title'],'Verified original heading')
        self.assertEqual(adapted['涉及行业'][0]['依据'],'inferred')
        self.assertEqual(adapted['涉及行业'][0]['名称'],'STATE COMMERCIAL BANKS')
        self.assertEqual(adapted['影响范围'],[])
        self.assertEqual(load(out/'adapted/audit/a.json')['classification_replacement']['source_record_hash'],digest(row))
        from structured_export import arrow
        pa,pq=arrow();rows=pq.read_table(pa.BufferReader((out/'structured/article_industries.parquet').read_bytes())).to_pylist()
        self.assertEqual(rows[0]['industry_code'],'6022')
        with zipfile.ZipFile(out/'dataset_bundle.zip') as z:
            self.assertIsNone(z.testzip());self.assertIn('adapted/evidence/a.json',z.namelist())
        from offline_enrichment.verify_retrace import verify
        checked=verify(out);self.assertTrue(checked['passed'],checked['errors'])
        dump(out/'live_progress.json',{'state':'finished'});dump(out/'coordinator_status.json',{'state':'finished'})
        checked=verify(out);self.assertTrue(checked['passed'],checked['errors'])


if __name__=='__main__':unittest.main()

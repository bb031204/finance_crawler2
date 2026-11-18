import copy,json,tempfile,unittest,shutil,hashlib
from pathlib import Path
from industry_dictionary import IndustryDictionary
from interface_schema import IndustryDictionaryConfig
from qwen_agent import Qwen,ContentNote
from simple_crawler import Config

ROOT=Path(__file__).resolve().parents[1]

class DictionaryTests(unittest.TestCase):
    def setUp(self):self.dictionary=IndustryDictionary(ROOT/'industry_data')
    def test_full_dictionary_has_namespaced_official_and_reference_entries(self):
        self.assertGreater(len(self.dictionary.rows),500)
        self.assertEqual(self.dictionary.by_key['SEC-SIC:3674']['industry_standard'],'SEC-SIC')
        self.assertTrue(any(r['industry_standard']=='TRSR-INDUSTRY' for r in self.dictionary.rows))
    def test_bounded_candidates_do_not_invent_codes(self):
        rows=self.dictionary.candidates('Semiconductor manufacturing and integrated microcircuits face a supply constraint.',12)
        self.assertLessEqual(len(rows),12);self.assertIn('SEC-SIC:3674',[r['key'] for r in rows])
        self.assertTrue(all(r['key'] in self.dictionary.by_key for r in rows))
        self.assertTrue(self.dictionary.selection_valid('SEC-SIC:3674','Semiconductor'))
        self.assertFalse(self.dictionary.selection_valid('SEC-SIC:3669','Semiconductor'))
        rows=self.dictionary.candidates('Production equipment product design delivery suppliers industry supply capacity semiconductor manufacturing.',12,['SEC-SIC'],headline='Semiconductor supply constraints')
        self.assertEqual(rows[0]['key'],'SEC-SIC:3674')
    def test_code_quote_confidence_and_dictionary_version_are_bound(self):
        quote='Semiconductor capacity is constrained across the industry.'
        note={'content':'An industry-wide capacity constraint affects semiconductor producers.',
            'industry_impacts':[{'industry_key':'SEC-SIC:3674','industry_name_original':'Semiconductor',
                'confidence':.95,'quote':quote,'industry_scope_quote':quote,'evidence_type':'inferred','mechanism':'Capacity constraints reduce the supply available to customers.'}]}
        result=self.dictionary.ground(copy.deepcopy(note),quote,[{'key':'SEC-SIC:3674'}])
        item=result['industry_impacts'][0]
        self.assertEqual(item['industry_code'],'3674');self.assertTrue(item['original_evidence_verified'])
        self.assertIsNone(item['summary_evidence_quote']);self.assertEqual(item['dictionary_sha256'],self.dictionary.sha256)
        self.assertEqual(note['industry_impacts'][0].get('industry_code'),None)
    def test_non_offered_code_and_fabricated_quotes_are_excluded(self):
        for key,quote,score in [('SEC-SIC:3674','Invented unsupported quote',.9),('FAKE:99','The original source has evidence.',.9),('SEC-SIC:3674','The original source has evidence.',None)]:
            out=self.dictionary.ground({'content':'','industry_impacts':[{'industry_key':key,'quote':quote,'confidence':score}]},
                'The original source has evidence.',[{'key':'SEC-SIC:3674'}])
            self.assertFalse(out['industry_impacts'])
    def test_dictionary_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'manifest.json').write_bytes((ROOT/'industry_data/manifest.json').read_bytes())
            (p/'industry_dictionary.jsonl').write_bytes((ROOT/'industry_data/industry_dictionary.jsonl').read_bytes()+b'\n')
            with self.assertRaises(ValueError):IndustryDictionary(p)
    def test_auxiliary_source_tampering_is_not_hidden_by_unchanged_dictionary_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'dictionary';shutil.copytree(ROOT/'industry_data',p)
            (p/'sec_sic_source.html').write_bytes(b'Changed evidence, unchanged dictionary rows')
            with self.assertRaisesRegex(ValueError,'source evidence SHA'):IndustryDictionary(p)
    def test_key_code_namespace_mismatch_is_rejected_even_with_rehashed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'dictionary';shutil.copytree(ROOT/'industry_data',p)
            records=[json.loads(line) for line in (p/'industry_dictionary.jsonl').read_text(encoding='utf-8').splitlines()]
            records[0]['key']='SEC-SIC:WRONG'
            raw=''.join(json.dumps(r)+'\n' for r in records).encode();(p/'industry_dictionary.jsonl').write_bytes(raw)
            meta=json.loads((p/'manifest.json').read_text(encoding='utf-8'));meta['dictionary_sha256']=hashlib.sha256(raw).hexdigest()
            meta['files']['industry_dictionary.jsonl']=meta['dictionary_sha256'];(p/'manifest.json').write_text(json.dumps(meta),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'namespace mismatch'):IndustryDictionary(p)
    def test_context_supplies_dictionary_without_an_extra_model_stage(self):
        cfg=Config(collection={'mode':'historical','start_date':'2013-01-01','end_date':'2020-12-31'},
            monitoring={'markets':['US']},search={'market_domains':{'US':['example.com']}})
        q=Qwen(cfg.ollama,ROOT,industry_settings=IndustryDictionaryConfig(enabled=True))
        try:
            context=q.read_context({'url':'https://example.test/news','title':'Semiconductor supply','origin_market':'US',
                'text':'Semiconductor manufacturers face industry-wide capacity constraints.'},cfg.monitoring,'')
            self.assertEqual(q.calls,0);self.assertTrue(context['industry_dictionary_candidates'])
            self.assertEqual(context['industry_dictionary_sha256'],self.dictionary.sha256)
        finally:q.close()

if __name__=='__main__':unittest.main()

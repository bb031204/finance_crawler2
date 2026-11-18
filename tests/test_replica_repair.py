"""Version conflicts require explicit, evidence-bound, backed-up maintenance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from finance_agent.core import digest,dump,Lock
from record_files import Dataset
from record_time import with_time_information
import repair_replicas as repair


class ReplicaRepairTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_replica_repair';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
        self.old=with_time_information(dict(source_id='fixture',url='https://example.com/item',时间='2012-03-15',
            分类='Banking',关键词=['Example Bank'],主要内容='Example Bank reported increased lending demand.',
            股票实体=[],涉及行业=[],影响范围=[]),available_at='2026-01-01T00:00:00+00:00',basis='migration')
        self.new=deepcopy(self.old);self.new.update(title=None,股票实体=[{'名称':'Example Bank','股票代码':None,'交易所':None}],
            涉及行业=[{'名称':'Banking','依据':'inferred'}],影响范围=['company'])
        self.new['时间信息']['feature_available_at']='2026-01-02T00:00:00+00:00'
        self.targets={m:self.root/'data'/m/'item.json' for m in ['GLOBAL','CN','US']}
        for m,p in self.targets.items():dump(p,self.old if m=='CN' else self.new)
        original=self.root/'data/US_original/item.json';dump(original,self.old)
        self.audit_path=self.root/'.runtime/simple/audit/fixture.json'
        self.audit={'source_id':'fixture','record_hash':digest(self.old),'canonical_market':'GLOBAL',
                    'source_body_hash':'original-source-body','feature_available_at':self.old['时间信息']['feature_available_at']}
        dump(self.audit_path,self.audit)
        outputs=[]
        for market in ['GLOBAL','US']:
            path=self.root/'data_enriched'/market/'item.json';dump(path,self.new)
            outputs.append({'path':str(path.relative_to(self.root/'data_enriched')),'sha256':repair.sha(path.read_bytes())})
        self.evidence_path=self.root/'data_enriched/analysis/fixture.json'
        self.evidence={'source_id':'fixture','status':'completed','canonical_market':'GLOBAL',
            'record_hash':digest(self.new),'original_record_hash':digest(self.old),'outputs':outputs,
            'legacy_producer':{'source_body_hash':'original-source-body'},
            'original_files':[{'path':str(original.relative_to(self.root)),'sha256':repair.sha(original.read_bytes())}],
            'feature_available_at':'2026-01-02T00:00:00+00:00','commit_verified_at':'2026-01-02T00:00:00+00:00',
            'llm_inferred_at':'2026-01-02T00:00:00+00:00','analysis_input':'legacy_summary',
            'source_verification':'original_webpage_not_revisited','model':'qwen3:8b'}
        dump(self.evidence_path,self.evidence)
    def tearDown(self):self.tmp.cleanup()
    def apply(self):
        with Lock(self.root):return repair.apply(self.root)

    def test_preview_does_not_modify_originals_or_restore_transactions(self):
        files=list(self.root.rglob('*.json'));before={p:p.read_bytes() for p in files}
        result=repair.preview(repair.inspect(self.root))
        self.assertEqual(result['repairable'],1);self.assertFalse(result['unresolved'])
        self.assertEqual(before,{p:p.read_bytes() for p in files})
        self.assertFalse((self.root/repair.OUTBOX).exists())

    def test_import_preserves_source_facts_and_backs_up_every_previous_version(self):
        before={str(p.relative_to(self.root)):p.read_bytes() for p in [*self.targets.values(),self.audit_path,self.evidence_path]}
        result=self.apply();self.assertEqual(result['repaired_records'],1);self.assertEqual(result['synchronized_files'],3)
        rows=[repair.read(p) for p in self.targets.values()]
        self.assertTrue(all(row==rows[0] for row in rows))
        for key in repair.CORE:self.assertEqual(rows[0][key],self.old[key])
        self.assertGreater(rows[0]['时间信息']['feature_available_at'],self.new['时间信息']['feature_available_at'])
        audit=repair.read(self.audit_path)
        self.assertEqual(audit['record_hash'],digest(rows[0]));self.assertEqual(audit['source_body_hash'],'original-source-body')
        self.assertEqual(audit['metadata_enrichment']['analysis_input'],'legacy_summary')
        with zipfile.ZipFile(self.root/result['backup']) as z:
            for name,raw in before.items():self.assertEqual(z.read(name.replace('\\','/')),raw)
        loaded=Dataset(self.root,'data');self.assertEqual(len(loaded.rows),1)
        self.assertEqual(len(loaded.replicas['fixture']),3)
        self.assertEqual(repair.read(self.root/'data/US_original/item.json'),self.old)
        self.assertEqual(repair.read(self.root/'data_enriched/GLOBAL/item.json'),self.new)

    def test_body_or_publication_conflict_cannot_be_automatically_chosen(self):
        changed=deepcopy(self.old);changed['时间']='2012-03-16';dump(self.targets['CN'],changed)
        plan=repair.inspect(self.root);self.assertEqual(plan['repairable'],0);self.assertEqual(len(plan['unresolved']),1)
        with self.assertRaises(ValueError):self.apply()
        self.assertFalse(list((self.root/'archive').glob('*.zip')))

    def test_evidence_hash_mismatch_never_overwrites_files(self):
        self.evidence['record_hash']='wrong';dump(self.evidence_path,self.evidence)
        before=self.targets['CN'].read_bytes()
        self.assertEqual(repair.inspect(self.root)['repairable'],0)
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.targets['CN'].read_bytes(),before)

    def test_original_input_change_invalidates_enrichment_proof(self):
        dump(self.root/'data/US_original/item.json',dict(self.old,主要内容='Different original text'))
        self.assertEqual(repair.inspect(self.root)['repairable'],0)
        with self.assertRaises(ValueError):self.apply()

    def test_moved_original_is_allowed_only_with_exact_old_replica_bytes(self):
        (self.root/'data/US_original/item.json').unlink()
        self.assertEqual(repair.inspect(self.root)['repairable'],1)
        dump(self.targets['CN'],self.old)  # Reformatting changes bytes only if explicitly forced below.
        self.targets['CN'].write_bytes(json.dumps(self.old,ensure_ascii=False).encode('utf-8'))
        self.assertEqual(repair.inspect(self.root)['repairable'],0)

    def interrupted(self,finalizing=False):
        original=repair.dump;writes=0
        def failing(path,value,*a,**kw):
            nonlocal writes
            if Path(path)==self.targets['CN']:
                writes+=1
                if writes==(2 if finalizing else 1):raise OSError('synthetic interruption')
            return original(path,value,*a,**kw)
        with patch.object(repair,'dump',side_effect=failing),self.assertRaises(OSError):self.apply()
        self.assertTrue((self.root/repair.OUTBOX).is_file())
        with self.assertRaisesRegex(ValueError,'修复事务'):Dataset(self.root,'data')

    def test_interrupted_replica_write_can_resume_without_a_new_backup(self):
        self.interrupted();result=self.apply()
        self.assertEqual(result['repaired_records'],1);self.assertEqual(len(list((self.root/'archive').glob('*.zip'))),1)
        self.assertFalse((self.root/repair.OUTBOX).exists());self.assertEqual(len(Dataset(self.root,'data').rows),1)

    def test_interrupted_final_timestamp_commit_can_resume(self):
        self.interrupted(finalizing=True)
        self.assertEqual(self.apply()['repaired_records'],1)
        rows=[repair.read(p) for p in self.targets.values()];self.assertTrue(all(v==rows[0] for v in rows))

    def test_external_edit_after_interruption_is_preserved_and_blocks_resume(self):
        self.interrupted();changed=dict(self.old,主要内容='External user edit');dump(self.targets['US'],changed)
        with self.assertRaisesRegex(ValueError,'额外修改'):self.apply()
        self.assertEqual(repair.read(self.targets['US']),changed)

    def test_corrupt_backup_blocks_every_write(self):
        self.interrupted();operation=repair.read(self.root/repair.OUTBOX)
        (self.root/operation['backup']).write_bytes(b'corrupted')
        before={p:p.read_bytes() for p in self.targets.values()}
        with self.assertRaisesRegex(ValueError,'备份'):self.apply()
        self.assertEqual(before,{p:p.read_bytes() for p in self.targets.values()})

    def test_second_apply_is_idempotent(self):
        self.apply();before={p:p.read_bytes() for p in [*self.targets.values(),self.audit_path]}
        self.assertEqual(self.apply()['status'],'unchanged')
        self.assertEqual(before,{p:p.read_bytes() for p in before})


if __name__=='__main__':unittest.main()

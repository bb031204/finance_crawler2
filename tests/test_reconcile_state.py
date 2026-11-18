"""State↔library reconciliation: dry-run report, explicit apply, archive restoration."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from finance_agent.runtime import setup
setup(Path(__file__).resolve().parents[1])
from finance_agent.core import digest,dump
from record_time import with_time_information
from reconcile_state import reconcile_state,restore_files
from test_simple_crawler import config


def make_row(identity,url=None):
    return with_time_information({'source_id':identity,'url':url or ('https://example.com/'+identity),
        '时间':'2026-11-02','分类':'监管政策','关键词':['离线测试'],'主要内容':'仅为离线测试夹具，不是真实金融新闻。'},
        available_at='2026-11-03T00:00:00+00:00')


class ReconcileStateTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_reconcile';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.temp.name);self.cfg=config()
    def tearDown(self):self.temp.cleanup()

    def write_state(self,records,pending=None):
        dump(self.root/'.runtime/simple/state.json',{'records':records,'pending':pending or {},'plans':{}})

    def read_state(self):
        return json.loads((self.root/'.runtime/simple/state.json').read_text(encoding='utf-8'))

    def commit(self,identity,market='CN'):
        row=make_row(identity)
        dump(self.root/'data'/market/(identity+'_fixture.json'),row)
        dump(self.root/'.runtime/simple/audit'/(identity+'.json'),{'record_hash':digest(row)})
        return row

    def test_dry_run_reports_without_writes(self):
        self.commit('aaaa');self.commit('bbbb')
        self.write_state({'aaaa':{'status':'saved','at':'2026-11-01T00:00:00+00:00'},
                          'cccc':{'status':'saved','at':'2026-11-01T00:00:00+00:00'}})
        before=(self.root/'.runtime/simple/state.json').read_text(encoding='utf-8')
        result=reconcile_state(self.root,self.cfg,apply=False)
        self.assertFalse(result['apply'])
        self.assertEqual(result['library_records'],2)
        self.assertEqual(result['fixed_state'],['bbbb'])
        self.assertEqual(result['restored_count'],0)
        self.assertEqual(result['lost'],['cccc'])
        self.assertEqual((self.root/'.runtime/simple/state.json').read_text(encoding='utf-8'),before)
        self.assertFalse((self.root/'data/CN/cccc_fixture.json').exists())

    def test_apply_fixes_state_and_marks_lost(self):
        self.commit('aaaa');self.commit('bbbb')
        self.write_state({'aaaa':{'status':'saved','at':'2026-11-01T00:00:00+00:00'},
                          'cccc':{'status':'saved','at':'2026-11-01T00:00:00+00:00'}})
        result=reconcile_state(self.root,self.cfg,apply=True)
        records=self.read_state()['records']
        self.assertEqual(records['bbbb']['status'],'saved')
        self.assertTrue(records['bbbb']['reconciled'])
        self.assertEqual(records['cccc']['status'],'lost')
        self.assertTrue(records['cccc']['lost_at'])

    def test_apply_restores_from_archive_with_matching_audit(self):
        row=make_row('dddd');(self.root/'archive').mkdir(parents=True)
        with zipfile.ZipFile(self.root/'archive'/'fixture_backup.zip','w') as z:
            z.writestr('data/CN/dddd_fixture.json',json.dumps(row,ensure_ascii=False))
            z.writestr('.runtime/simple/audit/dddd.json',json.dumps({'record_hash':digest(row)}))
        self.write_state({'dddd':{'status':'saved','at':'2026-11-01T00:00:00+00:00'}})
        with patch('structured_export.export_or_defer',return_value={'status':'completed'}) as export:
            result=reconcile_state(self.root,self.cfg,apply=True)
        self.assertEqual(result['restored'],['dddd'])
        self.assertEqual(result['restore_details'][0]['mode'],'verified_by_archive_audit')
        self.assertEqual(json.loads((self.root/'data/CN/dddd_fixture.json').read_text(encoding='utf-8')),row)
        audit=json.loads((self.root/'.runtime/simple/audit/dddd.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['record_hash'],digest(row))
        export.assert_called_once()
        self.assertEqual(self.read_state()['records']['dddd']['status'],'saved')

    def test_archive_guard_rejects_unrestored_user_zip(self):
        (self.root/'data').mkdir(parents=True)
        with zipfile.ZipFile(self.root/'data/US.zip','w') as z:
            z.writestr('US/a.json','{}');z.writestr('US/b.json','{}')
        (self.root/'data/US').mkdir(parents=True)
        dump(self.root/'data/US/one.json',make_row('eeee'))
        with self.assertRaisesRegex(ValueError,'归档未恢复'):
            reconcile_state(self.root,self.cfg,apply=False)

    def test_audit_mismatch_blocks_restoration(self):
        row=make_row('ffff');(self.root/'archive').mkdir(parents=True)
        with zipfile.ZipFile(self.root/'archive'/'fixture.zip','w') as z:
            z.writestr('data/CN/ffff_fixture.json',json.dumps(row,ensure_ascii=False))
        dump(self.root/'.runtime/simple/audit/ffff.json',{'record_hash':digest(make_row('other'))})
        self.write_state({'ffff':{'status':'saved','at':'2026-11-01T00:00:00+00:00'}})
        result=reconcile_state(self.root,self.cfg,apply=True)
        self.assertEqual(result['audit_mismatch'],['ffff'])
        self.assertFalse((self.root/'data/CN/ffff_fixture.json').exists())

    def test_conflict_without_matching_audit_is_reported_only(self):
        dump(self.root/'data/CN/gggg_fixture.json',make_row('gggg'))  # 磁盘有JSON但无审计
        self.write_state({})
        result=reconcile_state(self.root,self.cfg,apply=True)
        self.assertEqual(result['conflicts'],['gggg'])
        self.assertEqual(result['fixed_state'],[])
        self.assertNotIn('gggg',self.read_state()['records'])

    def test_alias_entry_restores_target_record(self):
        row=make_row('tttt');(self.root/'archive').mkdir(parents=True)
        with zipfile.ZipFile(self.root/'archive'/'fixture.zip','w') as z:
            z.writestr('data/US/tttt_fixture.json',json.dumps(row,ensure_ascii=False))
            z.writestr('.runtime/simple/audit/tttt.json',json.dumps({'record_hash':digest(row)}))
        self.write_state({'alias':{'status':'saved','record_id':'tttt',
            'request_url':'https://example.com/alias','at':'2026-11-01T00:00:00+00:00'}})
        with patch('structured_export.export_or_defer',return_value={'status':'completed'}):
            result=reconcile_state(self.root,self.cfg,apply=True)
        self.assertEqual(result['restored'],['tttt'])
        self.assertEqual(result['restore_details'][0]['via_alias'],'alias')
        self.assertTrue((self.root/'data/US/tttt_fixture.json').exists())
        records=self.read_state()['records']
        self.assertEqual(records['alias']['status'],'saved')  # 目标记录已找回，别名重新有效
        self.assertEqual(records['tttt']['status'],'saved')

    def test_requeue_lost_reenqueues_with_current_scope(self):
        self.write_state({'hhhh':{'status':'saved','record_id':'hhhh',
            'request_url':'https://example.com/hhhh','at':'2026-11-01T00:00:00+00:00'}})
        result=reconcile_state(self.root,self.cfg,apply=True,requeue_lost=True)
        self.assertEqual(result['lost'],['hhhh'])
        self.assertEqual(result['requeued'],['hhhh'])
        pending=self.read_state()['pending']
        from simple_crawler import collection_scope_digest
        self.assertEqual(pending['hhhh']['collection_scope'],collection_scope_digest(self.cfg))
        self.assertEqual(pending['hhhh']['origin_market'],'CN')
        self.assertEqual(pending['hhhh']['url'],'https://example.com/hhhh')

    def test_needs_audit_restore_does_not_fabricate_audit(self):
        row=make_row('jjjj');(self.root/'archive').mkdir(parents=True)
        with zipfile.ZipFile(self.root/'archive'/'fixture.zip','w') as z:
            z.writestr('data/CN/jjjj_fixture.json',json.dumps(row,ensure_ascii=False))
        self.write_state({'jjjj':{'status':'saved','at':'2026-11-01T00:00:00+00:00'}})
        result=reconcile_state(self.root,self.cfg,apply=True)
        self.assertEqual(result['restore_details'][0]['mode'],'needs_audit')
        self.assertEqual(json.loads((self.root/'data/CN/jjjj_fixture.json').read_text(encoding='utf-8')),row)
        self.assertFalse((self.root/'.runtime/simple/audit/jjjj.json').exists())

    def test_restore_files_never_overwrites(self):
        (self.root/'data/CN').mkdir(parents=True)
        existing=self.root/'data/CN/iiii_fixture.json'
        dump(existing,{'marker':'keep'})
        written,kept=restore_files(self.root,'data',[('data/CN/iiii_fixture.json','CN',make_row('iiii'))])
        self.assertEqual(written,[]);self.assertEqual([Path(p).as_posix() for p in kept],['data/CN/iiii_fixture.json'])
        self.assertEqual(json.loads(existing.read_text(encoding='utf-8')),{'marker':'keep'})


if __name__=='__main__':unittest.main()

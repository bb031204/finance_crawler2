import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from mapping_maintenance import repair_saved_mappings,plan_mapping_repairs
from record_files import Dataset,record_name
from record_time import with_time_information
from analysis_metadata import public_analysis
from finance_agent.core import digest,dump
from sec_discovery import resolve_current_ticker


class MappingMaintenanceTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_mapping_maintenance';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def fixture(self,code='Example Company',quote=None,matched=True):
        name='Example Company'
        row=with_time_information(dict(source_id='offline',url='https://example.com/offline',时间='2019-01-01',分类='Earnings',
            关键词=[name],主要内容='An offline company fixture, not a real announcement.',**public_analysis({})),available_at='2026-11-01T00:00:00Z')
        row['股票实体']=[{'名称':name,'股票代码':code,'交易所':None}]
        for market in ['CN','US','GLOBAL']:
            path=self.root/'data'/market/record_name(row,'Technology');dump(path,row)
        audit={'source_id':'offline','record_hash':digest(row) if matched else 'wrong','securities':[
            {'name':name,'ticker':code,'exchange':None,'quote':quote or name}],'date_quote':'Published 2019-01-01'}
        dump(self.root/'.runtime/simple/audit/offline.json',audit)
        return Dataset(self.root,'data'),row,audit

    def test_invalid_code_repair_is_backed_up_and_updates_all_views_and_audit(self):
        ds,original,audit=self.fixture();before={p.relative_to(self.root).as_posix():p.read_bytes() for p in ds.replicas['offline'].values()}
        report=repair_saved_mappings(ds)
        self.assertEqual((report['changed_records'],report['changed_files'],report['changed_entities']),(1,3,1))
        row=ds.rows['offline'][1]
        self.assertIsNone(row['股票实体'][0]['股票代码']);self.assertEqual(row['主要内容'],original['主要内容'])
        self.assertEqual(row['时间'],original['时间']);self.assertEqual(row['关键词'],original['关键词'])
        self.assertGreater(row['时间信息']['feature_available_at'],original['时间信息']['feature_available_at'])
        for path in ds.replicas['offline'].values():self.assertEqual(json.loads(path.read_text(encoding='utf-8')),row)
        updated=json.loads((self.root/'.runtime/simple/audit/offline.json').read_text(encoding='utf-8'))
        self.assertEqual(updated['record_hash'],digest(row));self.assertEqual(updated['date_quote'],audit['date_quote'])
        self.assertFalse(updated['mapping_repair']['text_reprocessed'])
        with ZipFile(report['backup']) as archive:
            for name,body in before.items():self.assertEqual(archive.read(name),body)
        self.assertEqual(repair_saved_mappings(Dataset(self.root,'data'))['changed_records'],0)

    def test_unsupported_pair_is_cleared_even_if_code_format_is_valid(self):
        ds,_,_=self.fixture('OTHER','Example Company reported revenue; Other Company (NASDAQ: OTHER) reported earnings.')
        repair_saved_mappings(ds)
        self.assertIsNone(ds.rows['offline'][1]['股票实体'][0]['股票代码'])

    def test_valid_local_declaration_is_not_rewritten(self):
        ds,_,_=self.fixture('EXMP','Example Company (NASDAQ: EXMP) reported earnings.')
        before={p:p.read_bytes() for p in ds.replicas['offline'].values()}
        self.assertEqual(repair_saved_mappings(ds)['changed_records'],0)
        self.assertTrue(all(p.read_bytes()==body for p,body in before.items()))

    def test_unmatched_audit_does_not_repair_valid_shaped_code_from_guesses(self):
        ds,_,_=self.fixture('EXMP',matched=False)
        report=repair_saved_mappings(ds)
        self.assertEqual(report['changed_records'],0);self.assertEqual(len(report['unverified_entities']),1)

    def test_backup_failure_preserves_formal_files(self):
        ds,_,_=self.fixture();before={p:p.read_bytes() for p in ds.replicas['offline'].values()}
        with patch('mapping_maintenance.zipfile.ZipFile',side_effect=OSError('offline backup failure')):
            with self.assertRaises(OSError):repair_saved_mappings(ds)
        self.assertTrue(all(p.read_bytes()==body for p,body in before.items()))

    def test_audit_failure_keeps_recoverable_transaction(self):
        ds,_,_=self.fixture()
        from finance_agent.core import dump as actual_dump
        def fail(path,value):
            if Path(path).parent==self.root/'.runtime/simple/audit':raise OSError('offline audit failure')
            return actual_dump(path,value)
        with patch('record_files.dump',side_effect=fail):
            with self.assertRaises(OSError):repair_saved_mappings(ds)
        self.assertTrue(ds.outbox.is_file())
        ds=Dataset(self.root,'data');self.assertFalse(ds.outbox.exists())
        self.assertIsNone(ds.rows['offline'][1]['股票实体'][0]['股票代码'])
        audit=json.loads((self.root/'.runtime/simple/audit/offline.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['record_hash'],digest(ds.rows['offline'][1]))

    def test_cli_repair_does_not_construct_collector(self):
        import simple_crawler
        from test_simple_crawler import config
        self.fixture()
        with patch.object(simple_crawler,'ROOT',self.root),patch.object(simple_crawler,'load_config',return_value=config()), \
                patch.object(simple_crawler,'Crawler',side_effect=AssertionError('must not collect')):
            self.assertEqual(simple_crawler.main(['--repair-mappings']),0)

    def test_current_class_alias_requires_unique_mapping_evidence(self):
        mapping={'0':{'ticker':'BRK-B','cik_str':1067983}}
        self.assertEqual(resolve_current_ticker(mapping,'BRK.B'),'0001067983')
        with self.assertRaises(ValueError):resolve_current_ticker({},'BRK.B')
        with self.assertRaises(ValueError):resolve_current_ticker({'0':{'ticker':'AA-B','cik_str':1},'1':{'ticker':'AA-B','cik_str':2}},'AA.B')

    def test_exact_ticker_match_takes_priority_over_alias(self):
        mapping={'0':{'ticker':'AA.B','cik_str':1},'1':{'ticker':'AA-B','cik_str':2}}
        self.assertEqual(resolve_current_ticker(mapping,'AA.B'),'0000000001')

    def test_missing_and_invalid_current_cik_are_rejected(self):
        with self.assertRaises(ValueError):resolve_current_ticker({'0':{'ticker':'AAPL','cik_str':0}},'AAPL')
        with self.assertRaises(ValueError):resolve_current_ticker({'0':{'ticker':'OTHER','cik_str':1}},'AAPL')


if __name__=='__main__':unittest.main()

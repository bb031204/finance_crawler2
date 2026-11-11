import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from finance_agent.core import dump,digest
from record_files import Dataset,FIELDS,LEGACY_ANALYSIS_FIELDS,record_name
from record_time import with_time_information,upgrade_time_information,validate_time_information


class RecordTimeTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).resolve().parents[1]/'.runtime/test_times';folder.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=folder);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def row(self):
        return {'source_id':'offline','url':'https://example.com/offline','时间':'2026-11-02',
            '分类':'测试','关键词':['离线夹具'],'主要内容':'仅用于离线测试，不是真实金融信息。'}
    def test_date_only_is_explicit_and_does_not_invent_midnight(self):
        row=with_time_information(self.row(),available_at='2026-11-03T10:00:00+08:00')
        self.assertEqual(row['时间'],'2026-11-02')
        self.assertEqual(row['时间信息']['publication_precision'],'date')
        self.assertEqual(row['时间信息']['feature_available_at'],'2026-11-03T02:00:00+00:00')
        self.assertIsNone(row['时间信息']['fetched_at'])
    def test_source_timezone_and_minute_precision_remain_distinct(self):
        source=dict(self.row(),时间='2026-11-02T20:30+08:00')
        row=with_time_information(source,{'first_seen_at':'2026-11-02T20:31:00+08:00'},
            {'fetched_at':'2026-11-02T20:32:00+08:00'},'2026-11-02T20:33:00+08:00')
        self.assertEqual(row['时间'],source['时间']);self.assertEqual(row['时间信息']['publication_precision'],'minute')
        self.assertEqual(row['时间信息']['fetched_at'],'2026-11-02T12:32:00+00:00')
    def test_availability_cannot_be_before_body_download(self):
        with self.assertRaises(ValueError):with_time_information(self.row(),page={'fetched_at':'2026-11-03T11:00:00Z'},available_at='2026-11-03T10:00:00Z')
    def test_system_timestamp_without_timezone_is_rejected(self):
        with self.assertRaises(ValueError):with_time_information(self.row(),available_at='2026-11-03T10:00:00')
    def test_matching_audit_restores_measured_time(self):
        row=self.row();audit={'record_hash':digest(row),'source_published_at':row['时间'],
            'first_seen_at':'2026-11-03T10:00:00Z','fetched_at':'2026-11-03T10:01:00Z','feature_available_at':'2026-11-03T10:02:00Z'}
        dump(self.root/'.runtime/simple/audit/offline.json',audit)
        result=upgrade_time_information(self.root,row)['时间信息']
        self.assertEqual(result['availability_basis'],'verified_audit');self.assertEqual(result['feature_available_at'],'2026-11-03T10:02:00+00:00')
    def test_mismatched_audit_cannot_backdate_the_new_text(self):
        row=self.row();dump(self.root/'.runtime/simple/audit/offline.json',{'record_hash':'different content','source_published_at':row['时间'],'feature_available_at':'2026-11-02T00:00:00Z'})
        with patch('record_time.now',return_value='2026-11-09T10:00:00Z'):
            result=upgrade_time_information(self.root,row)['时间信息']
        self.assertEqual(result['availability_basis'],'migration');self.assertEqual(result['feature_available_at'],'2026-11-09T10:00:00+00:00')
        self.assertIsNone(result['first_seen_at']);self.assertIsNone(result['fetched_at'])
    def test_repeated_migration_keeps_time_and_global_views_consistent(self):
        row=self.row();path=self.root/'data/GLOBAL'/record_name(row,'测试行业');dump(path,row)
        class NoModel:
            def industry(self,row):raise AssertionError('No Qwen call is needed')
        ds=Dataset(self.root,'data');first=ds.migrate(NoModel())
        timed=ds.rows['offline'][1];self.assertEqual(set(timed),LEGACY_ANALYSIS_FIELDS);self.assertEqual(first['time_upgraded'],1)
        second=Dataset(self.root,'data');report=second.migrate(NoModel());self.assertEqual(report['time_upgraded'],0)
        self.assertEqual(second.rows['offline'][1],timed)
        for market in ['CN','US','GLOBAL']:
            self.assertEqual(json.loads(next((self.root/'data'/market).glob('*.json')).read_text(encoding='utf-8')),timed)
    def test_reprocessing_cannot_reuse_old_feature_availability(self):
        old=with_time_information(self.row(),available_at='2026-11-03T10:00:00Z')
        current=with_time_information(dict(old,主要内容='这是一条重新整理后的离线测试文本。'),available_at='2026-11-04T10:00:00Z')
        self.assertNotEqual(current['时间信息']['feature_available_at'],old['时间信息']['feature_available_at'])
        self.assertEqual(current['时间'],old['时间'])
    def test_incorrect_publication_precision_is_rejected(self):
        row=with_time_information(self.row(),available_at='2026-11-03T10:00:00Z')
        row['时间信息']['publication_precision']='second'
        with self.assertRaises(ValueError):validate_time_information(row)


if __name__=='__main__':unittest.main()

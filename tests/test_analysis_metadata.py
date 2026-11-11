import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from analysis_metadata import ANALYSIS_FIELDS, public_analysis, preserve_analysis, validate_industries, validate_public_analysis, grounded_industries
from record_files import Dataset, FIELDS, record_name
from record_quality import validate_securities, grounded_securities, retain_explicit_tickers
from record_time import with_time_information
from finance_agent.core import dump, digest
from simple_crawler import Crawler
from test_simple_crawler import config, candidate, FakeQwen, FakeSources
from qwen_agent import ContentNote


SOURCE = 'Apple Inc. (NASDAQ: AAPL) reported stronger demand for semiconductor chips.'
ENTITY = {'name': 'Apple Inc.', 'ticker': 'AAPL', 'exchange': 'NASDAQ', 'quote': 'Apple Inc. (NASDAQ: AAPL)'}
TOPIC = {'name': 'semiconductor chips', 'basis': 'explicit', 'quote': 'demand for semiconductor chips'}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1]/'.runtime/test_analysis_fields'
        folder.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=folder); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def row(self):
        return with_time_information({'source_id': 'offline', 'url': 'https://example.com/offline', '时间': '2019-01-01',
            '分类': 'Earnings', '关键词': ['Apple', 'AAPL'], '主要内容': SOURCE}, available_at='2026-11-10T10:00:00Z')
    def old_dataset(self, market='GLOBAL', audit=True):
        row = self.row(); ds = Dataset(self.root, 'data'); ds.save(market, row, 'Technology')
        if audit:
            dump(self.root/'.runtime/simple/audit/offline.json', {'source_id': 'offline', 'record_hash': digest(row),
                'source_published_at': row['时间'], 'feature_available_at': row['时间信息']['feature_available_at'],
                'date_quote': 'Published 2019-01-01', 'securities': [ENTITY]})
        return ds, row
    class NoModel:
        def industry(self, row): raise AssertionError('metadata upgrade must not call Qwen')

    def test_new_crawler_records_keep_all_three_fields_and_internal_evidence(self):
        class Sources(FakeSources):
            def fetch(self, item): return dict(super().fetch(item), text=SOURCE)
        class Model(FakeQwen):
            def summarize(self, *args):
                return dict(super().summarize(*args), securities=[ENTITY], industries=[TOPIC], impact_scope=['company', 'industry'])
        crawler = Crawler(config(), self.root, Sources(), Model()); crawler.queue(candidate())
        result = crawler.collect(resume=True)
        self.assertEqual(result['saved'], 1)
        row = next(iter(crawler.dataset.rows.values()))[1]
        self.assertEqual(set(row), FIELDS)
        self.assertEqual(row['股票实体'], [{'名称': 'Apple Inc.', '股票代码': 'AAPL', '交易所': 'NASDAQ'}])
        self.assertEqual(row['涉及行业'], [{'名称': 'semiconductor chips', '依据': 'explicit'}])
        self.assertEqual(row['影响范围'], ['company', 'industry'])
        self.assertNotIn('quote', json.dumps(row))
        audit = json.loads(next((self.root/'.runtime/simple/audit').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(audit['industries'][0]['quote'], TOPIC['quote'])

    def test_market_news_without_stocks_does_not_fabricate_entities(self):
        fields = public_analysis({'securities': [], 'industries': [], 'impact_scope': ['market']})
        self.assertEqual(fields, {'股票实体': [], '涉及行业': [], '影响范围': ['market']})

    def test_unverified_code_does_not_erase_grounded_company_name(self):
        items=grounded_securities([dict(ENTITY,ticker='NVDA')],SOURCE)
        self.assertEqual(items[0]['name'],'Apple Inc.')
        self.assertIsNone(items[0]['ticker'])

    def test_invented_quote_can_only_recover_literal_name_not_code(self):
        items=grounded_securities([dict(ENTITY,quote='Apple Inc. trades on NASDAQ')],SOURCE)
        self.assertEqual(items[0],dict(name='Apple Inc.',ticker=None,exchange=None,quote='Apple Inc.'))
        self.assertEqual(grounded_securities([dict(ENTITY,name='Imaginary Company',quote='made up')],SOURCE),[])

    def test_explicit_ticker_declarations_survive_missing_model_entities(self):
        text='示例科技（股票代码：688999，上交所）; Ticker: BRK.B; '+SOURCE
        items=retain_explicit_tickers([],text)
        self.assertEqual({item['ticker'] for item in items},{'688999','BRK.B','AAPL'})
        self.assertTrue(all(item['name'] is None for item in items))

    def test_explicit_code_fallback_does_not_parse_ordinary_exchange_prose(self):
        self.assertEqual(retain_explicit_tickers([],'NASDAQ: trading grew. Stock code: missing. There are 600000 clients.'),[])

    def test_explicit_code_attaches_only_to_its_adjacent_company(self):
        nameonly=dict(name='Apple Inc.',ticker=None,exchange=None,quote='Apple Inc.')
        result=retain_explicit_tickers([nameonly],SOURCE)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['ticker'],'AAPL')
        self.assertEqual(result[0]['exchange'],'NASDAQ')

    def test_optional_industry_downgrades_inference_and_drops_invented_support(self):
        items=grounded_industries([dict(TOPIC,name='Semiconductors'),dict(TOPIC,quote='invented evidence')],SOURCE)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['basis'],'inferred')

    def test_exchange_from_another_company_is_rejected(self):
        text='Apple Inc. (NASDAQ: AAPL) and Other Company (NYSE: XYZ).'
        with self.assertRaises(ValueError):
            validate_securities([dict(ENTITY,exchange='NYSE',quote=text)],text)

    def test_company_without_ticker_keeps_internal_name_and_empty_public_stocks(self):
        entities = validate_securities([{'name': 'Apple', 'ticker': None, 'quote': 'Apple reported results'}], 'Apple reported results')
        fields = public_analysis({'securities': entities})
        self.assertEqual(entities[0]['name'],'Apple');self.assertEqual(fields['股票实体'],[])

    def test_cross_company_pairing_and_substring_ticker_are_rejected(self):
        text = 'Apple Inc. (NASDAQ: AAPL) and NVIDIA Corp. (NASDAQ: NVDA) reported results.'
        for entity in [dict(ENTITY, ticker='NVDA', quote='NVIDIA Corp. (NASDAQ: NVDA)'),
                       dict(ENTITY, ticker='NVDA', quote=text), dict(ENTITY, ticker='A')]:
            with self.assertRaises(ValueError): validate_securities([entity], text)
        with self.assertRaises(ValueError): validate_securities([dict(ENTITY, exchange='NYSE')], SOURCE)

    def test_explicit_and_inferred_industry_require_real_support(self):
        self.assertEqual(validate_industries([TOPIC], SOURCE)[0]['basis'], 'explicit')
        inferred = dict(TOPIC, name='Semiconductors', basis='inferred')
        self.assertEqual(validate_industries([inferred], SOURCE)[0]['name'], 'Semiconductors')
        with self.assertRaises(ValueError): validate_industries([dict(inferred, basis='explicit')], SOURCE)
        with self.assertRaises(ValueError): validate_industries([dict(TOPIC, quote='invented evidence')], SOURCE)

    def test_chunk_merge_preserves_multiple_industries_and_scopes(self):
        text = SOURCE + ' Banks reported financing conditions.'
        bank = {'name': 'Banks', 'basis': 'explicit', 'quote': 'Banks reported financing conditions'}
        result = preserve_analysis({'industries': [], 'impact_scope': []},
            [{'industries': [TOPIC], 'impact_scope': ['company']}, {'industries': [bank], 'impact_scope': ['industry', 'market']}], text)
        self.assertEqual(len(result['industries']), 2)
        self.assertEqual(result['impact_scope'], ['company', 'industry', 'market'])

    def test_partial_or_invalid_public_schema_is_rejected(self):
        with self.assertRaises(ValueError): Dataset.validate('US', dict(self.row(), 股票实体=[]))
        fields = public_analysis({})
        with self.assertRaises(ValueError): validate_public_analysis(dict(fields, 影响范围=['bullish']))
        with self.assertRaises(ValueError): validate_public_analysis(dict(fields, 股票实体=[{'名称': None, '股票代码': None, '交易所': None}]))

    def test_legacy_upgrade_backs_up_and_syncs_views_without_changing_original_text(self):
        ds, original = self.old_dataset()
        raw = {p.relative_to(self.root).as_posix():p.read_bytes() for p in ds.replicas['offline'].values()}
        report = ds.migrate(self.NoModel())
        self.assertEqual(report['analysis_upgraded'], 1)
        row = ds.rows['offline'][1]
        self.assertEqual(row['主要内容'], original['主要内容']); self.assertEqual(row['时间'], original['时间'])
        self.assertEqual(row['股票实体'][0]['股票代码'], 'AAPL')
        self.assertEqual(row['涉及行业'], [{'名称': 'Technology', '依据': 'legacy_metadata'}])
        self.assertEqual(row['影响范围'], ['company'])
        self.assertGreater(row['时间信息']['feature_available_at'], original['时间信息']['feature_available_at'])
        self.assertEqual(row['时间信息']['availability_basis'], 'migration')
        for p in ds.replicas['offline'].values(): self.assertEqual(json.loads(p.read_text(encoding='utf-8')), row)
        with ZipFile(report['backup']) as backup:
            manifest = json.loads(backup.read('manifest.json'))
            for name, body in raw.items():
                self.assertEqual(backup.read(name), body); self.assertEqual(manifest[name], digest(body))
        audit = json.loads((self.root/'.runtime/simple/audit/offline.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['record_hash'], digest(row)); self.assertEqual(audit['date_quote'], 'Published 2019-01-01')
        self.assertEqual(Dataset(self.root, 'data').migrate(self.NoModel())['analysis_upgraded'], 0)

    def test_unmatched_old_audit_cannot_supply_stock_identity(self):
        ds, original = self.old_dataset()
        dump(self.root/'.runtime/simple/audit/offline.json', {'record_hash': 'wrong', 'securities': [ENTITY]})
        ds.migrate(self.NoModel()); row = ds.rows['offline'][1]
        self.assertEqual(row['股票实体'], []); self.assertEqual(row['影响范围'], [])

    def test_audit_failure_leaves_recoverable_transaction_for_all_views(self):
        ds, _ = self.old_dataset()
        from finance_agent.core import dump as real_dump
        def fail(path, value):
            if Path(path).parent == self.root/'.runtime/simple/audit': raise OSError('offline audit failure')
            return real_dump(path, value)
        with patch('record_files.dump', side_effect=fail):
            with self.assertRaises(OSError): ds.migrate(self.NoModel())
        self.assertTrue(ds.outbox.exists())
        restored = Dataset(self.root, 'data'); row = restored.rows['offline'][1]
        self.assertEqual(set(row), FIELDS); self.assertFalse(restored.outbox.exists())
        audit = json.loads((self.root/'.runtime/simple/audit/offline.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['record_hash'], digest(row))

    def test_backup_failure_does_not_modify_formal_files(self):
        ds, _ = self.old_dataset()
        before = {p:p.read_bytes() for p in ds.replicas['offline'].values()}
        with patch('record_files.zipfile.ZipFile', side_effect=OSError('offline backup failure')):
            with self.assertRaises(OSError): ds.migrate(self.NoModel())
        self.assertTrue(all(p.read_bytes() == raw for p,raw in before.items()))

    def test_schema_supports_more_than_twelve_companies(self):
        entities = [{'name': f'Example {i}', 'ticker': None, 'quote': f'Example {i}'} for i in range(15)]
        note = ContentNote.model_validate({'useful': True, 'market': 'US', 'category': 'Earnings', 'industry': 'Technology',
            'securities': entities, 'industries': [], 'impact_scope': ['company'], 'keywords': ['fixture'],
            'content': 'This is an offline test of multiple companies.', 'reason': 'fixture'})
        self.assertEqual(len(note.securities), 15)

    def test_model_schema_requires_explicit_entity_decision(self):
        self.assertIn('securities',ContentNote.model_json_schema()['required'])

    def code_keyword_note(self, content):
        from qwen_agent import Qwen, Publication
        model=Qwen(config().ollama,self.root)
        def ask(purpose,prompt,payload,schema,validate=None):
            if schema is Publication:return {'time':'2019-01-01','date_quote':'Published 2019-01-01','date_kind':'published'}
            result={'useful':True,'market':'US','category':'Earnings','industry':'Technology','securities':[],
                'industries':[],'impact_scope':['company'],'keywords':['Apple Inc.','AAPL'],'content':content,'reason':'fixture'}
            return validate(result)
        model.ask=ask
        try:return model.summarize({'url':'https://example.com/a','title':'Earnings','origin_market':'US',
            'date_context':'Published 2019-01-01','text':'Apple Inc. reported earnings and revenue for its fiscal quarter.'},config().monitoring)
        finally:model.close()

    def test_absent_code_is_removed_from_keywords(self):
        result=self.code_keyword_note('Apple Inc. reported earnings and revenue for its fiscal quarter.')
        self.assertEqual(result['keywords'],['Apple Inc.'])

    def test_absent_keyword_code_is_cleaned_without_losing_article(self):
        result=self.code_keyword_note('Apple Inc. (AAPL) reported earnings and revenue for its fiscal quarter.')
        self.assertNotIn('AAPL',result['content']);self.assertNotIn('AAPL',result['keywords'])

    def test_upgrade_only_entry_does_not_construct_network_or_model_clients(self):
        import simple_crawler
        self.old_dataset('US')
        with patch.object(simple_crawler, 'ROOT', self.root), patch.object(simple_crawler, 'load_config', return_value=config()), \
             patch.object(simple_crawler, 'Crawler', side_effect=AssertionError('must not collect')):
            self.assertEqual(simple_crawler.main(['--upgrade-records']), 0)
        self.assertEqual(set(Dataset(self.root, 'data').rows['offline'][1]), FIELDS)


if __name__ == '__main__': unittest.main()

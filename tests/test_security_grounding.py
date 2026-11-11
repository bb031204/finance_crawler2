import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from security_grounding import (is_security_code,validate_securities,grounded_securities,
    preserve_securities,validate_summary_securities,retain_explicit_tickers,SECURITY_VALIDATION_VERSION)
from analysis_metadata import public_analysis
from record_files import Dataset
from record_time import with_time_information


class SecurityGroundingTests(unittest.TestCase):
    def note(self,content='Apple Inc. reported quarterly earnings.',entities=None,keywords=None):
        return {'content':content,'securities':entities or [],'keywords':keywords or ['earnings']}

    def test_company_name_is_not_a_stock_code(self):
        for value in ['Zijin Mining Group Co.','Serbia Zijin Copper','600000 clients','../AAPL']:
            self.assertFalse(is_security_code(value))
        for value in ['A','AAPL','BRK.B','BRK-B','600000','600000.SH','0005.HK']:
            self.assertTrue(is_security_code(value))

    def test_unbracketed_cross_company_pair_is_cleared(self):
        text='Apple Inc. reported earnings. NVIDIA Corp. (NASDAQ: NVDA) announced new chips.'
        entity=dict(name='Apple Inc.',ticker='NVDA',exchange='NASDAQ',quote=text)
        with self.assertRaises(ValueError):validate_securities([entity],text)
        result=grounded_securities([entity],text)
        self.assertEqual(result,[dict(name='Apple Inc.',ticker=None,exchange=None,quote='Apple Inc.')])

    def test_short_names_do_not_match_inside_another_word(self):
        self.assertEqual(grounded_securities([dict(name='AI',ticker=None,quote='retail demand')],'retail demand'),[])

    def test_pair_in_a_local_declaration_is_retained(self):
        for name,ticker,text in [('Apple Inc.','AAPL','Apple Inc. (NASDAQ: AAPL) reported results.'),
            ('浦发银行','600000','证券代码：600000 证券简称：浦发银行 公告编号：2026-001'),
            ('浦发银行','600000','证券简称：浦发银行\n证券代码：600000'),
            ('Apple Inc.','AAPL','Apple Inc. | AAPL | NASDAQ')]:
            with self.subTest(text=text):
                self.assertEqual(validate_securities([dict(name=name,ticker=ticker,exchange=None,quote=text)],text)[0]['ticker'],ticker)

    def test_table_with_multiple_companies_is_not_a_verified_pair(self):
        text='Apple Inc. | AAPL | NVIDIA Corp. | NVDA'
        with self.assertRaises(ValueError):validate_securities([dict(name='Apple Inc.',ticker='NVDA',quote=text)],text)

    def test_exchange_from_another_company_cannot_be_attached(self):
        text='Apple Inc. (NASDAQ: AAPL); Other Company (NYSE: XYZ).'
        result=grounded_securities([dict(name='Apple Inc.',ticker='AAPL',exchange='NYSE',quote=text)],text)
        self.assertIsNone(result[0]['exchange'])
        self.assertEqual(result[0]['ticker'],'AAPL')

    def test_legitimate_price_and_listing_declarations_are_retained(self):
        examples=[('Amazon','AMZN','Amazon (AMZN +0.62%) Echo'),
            ('Samsung','SSNLF','Samsung (NASDAQOTH: SSNLF) Family Hub refrigerator'),
            ('WestRock Company','WRK','WestRock Company (WestRock) (NYSE:WRK)'),
            ('Phillips 66','PSX','Phillips 66 has received approval for the listing of its common stock on the New York Stock Exchange under the symbol PSX.'),
            ('The Progressive Corporation','PGR','The Common Shares of The Progressive Corporation, the Mayfield Village, Ohio-based holding company, trade publicly at NYSE:PGR.')]
        for name,ticker,text in examples:
            with self.subTest(text=text):
                self.assertEqual(validate_securities([dict(name=name,ticker=ticker,exchange=None,quote=text)],text)[0]['ticker'],ticker)

    def test_parent_company_code_is_not_automatically_assigned_to_subsidiary(self):
        text='Google-parent Alphabet (GOOG -0.52%)(GOOGL -0.39%)'
        result=grounded_securities([dict(name='Google',ticker='GOOG',exchange=None,quote=text)],text)
        self.assertIsNone(result[0]['ticker'])

    def test_raw_cooccurrence_is_not_a_code_declaration(self):
        with self.assertRaises(ValueError):validate_securities([dict(name=None,ticker='EPS',quote='EPS rose 20 percent')],'EPS rose 20 percent')

    def test_unselected_related_code_does_not_reenter_summary(self):
        note=self.note('Example Bank reported net interest income.',keywords=['Example Bank'])
        source=note['content']+' Related item: Example Motors (NASDAQ: TSLA) advertised a car.'
        result=preserve_securities(note,[dict(note)],source,'en')
        self.assertEqual(result['securities'],[])
        self.assertNotIn('TSLA',result['content']);self.assertNotIn('TSLA',result['keywords'])

    def test_explicit_code_of_selected_company_can_be_recovered(self):
        source='示例科技（股票代码：688999，上交所）公布季度收入5亿元。'
        note=self.note('示例科技公布季度收入5亿元。',keywords=['示例科技'])
        result=preserve_securities(note,[dict(note)],source,'zh')
        self.assertEqual(result['securities'][0]['ticker'],'688999')
        self.assertEqual(result['securities'][0]['name'],'示例科技')

    def test_selected_company_header_code_is_recovered(self):
        source='证券代码：600000 证券简称：浦发银行 公告编号：2026-001\n浦发银行公布季度收入。'
        note=self.note('浦发银行公布季度收入。',keywords=['浦发银行'])
        result=preserve_securities(note,[dict(note)],source,'zh')
        self.assertEqual(result['securities'][0]['ticker'],'600000')
        self.assertEqual(result['securities'][0]['name'],'浦发银行')

    def test_parenthetical_other_company_is_not_an_alias(self):
        text='Apple Inc. (NVIDIA Corp.) (NASDAQ: NVDA) reported results.'
        with self.assertRaises(ValueError):validate_securities([dict(name='Apple Inc.',ticker='NVDA',quote=text)],text)

    def test_selected_code_is_preserved_without_inventing_name(self):
        source='Stock code: BRK.B. Reported quarterly earnings.'
        note=self.note('BRK.B reported quarterly earnings.',keywords=['BRK.B'])
        result=preserve_securities(note,[dict(note)],source,'en')
        self.assertEqual(result['securities'][0]['ticker'],'BRK.B')
        self.assertIsNone(result['securities'][0]['name'])

    def test_same_name_without_code_is_not_duplicated_after_recovery(self):
        source='Apple Inc. (NASDAQ: AAPL) reported quarterly earnings.'
        nameonly=dict(name='Apple Inc.',ticker=None,exchange=None,quote='Apple Inc.')
        full=dict(name='Apple Inc.',ticker='AAPL',exchange='NASDAQ',quote='Apple Inc. (NASDAQ: AAPL)')
        note=self.note(entities=[nameonly])
        result=preserve_securities(note,[self.note(entities=[full])],source,'en')
        self.assertEqual(len(result['securities']),1)
        self.assertEqual(result['securities'][0]['ticker'],'AAPL')

    def test_code_only_in_summary_is_cleaned_without_rejecting_article(self):
        source='Apple Inc. reported quarterly earnings.'
        for content in ['Apple Inc. (AAPL) reported quarterly earnings.','Apple Inc. shares trade under NASDAQ: AAPL.']:
            with self.subTest(content=content):
                result=validate_summary_securities(self.note(content),source)
                self.assertNotIn('AAPL',result['content']);self.assertTrue(result['security_cleanup'])

    def test_summary_cross_company_pair_is_cleaned_even_when_both_codes_exist(self):
        source='Apple Inc. (NASDAQ: AAPL) and NVIDIA Corp. (NASDAQ: NVDA) reported results.'
        note=self.note('Apple Inc. (NVDA) reported results.',entities=[dict(name='Apple Inc.',ticker=None)])
        result=validate_summary_securities(note,source)
        self.assertNotIn('NVDA',result['content']);self.assertIn('Apple Inc.',result['content'])

    def test_common_financial_abbreviations_are_not_guessed_tickers(self):
        note=self.note('Revenue rose while EPS and EBITDA improved under GAAP.',keywords=['EPS','earnings'])
        self.assertEqual(validate_summary_securities(note,'Revenue and profit increased.'),note)

    def test_new_writes_reject_invalid_tickers_but_old_files_remain_readable_for_repair(self):
        folder=Path(__file__).resolve().parents[1]/'.runtime/test_security_grounding';folder.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            root=Path(temp)
            row=with_time_information(dict(source_id='fixture',url='https://example.com/offline',时间='2019-01-01',分类='Earnings',
                关键词=['fixture'],主要内容='This is a synthetic offline fixture.',**public_analysis({})))
            row['股票实体']=[{'名称':'Example Company','股票代码':'Example Company','交易所':None}]
            path=root/'data/US/2019_01_01_Earnings_Technology_example.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps(row),encoding='utf-8')
            dataset=Dataset(root,'data')
            with self.assertRaises(ValueError):dataset.save('US',row,'Technology')
            self.assertEqual(len(dataset.rows),1)


if __name__=='__main__':unittest.main()

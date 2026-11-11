import unittest
from summary_quality import validate_summary_quality,trim_routine_sentences
from qwen_agent import normalize_query
from test_simple_crawler import config
from test_preflight_pipeline import OfflineQwen,PageSources
from pathlib import Path
from tempfile import TemporaryDirectory


class SummaryQualityTests(unittest.TestCase):
    def test_empty_evaluation_requests_repair_without_mutating_facts(self):
        text='Revenue rose 15% in 2025. This report provides valuable insights for investors.'
        with self.assertRaises(ValueError):validate_summary_quality(text,'en')
        self.assertIn('Revenue rose 15% in 2025',text)

    def test_marketing_copy_is_not_a_financial_fact(self):
        with self.assertRaises(ValueError):validate_summary_quality('The trailblazing company reported sales growth.','en')
        with self.assertRaises(ValueError):validate_summary_quality('本次披露提高透明度，具有重要参考价值。','zh')

    def test_real_disclosure_requirements_and_legal_conditions_are_kept(self):
        validate_summary_quality('The new rule requires banks to disclose loan exposures for greater transparency, effective in 2027.','en')
        validate_summary_quality('公司拟每10股派2元，尚需股东大会批准；如未达到债务契约要求则暂停派息。','zh')

    def test_attributed_forecast_with_conditions_is_kept(self):
        validate_summary_quality('Analyst Dana Li expects orders to recover, but warned that weaker demand could delay the recovery.','en')

    def test_routine_unanimous_votes_require_repair(self):
        with self.assertRaises(ValueError):
            validate_summary_quality('公司拟分红，尚需股东大会批准。董事会同意13票，反对0票，弃权0票。','zh')

    def test_contested_decision_and_signed_financial_contract_are_kept(self):
        validate_summary_quality('并购方案未通过，董事会同意6票、反对7票、弃权0票。','zh')
        validate_summary_quality('公司与客户签署2亿元采购合同，付款条件为验收后30天内支付。','zh')

    def test_overlong_summary_requests_repair_instead_of_truncation(self):
        text=' '.join(['Operating']*201)
        with self.assertRaises(ValueError):validate_summary_quality(text,'en')
        self.assertEqual(len(text.split()),201)
        with self.assertRaises(ValueError):validate_summary_quality('营收增长'*100,'zh')

    def test_one_repair_reports_both_noise_and_length(self):
        with self.assertRaises(ValueError) as error:
            validate_summary_quality('A trailblazing company. '+' '.join(['Operating']*201),'en')
        self.assertIn('trailblazing',str(error.exception));self.assertIn('偏长',str(error.exception))

    def test_missing_generated_calendar_dates_leave_no_fragments(self):
        monitor=config().monitoring
        self.assertEqual(normalize_query('在2012-01-01至2022-05-25期间 中国银行业财报',monitor),'中国银行业财报')
        self.assertEqual(normalize_query('bank earnings from 2012-01-01 to 2022-05-25',monitor),'bank earnings')

    def test_explicit_user_date_is_preserved_without_half_date(self):
        monitor=config().monitoring;monitor.include_keywords=['2020-01-01']
        value=normalize_query('2020-01-01至2022-05-25 银行业事件',monitor)
        self.assertIn('2020-01-01',value);self.assertNotIn('-05-25',value)

    def test_single_parser_bound_date_is_reused_without_model(self):
        root=Path(__file__).resolve().parents[1]/'.runtime/test_summary_quality';root.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=root) as tmp:
            model=OfflineQwen(config().ollama,Path(tmp));page=PageSources().fetch({'url':'https://example.com/main','title':'Example'})
            page.update(date_context='datePublished=2026-11-02',article_binding_version=3,cleaning='lxml')
            self.assertEqual(model.publication(page)['time'],'2026-11-02');self.assertEqual(model.calls,0)
            page['date_context']='dateModified=2026-11-02';model.day=None
            self.assertIsNone(model.publication(page)['time']);self.assertEqual(model.calls,1)

    def test_shortcut_does_not_invent_timestamp_precision_or_trust_old_cache(self):
        root=Path(__file__).resolve().parents[1]/'.runtime/test_summary_quality';root.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=root) as tmp:
            model=OfflineQwen(config().ollama,Path(tmp));page=PageSources().fetch({'url':'https://example.com/main','title':'Example'})
            page.update(date_context='datePublished=2026-11-02',article_binding_version=2)
            model.quote=page['date_context'];model.publication(page);self.assertEqual(model.calls,1)
            page.update(date_context='datePublished=2026-11-02\ndatePublished=2026-11-03',article_binding_version=3)
            model.quote='datePublished=2026-11-02';model.publication(page);self.assertEqual(model.calls,2)


    def test_trim_only_known_pure_procedural_sentences(self):
        facts='公司拟每10股派2元，尚需股东大会批准。'
        text=facts+'董事会表决同意13票，反对0票，弃权0票。董事长签署本公告，公司保证不存在虚假记载和重大遗漏。'
        result,tags=trim_routine_sentences(text)
        self.assertEqual(result,facts);self.assertEqual(tags,['unanimous_votes','signature'])

    def test_trim_preserves_mixed_facts_nonzero_opposition_and_contracts(self):
        for text in ['公司拟每10股派2元，董事会同意13票、反对0票、弃权0票。',
                '并购方案未通过，董事会同意6票、反对7票、弃权0票。',
                '董事长签署2亿元采购合同，付款条件为验收后30天内支付。']:
            self.assertEqual(trim_routine_sentences(text),(text,[]))


if __name__=='__main__':unittest.main()

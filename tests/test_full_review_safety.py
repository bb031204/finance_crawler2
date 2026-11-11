import tempfile
import unittest
from pathlib import Path
from financial_noise import trim_financial_noise,only_filing_wrapper
from summary_maintenance import clean_saved_summaries
import test_financial_noise as fixtures
from test_preflight_pipeline import OfflineQwen,content,PageSources
from test_simple_crawler import config
from qwen_agent import ContentNote


class FullReviewSafetyTests(unittest.TestCase):
    def test_financial_event_in_attachment_sentence_is_not_deleted(self):
        for text in [
            'The report attached as Exhibit 99.1 says the exchange suspended trading in its securities.',
            'The report attached as Exhibit 99.1 confirms the auditor was dismissed.',
            'The information incorporated by reference in the filing states a trading suspension is in effect.',
            'Acme, based in Germany, has 650 employees in 80 countries and faces an export ban.'
        ]:
            self.assertEqual(trim_financial_noise(text),(text,[]))

    def test_wrapper_keyword_match_does_not_quarantine_a_material_event(self):
        helper=fixtures.FinancialNoiseTests();helper.setUp()
        try:
            text='Acme Inc. announced financial results and suspended trading in its shares.'
            self.assertTrue(only_filing_wrapper(text))  # A suspicion, not a deletion decision.
            dataset,row=helper.dataset(text)
            report=clean_saved_summaries(dataset)
            self.assertEqual(report['quarantined_records'],0);self.assertEqual(report['cleaned_records'],0)
            self.assertIn(row['source_id'],report['needs_review']);self.assertEqual(dataset.rows[row['source_id']][1],row)
            self.assertIsNone(report['backup'])
        finally:helper.tearDown()

    def test_model_selected_financial_event_is_not_overridden_by_regex(self):
        text='Acme Inc. announced financial results and suspended trading in its shares.'
        class MaterialModel(OfflineQwen):
            def ask(self,purpose,prompt,payload,schema,validate=None,partial_parser=None,max_predict=None):
                if schema is ContentNote:
                    self.calls+=1;value=content(text)
                    return validate(value) if validate else value
                return super().ask(purpose,prompt,payload,schema,validate,partial_parser,max_predict)
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_full_review_safety';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            cfg=config();model=MaterialModel(cfg.ollama,Path(tmp))
            page=PageSources(body=text).fetch({'url':'https://example.com/main','title':'Trading suspension'})
            note=model.summarize(page,cfg.monitoring)
            self.assertTrue(note['useful']);self.assertIn('suspended trading',note['content'])

    def test_only_confirmed_empty_noise_can_be_quarantined(self):
        helper=fixtures.FinancialNoiseTests();helper.setUp()
        try:
            text='The information in this report is not deemed filed for purposes of Section 18 of the Securities Exchange Act of 1934.'
            dataset,row=helper.dataset(text);report=clean_saved_summaries(dataset)
            self.assertEqual(report['quarantined_records'],1);self.assertNotIn(row['source_id'],dataset.rows)
            self.assertTrue(report['backup'])
        finally:helper.tearDown()


if __name__=='__main__':unittest.main()

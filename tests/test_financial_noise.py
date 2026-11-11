import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from finance_agent.core import digest,dump
from financial_noise import trim_financial_noise,only_filing_wrapper
from summary_maintenance import clean_saved_summaries,load_summary_snapshot,plan_summary_cleanup
from record_files import Dataset,FIELDS,LEGACY_ANALYSIS_FIELDS
from record_time import with_time_information
from analysis_metadata import public_analysis

LEGAL='The information in this report is not deemed filed for purposes of Section 18 of the Securities Exchange Act of 1934.'


class FinancialNoiseTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_financial_noise';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def dataset(self,text,market='GLOBAL'):
        row=dict(source_id='noise_fixture',url='https://www.sec.gov/example.htm',时间='2019-01-29',分类='Financial Report',
            关键词=['Example Company'],主要内容=text,**public_analysis({}))
        row=with_time_information(row,available_at='2026-11-10T01:00:00+00:00')
        ds=Dataset(self.root,'data');audit={'source_id':row['source_id'],'record_hash':digest(row),'date_quote':'Published 2019-01-29'}
        ds.save(market,row,'Technology',audit_update=audit)
        return ds,row

    def test_preliminary_cash_and_audit_uncertainty_survive(self):
        text='Novavax, Inc. expects approximately $231 million in cash as of December 31, 2015. This amount is preliminary and unaudited, and may change after the audit.'
        cleaned,tags=trim_financial_noise(text+' '+LEGAL)
        self.assertEqual(cleaned,text);self.assertIn('filing_legal_boilerplate',tags)

    def test_real_legal_penalty_and_covenant_are_not_boilerplate(self):
        for text in ['The information describes a $18 million penalty under Section 18 of the Securities Exchange Act.',
                'The company faces a lawsuit and a possible restatement of revenue.',
                'Dividend payments are prohibited if the debt covenant is breached.']:
            self.assertEqual(trim_financial_noise(text),(text,[]))

    def test_transparency_can_be_an_attributed_supply_chain_policy(self):
        text='TSMC increased output by 60%. U.S. Commerce Secretary Gina Raimondo called for greater market transparency to address the chip shortage.'
        self.assertEqual(trim_financial_noise(text),(text,[]))

    def test_static_profile_is_left_to_semantic_review_not_keyword_deletion(self):
        text='Acme, based in Germany, has around 650 employees and operates in over 80 countries.'
        self.assertEqual(trim_financial_noise(text),(text,[]))
        changed='Acme, based in Germany, cut its workforce from 650 employees and closed operations in 80 countries.'
        self.assertEqual(trim_financial_noise(changed),(changed,[]))
        risk='Acme, based in Germany, has 650 employees in 80 countries and faces tariff exposure.'
        self.assertEqual(trim_financial_noise(risk),(risk,[]))

    def test_generic_tail_is_removed_without_losing_forecast_uncertainty(self):
        text='The report is based on analyst estimates rather than confirmed future orders and provides valuable insights for investors.'
        cleaned,tags=trim_financial_noise(text)
        self.assertEqual(cleaned,'The report is based on analyst estimates rather than confirmed future orders.')
        self.assertIn('trailing_empty_evaluation',tags)

    def test_filing_cover_is_not_substantive_results(self):
        cover='Cypress Semiconductor Corporation issued a press release regarding its financial results for the fourth quarter and full year 2019.'
        self.assertTrue(only_filing_wrapper(cover))
        meaningful=cover+' Revenue increased by 15%.'
        self.assertFalse(only_filing_wrapper(meaningful))
        self.assertFalse(only_filing_wrapper('Acme Inc. announced financial results showing deteriorating margins and weaker demand.'))

    def test_filing_with_qualitative_material_events_is_never_cover_only(self):
        for text in ['Acme Inc. announced financial results and a strategic review.',
                'Acme Inc. issued a press release about financial results and an acquisition.',
                'Acme Inc. filed its annual report after a delayed filing and a deadline extension.']:
            self.assertFalse(only_filing_wrapper(text))

    def test_actual_contract_and_appointment_are_kept(self):
        for text in ['The document was signed by the newly appointed chief financial officer after the former CFO resigned.',
                'The report includes a $20 million supply contract attached as Exhibit 99.1.']:
            self.assertEqual(trim_financial_noise(text),(text,[]))

    def test_initials_in_signature_do_not_leave_name_fragments(self):
        facts='The bank changed its auditor.'
        result,_=trim_financial_noise(facts+' The report is signed by Janice R. Fukakusa, Chief Financial Officer.')
        # The supported signature form may be retained, but it must never be partially deleted.
        self.assertNotIn(' Fukakusa,',result.replace('Janice R. Fukakusa,',''))
        result,_=trim_financial_noise(facts+' The report was signed by Janice R. Fukakusa, Chief Financial Officer.')
        self.assertEqual(result,facts)

    def test_mixed_basis_points_and_qualitative_demand_are_not_deleted(self):
        for text in ['The report includes a 25 basis point increase in interest attached as Exhibit 99.1.',
                'The report includes weaker demand and declining margins in the presentation attached as Exhibit 99.1.',
                'The report includes the change of auditor attached as Exhibit 99.1.']:
            self.assertEqual(trim_financial_noise(text),(text,[]))

    def test_signature_attachment_and_visit_promotions_are_removed(self):
        text='The press release is included as Exhibit 99.1 to this Current Report on Form 8-K. The document was signed by the Chief Financial Officer. For more information, visit our website.'
        cleaned,tags=trim_financial_noise(text)
        self.assertEqual(cleaned,'');self.assertEqual(len(tags),3)

    def test_existing_global_cleanup_is_backed_up_and_updates_all_audits(self):
        facts='Example Company reported approximately $231 million in cash; the figure is preliminary and unaudited.'
        ds,original=self.dataset(facts+' '+LEGAL);paths=list(ds.replicas[original['source_id']].values())
        report=clean_saved_summaries(ds);row=ds.rows[original['source_id']][1]
        self.assertEqual(report['cleaned_records'],1);self.assertEqual(report['cleaned_files'],3)
        self.assertEqual(row['主要内容'],facts);self.assertEqual(set(row),LEGACY_ANALYSIS_FIELDS);self.assertEqual(row['时间'],original['时间'])
        self.assertGreater(row['时间信息']['feature_available_at'],original['时间信息']['feature_available_at'])
        self.assertEqual(row['时间信息']['availability_basis'],'migration')
        for path in paths:self.assertEqual(json.loads(path.read_text(encoding='utf-8')),row)
        audit=json.loads((self.root/'.runtime/simple/audit/noise_fixture.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['record_hash'],digest(row));self.assertEqual(audit['date_quote'],'Published 2019-01-29')
        with ZipFile(report['backup']) as archive:
            self.assertEqual(json.loads(archive.read(str(paths[0].relative_to(self.root)).replace('\\','/'))),original)
        self.assertEqual(clean_saved_summaries(Dataset(self.root,'data'))['cleaned_records'],0)

    def test_suspected_cover_is_retained_for_review_without_deleting_raw_cache(self):
        text='Example Inc. issued a press release regarding its financial results for the fourth quarter 2019. '+LEGAL
        ds,row=self.dataset(text);raw=self.root/'.runtime/simple/pages/noise_fixture.json';dump(raw,{'preserve':'original'})
        report=clean_saved_summaries(ds)
        self.assertEqual(report['quarantined_records'],0);self.assertIn(row['source_id'],ds.rows)
        self.assertIn(row['source_id'],report['needs_review'])
        self.assertTrue(raw.exists());self.assertTrue(report['backup'])
        self.assertFalse(list((self.root/'.runtime/simple/quarantine').glob('*.json')))

    def test_backup_failure_preserves_original_files(self):
        ds,row=self.dataset('Example Company has $231 million in cash. '+LEGAL)
        with patch('summary_maintenance.zipfile.ZipFile',side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):clean_saved_summaries(ds)
        for path in ds.replicas[row['source_id']].values():self.assertEqual(json.loads(path.read_text(encoding='utf-8')),row)

    def test_failed_audit_commit_recovers_without_model(self):
        ds,row=self.dataset('Example Company has $231 million in cash. '+LEGAL)
        original_dump=dump
        def fail(path,value):
            if Path(path).parent==self.root/'.runtime/simple/audit':raise OSError('audit write interrupted')
            return original_dump(path,value)
        with patch('record_files.dump',side_effect=fail):
            with self.assertRaises(OSError):clean_saved_summaries(ds)
        self.assertTrue(ds.outbox.exists())
        recovered=Dataset(self.root,'data');self.assertNotIn('Section 18',recovered.rows[row['source_id']][1]['主要内容'])
        self.assertFalse(recovered.outbox.exists());self.assertEqual(clean_saved_summaries(recovered)['cleaned_records'],0)

    def test_unmatched_audit_is_not_silently_rewritten(self):
        ds,row=self.dataset('Example Company has $231 million in cash. '+LEGAL)
        dump(self.root/'.runtime/simple/audit/noise_fixture.json',{'record_hash':'wrong'})
        report=clean_saved_summaries(ds);self.assertEqual(report['unverified_skipped'],['noise_fixture'])
        self.assertEqual(ds.rows[row['source_id']][1],row);self.assertIsNone(report['backup'])

    def test_preview_loader_does_not_recover_or_write_outbox(self):
        ds,row=self.dataset('Example Company has $231 million in cash. '+LEGAL)
        dump(ds.outbox,{'preview_must_not_recover':True});before=ds.outbox.read_bytes()
        snapshot=load_summary_snapshot(self.root);operations,_=plan_summary_cleanup(snapshot)
        self.assertEqual(len(operations),1);self.assertEqual(ds.outbox.read_bytes(),before)


if __name__=='__main__':unittest.main()

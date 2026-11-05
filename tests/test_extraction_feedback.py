"""Synthetic fixtures for exact-field feedback and partial-success exit status."""
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from finance_agent.cli import main
from finance_agent.extraction import grounded
from finance_agent.pipeline import Engine
from test_agent import Base, FakeLLM, FakeProvider, event


class ExtractionFeedbackTests(Base):
    def test_relative_time_is_preserved_and_invented_year_rejected(self):
        item = event()
        item['event_time_raw'] = '11月1日'
        evidence = {1: {'text': '甲公司（600001）11月1日披露拟投资20亿元。'}}
        self.assertEqual(grounded({'events': [item]}, evidence)['events'][0]['event_time_raw'], '11月1日')
        item['event_time_raw'] = '2026-11-01'
        with self.assertRaises(ValueError) as caught:
            grounded({'events': [item]}, evidence)
        self.assertIn('events[0].event_time_raw', str(caught.exception))
        self.assertIn('2026-11-01', str(caught.exception))
        self.assertIn('填null', str(caught.exception))

    def test_subject_feedback_identifies_exact_event_without_accepting_paraphrase(self):
        first = event()
        second = event()
        second['subject'] = '多家银行'
        evidence = {1: {'text': '甲公司（600001）披露拟投资20亿元。招聘的银行不在少数。'}}
        with self.assertRaises(ValueError) as caught:
            grounded({'events': [first, second]}, evidence)
        self.assertIn('events[1].subject', str(caught.exception))
        self.assertIn('多家银行', str(caught.exception))
        self.assertIn('逐字证据', str(caught.exception))

    def engine(self):
        return Engine(self.cfg, self.root, repo=self.repo(), llm=FakeLLM(), provider=FakeProvider())

    def test_task_error_with_successful_work_is_partial(self):
        engine = self.engine()
        def process(report):
            report['documents_fetched'] = 1
            report['events_generated'] = 2
            report['errors'].append({'candidate_id': 'synthetic-failed-item', 'status': 'extraction_failed'})
        try:
            with patch.object(engine, 'process', side_effect=process):
                report = engine.cycle(discover=False)
            self.assertEqual(report['status'], 'partial')
            self.assertEqual(report['events_generated'], 2)
            self.assertEqual(len(report['errors']), 1)
        finally:
            engine.close()

    def test_top_level_error_remains_failed_even_with_other_progress(self):
        engine = self.engine()
        def process(report):
            report['documents_fetched'] = 1
            raise ValueError('synthetic fatal error')
        try:
            with patch.object(engine, 'process', side_effect=process):
                report = engine.cycle(discover=False)
            self.assertEqual(report['status'], 'failed')
        finally:
            engine.close()

    def test_only_task_failures_with_no_progress_remain_failed(self):
        engine = self.engine()
        def process(report):
            report['errors'].append({'candidate_id': 'synthetic-failed-item', 'status': 'network_error'})
        try:
            with patch.object(engine, 'process', side_effect=process):
                report = engine.cycle(discover=False)
            self.assertEqual(report['status'], 'failed')
        finally:
            engine.close()

    def test_cli_exit_code_follows_partial_failed_and_complete_status(self):
        for status, exit_code in [('partial', 2), ('failed', 1), ('completed', 0)]:
            with self.subTest(status=status), patch('finance_agent.cli.ROOT', self.root), \
                 patch('finance_agent.cli.load', return_value=self.cfg), \
                 patch('finance_agent.cli.Engine') as engine, redirect_stdout(io.StringIO()):
                engine.return_value.cycle.return_value = {'status': status, 'errors': [] if status == 'completed' else [{'candidate_id': 'synthetic'}]}
                self.assertEqual(main(['--extract-only']), exit_code)

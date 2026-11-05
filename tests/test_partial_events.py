"""Synthetic mixed-validity model output; no real inference or production records."""
import copy
import json
import httpx
from finance_agent.extraction import Extractor
from finance_agent.llm import EvidenceValidationError, OllamaClient
from test_agent import Base, event


class PartialEventTests(Base):
    def client(self, events=None, malformed=False):
        def handler(request):
            if request.url.path.endswith('/tags'):
                return httpx.Response(200, json={'models': [{'name': 'qwen3:8b', 'digest': 'fixture_partial_event_model'}]})
            content = 'not json' if malformed else json.dumps({'events': events}, ensure_ascii=False)
            return httpx.Response(200, json={'message': {'content': content}, 'done_reason': 'stop'})
        return OllamaClient(self.cfg.ollama, self.root,
                            httpx.Client(base_url='http://fixture-model', transport=httpx.MockTransport(handler)))

    def test_mixed_events_keep_only_proven_event_and_review_survives_cache(self):
        good = event()
        bad = copy.deepcopy(good)
        bad['subject'] = '没有证据的乙公司'
        repo = self.repo()
        doc = self.document(repo)
        llm = self.client([good, bad])
        try:
            result = Extractor(self.cfg, llm, repo, self.root).process(doc)
            self.assertEqual(llm.calls, 2)
            self.assertEqual(len(result['events']), 1)
            self.assertEqual(result['status'], 'needs_review')
            self.assertEqual(len(result['rejected_events']), 1)
            self.assertEqual(repo.candidate(doc['candidate_id'])['status'], 'needs_review')
            self.assertEqual(repo.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
            self.assertEqual(result['rejected_events'][0]['candidate']['subject'], bad['subject'])
            cached = Extractor(self.cfg, llm, repo, self.root).process(doc)
            self.assertEqual(llm.calls, 2)
            self.assertEqual(cached['status'], 'needs_review')
            self.assertEqual(cached['chunks_cached'], cached['total_chunks'])
            self.assertEqual(cached['chunks_processed'], 0)
            self.assertEqual(len(cached['rejected_events']), 1)
        finally:
            llm.close()
            repo.close()

    def test_all_invalid_events_stay_in_review_and_never_enter_event_table(self):
        bad = event()
        bad['event_time_raw'] = '2099-01-01'
        repo = self.repo()
        doc = self.document(repo)
        llm = self.client([bad])
        try:
            result = Extractor(self.cfg, llm, repo, self.root).process(doc)
            self.assertEqual(result['events'], [])
            self.assertEqual(result['status'], 'needs_review')
            self.assertEqual(repo.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
            self.assertEqual(repo.candidate(doc['candidate_id'])['status'], 'needs_review')
        finally:
            llm.close()
            repo.close()

    def test_broken_json_cannot_be_salvaged_as_valid_events(self):
        repo = self.repo()
        doc = self.document(repo)
        llm = self.client(malformed=True)
        try:
            with self.assertRaises(ValueError) as caught:
                Extractor(self.cfg, llm, repo, self.root).process(doc)
            self.assertNotIsInstance(caught.exception, EvidenceValidationError)
            self.assertEqual(llm.calls, 2)
            self.assertEqual(repo.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
            self.assertEqual(repo.candidate(doc['candidate_id'])['status'], 'fetched')
        finally:
            llm.close()
            repo.close()

    def test_review_status_and_events_roll_back_together_on_insert_error(self):
        repo = self.repo()
        doc = self.document(repo)
        invalid = {'event_id': 'E_synthetic_rollback', 'time': {'available_at': '2026-11-05T00:00:00+00:00'},
                   'market': 'CN', 'event_type': 'investment', 'securities': [{}]}
        try:
            with self.assertRaises(KeyError):
                repo.save_events(doc, [invalid], status='needs_review')
            self.assertEqual(repo.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
            self.assertEqual(repo.candidate(doc['candidate_id'])['status'], 'fetched')
        finally:
            repo.close()

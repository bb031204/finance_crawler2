import json
import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pydantic import SecretStr
from test_simple_crawler import FakeQwen, config
from simple_crawler import Config, Crawler, print_history_preview
from sec_discovery import quarter_jobs, parse_master, parse_submissions, filing_identity, filing_url, child_jobs
from web_sources import Sources
from finance_agent.safety import FetchError
from finance_agent.core import dump
from qwen_agent import Qwen, Publication, OutputError


def filing(n, day='2019-01-10', form='8-K', cik='0000320193'):
    return {'cik': cik, 'accession': f'0000320193-19-{n:06}', 'company': 'Example Company',
            'form': form, 'filing_date': day, 'primary_document': 'report.htm'}


def master(rows):
    return ('SEC index\nCIK|Company Name|Form Type|Date Filed|Filename\n-----\n' + '\n'.join(
        f"{int(r['cik'])}|{r['company']}|{r['form']}|{r['filing_date']}|edgar/data/{int(r['cik'])}/{r['accession']}.txt" for r in rows)).encode()


def columns(rows):
    return {key: [r[field] for r in rows] for key, field in [('accessionNumber', 'accession'),
        ('filingDate', 'filing_date'), ('form', 'form'), ('primaryDocument', 'primary_document')]}


def sec_config():
    return Config.model_validate({'monitoring': {'markets': ['US'], 'directions': 'all'},
        'collection': {'start_date': '2019-01-01', 'end_date': '2019-01-31', 'max_documents_per_cycle': 30},
        'search': {'discovery_mode': 'direct', 'market_domains': {'US': ['sec.gov']},
            'sec_edgar': {'user_agent': 'OfflineTests test@example.com', 'page_size': 2,
                'max_pages_per_round': 1, 'max_requests_per_round': 1}}})


class SecSources:
    def __init__(self, responses): self.responses = responses; self.requests = []; self.searches = 0; self.fetches = []
    def domains(self, market): return ['sec.gov']
    def allowed(self, url, market): return market == 'US' and 'sec.gov/' in url
    def sec_get(self, url):
        self.requests.append(url)
        value = self.responses[url]
        if isinstance(value, Exception): raise value
        return value if isinstance(value, tuple) else (value, {})
    def fetch(self, candidate):
        self.fetches.append(candidate['url'])
        return {'url': candidate['url'], 'title': candidate['title'], 'origin_market': 'US',
                'text': 'Example company reported an acquisition and operating results. ' * 20,
                'date_context': 'datePublished=' + candidate.get('sec_filing', filing(1))['filing_date'],
                'sec_filing_date': candidate.get('sec_filing', filing(1))['filing_date']}
    def search(self, *args, **kwargs): self.searches += 1; return []
    def close(self): pass


class SecModel(FakeQwen):
    def plan(self, *args):
        self.calls += 1
        return [{'market': 'US', 'query': 'company results'}]
    def summarize(self, page, monitoring):
        return dict(super().summarize(page, monitoring), market='US', time=page['sec_filing_date'])


class SecTestBase(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1]/'.runtime/test_sec'
        folder.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=folder); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def crawler(self, rows=None, cfg=None, responses=None):
        cfg = cfg or sec_config()
        responses = responses or {quarter_jobs(*[cfg.collection.start_date, cfg.collection.end_date])[0]['url']: master(rows or [])}
        return Crawler(cfg, self.root, SecSources(responses), SecModel())


class SecDiscoveryTests(SecTestBase):
    def test_config_normalizes_ids_and_rejects_unsupported_or_unsafe_settings(self):
        base = sec_config().model_dump()
        base['search']['sec_edgar']['user_agent'] = 'OfflineTests test@example.com'
        base['search']['sec_edgar'].update(tickers=['aapl', 'AAPL'], ciks=[320193, '0000320193'])
        cfg = Config.model_validate(base)
        self.assertEqual(cfg.search.sec_edgar.tickers, ['AAPL'])
        self.assertEqual(cfg.search.sec_edgar.ciks, ['0000320193'])
        for key, value in [('user_agent', 'invalid'), ('user_agent', 'Tests test@example.com\r\nInjected: true'),
                           ('page_size', 0), ('forms', []), ('ciks', ['../bad'])]:
            invalid = json.loads(json.dumps(base)); invalid['search']['sec_edgar'][key] = value
            with self.assertRaises(ValueError): Config.model_validate(invalid)
        base['monitoring']['markets'] = ['CN']
        with self.assertRaises(ValueError): Config.model_validate(base)

    def test_direct_needs_no_tavily_key_or_model_planning(self):
        crawler = self.crawler([filing(1)])
        with patch.object(crawler.model, 'plan', side_effect=AssertionError('must not plan')):
            report = crawler.collect()
        self.assertEqual(report['searched'], 0); self.assertEqual(report['saved'], 1)
        self.assertEqual(report['model_calls'], 1)
        self.assertEqual(report['screen_calls_avoided'],1)
        self.assertTrue(report['discovery']['complete'])
        self.assertEqual(report['summary']['direct_discovery']['requests'], 1)
        self.assertEqual(report['summary']['new_records'], 1)

    def test_pages_resume_after_restart_without_refetch_or_duplicates(self):
        crawler = self.crawler([filing(n) for n in range(1, 6)])
        first = crawler.collect()
        self.assertEqual(first['saved'], 2); self.assertFalse(first['discovery']['complete'])
        restored = Crawler(crawler.cfg, self.root, SecSources({}), SecModel())
        second = restored.collect(); third = restored.collect()
        self.assertEqual(second['saved'], 2); self.assertEqual(third['saved'], 1)
        self.assertEqual(restored.sources.requests, [])
        self.assertTrue(third['discovery']['complete']); self.assertEqual(len(restored.dataset.rows), 5)
        self.assertEqual(restored.collect()['saved'], 0)

    def test_complete_history_continues_catalog_pages_automatically(self):
        cfg = sec_config(); cfg.search.complete_history = True
        crawler = self.crawler([filing(n) for n in range(1, 6)], cfg)
        report = crawler.collect()
        self.assertEqual(report['saved'], 5); self.assertEqual(report['batches'], 3)
        self.assertEqual(report['direct_discovery']['pages'], 3)
        self.assertEqual(report['direct_discovery']['requests'], 1)
        self.assertEqual(report['year_coverage']['2019']['catalog_rows'], 5)

    def test_once_only_reads_one_catalog_page(self):
        cfg = sec_config(); cfg.search.complete_history = True
        report = self.crawler([filing(n) for n in range(1, 6)], cfg).collect(one_batch=True)
        self.assertEqual(report['saved'], 2); self.assertFalse(report['discovery']['complete'])

    def test_company_historical_files_and_request_budget(self):
        cfg = sec_config(); cfg.search.sec_edgar.tickers = ['AAPL']; cfg.search.complete_history = True
        company_url = 'https://data.sec.gov/submissions/CIK0000320193.json'
        history_url = 'https://data.sec.gov/submissions/CIK0000320193-submissions-001.json'
        history_other = 'https://data.sec.gov/submissions/CIK0000320193-submissions-002.json'
        company = {'cik': 320193, 'name': 'Example Company', 'filings': {'recent': columns([filing(2, '2026-01-01')]),
            'files': [{'name': history_url.rsplit('/', 1)[1], 'filingFrom': '2018-01-01', 'filingTo': '2020-01-01'},
                      {'name': history_other.rsplit('/', 1)[1], 'filingFrom': '2010-01-01', 'filingTo': '2011-01-01'}]}}
        responses = {'https://www.sec.gov/files/company_tickers.json': json.dumps({'0': {'ticker': 'AAPL', 'cik_str': 320193}}).encode(),
                     company_url: json.dumps(company).encode(), history_url: json.dumps(columns([filing(1)])).encode()}
        crawler = self.crawler(cfg=cfg, responses=responses)
        report = crawler.collect()
        self.assertEqual(report['saved'], 1); self.assertTrue(report['discovery']['complete'])
        self.assertEqual(crawler.sources.requests, list(responses))
        self.assertNotIn(history_other, crawler.sources.requests)
        self.assertEqual(report['direct_discovery']['requests'], 3)

    def test_explicit_cik_does_not_use_current_ticker_mapping(self):
        cfg = sec_config(); cfg.search.sec_edgar.ciks = ['0000320193']
        response = {'cik': 320193, 'name': 'Example', 'filings': {'recent': columns([filing(1)]), 'files': []}}
        crawler = self.crawler(cfg=cfg, responses={'https://data.sec.gov/submissions/CIK0000320193.json': json.dumps(response).encode()})
        self.assertEqual(crawler.collect()['saved'], 1)
        self.assertEqual(len(crawler.sources.requests), 1)

    def test_unmatched_ticker_pauses_without_falling_back_to_all(self):
        cfg = sec_config(); cfg.search.sec_edgar.tickers = ['MISSING']
        crawler = self.crawler(cfg=cfg, responses={'https://www.sec.gov/files/company_tickers.json': b'{}'})
        report = crawler.collect()
        self.assertTrue(report['direct_discovery']['paused']); self.assertFalse(report['discovery']['complete'])
        self.assertEqual(report['considered'], 0)
        self.assertEqual(len(crawler.sources.requests), 1)

    def test_date_form_and_amendment_filtering(self):
        rows = [filing(1), filing(2, form='8-K/A'), filing(3, form='4'), filing(4, day='2018-12-31'), filing(5, day='2019-02-01')]
        cfg = sec_config(); cfg.search.sec_edgar.page_size = 10
        self.assertEqual(self.crawler(rows, cfg).collect()['saved'], 2)
        # Fresh isolated output for the other form-selection scope.
        cfg.search.sec_edgar.include_amendments = False
        crawler = Crawler(cfg, self.root/'other', SecSources({quarter_jobs('2019-01-01', '2019-01-31')[0]['url']: master(rows)}), SecModel())
        self.assertEqual(crawler.collect()['saved'], 1)

    def test_catalog_failure_is_not_completion_and_next_run_retries(self):
        crawler = self.crawler(responses={quarter_jobs('2019-01-01', '2019-01-31')[0]['url']: FetchError('blocked', 'HTTP 403')})
        first = crawler.collect()
        self.assertFalse(first['discovery']['complete']); self.assertTrue(first['direct_discovery']['paused'])
        self.assertEqual(first['direct_discovery']['pages'], 0)
        crawler.sources.responses[crawler.sources.requests[0]] = master([filing(1)])
        self.assertEqual(crawler.collect()['saved'], 1)

    def test_invalid_catalog_not_silently_empty(self):
        for raw in [b'<html>denied</html>', b'CIK|Company Name|Form Type|Date Filed|Filename\ninvalid']:
            with self.assertRaises(ValueError): parse_master(raw)
        with self.assertRaises(ValueError): parse_submissions({'accessionNumber': ['x']}, '0000320193')
        with self.assertRaises(ValueError): child_jobs({'filings': {'files': [{'name': '../../bad.json'}]}}, '0000320193', '2019-01-01', '2019-01-31')

    def test_compressed_quarter_index_and_truncation(self):
        raw = gzip.compress(master([filing(1)]))
        self.assertEqual(parse_master(raw)[0]['accession'], filing(1)['accession'])
        with self.assertRaises(ValueError): parse_master(raw[:15])

    def test_quarter_boundaries_and_year_fairness(self):
        jobs = quarter_jobs('2018-02-10', '2020-05-25')
        self.assertEqual([(j['year'], j['quarter']) for j in jobs[:3]], [(2018, 1), (2019, 1), (2020, 1)])
        self.assertEqual(len(jobs), 10)
        self.assertEqual(len(quarter_jobs('2012-01-01', '2022-05-25')), 42)

    def test_resume_skips_both_discovery_channels(self):
        cfg = sec_config(); cfg.search.discovery_mode = 'hybrid'
        crawler = self.crawler(cfg=cfg)
        report = crawler.collect(resume=True)
        self.assertEqual(crawler.sources.requests, []); self.assertEqual(crawler.sources.searches, 0)
        self.assertEqual(report['model_calls'], 0)

    def test_hybrid_runs_search_and_directory(self):
        cfg = sec_config(); cfg.search.discovery_mode = 'hybrid'; cfg.search.api_key = SecretStr('offline')
        crawler = self.crawler([filing(1)], cfg)
        report = crawler.collect()
        self.assertEqual(report['searched'], 1); self.assertEqual(report['direct_discovery']['requests'], 1)
        self.assertEqual(report['saved'], 1); self.assertTrue(report['discovery']['complete'])

    def test_sec_document_viewer_and_index_share_one_identity(self):
        row = filing(1); index = filing_identity(row['cik'], row['accession'])
        document = 'https://www.sec.gov/Archives/edgar/data/320193/' + row['accession'].replace('-', '') + '/report.htm'
        self.assertEqual(filing_url(document), index)
        self.assertEqual(filing_url('https://www.sec.gov/ix?doc=' + document.split('www.sec.gov')[1]), index)
        self.assertEqual(filing_url('https://example.com/report.htm'), 'https://example.com/report.htm')
        crawler = self.crawler([row]); candidate = {'url': document, 'title': 'Example', 'snippet': 'SEC filing', 'origin_market': 'US'}
        crawler.queue(candidate)
        self.assertEqual(crawler.collect()['saved'], 1)
        self.assertEqual(len(crawler.dataset.rows), 1)

    def test_existing_primary_document_is_reused_without_migrating_old_files(self):
        from record_time import with_time_information
        from record_files import source_id
        cfg = sec_config(); cfg.search.discovery_mode = 'hybrid'; cfg.search.api_key = SecretStr('offline')
        crawler = self.crawler([filing(1)], cfg)
        document = 'https://www.sec.gov/Archives/edgar/data/320193/' + filing(1)['accession'].replace('-', '') + '/report.htm'
        row = with_time_information({'source_id': source_id(document), 'url': document, '时间': '2019-01-10',
            '分类': 'test', '关键词': ['Example'], '主要内容': 'Existing original record for this SEC filing.'}, {}, {})
        saved = crawler.dataset.save('US', row, 'technology'); original = saved.read_bytes()
        candidate = {'url': document, 'title': 'Example', 'snippet': 'SEC filing', 'origin_market': 'US'}
        with patch.object(crawler.sources, 'search', return_value=[candidate]): report = crawler.collect()
        self.assertEqual(report['saved'], 0); self.assertEqual(len(crawler.dataset.rows), 1)
        self.assertEqual(report['summary']['skipped_reasons'], {'duplicate_saved': 1})
        # The new schema may add metadata, while accession reuse preserves source content/path.
        migrated = json.loads(saved.read_text(encoding='utf-8'))
        previous = json.loads(original)
        self.assertEqual({key:migrated[key] for key in previous if key != '时间信息'},
                         {key:value for key,value in previous.items() if key != '时间信息'})
        self.assertEqual(migrated['时间信息']['first_seen_at'], previous['时间信息']['first_seen_at'])

    def test_hybrid_cannot_hide_incomplete_search_or_directory(self):
        cfg = sec_config(); cfg.search.discovery_mode = 'hybrid'; cfg.search.strategy = 'broad'
        cfg.search.api_key = SecretStr('offline')
        crawler = self.crawler([filing(1)], cfg)
        def search(crawler, report, start, end):
            report['discovery'] = {'complete': False, 'paused': False, 'tasks_remaining': 2, 'year_stats': {}}
        with patch('simple_crawler.broad_discover', side_effect=search): report = crawler.collect()
        self.assertTrue(report['direct_discovery']['complete']); self.assertFalse(report['discovery']['complete'])
        self.assertEqual(report['discovery']['tasks_remaining'], 2)

    def test_committed_page_survives_interrupt_with_pending_queue(self):
        crawler = self.crawler([filing(n) for n in range(1, 4)])
        persist = crawler.persist
        def interrupt():
            persist()
            progress = next(iter(crawler.state.get('sec_discovery', {}).values()), {})
            if progress.get('pages_scanned') == 1: raise KeyboardInterrupt()
        with patch.object(crawler, 'persist', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): crawler.collect()
        restored = Crawler(crawler.cfg, self.root, SecSources({}), SecModel())
        self.assertEqual(restored.collect()['saved'], 3)
        self.assertEqual(restored.sources.requests, [])

    def test_catalog_and_search_candidate_counts_remain_separate(self):
        report = self.crawler([filing(1)]).collect()
        self.assertEqual(report['summary']['search']['candidates_new'], 0)
        self.assertEqual(report['summary']['candidates']['candidates_new'], 1)
        self.assertEqual(report['summary']['direct_discovery']['candidates_new'], 1)

    def test_direct_scope_isolates_old_search_queue(self):
        crawler = self.crawler([filing(1)])
        crawler.state['pending']['old'] = {'url': filing_identity('320193', filing(2)['accession']), 'origin_market': 'US',
            'title': 'old', 'snippet': 'old', 'collection_scope': 'previous-search-scope'}
        report = crawler.collect()
        self.assertEqual(report['inactive_pending'], 1); self.assertEqual(report['saved'], 1)

    def test_preview_is_offline_and_hides_contact(self):
        cfg = sec_config(); cfg.search.sec_edgar.tickers = ['AAPL']
        with patch('builtins.print') as output:
            self.assertEqual(print_history_preview(cfg), 0)
        printed = output.call_args[0][0]
        self.assertIn('requires_ticker_resolution', printed)
        self.assertNotIn('test@example.com', printed)
        self.assertNotIn('test@example.com', json.dumps(cfg.model_dump()))

    def test_realtime_rechecks_catalog_for_new_filings(self):
        cfg = sec_config(); cfg.collection.mode = 'realtime'; cfg.collection.start_date = cfg.collection.end_date = None
        crawler = self.crawler(cfg=sec_config())
        crawler.cfg = cfg
        with patch.object(crawler, 'window', return_value=('2019-01-01', '2019-01-31')):
            crawler.sources.responses[quarter_jobs('2019-01-01', '2019-01-31')[0]['url']] = master([filing(1)])
            self.assertEqual(crawler.collect()['saved'], 1)
            crawler.sources.responses[crawler.sources.requests[0]] = master([filing(1), filing(2)])
            self.assertEqual(crawler.collect()['saved'], 1)
        self.assertEqual(len(crawler.sources.requests), 2)


class SecPageTests(SecTestBase):
    def page_source(self, extra='', day='2019-01-10', href=None):
        row = filing(1)
        prefix = '/Archives/edgar/data/320193/' + row['accession'].replace('-', '') + '/'
        href = href or prefix + 'report.htm'
        html = f'''<html><div class="infoHead">Filing Date</div><div class="info">{day}</div>
            <table class="tableFile"><tr><th>Seq</th></tr><tr><td>1</td><td>Main report</td><td><a href="{href}">report.htm</a></td><td>8-K</td></tr>
            <tr><td>2</td><td>Exhibit</td><td><a href="{prefix}exhibit.htm">exhibit.htm</a></td><td>EX-99.1</td></tr></table>{extra}</html>'''
        body = b'<html><title>Operating results</title><article>Example Company announced operating results and an acquisition. No exhibit content.</article></html>'
        cfg = sec_config(); cfg.fetch.use_crawl4ai = False
        sources = Sources(cfg, self.root)
        candidate = {'url': filing_identity('320193', row['accession']), 'origin_market': 'US', 'title': 'Example 8-K', 'sec_filing': row}
        return sources, candidate, html.encode(), body

    def test_primary_document_and_official_date_not_index_or_exhibit(self):
        sources, candidate, index, body = self.page_source()
        calls = []
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return (index if url.endswith('-index.html') else body), {'content_type': 'text/html'}
        with patch.object(sources.http, 'get', side_effect=get): page = sources.fetch(candidate)
        sources.close()
        self.assertEqual(len(calls), 2); self.assertTrue(calls[1][0].endswith('/report.htm'))
        self.assertEqual(page['url'], candidate['url'])
        self.assertEqual(page['date_context'], 'SEC filing datePublished=2019-01-10')
        self.assertIn('operating results', page['text']); self.assertNotIn('Main report', page['text'])
        self.assertEqual(calls[0][1]['headers']['User-Agent'], 'OfflineTests test@example.com')
        self.assertEqual(calls[0][1]['allowed'], ['sec.gov'])

    def test_ixbrl_viewer_unwraps_to_document(self):
        href = '/ix?doc=/Archives/edgar/data/320193/' + filing(1)['accession'].replace('-', '') + '/report.htm'
        sources, candidate, index, body = self.page_source(href=href)
        with patch.object(sources.http, 'get', side_effect=[(index, {}), (body, {})]): page = sources.fetch(candidate)
        sources.close(); self.assertTrue(page['document_url'].endswith('/report.htm'))

    def test_mismatched_date_and_external_document_are_rejected(self):
        for args in [{'day': '2019-01-11'}, {'href': 'https://example.com/report.htm'},
                     {'href': '/Archives/edgar/data/320193/' + filing(1)['accession'].replace('-', '') + '/%2e%2e/report.htm'}]:
            sources, candidate, index, body = self.page_source(**args)
            with patch.object(sources.http, 'get', return_value=(index, {})) as get:
                with self.assertRaises(ValueError): sources.fetch(candidate)
                self.assertEqual(get.call_count, 1)
            sources.close()

    def test_qwen_cannot_replace_verified_sec_date_with_report_period(self):
        cfg = sec_config()
        qwen = Qwen(cfg.ollama, self.root)
        page = {'url': filing_identity('320193', filing(1)['accession']), 'title': 'Report', 'origin_market': 'US',
            'date_context': 'SEC filing datePublished=2019-01-10', 'sec_filing_date': '2019-01-10',
            'text': 'Fiscal year results published 2018-12-31. Example Company reported results.'}
        def ask(purpose, prompt, payload, schema, validate=None):
            return validate({'time': '2018-12-31', 'date_quote': 'published 2018-12-31', 'date_kind': 'published'})
        with patch.object(qwen, 'ask', side_effect=ask):
            with self.assertRaises(ValueError): qwen.summarize(page, cfg.monitoring)
        qwen.close()


if __name__ == '__main__': unittest.main()

"""Synthetic fixtures for website policy. All provider traffic uses MockTransport."""
import json
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from finance_agent.config import Config, Search
from finance_agent.fetcher import ContentFetcher
from finance_agent.pipeline import Engine
from finance_agent.provider import RSSProvider, TavilyProvider
from finance_agent.safety import FetchError, PublicHTTP
from test_agent import Base, FakeLLM, candidate


def scoped_configuration(mode='restricted', markets=None):
    return Config(
        collection={'mode': 'historical', 'start_date': '2026-11-01', 'end_date': '2026-11-05'},
        monitoring={'markets': markets or ['CN', 'US']},
        search={
            'website_mode': mode,
            'market_domains': {'CN': ['cninfo.com.cn', 'xueqiu.com'], 'US': ['sec.gov', 'nasdaq.com']},
            'api_key': 'synthetic-scope-test-key',
        },
        fetch={'use_crawl4ai': False},
    )


class WebsiteScopeTests(Base):
    def test_market_domains_and_global_use_only_selected_markets(self):
        cfg = scoped_configuration()
        self.assertEqual(cfg.search.domains_for('CN'), ['cninfo.com.cn', 'xueqiu.com'])
        self.assertEqual(cfg.search.domains_for('US'), ['sec.gov', 'nasdaq.com'])
        self.assertEqual(cfg.search.domains_for('GLOBAL', ['US']), ['sec.gov', 'nasdaq.com'])
        self.assertFalse(cfg.search.allows_url('https://www.sec.gov/story', 'CN', ['CN', 'US']))
        self.assertFalse(cfg.search.allows_url('https://www.sec.gov/story', 'US', ['CN']))
        self.assertTrue(cfg.search.allows_url('https://static.cninfo.com.cn/story.pdf', 'CN'))
        self.assertFalse(cfg.search.allows_url('https://cninfo.com.cn.attacker.example/story', 'CN'))

    def test_unrestricted_ignores_market_lists_but_respects_explicit_exclusions(self):
        cfg = scoped_configuration('unrestricted')
        self.assertEqual(cfg.search.domains_for('CN'), [])
        self.assertTrue(cfg.search.allows_url('https://other.example/story', 'CN'))
        cfg.search.exclude_domains = ['excluded.example']
        self.assertFalse(cfg.search.allows_url('https://sub.excluded.example/story', 'CN'))

    def test_empty_restricted_active_market_is_rejected(self):
        raw = scoped_configuration().model_dump()
        raw['search']['market_domains']['US'] = []
        with self.assertRaises(ValidationError):
            Config.model_validate(raw)
        raw['monitoring']['markets'] = ['CN']
        self.assertEqual(Config.model_validate(raw).monitoring.markets, ['CN'])
        raw['search']['exclude_domains'] = ['cninfo.com.cn', 'xueqiu.com']
        with self.assertRaises(ValidationError):
            Config.model_validate(raw)

    def test_domain_input_normalization_and_invalid_values(self):
        s = Search(website_mode='restricted', market_domains={'CN': [' CNINFO.COM.CN. ', 'cninfo.com.cn']})
        self.assertEqual(s.market_domains['CN'], ['cninfo.com.cn'])
        for domain in ['https://sec.gov', 'sec.gov/path', '*.sec.gov', 'sec.gov:443', '', '-bad.example']:
            with self.subTest(domain=domain), self.assertRaises(ValidationError):
                Search(market_domains={'US': [domain]})
        with self.assertRaises(ValidationError):
            Search(market_domains={'A股': ['cninfo.com.cn']})

    def test_legacy_include_domains_preserves_existing_behavior(self):
        search = Search(include_domains=['sec.gov'])
        self.assertEqual(search.domains_for('CN'), ['sec.gov'])
        self.assertTrue(search.allows_url('https://www.sec.gov/story'))
        self.assertFalse(search.allows_url('https://other.example/story'))
        with self.assertRaises(ValidationError):
            Search(website_mode='unrestricted', include_domains=['sec.gov'])

    def test_tavily_routes_each_market_and_filters_outside_results(self):
        cfg = scoped_configuration()
        seen = []
        def handler(request):
            body = json.loads(request.content)
            seen.append(body['include_domains'])
            return httpx.Response(200, json={'results': [
                {'url': 'https://www.cninfo.com.cn/story'},
                {'url': 'https://www.sec.gov/story'},
                {'url': 'https://other.example/story'},
            ]})
        provider = TavilyProvider(cfg.search, httpx.Client(transport=httpx.MockTransport(handler)))
        try:
            cn, _ = provider.search('banking', '2026-11-01', '2026-11-05', 10, market='CN', markets=['CN', 'US'])
            us, _ = provider.search('banking', '2026-11-01', '2026-11-05', 10, market='US', markets=['CN', 'US'])
            self.assertEqual(seen, [['cninfo.com.cn', 'xueqiu.com'], ['sec.gov', 'nasdaq.com']])
            self.assertEqual([c['url'] for c in cn], ['https://www.cninfo.com.cn/story'])
            self.assertEqual([c['url'] for c in us], ['https://www.sec.gov/story'])
        finally:
            provider.close()

    def test_unrestricted_tavily_sends_empty_allowlist(self):
        cfg = scoped_configuration('unrestricted')
        def handler(request):
            self.assertEqual(json.loads(request.content)['include_domains'], [])
            return httpx.Response(200, json={'results': [{'url': 'https://other.example/story'}]})
        provider = TavilyProvider(cfg.search, httpx.Client(transport=httpx.MockTransport(handler)))
        try:
            rows, _ = provider.search('test', '2026-11-01', '2026-11-05', 1, market='CN')
            self.assertEqual(len(rows), 1)
        finally:
            provider.close()

    def test_fetcher_rejects_other_market_before_network(self):
        cfg = scoped_configuration()
        class HTTP:
            def get(self, *args, **kwargs):
                raise AssertionError('Out-of-scope page was requested')
        fetcher = ContentFetcher(cfg, HTTP(), self.root)
        c = candidate()
        c.update(url='https://www.sec.gov/story', query_market='CN')
        with self.assertRaises(FetchError) as caught:
            fetcher.fetch(c)
        self.assertEqual(caught.exception.status, 'out_of_scope')

    def test_fetcher_passes_market_policy_to_http_including_legacy_candidates(self):
        cfg = scoped_configuration(markets=['US'])
        seen = []
        class HTTP:
            def get(self, url, **kwargs):
                seen.append(kwargs)
                return b'<html></html>', {'url': url}
        fetcher = ContentFetcher(cfg, HTTP(), self.root)
        c = candidate()
        c['url'] = 'https://www.sec.gov/story'
        with patch.object(fetcher, 'parse', return_value={'fixture': True}):
            self.assertEqual(fetcher.fetch(c), {'fixture': True})
        self.assertEqual(seen[0]['allowed'], ['sec.gov', 'nasdaq.com'])

    def test_redirect_cannot_escape_allowlist_or_enter_excluded_subdomain(self):
        for target, exclusions in [('https://other.example/story', []), ('https://private.sec.gov/story', ['private.sec.gov'])]:
            with self.subTest(target=target):
                cfg = scoped_configuration()
                cfg.fetch.respect_robots = False
                http = PublicHTTP(cfg.fetch)
                http.client.close()
                requests = []
                def handler(request):
                    requests.append(str(request.url))
                    return httpx.Response(302, headers={'location': target})
                http.client = httpx.Client(transport=httpx.MockTransport(handler))
                def resolver(*args, **kwargs):
                    return [(2, 1, 6, '', ('93.184.216.34', 443))]
                try:
                    with patch('finance_agent.safety.socket.getaddrinfo', side_effect=resolver):
                        with self.assertRaises(FetchError):
                            http.get('https://www.sec.gov/story', allowed=['sec.gov'], excluded=exclusions)
                    self.assertEqual(requests, ['https://www.sec.gov/story'])
                finally:
                    http.close()

    def test_rss_obeys_market_policy_for_feeds_and_article_links(self):
        cfg = scoped_configuration()
        cfg.search.provider = 'rss'
        cfg.search.rss_feeds = ['https://www.cninfo.com.cn/feed', 'https://www.sec.gov/feed']
        requested = []
        class HTTP:
            def get(self, url, **kwargs):
                requested.append(url)
                return b'<rss><channel><item><link>https://www.sec.gov/story</link></item><item><link>https://other.example/story</link></item></channel></rss>', {'url': url}
        rows, _ = RSSProvider(cfg.search, HTTP()).search('test', '2026-11-01', '2026-11-05', 10, market='US', markets=['CN', 'US'])
        self.assertEqual(requested, ['https://www.sec.gov/feed'])
        self.assertEqual([r['url'] for r in rows], ['https://www.sec.gov/story'])

    def test_existing_queue_pauses_and_resumes_original_stage_without_model_calls(self):
        cfg = scoped_configuration(markets=['CN'])
        repo = self.repo()
        expected = {}
        for stage in ['new', 'approved', 'fetched']:
            c = candidate()
            c.update(candidate_id='C_scope_' + stage, url='https://other.example/' + stage)
            repo.upsert_candidate(c, 'synthetic')
            repo.set_status(c['candidate_id'], stage)
            expected[c['candidate_id']] = stage
        llm = FakeLLM()
        engine = Engine(cfg, self.root, repo=repo, llm=llm)
        try:
            engine.process({})
            self.assertEqual(llm.calls, 0)
            for cid in expected:
                self.assertEqual(repo.candidate(cid)['status'], 'out_of_scope')
            cfg.search.website_mode = 'unrestricted'
            engine.apply_website_scope()
            for cid, stage in expected.items():
                self.assertEqual(repo.candidate(cid)['status'], stage)
        finally:
            engine.close()

    def test_add_remove_site_rechecks_queue_and_preserves_first_seen(self):
        cfg = scoped_configuration(markets=['CN'])
        repo = self.repo()
        c = candidate()
        c.update(url='https://added.example/story', query_market='CN')
        repo.upsert_candidate(c, 'synthetic')
        llm = FakeLLM()
        engine = Engine(cfg, self.root, repo=repo, llm=llm)
        try:
            engine.apply_website_scope()
            self.assertEqual(repo.candidate(c['candidate_id'])['status'], 'out_of_scope')
            cfg.search.market_domains['CN'].append('added.example')
            engine.apply_website_scope()
            self.assertEqual(repo.candidate(c['candidate_id'])['status'], 'new')
            cfg.search.market_domains['CN'].remove('added.example')
            engine.apply_website_scope()
            row = repo.candidate(c['candidate_id'])
            self.assertEqual(row['status'], 'out_of_scope')
            self.assertEqual(row['first_seen'], c['first_seen_at'])
        finally:
            engine.close()

    def test_discovery_normal_entry_routes_market_and_persists_actual_scope(self):
        cfg = scoped_configuration()
        seen = []
        def handler(request):
            body = json.loads(request.content)
            seen.append(body['include_domains'])
            host = body['include_domains'][0]
            return httpx.Response(200, json={'results': [{'url': 'https://' + host + '/story'}]})
        provider = TavilyProvider(cfg.search, httpx.Client(transport=httpx.MockTransport(handler)))
        repo = self.repo()
        engine = Engine(cfg, self.root, repo=repo, llm=FakeLLM(), provider=provider)
        plans = [
            {'query': '银行业 公告', 'market': 'CN', 'direction': '银行业', 'reason': 'synthetic'},
            {'query': 'banking disclosure', 'market': 'US', 'direction': '银行业', 'reason': 'synthetic'},
        ]
        try:
            with patch('finance_agent.pipeline.plan_queries', return_value=plans):
                report = engine.cycle(discover=True, process=False)
            self.assertFalse(report['errors'])
            self.assertEqual(seen, [['cninfo.com.cn', 'xueqiu.com'], ['sec.gov', 'nasdaq.com']])
            self.assertEqual([r['market'] for r in report['search_requests']], ['CN', 'US'])
            self.assertEqual(report['candidates_new'], 2)
            for row in repo.pending('new', 10):
                c = json.loads(row['payload'])
                self.assertTrue(cfg.search.allows_url(c['url'], c['query_market'], cfg.monitoring.markets))
        finally:
            engine.close()

    def test_scope_changes_invalidate_search_checkpoint(self):
        cfg = scoped_configuration(markets=['CN'])
        requests = []
        def handler(request):
            requests.append(json.loads(request.content)['include_domains'])
            return httpx.Response(200, json={'results': []})
        provider = TavilyProvider(cfg.search, httpx.Client(transport=httpx.MockTransport(handler)))
        engine = Engine(cfg, self.root, repo=self.repo(), llm=FakeLLM(), provider=provider)
        plans = [{'query': '银行业 公告', 'market': 'CN', 'direction': '银行业', 'reason': 'synthetic'}]
        try:
            with patch('finance_agent.pipeline.plan_queries', return_value=plans):
                first = engine.cycle(discover=True, process=False)
                unchanged = engine.cycle(discover=True, process=False)
                cfg.search.market_domains['CN'].append('added.example')
                changed = engine.cycle(discover=True, process=False)
            self.assertEqual(first['queries_executed'], 1)
            self.assertEqual(unchanged['queries_executed'], 0)
            self.assertEqual(changed['queries_executed'], 1)
            self.assertEqual(requests, [['cninfo.com.cn', 'xueqiu.com'], ['cninfo.com.cn', 'xueqiu.com', 'added.example']])
        finally:
            engine.close()

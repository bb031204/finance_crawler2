"""Synthetic Fake-IP/proxy fixtures. No real external requests are made here."""
from unittest.mock import patch
import httpx
from pydantic import ValidationError
from finance_agent.config import Fetch
from finance_agent.pipeline import Engine
from finance_agent.safety import FetchError, PublicHTTP, public_url
from test_agent import Base, FakeLLM, candidate


def resolver_for(*ips):
    def resolve(host, port, **kwargs):
        return [(10 if ':' in ip else 2, 1, 6, '', (ip, port)) for ip in ips]
    return resolve


class NetworkCompatibilityTests(Base):
    def test_proxy_requires_explicit_local_http_endpoint(self):
        for proxy in ['https://127.0.0.1:7897', 'http://outside.example:7897',
                      'http://user:pass@127.0.0.1:7897', 'http://127.0.0.1',
                      'http://127.0.0.1:7897/path', 'http://127.0.0.1:99999']:
            with self.subTest(proxy=proxy), self.assertRaises(ValidationError):
                Fetch(proxy_url=proxy)
        with self.assertRaises(ValidationError):
            Fetch(allow_fake_ip=True)
        self.assertTrue(Fetch(proxy_url='http://127.0.0.1:7897', allow_fake_ip=True).allow_fake_ip)

    def test_fake_ip_without_opt_in_reports_actionable_network_error(self):
        with self.assertRaises(FetchError) as caught:
            public_url('https://www.cls.cn/article', resolver=resolver_for('198.18.0.159'))
        self.assertEqual(caught.exception.status, 'network_error')
        self.assertIn('fetch.proxy_url', str(caught.exception))
        self.assertEqual(caught.exception.info['addresses'], ['198.18.0.159'])

    def test_fake_ip_allowed_only_for_domain_with_explicit_opt_in(self):
        for ip in ['198.18.0.1', '198.19.255.254']:
            with self.subTest(ip=ip):
                self.assertEqual(public_url('https://www.cls.cn/article', allow_fake_ip=True,
                                           resolver=resolver_for(ip)), 'https://www.cls.cn/article')
        for url in ['http://198.18.0.1/', 'http://198.19.0.1/', 'http://localhost/',
                    'http://host.local/', 'http://metadata.google.internal/', 'http://printer.lan/']:
            with self.subTest(url=url), self.assertRaises(FetchError) as caught:
                public_url(url, allow_fake_ip=True, resolver=resolver_for('198.18.0.1'))
            self.assertEqual(caught.exception.status, 'blocked')

    def test_private_and_special_ranges_remain_rejected(self):
        for ip in ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.0.1', '169.254.169.254',
                   '0.0.0.0', '::1', 'fc00::1', 'fe80::1', '192.0.2.1', '198.51.100.1']:
            with self.subTest(ip=ip), self.assertRaises(FetchError) as caught:
                public_url('https://www.cls.cn/article', allow_fake_ip=True, resolver=resolver_for(ip))
            self.assertEqual(caught.exception.status, 'blocked')

    def test_mixed_fake_and_private_answer_remains_rejected(self):
        with self.assertRaises(FetchError) as caught:
            public_url('https://www.cls.cn/article', allow_fake_ip=True,
                       resolver=resolver_for('198.18.0.159', '127.0.0.1'))
        self.assertEqual(caught.exception.status, 'blocked')

    def test_domain_scope_still_checked_before_fake_ip_exception(self):
        with self.assertRaises(FetchError):
            public_url('https://other.example/article', allowed=['cls.cn'], allow_fake_ip=True,
                       resolver=resolver_for('198.18.0.159'))
        with self.assertRaises(FetchError):
            public_url('https://m.cls.cn/article', excluded=['m.cls.cn'], allow_fake_ip=True,
                       resolver=resolver_for('198.18.0.159'))

    def test_http_client_uses_configured_proxy_and_disables_implicit_environment(self):
        cfg = Fetch(proxy_url='http://127.0.0.1:7897', allow_fake_ip=True)
        with patch('finance_agent.safety.httpx.Client') as client:
            http = PublicHTTP(cfg)
            self.assertEqual(client.call_args.kwargs['proxy'], 'http://127.0.0.1:7897')
            self.assertFalse(client.call_args.kwargs['trust_env'])
            http.close()

    def test_requests_and_robots_work_with_fake_ip_and_private_redirect_is_blocked(self):
        cfg = Fetch(proxy_url='http://127.0.0.1:7897', allow_fake_ip=True)
        http = PublicHTTP(cfg)
        http.client.close()
        requests = []
        def handler(request):
            requests.append(str(request.url))
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text='User-agent: *\nAllow: /\n')
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        http.client = httpx.Client(transport=httpx.MockTransport(handler))
        def resolve(host, port, **kwargs):
            return resolver_for('127.0.0.1' if host == '127.0.0.1' else '198.18.0.159')(host, port, **kwargs)
        try:
            with patch('finance_agent.safety.socket.getaddrinfo', side_effect=resolve), patch('finance_agent.safety.time.sleep'):
                with self.assertRaises(FetchError) as caught:
                    http.get('https://www.cls.cn/article')
            self.assertEqual(caught.exception.status, 'blocked')
            self.assertEqual(requests, ['https://www.cls.cn/robots.txt', 'https://www.cls.cn/article'])
        finally:
            http.close()

    def test_recovery_targets_legacy_dns_error_only_and_revalidates_address(self):
        cfg = self.cfg.model_copy(deep=True)
        cfg.fetch = Fetch(proxy_url='http://127.0.0.1:7897', allow_fake_ip=True)
        repo = self.repo()
        entries = [
            ('C_fake', 'https://www.cls.cn/story', '禁止访问回环、私网、链路本地及特殊地址'),
            ('C_private', 'http://127.0.0.1/story', '禁止访问回环、私网、链路本地及特殊地址'),
            ('C_denied', 'https://www.cls.cn/denied', 'HTTP 403'),
        ]
        for cid, url, error in entries:
            c = candidate()
            c.update(candidate_id=cid, url=url)
            repo.upsert_candidate(c, 'synthetic')
            repo.set_status(cid, 'blocked', error)
        engine = Engine(cfg, self.root, repo=repo, llm=FakeLLM())
        def resolve(host, port, **kwargs):
            return resolver_for('127.0.0.1' if host == '127.0.0.1' else '198.18.0.159')(host, port, **kwargs)
        try:
            report = {}
            with patch('finance_agent.safety.socket.getaddrinfo', side_effect=resolve):
                engine.recover_dns_blocked(report)
                engine.recover_dns_blocked(report)
            self.assertEqual(repo.candidate('C_fake')['status'], 'approved')
            self.assertEqual(repo.candidate('C_private')['status'], 'blocked')
            self.assertEqual(repo.candidate('C_denied')['status'], 'blocked')
            self.assertEqual(report['network_recovered'], 1)
            self.assertEqual(repo.candidate('C_fake')['first_seen'], candidate()['first_seen_at'])
        finally:
            engine.close()

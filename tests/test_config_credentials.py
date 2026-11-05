"""Synthetic credentials only; all HTTP calls are mocked."""
import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from finance_agent.cli import main
from finance_agent.config import Config, load
from finance_agent.core import ROOT
from finance_agent.provider import TavilyProvider, tavily_api_key


def config(key=''):
    return Config(collection={'mode': 'historical', 'start_date': '2026-11-01', 'end_date': '2026-11-05'},
                  search={'api_key': key})


class ConfigCredentialTests(unittest.TestCase):
    def test_config_credential_precedes_environment_and_authenticates(self):
        cfg = config('  synthetic-config-credential  ')
        def handler(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer synthetic-config-credential')
            self.assertNotIn('synthetic-config-credential', request.content.decode())
            return httpx.Response(200, json={'results': []})
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'synthetic-environment-credential'}):
            provider = TavilyProvider(cfg.search, httpx.Client(transport=httpx.MockTransport(handler)))
            try:
                provider.preflight()
                _, snapshot = provider.search('test', '2026-11-01', '2026-11-05', 1)
                self.assertNotIn('synthetic-config-credential', json.dumps(snapshot))
            finally:
                provider.close()

    def test_blank_config_falls_back_to_environment(self):
        with patch.dict(os.environ, {'TAVILY_API_KEY': ' synthetic-env '}):
            for key in ('', '   '):
                with self.subTest(key=key):
                    self.assertEqual(tavily_api_key(config(key).search), 'synthetic-env')

    def test_missing_key_fails_with_config_guidance(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = TavilyProvider(config().search)
            try:
                with self.assertRaisesRegex(ValueError, 'search.api_key'):
                    provider.preflight()
            finally:
                provider.close()

    def test_serialization_and_cli_preview_never_contain_credential(self):
        secret = 'synthetic-private-config-key'
        cfg = config(secret)
        for preview in (repr(cfg), repr(cfg.search), cfg.model_dump_json(), repr(cfg.model_dump())):
            self.assertNotIn(secret, preview)
        with patch.dict(os.environ, {}, clear=True), patch('finance_agent.cli.load', return_value=cfg):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['--dry-run']), 0)
            self.assertNotIn(secret, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertTrue(result['tavily_key_present'])
            self.assertNotIn('api_key', result['config']['search'])

    def test_validation_error_does_not_echo_parent_config_secret(self):
        secret = 'synthetic-error-sensitive-key'
        cfg = config().model_dump()
        cfg['search']['api_key'] = secret
        cfg['monitoring']['directions'] = ['']
        try:
            Config.model_validate(cfg)
        except ValidationError as exc:
            failure = exc
        else:
            self.fail('Expected validation failure')
        with patch('finance_agent.cli.load', side_effect=failure):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['--validate-config']), 1)
            self.assertNotIn(secret, output.getvalue())
            self.assertIn('方向词不能为空', output.getvalue())

    def test_yaml_load_reads_config_key_without_environment(self):
        parent = ROOT / '.runtime/tests'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            path = root / 'config.yaml'
            # JSON is accepted by YAML and keeps this fixture explicit.
            raw = config().model_dump()
            raw['search']['api_key'] = 'synthetic-yaml-key'
            path.write_text(json.dumps(raw), encoding='utf-8')
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(tavily_api_key(load(path, root).search), 'synthetic-yaml-key')

    def test_malformed_yaml_error_never_echoes_key_line(self):
        parent = ROOT / '.runtime/tests'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            path = root / 'config.yaml'
            path.write_text('search:\n  api_key: "synthetic-unclosed-sensitive-key\n', encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                load(path, root)
            self.assertNotIn('synthetic-unclosed-sensitive-key', str(caught.exception))
            self.assertIn('YAML格式错误', str(caught.exception))

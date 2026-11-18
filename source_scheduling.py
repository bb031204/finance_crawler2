"""Persist observed access restrictions without changing or discarding candidates."""
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from finance_agent.core import dump, digest


class SourceAccess:
    def __init__(self, root, cfg, profile=None, clock=None):
        self.cfg, self.clock = cfg, clock or time.time
        self.path = Path(root)/'.runtime/simple/source_access.json'
        self.profile = digest([cfg.proxy_url, cfg.allow_fake_ip, cfg.respect_robots,
            cfg.access_cooldown_seconds,cfg.robots_cache_seconds,profile])
        self.state = {'profile': self.profile, 'hosts': {}, 'robots': {}}
        if self.path.is_file():
            previous = json.loads(self.path.read_text(encoding='utf-8'))
            if previous.get('profile') == self.profile:
                self.state = previous
        self.parsers = {}
        self.dirty = False

    def capture(self, http):
        if http is None:
            return
        changed = False
        for host, item in getattr(http, 'restrictions', {}).items():
            value = dict(item, until=item['observed_at']+self.cfg.access_cooldown_seconds)
            if value['until'] <= self.clock():
                continue
            if self.state['hosts'].get(host) != value:
                self.state['hosts'][host] = value; changed = True
        for origin, item in getattr(http, 'robots_evidence', {}).items():
            # Oversized robots can still be enforced by PublicHTTP; never copy
            # an arbitrarily large response into the scheduling sidecar.
            if len(item.get('text') or '') > 65536:
                continue
            value = dict(item, until=item['observed_at']+self.cfg.robots_cache_seconds)
            if value['until'] <= self.clock():
                continue
            if self.state['robots'].get(origin) != value:
                self.state['robots'][origin] = value; changed = True
        if changed:
            stamp = self.clock()
            for key in ['hosts', 'robots']:
                self.state[key] = {k:v for k,v in self.state[key].items() if v['until'] > stamp}
            self.dirty = True
        if self.dirty:
            dump(self.path, self.state, compact=True)
            self.dirty = False

    def paused(self, url):
        p = urlsplit(url)
        host, origin = p.netloc.lower(), p.scheme.lower()+'://'+p.netloc.lower()
        entry = self.state['hosts'].get(host)
        if entry and entry['until'] > self.clock():
            return dict(host=host, reason=entry['reason'], until=entry['until'])
        entry = self.state['robots'].get(origin)
        if self.cfg.respect_robots and entry and entry['until'] > self.clock() and entry.get('text') is not None:
            key = digest([entry['text'], entry['agent']])
            if key not in self.parsers:
                parser = RobotFileParser(); parser.parse(entry['text'].splitlines())
                self.parsers[key] = parser
            if not self.parsers[key].can_fetch(entry['agent'], url):
                return dict(host=host, reason='robots_path_disallowed', until=entry['until'])
        return None

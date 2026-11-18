"""Guarded launcher; keeps the original semantic batch/cache fingerprint unchanged."""
import argparse
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]

def validate_config(config,root=ROOT):
    output=Path(config['output']).resolve();output.relative_to((root/'data_enriched/industry_batches').resolve())
    if output==(root/'data_enriched/industry_batches').resolve():raise ValueError('Choose a versioned batch directory')
    url=urlsplit(config['ollama_url'])
    if url.scheme!='http' or url.hostname not in ('localhost','127.0.0.1','::1') or url.username or url.password:
        raise ValueError('Legacy news must be processed by the configured local model service')
    for key,lo,hi in (('num_ctx',1024,65536),('num_predict',128,4096),('candidate_limit',1,32),
      ('checkpoint_every',1,1000),('max_consecutive_failures',1,100),('timeout_seconds',1,600),('maximum_summary_characters',100,100000)):
        value=config[key]
        if type(value) is not int or not lo<=value<=hi:raise ValueError('Invalid '+key)
    value=config['min_confidence']
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1:
        raise ValueError('Invalid min_confidence')
    return output

@contextmanager
def lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell()==0:stream.write(b'1');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:raise ValueError('This industry batch is already running; do not launch concurrent writers') from exc
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

def main():
    import yaml
    parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--config',default=str(ROOT/'offline_enrichment/industry_config.yaml'))
    args,_=parser.parse_known_args();config=yaml.safe_load(Path(args.config).read_text(encoding='utf-8'))
    output=validate_config(config)
    identity=hashlib.sha256(str(output).casefold().encode()).hexdigest()[:20]
    with lock(ROOT/'.runtime'/('legacy-industry-'+identity+'.lock')):
        from industry_batch import main as run
        run()

if __name__=='__main__':main()

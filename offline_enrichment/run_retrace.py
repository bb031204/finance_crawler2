"""Run the authorized corpus job, bounded retry and offline acceptance to completion."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from finance_agent.core import bounded,dump,now
from offline_enrichment.run_industry_batch import lock


def main():
    import yaml
    parser=argparse.ArgumentParser();parser.add_argument('--config',default=str(ROOT/'offline_enrichment/retrace_config.yaml'))
    parser.add_argument('--retry-rounds',type=int,choices=range(3),default=1);args=parser.parse_args()
    config=bounded(args.config,ROOT);cfg=yaml.safe_load(config.read_text(encoding='utf-8-sig'))
    out=bounded(ROOT/cfg['output'],ROOT);out.mkdir(parents=True,exist_ok=True)
    state={'created_at':now(),'config':str(config),'output':str(out),'state':'starting','passes':[]}
    status=out/'coordinator_status.json'
    with lock(ROOT/'.runtime/retrace_coordinator.lock'):
        for number in range(args.retry_rounds+1):
            if (out/'STOP_REQUESTED').exists():
                state.update(state='stopped_by_request',updated_at=now());dump(status,state);return
            command=[sys.executable,'-X','utf8','-u','-B',str(ROOT/'offline_enrichment/retrace_news.py'),'--config',str(config)]
            if number:command.append('--retry-failed')
            state.update(state='running_initial' if not number else 'retrying_failed',updated_at=now());dump(status,state)
            started=time.monotonic();result=subprocess.run(command,cwd=ROOT)
            state['passes'].append({'round':number,'exit_code':result.returncode,'seconds':round(time.monotonic()-started,1),'finished_at':now()})
            if result.returncode:
                state.update(state='failed_requires_inspection',updated_at=now());dump(status,state);raise SystemExit(result.returncode)
            report=json.loads((out/'progress.json').read_text(encoding='utf-8'))
            if report['pending']:
                state.update(state='stopped_with_pending',updated_at=now(),progress=report);dump(status,state);return
            if not report['status_counts'].get('needs_review',0):break
        from offline_enrichment.verify_retrace import verify
        report=verify(out);dump(out/'acceptance.json',report)
        state.update(state='completed_with_review_items' if report['passed'] and json.loads((out/'progress.json').read_text(encoding='utf-8'))['status_counts'].get('needs_review',0)>0 else 'completed' if report['passed'] else 'acceptance_failed',
            updated_at=now(),acceptance=report)
        dump(status,state)
        print(json.dumps(state,ensure_ascii=False),flush=True)
        if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()

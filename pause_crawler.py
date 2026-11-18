"""Request a safe pause from another terminal."""
import argparse
from pathlib import Path
from crawler_control import request_pause, wait_paused


def main(argv=None):
    parser=argparse.ArgumentParser(description='暂停当前项目采集，等待安全保存，不强杀进程')
    parser.add_argument('--no-wait',action='store_true',help='只提交暂停请求，不等待确认')
    parser.add_argument('--timeout',type=float,default=300,help='等待确认的秒数，默认300')
    args=parser.parse_args(argv)
    if args.timeout<=0:parser.error('timeout必须大于0')
    root=Path(__file__).resolve().parent
    result=request_pause(root);print(result['message'],flush=True)
    if result['status']=='pause_requested' and not args.no_wait:
        result=wait_paused(root,result['run_id'],args.timeout);print(result['message'],flush=True)
    return 0 if result['status'] in {'idle','paused','pause_requested'} else 2 if result['status']=='waiting' else 1


if __name__=='__main__':raise SystemExit(main())

"""Continue search discovery AND extraction using the saved run options."""
from pathlib import Path
import argparse
from finance_agent.runtime import setup
setup(Path(__file__).resolve().parent)


def main(argv=None):
    argparse.ArgumentParser(description='恢复本项目暂停的采集：保留原运行方式，继续搜索/目录及正文断点').parse_args(argv)
    from crawler_control import read
    from simple_crawler import main as collect
    root=Path(__file__).resolve().parent
    record=read(root/'.runtime/control/run.json')
    config=Path(record.get('config_path',str(root/'config.yaml'))).resolve()
    if not config.is_relative_to(root):
        print('恢复配置路径超出项目，未启动采集');return 1
    print('恢复采集：继续已保存的搜索/目录及正文断点；不是仅处理队列的 --resume',flush=True)
    return collect(['--continue-paused','--config',str(config)])


if __name__=='__main__':raise SystemExit(main())

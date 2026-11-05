import argparse
import json
from pathlib import Path
from .core import ROOT,Lock,now,dump
from .config import load
from .provider import tavily_api_key
from .pipeline import Engine,import_document
from .llm import OllamaClient

def main(argv=None):
    parser=argparse.ArgumentParser(description="金融信息Agent：受控搜索、证据抽取、事件入库")
    parser.add_argument("--config",type=Path,default=ROOT/"config.yaml")
    parser.add_argument("--validate-config",action="store_true")
    parser.add_argument("--dry-run",action="store_true",help="只校验配置，不搜索、不调用模型")
    parser.add_argument("--doctor",action="store_true",help="检查本机Ollama与依赖")
    parser.add_argument("--plan-only",action="store_true",help="仅本机规划，不搜索")
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--stage",choices=["all","discover","process"],default="all")
    parser.add_argument("--extract-only",action="store_true",help="处理已有队列，不请求搜索服务")
    parser.add_argument("--import-document",type=Path,help="显式导入项目内合法保存的JSON正文")
    parser.add_argument("--export",action="store_true",help="从SQLite重建JSONL")
    args=parser.parse_args(argv)
    try:
        cfg=load(args.config)
        if args.validate_config or args.dry_run:
            print(json.dumps({"status":"configuration_valid","dry_run":True,
                "search_executed":False,"model_inference_executed":False,
                "tavily_key_present":bool(tavily_api_key(cfg.search)),
                "config":cfg.model_dump()},ensure_ascii=False,indent=2));return 0
        if args.doctor:
            import importlib.metadata as metadata
            client=OllamaClient(cfg.ollama,ROOT)
            try:model=client.check()
            finally:client.close()
            report={"checked_at":now(),"ollama":model,"tavily_key_present":bool(tavily_api_key(cfg.search)),
                    "packages":{x:metadata.version(x) for x in ["httpx","pydantic","crawl4ai","PyYAML"]},
                    "model_inference_executed":False}
            dump(ROOT/"logs/doctor.json",report)
            print(json.dumps(report,ensure_ascii=False,indent=2));return 0
        with Lock(ROOT):
            if cfg.collection.mode=="realtime" and not args.once and not (
                args.import_document or args.export or args.plan_only or args.extract_only):
                from .realtime import run
                return run(cfg,ROOT,args.stage)
            engine=Engine(cfg,ROOT)
            try:
                engine.repo.recover()
                if args.import_document:
                    doc=import_document(args.import_document,cfg,engine.repo,ROOT)
                    print(json.dumps({"imported":doc["candidate_id"],"method":doc["fetch_method"]},ensure_ascii=False))
                    if not args.extract_only:return 0
                if args.export:
                    from .query import export_events
                    print(json.dumps(export_events(engine.repo,ROOT/cfg.output.export_dir),ensure_ascii=False));return 0
                if args.plan_only:
                    from .pipeline import window
                    from .agents import plan_queries
                    start,end=window(cfg)
                    result=plan_queries(engine.llm,cfg,engine.repo,start,end)
                    dump(ROOT/"logs/local_planner_validation.json",
                         {"time":now(),"queries":result,"source_requests":0,"model_calls":engine.llm.calls})
                    print(json.dumps(result,ensure_ascii=False,indent=2));return 0
                discover=not args.extract_only and args.stage in {"all","discover"}
                process=args.extract_only or args.stage in {"all","process"}
                if discover:engine.provider.preflight()
                print(f"模式：{cfg.collection.mode}；网站范围：{cfg.search.website_mode or 'legacy'}；搜索：{discover}；处理队列：{process}；模型：{cfg.ollama.model}",flush=True)
                result=engine.cycle(discover,process)
                return 1 if result["status"]=="failed" else 2 if result["status"]=="partial" else 0
            finally:engine.close()
    except KeyboardInterrupt:
        print("已停止，队列和已校验缓存保留。");return 130
    except Exception as exc:
        print("运行失败："+str(exc));return 1

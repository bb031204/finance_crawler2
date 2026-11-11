import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import httpx
from finance_agent.core import digest,dump
from qwen_agent import Qwen,OutputError,LEGACY_COVERAGE_PROMPT,PLANNING_SCOPE_VERSION,COVERAGE_PROMPT
from history_discovery import history_context,VERSION
from test_simple_crawler import config
from test_broad_discovery import WideModel,WideSources


class RelaxedSearchTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_relaxed_search';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def model(self,rows,limit=80):
        self.requests=[]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            self.requests.append(json.loads(request.content))
            return httpx.Response(200,json={'message':{'content':json.dumps({'queries':rows},ensure_ascii=False)}})
        model=Qwen(config().ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)),query_limit=limit)
        self.addCleanup(model.close)
        return model
    def monitoring(self,markets=None):
        value=config().monitoring;value.markets=markets or ['US'];value.directions=['医疗服务','机械设备'];return value

    def test_us_coverage_accepts_chinese_english_and_mixed_queries_in_one_call(self):
        rows=[{'direction':'医疗服务','market':'US','query':text} for text in ['医疗服务 公司分红','Healthcare earnings','美国 healthcare 政策']]
        rows.append({'direction':'机械设备','market':'US','query':'机械订单增长'})
        model=self.model(rows)
        result=model.coverage_plan(self.monitoring(),['医疗服务','机械设备'],'2012-01-01','2022-05-25')
        self.assertEqual(len(result),3);self.assertEqual(model.calls,1)
        self.assertEqual({r['query'] for r in result},{r['query'] for r in rows[:2]+rows[-1:]})

    def test_cn_query_can_be_english(self):
        model=self.model([{'market':'CN','query':'bank earnings guidance'}])
        result=model.plan(self.monitoring(['CN']),1)
        self.assertEqual(result[0]['query'],'bank earnings guidance');self.assertEqual(model.calls,1)

    def test_group_caps_two_while_word_and_length_limits_stay_removed(self):
        rows=[{'direction':'医疗服务','market':'US','query':term} for term in ['医','healthcare '+('outlook '*40),'医疗 policy','医药公司经营','医疗设备更新']]
        model=self.model(rows)
        result=model.coverage_plan(self.monitoring(),['医疗服务'],'2012-01-01','2022-05-25')
        self.assertEqual(len(result),2);self.assertGreater(len(result[1]['query']),220);self.assertEqual(model.calls,1)

    def test_valid_partial_group_is_retained_for_missing_task_fallback(self):
        model=self.model([{'direction':'医疗服务','market':'US','query':'医疗服务 公告'}])
        self.assertEqual(len(model.coverage_plan(self.monitoring(),['医疗服务','机械设备'],'2012-01-01','2022-05-25')),1)
        self.assertEqual(model.calls,3)

    def test_duplicate_queries_are_removed_without_retry(self):
        rows=[{'direction':'医疗服务','market':'US','query':term} for term in ['Healthcare earnings',' Healthcare   earnings ','Healthcare outlook']]
        model=self.model(rows)
        result=model.coverage_plan(self.monitoring(),['医疗服务'],'2012-01-01','2022-05-25')
        self.assertEqual(len(result),2);self.assertEqual(model.calls,1)

    def test_config_limit_bounds_retained_queries_fairly_without_retry(self):
        rows=[{'direction':'医疗服务','market':'US','query':term} for term in ['医疗服务 公告','医疗服务 分红','医疗服务 政策']]
        rows.append({'direction':'机械设备','market':'US','query':'机械订单'})
        model=self.model(rows,limit=2)
        result=model.coverage_plan(self.monitoring(),['医疗服务','机械设备'],'2012-01-01','2022-05-25')
        self.assertEqual(len(result),2);self.assertEqual({r['direction'] for r in result},{'医疗服务','机械设备'})
        self.assertEqual(model.calls,1)

    def test_focused_plan_can_exceed_old_eight_query_limit_but_respects_config(self):
        rows=[{'market':'US','query':'行业 '+str(index)} for index in range(12)]
        model=self.model(rows)
        self.assertEqual(len(model.plan(self.monitoring(),10)),10);self.assertEqual(model.calls,1)

    def test_unconfigured_market_is_still_rejected(self):
        model=self.model([{'market':'CN','query':'银行 经营'}])
        with self.assertRaises(OutputError):model.plan(self.monitoring(),1)

    def test_unconfigured_direction_is_still_rejected(self):
        model=self.model([{'direction':'无关方向','market':'US','query':'医疗 公司'}])
        with self.assertRaises(OutputError):model.coverage_plan(self.monitoring(),['医疗服务'],'2012-01-01','2022-05-25')

    def test_whitespace_only_query_is_still_rejected(self):
        model=self.model([{'market':'US','query':'   '}])
        with self.assertRaises(OutputError):model.plan(self.monitoring(),1)

    def test_historical_checkpoint_migrates_exactly_without_replaying_done_or_active_stage(self):
        from simple_crawler import Crawler
        from test_broad_discovery import WideSources
        cfg=config();cfg.monitoring.directions=['银行业'];cfg.search.strategy='broad'
        crawler=Crawler(cfg,self.root,WideSources(cfg,empty=True),WideModel())
        start,end=crawler.window()
        inputs=[VERSION,cfg.monitoring.model_dump(),start,end,cfg.search.model_dump(exclude={'complete_history','discovery_mode','sec_edgar'})]
        oldkey=digest(inputs+[LEGACY_COVERAGE_PROMPT])
        old={'version':VERSION,'cursor':1,'done':{'0':{'at':'fixture'}},'failed':{},'plans':{'0':[{'direction':'银行业','market':'CN','query':'银行 经营'}]},
            'year_stats':{'2026':{'searches':1}},'active':{'index':1,'stage':'undated'}}
        crawler.state['discovery']={oldkey:deepcopy(old)}
        scope,jobs,progress=history_context(crawler,start,end)
        self.assertEqual(scope,digest(inputs+[PLANNING_SCOPE_VERSION]))
        for key,value in old.items():self.assertEqual(progress[key],value)
        self.assertEqual(crawler.state['discovery'][oldkey],old)
        self.assertEqual(progress['migrated_from_scope'],oldkey)
        with patch('history_discovery.COVERAGE_PROMPT','wording changed later'):
            self.assertEqual(history_context(crawler,start,end)[0],scope)

    def test_production_model_receives_config_query_budget(self):
        from simple_crawler import Crawler
        from test_simple_crawler import FakeSources
        cfg=config();cfg.collection.max_queries_per_cycle=37
        crawler=Crawler(cfg,self.root,FakeSources())
        try:self.assertEqual(crawler.model.query_limit,37)
        finally:crawler.close()

    def partial_crawler(self,realtime=False):
        from simple_crawler import Crawler
        from pydantic import SecretStr
        class Partial(WideModel):
            def coverage_plan(self,monitoring,directions,*args):
                self.calls+=1
                return [{'direction':directions[0],'market':monitoring.markets[0],'query':'保留已有 mixed query'}]
            def plan(self,monitoring,*args):
                self.calls+=1
                return [{'market':monitoring.markets[0],'query':monitoring.directions[0]+' 单独补齐'}]
        cfg=config();cfg.monitoring.directions=['银行业','半导体'];cfg.collection.max_queries_per_cycle=2
        cfg.search.strategy='broad';cfg.search.undated_fallback=False;cfg.search.api_key=SecretStr('offline')
        if realtime:
            cfg.collection.mode='realtime';cfg.collection.start_date=cfg.collection.end_date=None
        else:
            cfg.collection.start_date='2016-01-01';cfg.collection.end_date='2016-12-31';cfg.search.date_window_days=0
        return Crawler(cfg,self.root,WideSources(cfg,empty=True),Partial())

    def test_historical_partial_plan_only_replans_missing_task(self):
        crawler=self.partial_crawler()
        result=crawler.collect()
        self.assertEqual(result['searched'],2);self.assertEqual(crawler.model.calls,2)
        self.assertEqual({row['direction'] for row in result['queries']},{'银行业','半导体'})
        self.assertTrue(any(row['query'].startswith('保留已有') for row in result['queries']))

    def test_realtime_partial_plan_only_replans_missing_task(self):
        crawler=self.partial_crawler(realtime=True)
        result=crawler.collect()
        self.assertEqual(result['searched'],2);self.assertEqual(crawler.model.calls,2)
        self.assertEqual({row['direction'] for row in result['queries']},{'银行业','半导体'})

    def test_realtime_legacy_cursor_is_preserved(self):
        from discovery import date_windows
        crawler=self.partial_crawler(realtime=True);cfg=crawler.cfg;start,end=crawler.window()
        inputs=[cfg.monitoring.model_dump(),cfg.monitoring.resolved_directions,start,end,cfg.search.model_dump(exclude={'discovery_mode','sec_edgar'})]
        oldkey=digest(inputs+[LEGACY_COVERAGE_PROMPT]);crawler.state['discovery']={oldkey:{'cursor':3}}
        result=crawler.collect()
        self.assertEqual(result['discovery']['cursor_start'],3)
        self.assertEqual(crawler.state['discovery'][oldkey]['cursor'],3)


if __name__=='__main__':unittest.main()

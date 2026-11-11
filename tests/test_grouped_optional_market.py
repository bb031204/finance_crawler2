import json
import tempfile
import unittest
from pathlib import Path
import httpx
from qwen_agent import Qwen,OutputError,ContentNote
from simple_crawler import Crawler
from test_simple_crawler import config,FakeSources,FakeQwen,candidate
from analysis_metadata import public_analysis
from security_grounding import validate_summary_securities
from search_topics import ALL_DIRECTIONS,ENGLISH_DIRECTION_HINTS


class GroupedOptionalMarketTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_grouped_optional';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def model(self,reply):
        self.requests=[]
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'qwen3:8b','digest':'offline'}]})
            body=json.loads(request.content);self.requests.append(body)
            value=reply(body) if callable(reply) else reply
            return httpx.Response(200,json={'message':{'content':json.dumps(value,ensure_ascii=False)}})
        model=Qwen(config().ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)),query_limit=80)
        self.addCleanup(model.close);return model
    def monitoring(self):
        monitor=config().monitoring;monitor.markets=['US'];monitor.directions=['医疗服务','机械设备'];return monitor

    def test_schema_caps_each_group_and_supplies_english_sector_hints(self):
        model=self.model({'groups':[{'direction':d,'market':'US','queries':[d+' 公告','industry earnings']} for d in self.monitoring().directions]})
        rows=model.coverage_plan(self.monitoring(),self.monitoring().directions,'2012-01-01','2022-05-25')
        self.assertEqual(len(rows),4);self.assertEqual(model.calls,1)
        definition=self.requests[0]['format']['$defs']['CoverageGroup']
        self.assertEqual(definition['properties']['queries']['maxItems'],2)
        tasks=json.loads(self.requests[0]['messages'][1]['content'])['tasks']
        self.assertEqual(tasks[0]['english_hint'],'Healthcare services');self.assertEqual(tasks[1]['english_hint'],'Machinery')

    def test_only_invalid_group_is_repaired_and_valid_group_is_cached(self):
        def reply(body):
            payload=json.loads(body['messages'][1]['content'])
            if 'task' in payload:
                self.assertEqual(payload['task']['direction'],'机械设备')
                return {'queries':['Machinery orders','机械设备 盈利']}
            return {'groups':[{'direction':'医疗服务','market':'US','queries':['医疗服务 保留原查询','Healthcare policy']},
                {'direction':'机械设备','market':'US','queries':None}]}
        model=self.model(reply)
        rows=model.coverage_plan(self.monitoring(),self.monitoring().directions,'2012-01-01','2022-05-25')
        self.assertEqual(model.calls,2);self.assertEqual(len(rows),4)
        self.assertEqual([r['query'] for r in rows if r['direction']=='医疗服务'],['医疗服务 保留原查询','Healthcare policy'])
        self.assertEqual(model.coverage_plan(self.monitoring(),self.monitoring().directions,'2012-01-01','2022-05-25'),rows)
        self.assertEqual(model.calls,2);self.assertEqual(model.cache_hits,2)

    def test_bad_item_does_not_discard_the_good_query(self):
        model=self.model({'groups':[{'direction':'医疗服务','market':'US','queries':['有效 医疗查询',17]}]})
        rows=model.coverage_plan(self.monitoring(),['医疗服务'],'2012-01-01','2022-05-25')
        self.assertEqual([r['query'] for r in rows],['有效 医疗查询']);self.assertEqual(model.calls,1)

    def test_overflow_or_duplicate_groups_are_locally_limited_to_two(self):
        model=self.model({'groups':[{'direction':'医疗服务','market':'US','queries':['医疗 收入','医疗 政策','医疗 订单']},
            {'direction':'医疗服务','market':'US','queries':['医疗 财报']}]})
        rows=model.coverage_plan(self.monitoring(),['医疗服务'],'2012-01-01','2022-05-25')
        self.assertEqual([r['query'] for r in rows],['医疗 收入','医疗 政策']);self.assertEqual(model.calls,1)

    def test_all_builtin_directions_have_english_hints(self):
        self.assertTrue(set(ALL_DIRECTIONS)<=set(ENGLISH_DIRECTION_HINTS))
        self.assertTrue(all(ENGLISH_DIRECTION_HINTS[d].isascii() for d in ALL_DIRECTIONS))

    def test_no_ticker_company_article_is_saved_with_empty_public_stock_field(self):
        class Model(FakeQwen):
            def summarize(self,*args):
                return dict(super().summarize(*args),securities=[{'name':'Example Company','ticker':None,'exchange':None,'quote':'Example Company'}],
                    content='Example Company reported growing demand and discussed expansion plans.',keywords=['Example Company','demand'],
                    industries=[{'name':'Consumer services','basis':'inferred','quote':'Example Company'}],impact_scope=['company','industry'])
        crawler=Crawler(config(),self.root,FakeSources(),Model());crawler.queue(candidate())
        report=crawler.collect(resume=True)
        self.assertEqual(report['saved'],1)
        row=next(iter(crawler.dataset.rows.values()))[1]
        self.assertEqual(row['股票实体'],[]);self.assertIn('Example Company',row['主要内容'])
        self.assertEqual(row['涉及行业'][0]['名称'],'Consumer services');self.assertEqual(row['影响范围'],['company','industry'])

    def content_reply(self,content,securities=None):
        return dict(useful=True,market='US',category='Earnings',industry='Retail',securities=securities or [],
            industries=[],impact_scope=['company'],keywords=['Home Depot','DIY'],content=content,reason='financial operating facts')

    def test_guessed_hd_is_cleaned_but_diy_and_article_survive_without_model_retry(self):
        def reply(body):
            if 'time' in body['format']['properties']:
                return {'time':'2022-05-17','date_quote':'datePublished=2022-05-17','date_kind':'published'}
            result=self.content_reply('Home Depot (HD) reported quarterly revenue growth as DIY spending remained strong.',
                [{'name':'Home Depot','ticker':'HD','exchange':None,'quote':'Home Depot'}])
            result['keywords'].append('HD');return result
        model=self.model(reply)
        note=model.summarize({'url':'https://example.com/article','title':'Home Depot quarterly earnings','origin_market':'US',
            'date_context':'datePublished=2022-05-17','text':'Home Depot reported quarterly revenue growth as home improvement spending remained strong.'},self.monitoring())
        self.assertEqual(model.calls,2);self.assertTrue(note['useful']);self.assertIn('DIY',note['content'])
        self.assertNotIn('(HD)',note['content']);self.assertNotIn('HD',note['keywords']);self.assertIn('DIY',note['keywords'])
        self.assertEqual(public_analysis(note)['股票实体'],[]);self.assertTrue(note['security_cleanup'])

    def test_institution_and_sector_acronyms_are_not_stock_gates(self):
        note={'securities':[],'content':'The IMF discussed growth and DIY retail demand.','keywords':['IMF','DIY','growth']}
        self.assertEqual(validate_summary_securities(note,'The International Monetary Fund discussed growth and do-it-yourself retail demand.')['content'],note['content'])
        self.assertEqual(public_analysis(note)['股票实体'],[])

    def test_non_whitelisted_initialism_can_be_grounded_in_the_full_name(self):
        note={'securities':[],'content':'NATO discussed the policy outlook.','keywords':['NATO']}
        result=validate_summary_securities(note,'The North Atlantic Treaty Organization discussed the policy outlook.')
        self.assertEqual(result['keywords'],['NATO'])

    def test_optional_bad_code_cannot_remove_otherwise_valid_market_article(self):
        note={'securities':[{'name':'Example Company','ticker':'GUESS'}],'content':'Example Company reported stronger demand.',
            'keywords':['GUESS'],'category':'Operating update','industry':'Retail'}
        result=validate_summary_securities(note,'Example Company reported stronger demand.')
        self.assertTrue(result['keywords']);self.assertNotIn('GUESS',result['keywords'])

    def test_publication_dateline_is_not_tainted_by_another_modified_block(self):
        dateline='NEW YORK, May 16, 2022 /PRNewswire/ --'
        def reply(body):
            if 'time' in body['format']['properties']:
                return {'time':'2022-05-16','date_quote':dateline,'date_kind':'published'}
            return self.content_reply('Cosnova Beauty reported 15 percent sales growth and stronger online sales.')
        model=self.model(reply)
        note=model.summarize({'url':'https://example.com/article','title':'Cosnova Beauty quarterly results','origin_market':'US',
            'date_context':'page.datePublished=2022-05-16T14:00:00+01:00\npage.dateModified=2022-05-16T14:01:56+01:00',
            'text':dateline+' Cosnova Beauty reported strong operating results and online sales growth.'},self.monitoring())
        self.assertEqual(note['time'],'2022-05-16');self.assertEqual(model.calls,2)

    def test_modified_only_evidence_is_still_rejected(self):
        model=self.model({'time':'2022-05-16','date_quote':'2022-05-16','date_kind':'published'})
        with self.assertRaises(OutputError):
            model.summarize({'url':'https://example.com/article','title':'A company article','origin_market':'US',
                'date_context':'dateModified=2022-05-16','text':'Last Modified Date: 2022-05-16. The company reported operating results.'},self.monitoring())
        self.assertEqual(model.calls,2)


if __name__=='__main__':unittest.main()

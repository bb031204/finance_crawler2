import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
import httpx
from qwen_agent import Qwen,ContentNote,READ_PROMPT,MERGE_PROMPT,OutputError,tokens,output_repair_hint
from summary_quality import validate_summary_quality
from analysis_metadata import public_analysis,grounded_industries
from test_simple_crawler import config
from test_preflight_pipeline import OfflineQwen,content


class OutputBudgetRecoveryTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_output_budget'
        parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
        self.cfg=config();self.cfg.ollama.num_predict=1400
    def tearDown(self):self.tmp.cleanup()

    def model(self,handler):
        requests=[]
        def respond(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':self.cfg.ollama.model,'digest':'offline'}]})
            payload=json.loads(request.content);requests.append(payload)
            return httpx.Response(200,json=handler(payload,len(requests)))
        client=httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(respond))
        return Qwen(self.cfg.ollama,self.root,client),requests

    def test_truncation_retry_names_current_limits_and_factual_obligations(self):
        def response(payload,attempt):
            if attempt==1:return {'done_reason':'length','eval_count':1400,'prompt_eval_count':900,'message':{'content':'{"content":"unfinished'}}
            return {'done_reason':'stop','message':{'content':json.dumps(content())}}
        model,requests=self.model(response)
        try:
            result=model.ask('正文整理',READ_PROMPT,{'text':'Original financial facts.','output_language':'en'},ContentNote)
            retry=requests[1]['messages'][-1]['content']
            for value in ['200至350','120至190','时间/报告期','公司/股票代码','具体行业','逐字','JSON']:
                self.assertIn(value,retry)
            self.assertEqual(len(requests),2);self.assertTrue(result['useful'])
            self.assertTrue(all(p['options']['num_predict']==1400 and p['options']['num_ctx']==4096 for p in requests))
            self.assertEqual(requests[1]['format']['properties']['securities']['maxItems'],32)
        finally:model.close()

    def test_repeated_truncation_is_typed_and_never_cached_as_success(self):
        model,requests=self.model(lambda *_:{'done_reason':'length','message':{'content':'{"content":'}})
        try:
            with self.assertRaises(OutputError) as error:model.ask('正文整理',READ_PROMPT,{'text':'Facts'},ContentNote)
            self.assertEqual(error.exception.code,'output_length');self.assertEqual(len(requests),2)
            self.assertFalse(list((self.root/'.runtime/simple/model_cache').glob('*.json')))
        finally:model.close()

    def test_long_body_repair_keeps_the_existing_word_limit(self):
        def response(payload,attempt):
            note=content(' '.join(['Operating']*210)) if attempt==1 else content()
            return {'done_reason':'stop','message':{'content':json.dumps(note)}}
        model,requests=self.model(response)
        def check(note):validate_summary_quality(note['content'],'en');return note
        try:
            note=model.ask('正文整理',READ_PROMPT,{'text':'Financial report','output_language':'en'},ContentNote,check)
            self.assertLess(len(note['content'].split()),200)
            self.assertIn('主要内容偏长',requests[1]['messages'][-1]['content'])
            self.assertIn('120至190',requests[1]['messages'][-1]['content'])
        finally:model.close()

    def test_schema_body_character_overflow_is_typed_as_output_length(self):
        def response(*_):return {'done_reason':'stop','message':{'content':json.dumps(content('x'*5001))}}
        model,requests=self.model(response)
        try:
            with self.assertRaises(OutputError) as error:model.ask('正文整理',READ_PROMPT,{'text':'Facts'},ContentNote)
            self.assertEqual(error.exception.code,'output_length');self.assertEqual(len(requests),2)
        finally:model.close()

    def test_merge_preserves_verified_metadata_without_regenerating_quote_lists(self):
        model,requests=self.model(lambda *_:{'done_reason':'stop','message':{'content':json.dumps(content())}})
        entity=dict(name='Example Bank',ticker='EXB',exchange='NYSE',quote='Example Bank (NYSE: EXB)')
        sector=dict(name='Regional banking',basis='explicit',quote='Regional banking')
        group=[dict(content(),securities=[entity],industries=[sector],impact_scope=['company']),
            dict(content(),securities=[entity],industries=[sector],impact_scope=['industry'])]
        original=copy.deepcopy(group)
        try:
            result=model.merge_pair(group,{'origin_market':'US','output_language':'en','date_context':'Published 2021-03-31'},lambda note:note)
            self.assertEqual(result['securities'],[entity]);self.assertEqual(result['industries'],[sector])
            self.assertEqual(result['impact_scope'],['company','industry'])
            for field in ['securities','industries','impact_scope']:
                self.assertEqual(requests[0]['format']['properties'][field]['maxItems'],0)
            self.assertEqual(group,original)
            model.ask('正文整理',READ_PROMPT,{'text':'Financial facts'},ContentNote)
            self.assertEqual(requests[1]['format']['properties']['securities']['maxItems'],32)
        finally:model.close()

    def test_dense_and_normal_blocks_are_lossless_and_bounded(self):
        model=OfflineQwen(self.cfg.ollama,self.root)
        context={'output_language':'en','origin_market':'US','title':'Example Bank (EXB)','date_context':'Published March 31, 2021'}
        dense='Example Bank (NYSE: EXB), Regional banking.\n'+('2021 2020 2019 100 90 80 10 9 8 2.5 2.4 2.3\n'*180)
        normal=('Example Bank reported revenue growth during the first quarter of 2021. '*150)
        for text,limit in [(dense,601),(normal,901)]:
            chunks=model.read_chunks(text,context)
            self.assertEqual(''.join(chunks),text);self.assertGreater(len(chunks),1)
            self.assertTrue(all(tokens(part)<=limit for part in chunks))
            hint=output_repair_hint(ContentNote,{},'模型输出被截断')
            for part in chunks:
                user=json.dumps(dict(context,text=part),ensure_ascii=False)+'\n修正：'+'错'*80+'；'+hint
                cost=tokens(READ_PROMPT+'\n/no_think')+tokens(user)+1400+350
                self.assertLessEqual(cost,4096)

    def prepared(self,model,source):
        quote='Published March 31, 2021 at 18:45'
        page={'url':'https://example.com/report','origin_market':'US','title':'Original report','text':source}
        publication={'time':'2021-03-31T18:45:00-04:00','date_quote':quote,'date_kind':'published'}
        return dict(status='ready',page=page,publication=publication,
            context=model.read_context(page,self.cfg.monitoring,quote),chunks=[source])

    def test_adaptive_split_preserves_date_two_companies_codes_and_specific_sectors(self):
        source=('Example Bank (NYSE: EXB) operates in Regional banking. Reporting period: 2021Q1.\n'+
            'Revenue increased and credit losses fell. '*22+'\n\n'+
            'Example Chip (NASDAQ: EXCP) operates in Semiconductor equipment. Reporting period: 2021Q1.\n'+
            'Revenue increased and orders increased. '*22)
        class Adaptive(OfflineQwen):
            failed_parents=0
            def ask(self,purpose,prompt,payload,schema,validate=None,**kwargs):
                self.calls+=1
                if purpose=='正文整理' and payload['text']==source:
                    self.failed_parents+=1;raise OutputError('连续超长',code='output_length')
                note=content('The two companies reported revenue growth in 2021Q1.')
                note.update(market='US',securities=[],industries=[],impact_scope=['company'])
                if purpose=='正文整理':
                    text=payload['text']
                    if 'EXB' in text:name,code,exchange,sector='Example Bank','EXB','NYSE','Regional banking'
                    else:name,code,exchange,sector='Example Chip','EXCP','NASDAQ','Semiconductor equipment'
                    note.update(content=f'{name} ({exchange}: {code}) reported revenue growth in 2021Q1 in {sector}.',
                        keywords=[name,code,sector],securities=[dict(name=name,ticker=code,exchange=exchange,quote=f'{name} ({exchange}: {code})')],
                        industries=[dict(name=sector,basis='explicit',quote=sector)])
                return validate(note) if validate else note
        model=Adaptive(self.cfg.ollama,self.root);prepared=self.prepared(model,source)
        original=copy.deepcopy(prepared)
        for _ in range(2):
            note=model.summarize(prepared['page'],self.cfg.monitoring,prepared)
            self.assertEqual(note['time'],'2021-03-31T18:45:00-04:00')
            self.assertEqual(note['date_quote'],original['publication']['date_quote'])
            metadata=public_analysis(note)
            self.assertEqual({e['股票代码'] for e in metadata['股票实体']},{'EXB','EXCP'})
            self.assertEqual({e['名称'] for e in metadata['涉及行业']},{'Regional banking','Semiconductor equipment'})
            self.assertIn('EXB',note['content']);self.assertIn('EXCP',note['content'])
        self.assertEqual(model.failed_parents,1)
        self.assertEqual(prepared,original)

    def test_fact_failure_does_not_trigger_length_splitting(self):
        class Invalid(OfflineQwen):
            def ask(self,*args,**kwargs):self.calls+=1;raise OutputError('证券证据不符')
        model=Invalid(self.cfg.ollama,self.root);prepared=self.prepared(model,'Original financial facts. '*100)
        with self.assertRaises(OutputError):model.summarize(prepared['page'],self.cfg.monitoring,prepared)
        self.assertEqual(model.calls,1);self.assertFalse(model._oversize_chunks)

    def test_split_depth_is_bounded_and_never_discards_remaining_text(self):
        class AlwaysLong(OfflineQwen):
            def ask(self,*args,**kwargs):self.calls+=1;raise OutputError('连续超长',code='output_length')
        model=AlwaysLong(self.cfg.ollama,self.root);prepared=self.prepared(model,'Financial facts in the report. '*1500)
        with self.assertRaises(OutputError):model.summarize(prepared['page'],self.cfg.monitoring,prepared)
        self.assertEqual(model.calls,3)
        self.assertEqual(len(prepared['chunks']),1)

    def test_date_only_model_output_retains_explicit_quoted_publication_instant(self):
        quote='page.datePublished=2018-10-02T13:30:00+01:00'
        model=OfflineQwen(self.cfg.ollama,self.root,'2018-10-02',quote)
        page={'url':'https://example.com/main','title':'Financial news','origin_market':'US',
            'text':'Original financial report. '*10,'date_context':quote}
        result=model.publication(page)
        self.assertEqual(result['time'],'2018-10-02T13:30:00+01:00')
        self.assertEqual(result['date_quote'],quote)

    def test_date_only_source_never_gets_an_invented_clock(self):
        quote='datePublished=2018-10-02'
        model=OfflineQwen(self.cfg.ollama,self.root,'2018-10-02',quote)
        page={'url':'https://example.com/main','title':'Financial news','origin_market':'US',
            'text':'Original financial report. '*10,'date_context':quote}
        self.assertEqual(model.publication(page)['time'],'2018-10-02')

    def test_conflicting_quoted_instants_are_not_silently_chosen(self):
        quote='datePublished=2018-10-02T13:30:00+01:00\ndatePublished=2018-10-02T14:30:00+01:00'
        model=OfflineQwen(self.cfg.ollama,self.root,'2018-10-02',quote)
        page={'url':'https://example.com/main','title':'Financial news','origin_market':'US',
            'text':'Original financial report. '*10,'date_context':quote}
        with self.assertRaisesRegex(ValueError,'多个不同时刻'):model.publication(page)

    def test_exact_sector_survives_a_bad_long_model_quote(self):
        source='Example Chip operates in semiconductor equipment.'
        result=grounded_industries([dict(name='Semiconductor Equipment',basis='explicit',quote='A fabricated long quote.')],source)
        self.assertEqual(result,[dict(name='Semiconductor Equipment',basis='explicit',quote='semiconductor equipment')])

    def test_industry_recovery_does_not_invent_or_match_partial_words(self):
        for name in ['Coal Mining','bank']:
            self.assertEqual(grounded_industries([dict(name=name,basis='explicit',quote='fabricated')],
                'Semiconductor equipment faces bankruptcy risks.'),[])


if __name__=='__main__':unittest.main()

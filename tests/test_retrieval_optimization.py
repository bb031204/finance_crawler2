"""Efficiency regressions keep dates, entities and evidence checks authoritative."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from finance_agent.core import digest,dump
from collection_scheduling import fair_pending,set_retry_guard,retry_unchanged
from market_relations import EXTRACTION_DEPENDENCIES,extraction_fingerprint
from simple_crawler import Crawler,control_config_digest
from history_discovery import historical_jobs,history_context,broad_historical_discover
from qwen_agent import Qwen,ContentNote,OutputError,READ_PROMPT,MERGE_PROMPT,tokens,output_repair_hint
from record_files import source_id
from test_simple_crawler import config,candidate,FakeSources,FakeQwen
from test_preflight_pipeline import OfflineQwen,PageSources,content


class RetrievalOptimizationTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_retrieval_optimization'
        parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name);self.cfg=config()
    def tearDown(self):self.tmp.cleanup()

    def crawler(self,sources=None,model=None):
        return Crawler(self.cfg,self.root,sources or FakeSources(),model or FakeQwen())

    def test_years_rotate_before_a_large_sec_bucket_can_drain(self):
        pending={}
        for year in [2016,2017,2018]:
            for n in range(10):
                item=dict(candidate(str(year)+'-'+str(n)),search_year=year,
                    sec_filing={'form':'10-K','filing_date':str(year)+'-02-01'})
                pending[source_id(item['url'])]=item
        order=list(fair_pending(pending))
        self.assertEqual([c['search_year'] for _,c,_ in order[:3]],[2016,2017,2018])
        self.assertEqual(len({i for i,_,_ in order}),30)

    def test_short_and_long_sources_both_receive_a_turn_across_batches(self):
        self.cfg.collection.max_documents_per_cycle=1
        c=self.crawler()
        for n in range(5):c.queue(dict(candidate('long'+str(n)),sec_filing={'form':'10-K','filing_date':'2026-11-02'}))
        c.queue(dict(candidate('short'),search_year=2026))
        one=c.run(resume=True);two=c.run(resume=True)
        self.assertEqual(one['attempted_ids'],[source_id(candidate('short')['url'])])
        self.assertEqual(two['attempted_ids'],[source_id(candidate('long0')['url'])])
        c.close()

    def test_schedule_cursor_survives_a_restart(self):
        c=self.crawler();self.cfg.collection.max_documents_per_cycle=1
        for year in [2016,2017]:
            for n in range(2):c.queue(dict(candidate(str(year)+str(n)),search_year=year))
        first=c.run(resume=True);c.close()
        restored=self.crawler();second=restored.run(resume=True)
        self.assertEqual(first['attempted_ids'],[source_id(candidate('20160')['url'])])
        self.assertEqual(second['attempted_ids'],[source_id(candidate('20170')['url'])])
        restored.close()

    def test_unchanged_oversize_record_waits_without_downloading_or_qwen(self):
        c=self.crawler(sources=PageSources(body='Example Bank reported financial operating changes. '*80))
        self.cfg.fetch.max_text_chars=1000;c.queue(candidate())
        one=c.run(resume=True);fetches=c.sources.fetched
        two=c.collect(resume=True)
        self.assertEqual(one['saved'],0);self.assertEqual(two['considered'],0)
        self.assertEqual(c.sources.fetched,fetches);self.assertEqual(two['model_calls'],0)
        self.assertEqual(two['summary']['retry_waiting_records'],1)
        self.assertEqual(two['summary']['unattempted_records'],0)
        self.assertEqual(len(c.state['pending']),1);c.close()

    def test_changed_length_policy_unlocks_same_pending_record(self):
        c=self.crawler(sources=PageSources(body='Example Bank operating facts. '*100))
        self.cfg.fetch.max_text_chars=1000;c.queue(candidate());c.run(resume=True)
        self.cfg.fetch.max_text_chars=10000
        result=c.run(resume=True)
        self.assertEqual(result['saved'],1);self.assertFalse(c.state['pending']);c.close()

    def test_model_error_retries_only_after_semantic_policy_changes(self):
        class Model(FakeQwen):
            broken=True
            def summarize(self,*args,**kw):
                if self.broken:self.calls+=1;raise OutputError('数字证据不匹配')
                return super().summarize(*args,**kw)
        model=Model();c=self.crawler(model=model);c.queue(candidate());c.run(resume=True)
        model.broken=False
        held=c.run(resume=True)
        self.assertEqual(held['considered'],0)
        self.cfg.ollama.num_predict+=1
        good=c.run(resume=True);self.assertEqual(good['saved'],1)
        self.assertEqual(c.sources.fetched,1);c.close()

    def test_force_can_retry_guarded_record_without_erasing_failure_history(self):
        c=self.crawler(sources=PageSources(body='Example Bank financial facts. '*100))
        self.cfg.fetch.max_text_chars=1000;c.queue(candidate());c.run(resume=True)
        retried=c.run(resume=True,force=True)
        self.assertEqual(retried['considered'],1);self.assertEqual(retried['deferred_stopped'],1)
        held=next(iter(c.state['deferred'].values()))
        self.assertEqual(held['failure_attempts'],2);self.assertTrue(held['retry_history']);c.close()

    def test_guarded_deferred_record_reactivates_when_limit_changes(self):
        c=self.crawler(sources=PageSources(body='Example Bank financial facts. '*100))
        self.cfg.fetch.max_text_chars=1000;c.queue(candidate())
        c.run(resume=True);c.run(resume=True,force=True)
        self.cfg.fetch.max_text_chars=10000
        report=c.run(resume=True)
        self.assertEqual(report['retry_reactivated'],1);self.assertEqual(report['saved'],1)
        self.assertFalse(c.state['deferred']);c.close()

    def test_ttl_cache_eviction_does_not_unlock_unchanged_model_failure(self):
        item=candidate();path=self.root/'.runtime/simple/pages'/(source_id(item['url'])+'.json')
        dump(path,dict(text='Original financial facts',url=item['url']))
        item['page_path']=str(path.relative_to(self.root));model=FakeQwen()
        set_retry_guard(self.root,item,self.cfg,model,'model_error',ValueError('invalid'))
        path.unlink()
        self.assertTrue(retry_unchanged(self.root,item,self.cfg,model))

    def test_actual_cache_replacement_unlocks_retry(self):
        item=candidate();path=self.root/'.runtime/simple/pages'/(source_id(item['url'])+'.json')
        dump(path,dict(text='Original financial facts',url=item['url']))
        item['page_path']=str(path.relative_to(self.root));model=FakeQwen()
        set_retry_guard(self.root,item,self.cfg,model,'model_error',ValueError('invalid'))
        dump(path,dict(text='Changed company report with additional evidence',url=item['url']))
        self.assertFalse(retry_unchanged(self.root,item,self.cfg,model))

    def test_unrelated_script_changes_do_not_change_extraction_fingerprint(self):
        for name in EXTRACTION_DEPENDENCIES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('semantic code')
        first=extraction_fingerprint(self.root)
        (self.root/'pause_crawler.py').write_text('changed pause UI')
        (self.root/'resume_crawler.py').write_text('changed resume UI')
        extraction_fingerprint.cache_clear()
        self.assertEqual(first,extraction_fingerprint(self.root))
        (self.root/'numeric_qualifiers.py').write_text('new numeric validation')
        extraction_fingerprint.cache_clear()
        self.assertNotEqual(first,extraction_fingerprint(self.root))

    def test_full_code_audit_change_reuses_validated_semantic_cache(self):
        calls=[]
        def respond(request):
            calls.append(1);return httpx.Response(200,json={'message':{'content':json.dumps(content())},'prompt_eval_count':50,'eval_count':40})
        model=Qwen(self.cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(respond)))
        model.model_digest='offline-model'
        payload={'text':'Original financial facts','source_id':'synthetic'}
        model.ask('正文整理',READ_PROMPT,payload,ContentNote)
        stamp=model.inferred_by_source['synthetic'];model.code_version='unrelated-cli-change'
        model.ask('正文整理',READ_PROMPT,payload,ContentNote)
        self.assertEqual(len(calls),1);self.assertEqual(model.cache_hits,1)
        self.assertEqual(model.inferred_by_source['synthetic'],stamp)
        self.assertEqual(model.usage['prompt_tokens'],50)
        model.extraction_fingerprint='numeric-rules-changed'
        model.ask('正文整理',READ_PROMPT,payload,ContentNote)
        self.assertEqual(len(calls),2);model.close()

    def test_every_cache_hit_still_passes_current_validator(self):
        calls=[]
        def respond(request):
            calls.append(1);return httpx.Response(200,json={'message':{'content':json.dumps(content())}})
        model=Qwen(self.cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(respond)))
        model.model_digest='offline-model';payload={'text':'Original facts'}
        model.ask('正文整理',READ_PROMPT,payload,ContentNote)
        def reject(note):raise ValueError('changed evidence validation rejects this result')
        with self.assertRaises(OutputError):model.ask('正文整理',READ_PROMPT,payload,ContentNote,reject)
        self.assertEqual(model.cache_hits,0);self.assertEqual(len(calls),3);model.close()

    def test_multi_note_merge_is_bounded_and_preserves_all_verified_entities(self):
        class MergeModel(OfflineQwen):
            def ask(self,purpose,prompt,payload,schema,validate=None,**kwargs):
                note=content(' '.join(name+' reported operating results.' for name in payload.get('required_subjects',[])))
                return validate(note) if validate else note
        self.cfg.ollama.num_predict=1400;model=MergeModel(self.cfg.ollama,self.root)
        notes=[dict(content(),securities=[dict(name=name,ticker=None,exchange=None,quote=name)],
                    industries=[dict(name='Regional banking',basis='explicit',quote='Regional banking')])
               for name in ['Example Bank','Other Bank','Third Bank','Fourth Bank']]
        context={'origin_market':'US','output_language':'en','date_context':'Published November 2, 2026'}
        groups=list(model.merge_groups(notes,context))
        self.assertEqual(len(groups),1)
        reserve=tokens(output_repair_hint(ContentNote,{},'模型输出被截断'))+200
        self.assertLessEqual(model.request_cost(MERGE_PROMPT,model.merge_payload(context,groups[0]))+reserve,4096)
        result=model.merge_pair(groups[0],context,lambda n:n)
        self.assertEqual({x['name'] for x in result['securities']},{x['securities'][0]['name'] for x in notes})
        self.assertTrue(result['industries']);model.close()

    def test_long_notes_fall_back_to_safe_two_note_path(self):
        self.cfg.ollama.num_predict=1400;model=OfflineQwen(self.cfg.ollama,self.root)
        notes=[content('Original financial conditions. '*150) for _ in range(5)]
        groups=list(model.merge_groups(notes,{'origin_market':'US','output_language':'en','date_context':'published'}))
        self.assertEqual([len(group) for group in groups],[2,2,1]);model.close()

    def test_short_article_no_longer_needs_a_separate_screen_request(self):
        model=OfflineQwen(self.cfg.ollama,self.root)
        c=self.crawler(PageSources(),model);c.queue(candidate());report=c.run(resume=True)
        self.assertEqual(model.purposes,['来源发布时间','正文整理'])
        self.assertEqual(report['saved'],1);self.assertEqual(report['screen_calls_avoided'],1);c.close()

    def test_unuseful_short_article_still_cannot_be_saved(self):
        class Reject(OfflineQwen):
            def summarize(self,*args,**kwargs):return {'useful':False,'reason':'No substantive market information'}
        c=self.crawler(PageSources(),Reject(self.cfg.ollama,self.root));c.queue(candidate())
        report=c.run(resume=True)
        self.assertEqual(report['saved'],0);self.assertFalse(c.state['pending']);c.close()

    def test_added_history_pass_keeps_all_old_task_indices_and_scope(self):
        self.cfg.search.strategy='broad';self.cfg.search.history_refinement_passes=1
        c=self.crawler();start,end=c.window();old_jobs=historical_jobs(self.cfg,start,end)
        scope,_,progress=history_context(c,start,end);progress['done']['0']={'at':'fixture'}
        self.cfg.search.history_refinement_passes=2
        new_scope,new_jobs,new_progress=history_context(c,start,end)
        self.assertEqual(new_jobs[:len(old_jobs)],old_jobs);self.assertEqual(scope,new_scope)
        self.assertEqual(new_progress['done']['0'],{'at':'fixture'})
        self.assertGreater(len(new_jobs),len(old_jobs));c.close()

    def test_two_monthly_passes_diversify_same_window_without_exceeding_config(self):
        self.cfg.monitoring.directions='all';self.cfg.collection.start_date='2012-01-01';self.cfg.collection.end_date='2022-05-25'
        jobs=historical_jobs(self.cfg,self.cfg.collection.start_date,self.cfg.collection.end_date)
        self.assertEqual(len(jobs),822)
        base=[j for j in jobs if j['phase']=='window' and 'refinement_pass' not in j]
        added=[j for j in jobs if j.get('refinement_pass')==2]
        for before,after in zip(base,added):
            self.assertEqual(before['range'],after['range']);self.assertNotEqual(before['direction'],after['direction'])
            self.assertNotEqual(before['source_turn']%34,after['source_turn']%34)

    def test_nonempty_strict_results_with_no_new_candidate_get_one_fallback(self):
        self.cfg.search.strategy='broad';self.cfg.search.date_window_days=0
        self.cfg.monitoring.directions=['Banking'];self.cfg.collection.max_queries_per_cycle=2
        class Sources(FakeSources):
            stages=[]
            def search(self,*args,**kwargs):self.stages.append(kwargs['strict_dates']);return [candidate('known')]
        class Model(FakeQwen):
            def coverage_plan(self,*args):return [{'market':'CN','direction':'Banking','query':'Banking earnings'}]
        sources=Sources();c=self.crawler(sources,Model())
        identity=source_id(candidate('known')['url']);c.dataset.rows[identity]=('CN',{'url':candidate('known')['url']})
        report={'searched':0,'found':0,'queries':[],'errors':[]}
        broad_historical_discover(c,report,*c.window())
        self.assertEqual(sources.stages,[True,False]);self.assertEqual(report['searched'],2)
        self.assertTrue(report['discovery']['complete']);c.close()

    def test_implicit_new_defaults_keep_legacy_pause_fingerprint(self):
        before=self.cfg.model_dump(mode='json');before['search'].pop('history_refinement_passes')
        self.assertEqual(control_config_digest(self.cfg),digest(before))
        self.cfg.search.history_refinement_passes=3
        self.assertNotEqual(control_config_digest(self.cfg),digest(before))

    def test_rejected_rediscovery_does_not_reset_screening_or_failure_state(self):
        self.cfg.search.strategy='broad';self.cfg.search.date_window_days=0;self.cfg.monitoring.directions=['Banking']
        self.cfg.search.undated_fallback=False;self.cfg.collection.max_queries_per_cycle=1
        class Sources(FakeSources):
            def search(self,*args,**kwargs):return [candidate('rejected')]
        class Model(FakeQwen):
            def coverage_plan(self,*args):return [{'market':'CN','direction':'Banking','query':'Banking facts'}]
        c=self.crawler(Sources(),Model());identity=source_id(candidate('rejected')['url'])
        c.state['records'][identity]={'status':'rejected','scope':c.scope,'reason':'No facts'}
        report={'searched':0,'found':0,'queries':[],'errors':[]}
        broad_historical_discover(c,report,*c.window())
        self.assertEqual(report['candidates_new'],0);self.assertFalse(c.state['pending'])
        self.assertEqual(report['discovery']['already_rejected'],1);c.close()

    def test_eight_bounded_notes_need_three_merges_instead_of_seven(self):
        self.cfg.ollama.num_predict=1400;model=OfflineQwen(self.cfg.ollama,self.root)
        context={'origin_market':'US','output_language':'en','date_context':'Published November 2, 2026'}
        notes=[content() for _ in range(8)];merges=0
        while len(notes)>1:
            next_notes=[]
            for group in model.merge_groups(notes,context):
                if len(group)>1:next_notes.append(model.merge_pair(group,context,lambda n:n));merges+=1
                else:next_notes.extend(group)
            notes=next_notes
        self.assertEqual(merges,3);self.assertEqual(model.purposes.count('段落合并'),3);model.close()

    def test_rss_has_one_read_per_market_without_qwen_query_planning(self):
        self.cfg.search.provider='rss';self.cfg.search.rss_feeds=['https://example.com/feed']
        class Sources(FakeSources):
            searches=0
            def search(self,*args):self.searches+=1;return [candidate('rss')]
        class Model(FakeQwen):
            def plan(self,*args):raise AssertionError('RSS must not plan repeated search queries')
        c=self.crawler(Sources(),Model());report=c.collect()
        self.assertEqual(c.sources.searches,1);self.assertEqual(report['searched'],1)
        self.assertEqual(report['saved'],1);c.close()

    def test_sec_hidden_xbrl_header_does_not_inflate_visible_article(self):
        from web_sources import Sources
        source=Sources(self.cfg,self.root);self.cfg.fetch.max_text_chars=1000
        raw=('<html><head><title>Example Bank earnings</title><meta name="datePublished" content="2021-03-31"></head><body>'
             '<ix:header><ix:hidden>'+('NONVISIBLE hidden resources '*10000)+'</ix:hidden></ix:header>'
             '<article><h1>Example Bank earnings</h1><p>Example Bank (NYSE: EXB), Regional banking.</p>'
             '<p>Revenue was <ix:nonfraction>$15 million</ix:nonfraction> in 2021Q1.</p></article></body></html>').encode()
        page=source.page_from_bytes(raw,'https://www.sec.gov/Archives/edgar/data/1/000000000121000001/report.htm','US')
        self.assertEqual(page['removed_inline_xbrl_headers'],1)
        for token in ['Example Bank','EXB','Regional banking','$15 million','2021Q1']:self.assertIn(token,page['text'])
        self.assertIn('2021-03-31',page['date_context']);self.assertNotIn('NONVISIBLE',page['text'])
        self.assertEqual(page['raw_sha256'],digest(raw));source.close()

    def test_xbrl_header_cleanup_is_sec_archive_only(self):
        from web_sources import read_html
        page=read_html(b'<title>Report</title><ix:header>Visible article text for another provider.</ix:header>',
                       'https://example.com/report',False)
        self.assertIn('Visible article text',page['text']);self.assertEqual(page['removed_inline_xbrl_headers'],0)

    def test_parquet_errors_name_stage_and_table_without_raw_messages(self):
        import structured_export as export
        cause=ValueError('SENSITIVE SOURCE ROW OR CREDENTIAL')
        with patch.object(export,'export_parquet',side_effect=export.ProjectionError('table_schema','article_events',cause)):
            result=export.export_or_defer(self.root)
        self.assertEqual(result['error_type'],'ValueError');self.assertEqual(result['error_stage'],'table_schema')
        self.assertEqual(result['error_table'],'article_events')
        self.assertNotIn('SENSITIVE',json.dumps(result));self.assertEqual(len(result['error_fingerprint']),64)

    def test_replaced_model_weights_under_same_name_unlock_retry_on_restart(self):
        class Model(FakeQwen):
            model_digest='old-weights'
            def summarize(self,*a,**kw):raise OutputError('数字证据不匹配')
            def check(self):self.model_digest='replacement-weights'
        c=self.crawler(model=Model());c.queue(candidate());c.run(resume=True);c.close()
        class Fixed(Model):
            model_digest=None
            def summarize(self,*a,**kw):return FakeQwen.summarize(self,*a,**kw)
        restored=self.crawler(model=Fixed());result=restored.run(resume=True)
        self.assertEqual(result['saved'],1);self.assertEqual(restored.model.model_digest,'replacement-weights')
        self.assertEqual(restored.sources.fetched,0);restored.close()

    def test_extra_pass_never_repeats_identical_single_site_single_direction_request(self):
        self.cfg.monitoring.directions=['Banking'];self.cfg.search.history_refinement_passes=1
        base=historical_jobs(self.cfg,*[self.cfg.collection.start_date,self.cfg.collection.end_date])
        self.cfg.search.history_refinement_passes=4
        self.assertEqual(historical_jobs(self.cfg,self.cfg.collection.start_date,self.cfg.collection.end_date),base)

    def test_merge_rejects_omitted_primary_company_instead_of_appending_a_name_later(self):
        model=OfflineQwen(self.cfg.ollama,self.root)
        notes=[dict(content('Alpha Bank reported higher revenue.'),securities=[{'name':'Alpha Bank'}]),
               dict(content('Beta Chip reported new orders.'),securities=[{'name':'Beta Chip'}])]
        context={'origin_market':'US','output_language':'en','date_context':'Published November 2, 2026'}
        with self.assertRaisesRegex(ValueError,'主要公司'):model.merge_pair(notes,context,lambda note:note)
        payload=model.merge_payload(context,[notes[0],notes[0],notes[1]])
        self.assertEqual(len(payload['notes']),2)
        self.assertEqual(payload['required_subjects'],['Alpha Bank','Beta Chip']);model.close()

    def test_old_cached_fragment_never_moves_known_inference_completion_backwards(self):
        def respond(request):return httpx.Response(200,json={'message':{'content':json.dumps(content())}})
        model=Qwen(self.cfg.ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(respond)))
        model.model_digest='offline-model';payload={'text':'Original financial facts','source_id':'synthetic'}
        model.ask('正文整理',READ_PROMPT,payload,ContentNote);latest=model.inferred_by_source['synthetic']
        cache=next((self.root/'.runtime/simple/model_cache').glob('*.json'))
        saved=json.loads(cache.read_text(encoding='utf-8'));saved['llm_inferred_at']='2000-01-01T00:00:00+00:00';dump(cache,saved)
        model.ask('正文整理',READ_PROMPT,payload,ContentNote)
        self.assertEqual(model.inferred_by_source['synthetic'],latest);model.close()


if __name__=='__main__':unittest.main()

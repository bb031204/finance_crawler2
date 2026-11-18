"""Synthetic-only acceptance for the article interface and transient raw policy."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
import httpx
from test_simple_crawler import config,FakeQwen,FakeSources,candidate
from simple_crawler import Crawler
from finance_agent.core import dump,digest,now
from record_files import Dataset,source_id,FIELDS
from record_time import with_time_information
from record_quality import prepare_record_audit,finalize_record
from analysis_metadata import public_analysis
from interface_schema import RawContentConfig,feature_is_available,public_schema_version
from market_relations import ground_relations,preserve_relations,normalized_content_hash
from structured_export import export_parquet,read_export,export_or_defer,schemas,project,snapshot
from raw_cleanup import cleanup_transients,register_page,cleanup_page
from qwen_agent import Qwen,ContentNote,READ_PROMPT,OutputError

TEXT='Example Motors (NYSE: EXM) reported revenue of $12 million. Battery costs rose 5 percent. Nova Cells supplies Example Motors with batteries.'


class MarketInterfaceTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'.runtime/test_market_interface';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def fixture(self,market='US'):
        url='https://example.com/market-interface';identity=source_id(url)
        page=dict(url=url,title='Synthetic results',source_title='Synthetic results',origin_market=market,
            text=TEXT,fetched_at=now(),raw_sha256=digest(b'synthetic source bytes'))
        note=dict(useful=True,market=market,category='Earnings',industry='Batteries',keywords=['Example Motors','EXM','Batteries'],
            content='Example Motors reported revenue of $12 million while battery costs increased 5 percent, increasing its manufacturing cost exposure.',
            securities=[dict(name='Example Motors',ticker='EXM',exchange='NYSE',quote='Example Motors (NYSE: EXM)'),
                dict(name='Nova Cells',ticker=None,exchange=None,quote='Nova Cells',entity_type='company',entity_role='supplier',role_quote='Nova Cells supplies Example Motors with batteries.')],
            industries=[dict(name='Batteries',basis='inferred',quote='Battery costs rose 5 percent.')],impact_scope=['company','industry'],
            events=[dict(event_type='earnings',subject='Example Motors',action='reported',object='revenue',key_period=None,
                key_values='$12 million',conditions=None,evidence_type='explicit',quote='Example Motors (NYSE: EXM) reported revenue of $12 million.')],
            industry_impacts=[dict(industry_name_original='Batteries',event_index=0,impact_role='cost_exposure',direction='negative_conditional',
                mechanism='Higher battery input costs may increase manufacturing expenses.',horizon='short',evidence_type='inferred',confidence=None,quote='Battery costs rose 5 percent.')])
        from security_grounding import grounded_securities
        entities=copy.deepcopy(note['securities']);note['securities']=grounded_securities(entities,TEXT);ground_relations(note,TEXT,entities)
        note.update(time='2026-11-01',date_kind='published',date_quote='Published 2026-11-01')
        row=dict(source_id=identity,url=url,title=page['title'],时间=note['time'],分类=note['category'],关键词=note['keywords'],主要内容=note['content'],**public_analysis(note))
        candidate=dict(url=url,origin_market=market,first_seen_at=now())
        row=with_time_information(row,candidate,page)
        path=self.root/'.runtime/simple/pages'/(identity+'.json');dump(path,page);register_page(self.root,path,page,identity)
        model=FakeQwen();model.model_digest='synthetic-digest'
        return page,note,row,candidate,path,model
    def commit(self,market='US'):
        page,note,row,candidate,path,model=self.fixture(market)
        ds=Dataset(self.root,'data');audit=prepare_record_audit(self.root,row,note,page,candidate,model,market)
        ds.save(market,row,note['industry'],audit)
        return ds,page,note,row,candidate,path,model
    def test_name_only_entities_and_roles_are_retained(self):
        _,note,row,_,_,_=self.fixture()
        self.assertEqual(row['股票实体'][1],{'名称':'Nova Cells','股票代码':None,'交易所':None})
        self.assertEqual(note['entity_details'][1]['entity_role'],'supplier')
    def test_unverified_role_stays_unknown(self):
        _,note,_,_,_,_=self.fixture();note['entity_details'][1]['role_quote']='Invented supplier claim'
        ground_relations(note,TEXT)
        self.assertEqual(note['entity_details'][1]['entity_role'],'unknown')
    def test_no_same_paragraph_ticker_pairing(self):
        from security_grounding import grounded_securities
        entities=grounded_securities([dict(name='Nova Cells',ticker='EXM',exchange='NYSE',quote=TEXT)],TEXT)
        self.assertIsNone(entities[0]['ticker']);self.assertEqual(entities[0]['name'],'Nova Cells')
    def test_scopes_can_coexist(self):
        self.assertEqual(public_analysis(dict(impact_scope=['market','company','industry']))['影响范围'],['company','industry','market'])
    def test_macro_without_entities_can_export(self):
        _,_,row,_,_,_=self.fixture();row['股票实体']=[];row['影响范围']=['market'];row['涉及行业']=[]
        tables=project([('GLOBAL',row,{})]);self.assertEqual(tables['article_entities'],[])
        self.assertEqual(tables['article_metadata'][0]['impact_scope'],['market'])
    def test_bad_event_quote_drops_optional_relation(self):
        _,note,_,_,_,_=self.fixture();note['events'][0]['quote']='Invented quote'
        ground_relations(note,TEXT);self.assertEqual(note['events'],[])
        self.assertIn('event_unverified',note['relation_quality_flags'])
    def test_supply_contract_cannot_be_labeled_regulation_without_evidence(self):
        _,note,_,_,_,_=self.fixture();note['events'][0]['event_type']='regulation_policy'
        ground_relations(note,TEXT)
        self.assertEqual(note['events'][0]['event_type'],'unknown');self.assertIn('event_type_unverified',note['relation_quality_flags'])
    def test_fed_publication_node_is_bound_to_known_publisher(self):
        from web_sources import read_html
        raw=b'<main><p class="article__time">September 18, 2024</p><h1>FOMC statement</h1><p>The Federal Reserve lowered interest rates.</p></main>'
        page=read_html(raw,'https://www.federalreserve.gov/newsevents/pressreleases/example.htm',False)
        self.assertIn('datePublished=September 18, 2024',page['date_context'])
        other=read_html(raw,'https://example.com/article',False)
        self.assertNotIn('datePublished=September 18, 2024',other['date_context'])
        duplicate=read_html(raw.replace(b'<h1>',b'<p class="article__time">September 19, 2024</p><h1>'),
            'https://www.federalreserve.gov/newsevents/pressreleases/example.htm',False)
        self.assertNotIn('datePublished=September',duplicate['date_context'])
    def test_document_title_excludes_svg_accessibility_title(self):
        from web_sources import read_html
        raw=b'<html><head><title>Original financial release</title></head><body><svg><title>Lock</title></svg><main>Revenue rose during the quarter.</main></body></html>'
        page=read_html(raw,'https://example.com/article',False)
        self.assertEqual(page['title'],'Original financial release');self.assertEqual(page['source_title'],'Original financial release')
    def test_stock_forecast_is_not_an_economic_mechanism(self):
        _,note,_,_,_,_=self.fixture();note['industry_impacts'][0]['mechanism']='The stock price will rise 10 percent.'
        ground_relations(note,TEXT);self.assertEqual(note['industry_impacts'],[])
    def test_unknown_industry_impact_is_omitted(self):
        _,note,_,_,_,_=self.fixture();note['industry_impacts'][0]['industry_name_original']='Oil drilling'
        ground_relations(note,TEXT);self.assertEqual(note['industry_impacts'],[])
    def test_mechanism_driver_requires_its_own_evidence(self):
        _,note,_,_,_,_=self.fixture();note['industry_impacts'][0]['mechanism']='Lower interest rates reduce borrowing costs.'
        ground_relations(note,TEXT);self.assertEqual(note['industry_impacts'],[])
    def test_role_cannot_be_invented_from_a_name_only_quote(self):
        _,note,_,_,_,_=self.fixture();note['entity_details'][1]['role_quote']='Nova Cells'
        ground_relations(note,TEXT);self.assertEqual(note['entity_details'][1]['entity_role'],'unknown')
    def test_leaf_relations_survive_merge_and_deduplicate(self):
        _,note,_,_,_,_=self.fixture();final=dict(note,events=[],industry_impacts=[],entity_details=[])
        result=preserve_relations(final,[note,note],TEXT)
        self.assertEqual(len(result['events']),1);self.assertEqual(len(result['industry_impacts']),1)
    def test_hash_normalizes_whitespace_only(self):
        self.assertEqual(normalized_content_hash('Alpha\n beta'),normalized_content_hash('Alpha beta'))
        self.assertNotEqual(normalized_content_hash('Alpha beta'),normalized_content_hash('Alpha gamma'))
    def test_commit_sets_verified_availability_and_version(self):
        ds,_,_,row,_,_,_=self.commit('GLOBAL')
        audit=json.loads((self.root/'.runtime/simple/audit'/(row['source_id']+'.json')).read_text())
        self.assertEqual(set(row),FIELDS);self.assertEqual(audit['record_hash'],digest(row))
        self.assertGreaterEqual(row['时间信息']['feature_available_at'],audit['extracted_at'])
        self.assertEqual(row['时间信息']['feature_available_at'],audit['commit_verified_at'])
        self.assertTrue(audit['extraction_version']);self.assertEqual(len(ds.replicas[row['source_id']]),3)
    def test_cutoff_is_strict_and_unknown_is_unavailable(self):
        value='2026-11-12T12:00:00+00:00'
        self.assertFalse(feature_is_available({'feature_available_at':value},value))
        self.assertTrue(feature_is_available({'feature_available_at':value},'2026-11-12T12:00:01+00:00'))
        self.assertFalse(feature_is_available({},value))
        with self.assertRaises(ValueError):feature_is_available({'feature_available_at':value},'2026-11-12T12:00:01')
    def test_date_only_and_timezone_are_preserved(self):
        _,_,row,_,_,_=self.fixture();tables=project([('US',row,{})])
        self.assertEqual(tables['article_metadata'][0]['published_at'],'2026-11-01')
        row['时间']='2026-11-01T17:05:00-05:00';row['时间信息']['publication_precision']='second'
        self.assertEqual(project([('US',row,{})])['article_metadata'][0]['published_at'],row['时间'])
    def test_new_extraction_has_new_version_and_availability(self):
        ds,page,note,row,candidate,path,model=self.commit()
        audit_path=self.root/'.runtime/simple/audit'/(row['source_id']+'.json');old=json.loads(audit_path.read_text())
        updated=with_time_information(dict(row,主要内容=row['主要内容']+' Costs remained under review.'),candidate,page)
        ds.save('US',updated,'Batteries',prepare_record_audit(self.root,updated,note,page,candidate,model,'US'))
        audit=json.loads(audit_path.read_text())
        self.assertNotEqual(audit['extraction_version'],old['extraction_version'])
        self.assertGreater(audit['feature_available_at'],old['feature_available_at'])
    def test_second_phase_commit_recovers_without_backdating(self):
        page,note,row,candidate,path,model=self.fixture('GLOBAL');ds=Dataset(self.root,'data');real_dump=dump;count=0
        def fail(file,value):
            nonlocal count
            if Path(file).parent==self.root/'data/CN':
                count+=1
                if count==2:raise OSError('synthetic final phase failure')
            return real_dump(file,value)
        with patch('record_files.dump',side_effect=fail):
            with self.assertRaises(OSError):ds.save('GLOBAL',row,'Batteries',prepare_record_audit(self.root,row,note,page,candidate,model,'GLOBAL'))
        operation=json.loads(ds.outbox.read_text());stamp=operation['row']['时间信息']['feature_available_at']
        self.assertTrue(operation['commit_finalized']);self.assertTrue(path.exists())
        restored=Dataset(self.root,'data');saved=restored.rows[row['source_id']][1]
        self.assertEqual(saved['时间信息']['feature_available_at'],stamp)
        for file in restored.replicas[row['source_id']].values():self.assertEqual(json.loads(file.read_text()),saved)
    def test_export_is_idempotent_and_deduplicates_global(self):
        self.commit('GLOBAL');first=export_parquet(self.root);second=export_parquet(self.root)
        self.assertEqual(first['rows'],second['rows']);self.assertEqual(first['rows']['article_metadata'],1)
        tables=read_export(self.root);self.assertEqual(len(tables['article_entities']),2)
        self.assertEqual(len(tables['article_events']),1);self.assertEqual(len(tables['article_industry_impacts']),1)
        self.assertEqual(tables['security_master'],[]);self.assertTrue(tables['article_entities'][0]['entity_id'].startswith('unresolved:'))
        self.assertIsNone(tables['article_entities'][0]['security_id']);self.assertIsNone(tables['article_metadata'][0]['event_cluster_id'])
    def test_all_eight_tables_have_typed_empty_schemas(self):
        manifest=export_parquet(self.root);self.assertEqual(len(schemas()),8)
        self.assertTrue(all(n==0 for n in manifest['rows'].values()));self.assertEqual(len(read_export(self.root)),8)
    def test_old_json_export_does_not_rewrite_or_guess(self):
        _,_,row,_,_,_=self.fixture();row={k:v for k,v in row.items() if k in {'source_id','url','时间','分类','关键词','主要内容'}}
        path=self.root/'data/US/old.json';dump(path,row);before=path.read_bytes()
        export_parquet(self.root);metadata=read_export(self.root)['article_metadata'][0]
        self.assertEqual(path.read_bytes(),before);self.assertIsNone(metadata['feature_available_at'])
        self.assertIsNone(metadata['content_hash']);self.assertEqual(metadata['record_schema_version'],'legacy-6')
    def test_mismatched_audit_is_not_restored(self):
        _,_,_,row,_,_,_=self.commit();path=self.root/'.runtime/simple/audit'/(row['source_id']+'.json')
        audit=json.loads(path.read_text());audit['record_hash']='wrong';dump(path,audit)
        export_parquet(self.root);self.assertEqual(read_export(self.root)['article_events'],[])
    def test_different_global_copies_block_projection(self):
        ds,_,_,row,_,_,_=self.commit('GLOBAL');dump(ds.replicas[row['source_id']]['CN'],dict(row,title='different'))
        self.assertEqual(export_or_defer(self.root)['status'],'pending')
    def test_projection_recovers_interrupted_replacements(self):
        self.commit();from structured_export import recover_export
        real_replace=Path.replace
        def fail(path,target):
            if Path(target).name=='article_events.parquet':raise OSError('synthetic interruption')
            return real_replace(path,target)
        with patch.object(Path,'replace',fail):self.assertEqual(export_or_defer(self.root)['status'],'pending')
        self.assertTrue((self.root/'.runtime/simple/parquet_outbox.json').is_file())
        recover_export(self.root);self.assertEqual(len(read_export(self.root)['article_metadata']),1)
    def test_bad_foreign_key_is_rejected(self):
        from structured_export import validate_tables
        _,note,row,_,_,_=self.fixture();tables=project([('US',row,{'events':note['events'],'industry_impacts':note['industry_impacts']})])
        tables['article_industry_impacts'][0]['event_id']='missing'
        with self.assertRaises(ValueError):validate_tables(tables)
    def test_success_deletes_owned_page_and_records_proof(self):
        ds,page,note,row,candidate,path,model=self.commit('GLOBAL')
        unrelated=self.root/'.runtime/validation/unrelated.pdf';unrelated.parent.mkdir(parents=True,exist_ok=True);unrelated.write_bytes(b'keep')
        audit=finalize_record(self.root,ds,row,note,page,candidate,model,path)
        self.assertFalse(path.exists());self.assertTrue(unrelated.exists())
        self.assertEqual(audit['raw_cleanup_status'],'deleted');self.assertTrue(audit['raw_deleted_at'])
        self.assertEqual(audit['record_hash'],digest(row))
    def test_failed_json_commit_never_deletes_source(self):
        page,note,row,candidate,path,model=self.fixture();ds=Dataset(self.root,'data');real_dump=dump
        def fail(file,value):
            if Path(file).parent==self.root/'.runtime/simple/audit':raise OSError('synthetic audit failure')
            return real_dump(file,value)
        with patch('record_files.dump',side_effect=fail):
            with self.assertRaises(OSError):ds.save('US',row,'Batteries',prepare_record_audit(self.root,row,note,page,candidate,model,'US'))
        self.assertTrue(path.exists());Dataset(self.root,'data');self.assertTrue(path.exists())
    def test_parquet_failure_never_requires_success_raw(self):
        ds,page,note,row,candidate,path,model=self.commit()
        with patch('structured_export.export_parquet',side_effect=OSError('offline disk fault')):
            self.assertEqual(export_or_defer(self.root)['status'],'pending')
        finalize_record(self.root,ds,row,note,page,candidate,model,path)
        self.assertFalse(path.exists());export_parquet(self.root)
        self.assertEqual(len(read_export(self.root)['article_metadata']),1)
    def test_wrong_page_hash_is_preserved_and_flagged(self):
        ds,page,note,row,candidate,path,model=self.commit();dump(path,dict(page,text='different source'))
        audit=finalize_record(self.root,ds,row,note,page,candidate,model,path)
        self.assertTrue(path.exists());self.assertEqual(audit['raw_cleanup_status'],'failed')
    def test_cleanup_failure_can_be_retried_without_qwen(self):
        ds,page,note,row,candidate,path,model=self.commit();real_unlink=Path.unlink
        def fail(file,*args,**kwargs):
            if file==path:raise OSError('synthetic delete fault')
            return real_unlink(file,*args,**kwargs)
        with patch.object(Path,'unlink',fail):
            audit=finalize_record(self.root,ds,row,note,page,candidate,model,path)
        self.assertEqual(audit['raw_cleanup_status'],'failed')
        report=cleanup_transients(self.root);self.assertFalse(path.exists());self.assertEqual(report['failed'],0)
    def test_cleanup_audit_failure_after_unlink_recovers_from_metadata_only(self):
        ds,page,note,row,candidate,path,model=self.commit();real_dump=dump
        def fail(file,value):
            if Path(file).parent==self.root/'.runtime/simple/audit' and value.get('raw_cleanup_status'):raise OSError('synthetic cleanup audit failure')
            return real_dump(file,value)
        with patch('raw_cleanup.dump',side_effect=fail):
            with self.assertRaises(OSError):finalize_record(self.root,ds,row,note,page,candidate,model,path)
        self.assertFalse(path.exists());report=cleanup_transients(self.root)
        self.assertEqual(report['failed'],0)
        audit=json.loads((self.root/'.runtime/simple/audit'/(row['source_id']+'.json')).read_text())
        self.assertEqual(audit['raw_cleanup_status'],'deleted')
        self.assertFalse((self.root/'.runtime/simple/raw_manifest'/(row['source_id']+'.json')).exists())
    def test_pending_ttl_and_rejected_cleanup_preserve_url_state(self):
        page,_,_,candidate,path,_=self.fixture();identity=path.stem
        dump(self.root/'.runtime/simple/state.json',dict(records={identity:{'status':'pending'}},pending={identity:dict(candidate,page_path=str(path.relative_to(self.root))) }))
        report=cleanup_transients(self.root,clock=datetime.now(timezone.utc)+timedelta(hours=25))
        self.assertFalse(path.exists());self.assertEqual(report['deleted'],1)
        self.assertIn(candidate['url'],(self.root/'.runtime/simple/state.json').read_text())
    def test_fresh_pending_cache_not_deleted(self):
        _,_,_,_,path,_=self.fixture();cleanup_transients(self.root);self.assertTrue(path.exists())
    def test_known_nested_validation_page_obeys_ttl_but_user_folder_is_preserved(self):
        page,_,_,_,_,_=self.fixture();page['fetched_at']='2020-01-01T00:00:00+00:00';identity=source_id(page['url'])
        known=self.root/'.runtime/validation/output_budget_20261111/sources'/(identity+'.json')
        unknown=self.root/'.runtime/validation/user_documents/sources'/(identity+'.json')
        dump(known,page);dump(unknown,page);cleanup_transients(self.root)
        self.assertFalse(known.exists());self.assertTrue(unknown.exists())
    def test_retention_and_long_ttl_are_forbidden(self):
        for value in [dict(retain_after_success=True),dict(pending_cache_ttl_hours=25),dict(delete_after_verified_commit=False)]:
            with self.assertRaises(ValueError):RawContentConfig.model_validate(value)
    def test_structured_directory_is_validated_before_collection(self):
        import yaml
        from simple_crawler import load_config
        for directory in ['../outside','.runtime/private','']:
            value=config().model_dump();value['search']['api_key']='';value['search']['sec_edgar']['user_agent']=''
            value['data_interface']['directory']=directory
            path=self.root/'config.yaml';path.write_text(yaml.safe_dump(value),encoding='utf-8')
            with self.assertRaises(ValueError):load_config(path,self.root)
    def test_legacy_error_full_reply_is_sanitized_without_archive(self):
        path=self.root/'.runtime/simple/model_errors'/('a'*64+'_0.json');dump(path,dict(purpose='read',error='failed',response={'message':'SYNTHETIC FULL ARTICLE'}))
        report=cleanup_transients(self.root);self.assertEqual(report['sanitized_error_logs'],1)
        self.assertNotIn('SYNTHETIC FULL ARTICLE',path.read_text());self.assertFalse((self.root/'archive').exists())
    def test_model_failure_logs_no_raw_reply_or_validation_input(self):
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[dict(name='qwen3:8b',digest='synthetic') ]})
            return httpx.Response(200,json={'message':{'content':json.dumps({'content':'SYNTHETIC PRIVATE FULL SOURCE'})}})
        model=Qwen(config().ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            with self.assertRaises(OutputError):model.ask('read',READ_PROMPT,dict(text='synthetic source'),ContentNote)
        finally:model.close()
        for path in (self.root/'.runtime/simple/model_errors').glob('*.json'):
            self.assertNotIn('SYNTHETIC PRIVATE FULL SOURCE',path.read_text());self.assertNotIn('response',json.loads(path.read_text()))
    def test_verbatim_full_source_cannot_be_a_success_cache(self):
        source=' '.join('Synthetic operating result '+str(i)+'.' for i in range(50))
        value=dict(useful=True,market='US',category='Earnings',industry='Retail',securities=[],industries=[],impact_scope=['company'],keywords=['results'],content=source,reason='test')
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[dict(name='qwen3:8b',digest='synthetic')]})
            return httpx.Response(200,json={'message':{'content':json.dumps(value)}})
        model=Qwen(config().ollama,self.root,httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(handler)))
        try:
            with self.assertRaises(OutputError):model.ask('read',READ_PROMPT,dict(text=source),ContentNote)
        finally:model.close()
        self.assertFalse(list((self.root/'.runtime/simple/model_cache').glob('*.json')))
    def test_plain_collection_does_not_migrate_historical_json(self):
        _,_,row,_,_,_=self.fixture();row={k:v for k,v in row.items() if k in {'source_id','url','时间','分类','关键词','主要内容'}}
        path=self.root/'data/US/old.json';dump(path,row);before=path.read_bytes()
        crawler=Crawler(config(),self.root,FakeSources(),FakeQwen())
        try:crawler.collect(resume=True)
        finally:crawler.close()
        self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()

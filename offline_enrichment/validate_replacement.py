"""Real local-Qwen smoke check of replacement, proof packs and offline export acceptance."""
import json,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from finance_agent.core import dump,digest,now
from offline_enrichment.retrace_news import LocalQwen,inventory,validate_result,publish,sha,PROMPT
from offline_enrichment.verify_retrace import verify
from industry_dictionary import IndustryDictionary
from market_scope import evidence_pack

def main():
    import argparse,yaml
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    out=(ROOT/args.output).resolve();out.relative_to(ROOT/'data_enriched/retraced')
    if out.exists():raise ValueError('validation_output_must_be_new')
    cfg=yaml.safe_load((ROOT/'offline_enrichment/retrace_config.yaml').read_text(encoding='utf-8'))
    all_jobs=inventory(ROOT,cfg['markets']);ids=['0076065ceba585096b365f59','17ca712cdd132f39203a5aea','1b02e559d81c568ffa4b6ff8']
    jobs={sid:all_jobs[sid] for sid in ids};dictionary=IndustryDictionary(ROOT/cfg['dictionary_directory'])
    model=LocalQwen(cfg);out.mkdir(parents=True)
    try:
        for sid,job in jobs.items():
            row=job['row'];source=row['主要内容'];offered=dictionary.candidates(source,cfg['candidate_limit'],cfg['active_standards'],headline=row.get('title') or '')
            payload={'articles':[{'source_id':sid,'TEXT':source,'industry_dictionary_candidates':offered}]}
            values,meta=model.infer(payload)
            note=validate_result(values[0],row,source,offered,dictionary,'stored_summary_only',cfg['min_confidence'])
            if note['relation_quality_flags'] and cfg.get('semantic_repair_attempts',0):
                values,meta=model.infer(dict(payload,repair_instruction='Some optional claims failed source grounding. Use literal industry-identifying quotes for each selected dictionary code; use separate literal collective-context evidence for industry scope. Omit unsupported claims.'))
                note=validate_result(values[0],row,source,offered,dictionary,'stored_summary_only',cfg['min_confidence'])
            note.update(source_id=sid,url=row['url'],record_hash=digest(row),source_body_hash=digest(source.encode()),
                llm_inferred_at=meta['llm_inferred_at'],schema_version='1.1',model=cfg['model'],model_digest=model.revision,
                prompt_version=digest(PROMPT.encode()),language='en',extracted_at=now())
            proof=evidence_pack(row,note,source);proof.update(original_evidence_verified=False,evidence_basis='stored_summary_only')
            dump(out/'evidence'/(sid+'.json'),proof)
            result={'source_id':sid,'status':'completed','source_record_hash':digest(row),'feature_available_at':now(),
                'source_trace':{'status':'summary_only'},'audit':note,'selection':values[0],'response':meta}
            dump(out/'analysis'/(sid+'.json'),result)
            print(json.dumps({'source_id':sid,'impact_scope':note['impact_scope'],'typed_market':note['market_scope'],
                'industry_keys':[i['industry_key'] for i in note['industries']],
                'approved_industry':sum(i.get('propagation_approved') is True for i in note['industry_impacts']),
                'flags':note['relation_quality_flags']},ensure_ascii=False),flush=True)
        dump(out/'run.json',{'model_digest':model.revision,'classification_contract':'dictionary-scope-replacement-1'})
        dump(out/'input_index.json',{sid:j['inputs'] for sid,j in jobs.items()})
        with zipfile.ZipFile(out/'raw_news.zip','w',zipfile.ZIP_DEFLATED) as z:
            for sid,j in jobs.items():
                z.writestr(sid+'.json',json.dumps(j['row'],ensure_ascii=False))
                for p in j['inputs']:z.write(ROOT/p['path'],'originals/'+p['path'])
        with zipfile.ZipFile(out/'source_code.zip','w') as z:z.write(__file__,'offline_enrichment/validate_replacement.py')
        publish(ROOT,out,jobs,cfg,model.revision)
        report=verify(out);dump(out/'acceptance.json',report)
        print(json.dumps(report,ensure_ascii=False),flush=True)
        if not report['passed']:raise ValueError('replacement_validation_failed')
    finally:model.client.close()

if __name__=='__main__':main()

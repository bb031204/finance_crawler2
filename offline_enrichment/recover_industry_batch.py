"""Versioned recovery of failed industry records; completed parent bytes/inference times are reused."""
from collections import Counter
import hashlib,json,shutil,sys,time,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).parent))
from industry_batch import PROMPT,VERSION,load,save,rows,digest,file_sha,now,response_schema,validate
from industry_dictionary import IndustryDictionary

def main():
    import argparse,yaml
    parser=argparse.ArgumentParser();parser.add_argument('--config',default=str(ROOT/'offline_enrichment/industry_recovery_config.yaml'));args=parser.parse_args()
    recovery=yaml.safe_load(Path(args.config).read_text(encoding='utf-8'));parent=Path(recovery['parent_output']).resolve();out=Path(recovery['output']).resolve()
    out.relative_to(ROOT/'data_enriched/industry_batches')
    pm=load(parent/'manifest.json');pr=load(parent/'run.json')
    if pm['pending']!=0 or pm.get('original_inputs_unchanged') is not True:raise ValueError('Parent batch is incomplete or source protection failed')
    for name,expected in pm['files'].items():
        if file_sha(parent/name)!=expected:raise ValueError('Parent artifact changed')
    parent_sha=file_sha(parent/'manifest.json');settings=dict(pr['settings'])
    prompt=PROMPT+'\n'+recovery['confidence_instruction']
    settings.update(output=str(out),num_predict=recovery['num_predict'],prompt_sha256=digest(prompt),code_sha256=file_sha(__file__),parent_manifest_sha256=parent_sha)
    fingerprint=digest(settings);dictionary=IndustryDictionary(ROOT/settings['dictionary_directory'])
    old={r['source_id']:r for r in (load(p) for p in sorted((parent/'analysis').glob('*.json')))}
    targets=sorted(sid for sid,r in old.items() if r['status']!='completed')
    out.mkdir(parents=True,exist_ok=True);(out/'analysis').mkdir(exist_ok=True)
    if (out/'run.json').exists():
        if load(out/'run.json')['fingerprint']!=fingerprint:raise ValueError('Recovery settings changed; choose another version')
    else:
        for name in ('raw_news.zip','input_index.jsonl'):shutil.copy2(parent/name,out/name)
        with zipfile.ZipFile(out/'parent_batch.zip','w',zipfile.ZIP_DEFLATED) as z:
            for name,expected in dict(pm['files'],**{'manifest.json':parent_sha}).items():
                body=(parent/name).read_bytes()
                if hashlib.sha256(body).hexdigest()!=expected:raise ValueError('Parent changed while archiving')
                z.writestr(name,body)
        for sid in old:shutil.copy2(parent/'analysis'/(sid+'.json'),out/'analysis'/(sid+'.json'))
        (out/'industry_prompt.txt').write_text(prompt,encoding='utf-8')
        save(out/'run.json',{'kind':VERSION,'batch_version':2,'created_at':now(),'fingerprint':fingerprint,'settings':settings,
            'unique_articles':len(old),'parent_manifest_sha256':parent_sha,'recovery_target_ids':targets,
            'reuse_policy':'Completed parent analysis bytes and original inference times are unchanged; only failed parent records use the new confidence-unit prompt.'})
    with zipfile.ZipFile(out/'raw_news.zip') as z:raw={r['source_id']:r for r in (json.loads(z.read(n)) for n in z.namelist())}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def http(endpoint,payload=None):
        req=urllib.request.Request(settings['ollama_url'].rstrip('/')+endpoint,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=settings['timeout_seconds']) as stream:return json.load(stream)
    model=next(m for m in http('/api/tags')['models'] if m['name']==settings['model'])
    if model['digest']!=settings['model_digest']:raise ValueError('Local model differs from parent')
    for number,sid in enumerate(targets,1):
        current=load(out/'analysis'/(sid+'.json'))
        if current.get('fingerprint')==fingerprint and current.get('recovery_attempted_at'):continue
        summary=raw[sid]['主要内容'];title=raw[sid].get('title') or ''
        offered=dictionary.candidates(summary,settings['candidate_limit'],settings['active_standards'],headline=title)
        result=dict(old[sid],fingerprint=fingerprint,status='completed',industries=[],industry_impacts=[],llm_inferred_at=None,
            recovery_attempted_at=now(),recovery_parent_analysis_sha256=digest(old[sid]),prompt_sha256=digest(prompt))
        for name in ('error_type','error_code','response_sha256'):result.pop(name,None)
        try:
            payload={'source_id':sid,'title':title,'summary':summary,'candidates':offered}
            response=http('/api/chat',{'model':settings['model'],'stream':False,'think':False,'keep_alive':settings['keep_alive'],
                'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
                'format':response_schema([r['key'] for r in offered]),'options':{'temperature':0,'seed':42,'num_ctx':settings['num_ctx'],'num_predict':settings['num_predict']}})
            result['llm_inferred_at']=now();text=response['message']['content'];result['response_sha256']=hashlib.sha256(text.encode()).hexdigest()
            mentions,flags=validate(json.loads(text),summary,offered,dictionary,settings['min_confidence'],sid)
            result.update(industries=mentions,quality_flags=flags,model_duration_seconds=response.get('total_duration',0)/1e9)
            for item in mentions:
                if item['propagation_approved']:
                    result['industry_impacts'].append(dict(item,industry_name_original=item['industry_name_standard'],industry_name=item['industry_name_standard'],
                        evidence=item['quote'],industry_scope_quote=item['scope_quote'],evidence_type='inferred',impact_role='affected',horizon='unspecified',llm_inferred_at=result['llm_inferred_at']))
        except Exception as exc:result.update(status='needs_review',error_type=type(exc).__name__,error_code=str(exc)[:100])
        result['feature_available_at']=now();save(out/'analysis'/(sid+'.json'),result)
        print('[RECOVERY]',number,len(targets),sid,result['status'],result.get('error_code',''),flush=True)
    values=[load(p) for p in sorted((out/'analysis').glob('*.json'))]
    rows(out/'industry_classifications.jsonl',values)
    index=[json.loads(line) for line in (out/'input_index.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    unchanged=all(file_sha(r['path'])==r['sha256'] for r in index)
    summary={'total':len(values),'processed':len(values),'pending':0,'status_counts':dict(Counter(r['status'] for r in values)),
      'articles_with_dictionary_mentions':sum(bool(r['industries']) for r in values),'articles_with_industry_impacts':sum(bool(r['industry_impacts']) for r in values),
      'industry_impacts':sum(len(r['industry_impacts']) for r in values),'original_inputs_unchanged':unchanged,'updated_at':now(),
      'reused_completed_records':len(old)-len(targets),'recovery_attempts':len(targets),'parent_manifest_sha256':parent_sha}
    save(out/'progress.json',summary)
    files={p.relative_to(out).as_posix():file_sha(p) for p in out.rglob('*') if p.is_file() and p.name not in ('manifest.json','progress.json') and not p.name.endswith('.tmp')}
    save(out/'manifest.json',dict(kind=VERSION,batch_version=2,created_at=now(),fingerprint=fingerprint,files=files,**summary))
    print(json.dumps(summary),flush=True)
    if not unchanged:raise ValueError('Original input changed during recovery')

if __name__=='__main__':main()

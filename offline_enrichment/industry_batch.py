"""Versioned, resumable industry-only enrichment. Original news are never rewritten."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from industry_dictionary import IndustryDictionary, compact

VERSION = 'legacy-dictionary-industry-v1'
PROMPT = '''Classify the supplied historical news SUMMARY, not a newly downloaded original.
Use only industry_key values from candidates; never invent an industry code, name or ticker.
Return mentions=[] if the candidates do not fit. A company's own earnings, financing,
appointments, delisting or product announcement is company_only, even when its industry is named.
industry_wide requires an explicit statement affecting multiple independent companies or a
sector-wide policy, supply/demand or competitive mechanism. Mentioning the word industry alone
does not prove industry-wide impact. Do not infer peer effects from company membership.
quote and scope_quote must be exact contiguous substrings of SUMMARY (not the title).
For company_only, scope_quote and mechanism must be empty strings. For industry_wide,
scope_quote must explicitly demonstrate the multi-company/sector scope, and mechanism must
explain the economic transmission. Confidence is a self-assessment, not calibrated accuracy.
Do not predict returns, revise the summary, infer a publication date or resolve stock identities.
Output JSON only. /no_think'''
EXCLUDED = {'1b02e559d81c568ffa4b6ff8', '62f0ea7d749999a245b9525b', '777b97f602ed63a72ee4f488'}
QUARANTINE = {'00cf47b880e8fc0684b11eaa', '01d91eb93988b6150f2cc241'}

def now(): return datetime.now(timezone.utc).isoformat()
def digest(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
def file_sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(path)
def rows(path,values):
    Path(path).write_text(''.join(json.dumps(v,ensure_ascii=False,allow_nan=False)+'\n' for v in values),encoding='utf-8')

def scope_evidence(quote):
    from market_scope import industry_scope_candidate
    return industry_scope_candidate(quote)

def response_schema(keys):
    props={'industry_key':{'enum':keys},'quote':{'type':'string','minLength':3,'maxLength':240},
      'scope':{'enum':['company_only','industry_wide']},'scope_quote':{'type':'string','maxLength':400},
      'mechanism':{'type':'string','maxLength':300},'direction':{'enum':['positive','negative','mixed','unclear']},
      'confidence':{'type':'number','minimum':0,'maximum':1}}
    return {'type':'object','properties':{'mentions':{'type':'array','maxItems':3,
      'items':{'type':'object','properties':props,'required':list(props),'additionalProperties':False}}},
      'required':['mentions'],'additionalProperties':False}

def validate(value,summary,candidates,dictionary,threshold,sid):
    if not isinstance(value,dict) or set(value)!={'mentions'} or not isinstance(value['mentions'],list) or len(value['mentions'])>3:
        raise ValueError('response_shape_invalid')
    allowed={r['key'] for r in candidates}; result=[]; flags=[]; seen=set()
    for item in value['mentions']:
        if not isinstance(item,dict) or set(item)!={'industry_key','quote','scope','scope_quote','mechanism','direction','confidence'}:
            raise ValueError('mention_shape_invalid')
        key=item['industry_key'];quote=item['quote'];score=item['confidence']
        if key not in allowed: raise ValueError('dictionary_key_not_offered')
        if key in seen: continue
        if not isinstance(quote,str) or not 3<=len(quote)<=240 or quote not in summary:
            flags.append('mention_quote_not_verbatim');continue
        if not dictionary.evidence_matches(key,quote):
            flags.append('dictionary_quote_semantics_unresolved');continue
        if isinstance(score,bool) or not isinstance(score,(int,float)) or not 0<=score<=1: raise ValueError('confidence_invalid')
        if item['scope'] not in ('company_only','industry_wide') or item['direction'] not in ('positive','negative','mixed','unclear'):
            raise ValueError('scope_or_direction_invalid')
        scope=item['scope_quote'];mechanism=item['mechanism']
        if not isinstance(scope,str) or len(scope)>400 or not isinstance(mechanism,str) or len(mechanism)>300:
            raise ValueError('scope_evidence_shape_invalid')
        approved=(item['scope']=='industry_wide' and len(scope)>=8 and scope in summary
            and scope_evidence(scope) and len(mechanism)>=12 and score>=threshold
            and sid not in EXCLUDED and sid not in QUARANTINE)
        row=dictionary.by_key[key]
        result.append(dict(item,industry_standard=row['industry_standard'],industry_version=row['industry_version'],
          industry_code=row['industry_code'],industry_name_standard=row['name'],dictionary_sha256=dictionary.sha256,
          dictionary_version=dictionary.manifest['version'],evidence_basis='stored_summary_only',
          original_evidence_verified=False,industry_scope_status='automated_evidence_gate_passed' if approved else 'not_approved',
          propagation_approved=approved))
        seen.add(key)
    return result,sorted(set(flags))

def main():
    import yaml
    parser=argparse.ArgumentParser();parser.add_argument('--config',default=str(ROOT/'offline_enrichment/industry_config.yaml'))
    parser.add_argument('--limit',type=int);parser.add_argument('--retry-failed',action='store_true');args=parser.parse_args()
    cfg=yaml.safe_load(Path(args.config).read_text(encoding='utf-8')); out=Path(cfg['output']).resolve()
    out.relative_to(ROOT.resolve())
    dictionary=IndustryDictionary(ROOT/cfg['dictionary_directory']);out.mkdir(parents=True,exist_ok=True)
    code_sha=file_sha(__file__);settings=dict(cfg,prompt_sha256=digest(PROMPT),code_sha256=code_sha,dictionary_sha256=dictionary.sha256)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def http(endpoint,payload=None):
        request=urllib.request.Request(cfg['ollama_url'].rstrip('/')+endpoint,
          data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=cfg['timeout_seconds']) as response:return json.load(response)
    models=http('/api/tags')['models'];chosen=next((m for m in models if m['name']==cfg['model']),None)
    if chosen is None: raise ValueError('Configured local model is not installed')
    settings['model_digest']=chosen['digest']; fingerprint=digest(settings)
    inventory_path=out/'input_index.jsonl'
    if inventory_path.exists():
        manifest=load(out/'run.json')
        if manifest['fingerprint']!=fingerprint:raise ValueError('Batch code/config/model changed; choose a new versioned output')
        index=[json.loads(line) for line in inventory_path.read_text(encoding='utf-8').splitlines() if line]
    else:
        unique={};index=[];source=Path(cfg['input_root']).resolve()
        for market in ('US','GLOBAL'):
            for path in sorted((source/market).glob('*.json')):
                raw=path.read_bytes();record=json.loads(raw.decode('utf-8-sig'));sid=record['source_id']
                if not re.fullmatch('[a-zA-Z0-9_-]{1,128}',sid):raise ValueError('Unsafe source_id')
                if sid in unique and digest(unique[sid])!=digest(record):raise ValueError('Conflicting source versions')
                unique[sid]=record
                index.append({'source_id':sid,'path':str(path),'archive_path':market+'/'+path.name,'sha256':hashlib.sha256(raw).hexdigest(),'record_hash':digest(record)})
        if not index:raise ValueError('No input news')
        with zipfile.ZipFile(out/'raw_news.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for entry in index:archive.writestr(entry['archive_path'],Path(entry['path']).read_bytes())
        rows(inventory_path,index);save(out/'run.json',{'kind':VERSION,'created_at':now(),'fingerprint':fingerprint,'settings':settings,
          'unique_articles':len(unique),'physical_files':len(index),'historical_observed_pit_ready':False})
        (out/'industry_prompt.txt').write_text(PROMPT,encoding='utf-8')
    original={}
    with zipfile.ZipFile(out/'raw_news.zip') as archive:
        for entry in index:
            raw=archive.read(entry['archive_path'])
            if hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('Frozen input hash mismatch')
            row=json.loads(raw.decode('utf-8-sig')); original[row['source_id']]=row
    def stats():
        records=[load(p) for p in sorted((out/'analysis').glob('*.json'))] if (out/'analysis').exists() else []
        counts=Counter(r['status'] for r in records)
        result={'total':len(original),'processed':len(records),'status_counts':dict(counts),'pending':len(original)-len(records),
          'articles_with_dictionary_mentions':sum(bool(r.get('industries')) for r in records),
          'articles_with_industry_impacts':sum(bool(r.get('industry_impacts')) for r in records),
          'industry_impacts':sum(len(r.get('industry_impacts',[])) for r in records),'updated_at':now()}
        save(out/'progress.json',result);return result
    count=0;consecutive=0;started=time.monotonic()
    for sid,row in sorted(original.items()):
        dest=out/'analysis'/(sid+'.json')
        if dest.exists():
            previous=load(dest)
            if previous.get('record_hash')!=digest(row) or previous.get('fingerprint')!=fingerprint:raise ValueError('Existing analysis binding mismatch')
            if previous['status']=='completed' or not args.retry_failed:continue
        if args.limit is not None and count>=args.limit:break
        summary=row.get('\u4e3b\u8981\u5185\u5bb9') or ''; title=row.get('title') or ''
        candidates=dictionary.candidates(summary,cfg['candidate_limit'],cfg['active_standards'],headline=title)
        result={'source_id':sid,'record_hash':digest(row),'summary_sha256':digest(summary),'fingerprint':fingerprint,
          'source_published_at':row.get('\u65f6\u95f4'),'text_basis':'stored_summary_only','historical_observed_pit_ready':False,
          'dictionary':{'sha256':dictionary.sha256,'version':dictionary.manifest['version']},'candidate_keys':[v['key'] for v in candidates],
          'model':cfg['model'],'model_digest':chosen['digest'],'industries':[],'industry_impacts':[],'status':'completed','llm_inferred_at':None}
        try:
            if len(summary)>cfg['maximum_summary_characters']: raise ValueError('Summary exceeds configured lossless input budget')
            if candidates and sid not in QUARANTINE:
                schema=response_schema([r['key'] for r in candidates]);payload={'source_id':sid,'title':title,'summary':summary,'candidates':candidates}
                response=http('/api/chat',{'model':cfg['model'],'stream':False,'think':False,'keep_alive':cfg['keep_alive'],
                  'messages':[{'role':'system','content':PROMPT},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
                  'format':schema,'options':{'temperature':0,'seed':42,'num_ctx':cfg['num_ctx'],'num_predict':cfg['num_predict']}})
                result['llm_inferred_at']=now();raw_response=response['message']['content'];value=json.loads(raw_response)
                mentions,flags=validate(value,summary,candidates,dictionary,cfg['min_confidence'],sid)
                result.update(industries=mentions,quality_flags=flags,response_sha256=hashlib.sha256(raw_response.encode()).hexdigest(),
                  model_duration_seconds=response.get('total_duration',0)/1e9)
                for item in mentions:
                    if item['propagation_approved']:
                        result['industry_impacts'].append(dict(item,industry_name_original=item['industry_name_standard'],
                          industry_name=item['industry_name_standard'],quote=item['quote'],evidence=item['quote'],
                          industry_scope_quote=item['scope_quote'],evidence_type='inferred',impact_role='affected',
                          horizon='unspecified',llm_inferred_at=result['llm_inferred_at']))
            else: result['quality_flags']=['semantic_quarantine' if sid in QUARANTINE else 'no_dictionary_candidates']
            consecutive=0
        except Exception as exc:
            result.update(status='needs_review',error_type=type(exc).__name__,error_code='industry_response_or_service_validation_failed')
            consecutive+=1
        result['feature_available_at']=now();save(dest,result);count+=1
        if count%cfg['checkpoint_every']==0 or count==1:
            print(json.dumps(dict(stats(),new_calls=count,elapsed_seconds=round(time.monotonic()-started,1))),flush=True)
        if consecutive>=cfg['max_consecutive_failures']:break
    result=stats();unchanged=all(file_sha(r['path'])==r['sha256'] for r in index)
    result['original_inputs_unchanged']=unchanged
    rows(out/'industry_classifications.jsonl',[load(p) for p in sorted((out/'analysis').glob('*.json'))])
    files={str(p.relative_to(out)).replace('\\','/'):file_sha(p) for p in out.rglob('*') if p.is_file() and p.name not in ('manifest.json','progress.json') and not p.name.endswith('.tmp')}
    save(out/'manifest.json',dict(kind=VERSION,created_at=now(),fingerprint=fingerprint,files=files,**result))
    print(json.dumps(result),flush=True)
    if not unchanged:raise ValueError('Original input changed during processing')

if __name__=='__main__':main()

"""Offline acceptance of a frozen retrace batch; never uses Ollama or the network."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from finance_agent.core import digest
from market_scope import VERSION


def load(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(data):return hashlib.sha256(data).hexdigest()


def verify(directory):
    directory=Path(directory).resolve();manifest=load(directory/'manifest.json');errors=[]
    for name,expected in manifest['files'].items():
        path=(directory/name).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or sha(path.read_bytes())!=expected:
            errors.append('file_missing_or_changed:'+name)
    inventory=load(directory/'input_index.json');analyses={p.stem:load(p) for p in (directory/'analysis').glob('*.json')}
    if set(analyses)-set(inventory):errors.append('unknown_analysis_source')
    completed={k:v for k,v in analyses.items() if v['status']=='completed'}
    actual=Counter(v['status'] for v in analyses.values())
    if manifest['processed']!=len(analyses) or manifest['pending']!=len(inventory)-len(analyses) or manifest['status_counts']!=dict(actual):
        errors.append('progress_count_mismatch')
    with zipfile.ZipFile(directory/'raw_news.zip') as z:
        if z.testzip():errors.append('raw_snapshot_crc_failed')
        raw={Path(name).stem:json.loads(z.read(name)) for name in z.namelist() if not name.startswith('originals/')}
        if set(raw)!=set(inventory):errors.append('raw_snapshot_source_mismatch')
        for sid,inputs in inventory.items():
            for item in inputs:
                member='originals/'+item['path']
                if member not in z.namelist() or sha(z.read(member))!=item['sha256']:
                    errors.append('raw_replica_bytes_missing_or_changed:'+sid)
    for sid,result in analyses.items():
        if result['source_record_hash']!=digest(raw[sid]):errors.append('raw_record_hash_mismatch:'+sid)
        pin=result.get('old_audit')
        if pin and sha((directory/pin['bundled_path']).read_bytes())!=pin['sha256']:errors.append('legacy_audit_pin_invalid:'+sid)
        proof=load(directory/'evidence'/(sid+'.json'))
        if proof.get('record_hash')!=digest(raw[sid]) or proof.get('schema_version')!=VERSION:
            errors.append('original_proof_binding_invalid:'+sid)
        for item in proof['evidence']:
            if sha(item['quote'].encode())!=item['quote_sha256']:errors.append('quote_hash_mismatch:'+sid)
        if sid in completed:
            row=load(directory/'adapted/data/US'/(sid+'.json'));audit=load(directory/'adapted/audit'/(sid+'.json'))
            proof=load(directory/'adapted/evidence'/(sid+'.json'))
            if audit['record_hash']!=digest(row) or proof['record_hash']!=digest(row):errors.append('adapted_proof_binding_invalid:'+sid)
            if row['主要内容']!=raw[sid]['主要内容'] or row['时间']!=raw[sid]['时间'] or row.get('股票实体')!=raw[sid].get('股票实体'):
                errors.append('immutable_article_fields_changed:'+sid)
            if manifest.get('classification_contract')=='dictionary-scope-replacement-1':
                from classification_projection import replace_classification
                try:
                    expected,_=replace_classification(raw[sid],audit)
                    if any(row[k]!=expected[k] for k in ('涉及行业','影响范围')):
                        errors.append('classification_replacement_mismatch:'+sid)
                    if audit.get('classification_replacement',{}).get('previous_industries')!=raw[sid].get('涉及行业',[]):
                        errors.append('old_classification_audit_missing:'+sid)
                    if audit.get('classification_replacement',{}).get('source_record_hash')!=digest(raw[sid]):
                        errors.append('old_classification_record_pin_mismatch:'+sid)
                except ValueError:errors.append('fresh_classification_missing:'+sid)
            pin=audit.get('evidence_pack',{})
            if pin.get('sha256')!=sha((directory/pin.get('path','')).read_bytes()):errors.append('evidence_pack_pin_invalid:'+sid)
    from structured_export import arrow,validate_tables
    pa,pq=arrow();projection=load(directory/'structured/manifest.json')
    tables={}
    for name,expected in projection['files'].items():
        data=(directory/'structured'/name).read_bytes()
        if sha(data)!=expected:errors.append('parquet_sha_mismatch:'+name)
        tables[Path(name).stem]=pq.read_table(pa.BufferReader(data)).to_pylist()
    validate_tables(tables)
    if {r['source_id'] for r in tables['article_metadata']}!=set(completed):errors.append('parquet_article_axis_mismatch')
    if manifest['pending']==0:
        bundle=directory/'dataset_bundle.zip'
        if not bundle.is_file():errors.append('completed_batch_bundle_missing')
        else:
            with zipfile.ZipFile(bundle) as z:
                if z.testzip():errors.append('bundle_crc_failed')
                for name in z.namelist():
                    p=(directory/name).resolve()
                    if not p.is_relative_to(directory) or sha(z.read(name))!=sha(p.read_bytes()):errors.append('bundle_member_mismatch:'+name)
    return {'passed':not errors,'total':len(inventory),'processed':len(analyses),'pending':len(inventory)-len(analyses),
        'completed':len(completed),'errors':errors,'strict_historical_PIT':False,'model_calls':0,'network_requests':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
    report=verify(args.directory);print(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['passed']:raise SystemExit(1)

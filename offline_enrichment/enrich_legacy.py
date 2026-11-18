"""Read-only legacy inputs, separate resumable enrichment and per-record provenance."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Literal

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pydantic import Field
from finance_agent.core import bounded, digest, dump, now, Lock
from qwen_agent import Qwen, Strict, IndustryTopic, tokens, source_cut, ModelServiceError, OutputError
from security_grounding import grounded_securities, retain_explicit_tickers
from analysis_metadata import grounded_industries, public_analysis, validate_public_analysis, validate_scopes
from record_files import Dataset
from record_time import publication_precision, utc_timestamp
from simple_crawler import load_config
from record_quality import language_hint

VERSION = 'legacy-offline-enrichment-1'
PROMPT = (Path(__file__).with_name('prompt.md')).read_text(encoding='utf-8')
FIELDS = ['source_id', 'url', 'title', '时间', '分类', '关键词', '主要内容',
          '时间信息', '股票实体', '涉及行业', '影响范围']


class Entity(Strict):
    name: str = Field(min_length=1,max_length=200)
    kind: Literal['company','issuer','brand','organization','person','product','other']
    quote: str = Field(max_length=180)


class Metadata(Strict):
    useful: bool
    reason: str = Field(max_length=160)
    entities: list[Entity] = Field(default_factory=list,max_length=12,
        description='Named companies, issuers, brands or organizations in this text. Names are required even with no stock code.')
    industries: list[IndustryTopic] = Field(default_factory=list, max_length=6)
    impact_scope: list[Literal['company', 'industry', 'market']] = Field(default_factory=list)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def crawler_fingerprints(root):
    paths = list(root.glob('*.py')) + list((root/'finance_agent').glob('*.py'))
    paths += list((root/'prompts').glob('*.md'))
    paths += [root/'config.yaml', root/'requirements.txt', root/'AGENTS.md']
    return {str(path.relative_to(root)): sha(path) for path in paths if path.is_file()}


def inventory(root):
    jobs = {}
    for market in ['US', 'GLOBAL']:
        folder = root/'data'/(market+'_original')
        if not folder.is_dir():
            raise ValueError('Missing input directory: '+str(folder))
        for path in sorted(folder.rglob('*.json')):
            row = read_json(path)
            Dataset.validate(market, row)
            identity = row['source_id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity):
                raise ValueError('Unsafe source_id: '+str(path))
            entry = jobs.setdefault(identity, {'row': row, 'inputs': [], 'audit': {}})
            if digest(entry['row']) != digest(row):
                raise ValueError('Conflicting copies of source_id: '+identity)
            entry['inputs'].append({'market': market, 'path': str(path.relative_to(root)),
                                    'sha256': sha(path), 'filename': path.name})
    for identity, entry in jobs.items():
        path = root/'.runtime/simple/audit'/(identity+'.json')
        if path.is_file():
            audit = read_json(path)
            if (audit.get('record_hash') == digest(entry['row'])
                    and audit.get('source_published_at') == entry['row']['时间']
                    and audit.get('url') == entry['row']['url']):
                entry['audit'] = audit
                entry['audit_sha256'] = sha(path)
        entry['input_hash'] = digest(entry['inputs'])
    return jobs


def verified_legacy_entities(row, audit):
    entities = []
    flags = []
    for item in audit.get('securities', []):
        checked = grounded_securities([item], item.get('quote', ''))
        if any(entity.get('ticker') != item.get('ticker') for entity in checked):
            flags.append('legacy_security_attribute_unverified')
        entities.extend(checked)
    # Names are preserved even when the summary omitted the original declaration.
    for item in row.get('股票实体', []):
        name = item.get('名称')
        if not name:
            continue
        if not any((entity.get('name') or '').casefold() == name.casefold() for entity in entities):
            entities.append({'name': name, 'ticker': None, 'exchange': None, 'quote': name})
            if item.get('股票代码'):
                flags.append('legacy_security_attribute_unverified')
    return entities, flags


def merge_entities(items):
    selected = {}
    conflicts = set()
    for item in items:
        key = (item.get('name') or item.get('ticker') or '').casefold()
        if not key:
            continue
        previous = selected.get(key)
        if key in conflicts:
            continue
        if previous and previous.get('ticker') and item.get('ticker') and previous['ticker'] != item['ticker']:
            selected[key] = dict(previous, ticker=None, exchange=None)
            conflicts.add(key)
        elif previous is None or (not previous.get('ticker') and item.get('ticker')):
            selected[key] = item
    return list(selected.values())


def legacy_analysis(entry):
    row, audit = entry['row'], entry['audit']
    entities, flags = verified_legacy_entities(row, audit)
    sectors = [{'name': item['名称'], 'basis': item['依据'], 'quote': ''}
               for item in row.get('涉及行业', [])]
    known = {item['name'].casefold() for item in sectors}
    for item in audit.get('industries', []):
        if item.get('name') and item['name'].casefold() not in known:
            sectors.append(item)
            known.add(item['name'].casefold())
    scopes = validate_scopes(row.get('影响范围', [])+audit.get('impact_scope', []))
    return {'useful': None, 'reason': 'Recovered from unchanged legacy record and matching audit',
            'securities': merge_entities(entities), 'industries': sectors, 'impact_scope': scopes,
            'method': 'verified_legacy_metadata', 'quality_flags': flags}


def infer(model, row):
    text = row['主要内容']
    base = {'source_id': row['source_id'], 'published_at': row['时间'], 'category': row['分类'],
            'enrichment_version': VERSION, 'content_hash': digest(text)}
    # All text is read. Long inputs are split losslessly; metadata merges in Python.
    overhead = tokens(PROMPT)+tokens(json.dumps(base, ensure_ascii=False))+800+350+220
    allowance = model.cfg.num_ctx-overhead
    if allowance < 300:
        raise ValueError('Insufficient context for offline metadata extraction')
    chunks = []
    start = 0
    while start < len(text):
        end = start
        spent = 0
        while end < len(text) and spent+(1.5 if ord(text[end]) > 127 else .35) < allowance:
            spent += 1.5 if ord(text[end]) > 127 else .35
            end += 1
        end = source_cut(text, start, end)
        chunks.append(text[start:end])
        start = end
    notes = []
    timestamps = []
    work=[(chunk,0) for chunk in chunks]
    index=0
    while index<len(work):
        chunk,depth=work[index]
        def validate(value):
            raw=[item for item in value['entities'] if item['kind'] in {'company','issuer','brand','organization'}]
            checked=grounded_securities(raw,chunk)
            value['entities']=[]
            for item in checked:
                if not item.get('name'):continue
                original=next(candidate for candidate in raw if candidate['name'].strip()==item['name'])
                person_role=re.escape(item['name'])+r',?\s+(?:Chief|President|Vice President|Senior Vice President|General Counsel|董事长|首席执行官)\b'
                if re.search(person_role,item['quote'],re.I):continue
                value['entities'].append({'name':item['name'],'quote':item['quote'],'kind':original['kind']})
            value['industries'] = grounded_industries(value['industries'], chunk)
            value['impact_scope'] = validate_scopes(value['impact_scope'])
            if language_hint(chunk)=='en' and any(re.search(r'[\u4e00-\u9fff]',item['name']) and item['name'] not in chunk for item in value['industries']):
                raise ValueError('Use English industry names for the English input; preserve its language')
            return value
        before = model.calls
        try:
            value = model.ask('旧记录元数据增强', PROMPT, dict(base, text=chunk), Metadata, validate, max_predict=800)
        except OutputError as error:
            if '截断' not in str(error) or depth>=2 or len(chunk)<400:
                raise
            middle=source_cut(chunk,0,len(chunk)//2)
            work[index:index+1]=[(chunk[:middle],depth+1),(chunk[middle:],depth+1)]
            continue
        if model.calls > before:
            stamp = now()
        else:
            stamp = cached_inference_time(model.root, row['source_id'], base['content_hash'])
        if stamp:
            timestamps.append(stamp)
        notes.append(value)
        index+=1
    merged = {'useful': any(note['useful'] for note in notes),
              'reason': '; '.join(dict.fromkeys(note['reason'] for note in notes))[:240],
              'securities': merge_entities([entity for note in notes for entity in grounded_securities(note['entities'],text)]),
              'industries': [], 'impact_scope': validate_scopes([scope for note in notes for scope in note['impact_scope']])}
    sectors = {}
    for note in notes:
        for item in note['industries']:
            key = item['name'].casefold()
            if key not in sectors or item['basis'] == 'explicit':
                sectors[key] = item
    merged['industries'] = list(sectors.values())
    merged['securities']=retain_explicit_tickers(merged['securities'],text,selected_text=text)
    if any(item['kind'] in {'company','issuer','brand'} for note in notes for item in note['entities']):
        if 'company' not in merged['impact_scope']:
            merged['impact_scope']=validate_scopes(merged['impact_scope']+['company'])
            merged['company_scope_basis']='literal_corporate_entity_mention'
    merged['entity_types']={item['name']:item['kind'] for note in notes for item in note['entities']}
    merged['method']='local_qwen_legacy_summary'
    return merged, max(timestamps) if timestamps else None, len(work)


def cached_inference_time(root, identity, content_hash):
    index = root/'.runtime/simple/model_cache_index'/(identity+'.json')
    stamps = []
    if index.is_file():
        for item in read_json(index):
            path = bounded(root/item['path'], root)
            if not path.is_file() or sha(path) != item['sha256']:
                continue
            cached = read_json(path)
            if (cached.get('source_id') == identity and cached.get('content_hash') == content_hash
                    and cached.get('llm_inferred_at')):
                stamps.append(cached['llm_inferred_at'])
    return max(stamps) if stamps else None


def build_record(entry, analysis, stamp):
    original, audit = entry['row'], entry['audit']
    row = {key: original.get(key) for key in FIELDS}
    title = original.get('title') or audit.get('title')
    row['title'] = title.strip() if isinstance(title, str) and title.strip() else None
    old_entities, flags = verified_legacy_entities(original, audit)
    new_entities = analysis.get('securities', [])
    assignments = {}
    for item in old_entities+new_entities:
        if item.get('name') and item.get('ticker'):
            assignments.setdefault(item['name'].casefold(),set()).add(item['ticker'])
    if any(len(values)>1 for values in assignments.values()):
        flags.append('security_evidence_conflict')
    merged = merge_entities(old_entities+new_entities)
    metadata = public_analysis({'securities': merged, 'industries': analysis.get('industries', []),
                                'impact_scope': analysis.get('impact_scope', [])})
    old_sectors = {item['名称'].casefold(): item for item in original.get('涉及行业', [])}
    for item in metadata['涉及行业']:
        key = item['名称'].casefold()
        if key not in old_sectors or old_sectors[key]['依据'] == 'legacy_metadata' or item['依据'] == 'explicit':
            old_sectors[key] = item
    row['股票实体'] = metadata['股票实体']
    row['涉及行业'] = list(old_sectors.values())
    row['影响范围'] = validate_scopes(original.get('影响范围', [])+metadata['影响范围'])
    old_times = original.get('时间信息', {})
    row['时间信息'] = {'publication_precision': publication_precision(row['时间']),
        'first_seen_at': old_times.get('first_seen_at'), 'fetched_at': old_times.get('fetched_at'),
        'feature_available_at': stamp, 'availability_basis': 'migration'}
    if not title:
        flags.append('title_unavailable_offline')
    if not audit:
        flags.append('matching_legacy_audit_unavailable')
    if not row['股票实体']:
        flags.append('no_verified_company_entity')
    if not row['涉及行业']:
        flags.append('industry_unknown')
    if not row['影响范围']:
        flags.append('impact_scope_unknown')
    for key in ['first_seen_at', 'fetched_at']:
        if row['时间信息'][key]:
            row['时间信息'][key] = utc_timestamp(row['时间信息'][key])
    Dataset.validate('US', row)
    validate_public_analysis(row, strict_codes=True)
    # Identity, publication, original prose and keywords are immutable inputs.
    for key in ['source_id', 'url', '时间', '分类', '关键词', '主要内容']:
        if row[key] != original[key]:
            raise ValueError('Original content changed: '+key)
    return row, sorted(set(flags))


def save_result(root, output, identity, entry, analysis, model, inferred_at, status, reason=None):
    stamp = now()
    row, flags = build_record(entry, analysis, stamp)
    if reason:
        flags.append(reason)
    targets = []
    paths = []
    for item in entry['inputs']:
        path = bounded(output/item['market']/item['filename'], output)
        if path.is_file() and read_json(path).get('source_id') != identity:
            raise ValueError('Output filename belongs to another source_id')
        dump(path, row)
        if read_json(path) != row:
            raise OSError('Output read-back failed')
        paths.append(path)
    # Match the existing collector's commit protocol in this separate output:
    # first verify all payloads, then stamp availability and publish their hashes.
    stamp=now()
    row['时间信息']['feature_available_at']=stamp
    for path in paths:
        dump(path,row)
        if read_json(path)!=row:raise OSError('Final output read-back failed')
        targets.append({'path':str(path.relative_to(output)),'sha256':sha(path)})
    original = entry['row']
    audit = {'source_id': identity, 'status': status, 'schema_version': VERSION,
        'original_files': entry['inputs'], 'original_record_hash': digest(original),
        'input_hash': entry['input_hash'], 'matching_audit_hash': entry.get('audit_sha256'),
        'legacy_producer':{key:entry['audit'].get(key) for key in ['model','model_digest','source_body_hash',
            'raw_sha256','date_quote','date_kind','source_document_url','publication_validation_version','security_validation_version']},
        'legacy_security_evidence':verified_legacy_entities(original,entry['audit'])[0],
        'record_hash': digest(row), 'outputs': targets, 'analysis_input': 'legacy_summary',
        'source_verification': 'original_webpage_not_revisited', 'published_at': row['时间'],
        'evidence_sources':{'legacy_metadata':'original_record_and_matching_audit',
                            'new_semantic_quotes':'legacy_summary'},
        'publication_basis': 'unchanged_legacy_record', 'timezone': source_timezone(row['时间']),
        'original_time_information': original.get('时间信息'),
        'llm_inferred_at': inferred_at, 'feature_available_at': stamp, 'commit_verified_at':stamp,
        'model': model.cfg.model if model else None, 'model_digest': model.model_digest if model else None,
        'prompt_version': digest(PROMPT), 'code_version': sha(Path(__file__)),
        'economic_direction_is_not_return_label': True, 'quality_flags': sorted(set(flags)),
        'event_and_economic_inference':'separate_downstream_stage',
        'canonical_market': 'GLOBAL' if any(item['market']=='GLOBAL' for item in entry['inputs']) else 'US',
        'analysis': analysis}
    dump(output/'analysis'/(identity+'.json'), audit)
    return audit


def source_timezone(value):
    if 'T' not in value:
        return None
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.utcoffset() is None:
        return None
    return stamp.strftime('%z')[:3]+':'+stamp.strftime('%z')[3:]


def completed(output, identity, entry):
    path = output/'analysis'/(identity+'.json')
    if not path.is_file():
        return False
    item = read_json(path)
    if (item.get('input_hash') != entry['input_hash'] or item.get('status') != 'completed'
            or item.get('code_version')!=sha(Path(__file__)) or item.get('prompt_version')!=digest(PROMPT)):
        return False
    if not item.get('outputs'):
        return False
    return all(bounded(output/target['path'],output).is_file() and sha(output/target['path'])==target['sha256']
               for target in item.get('outputs', []))


def totals(output, jobs):
    status=Counter();coverage=Counter();physical=0
    for identity in jobs:
        audit_path=output/'analysis'/(identity+'.json')
        if not audit_path.is_file():
            status['missing']+=1
            continue
        audit=read_json(audit_path)
        status[audit['status']]+=1
        coverage['audit_only']+=audit['analysis'].get('method')=='verified_legacy_metadata'
        coverage['qwen_analyzed']+=audit['analysis'].get('method')=='local_qwen_legacy_summary'
        for target in audit['outputs']:
            path=bounded(output/target['path'],output)
            if not path.is_file() or sha(path)!=target['sha256']:
                status['output_hash_mismatch']+=1
            physical+=1
        if not audit['outputs']:
            continue
        row=read_json(output/audit['outputs'][0]['path'])
        coverage['title_known']+=bool(row['title'])
        coverage['entities_known']+=bool(row['股票实体'])
        coverage['industries_known']+=bool(row['涉及行业'])
        coverage['scope_known']+=bool(row['影响范围'])
        coverage['useful_true']+=audit['analysis'].get('useful') is True
        coverage['useful_false']+=audit['analysis'].get('useful') is False
    return {'status_counts':dict(status),'coverage':dict(coverage),'physical_output_files':physical}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent legacy JSON enrichment, existing crawler files are read-only')
    parser.add_argument('--output', default='data_enriched')
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args(argv)
    output = bounded(PROJECT/args.output, PROJECT)
    if output == PROJECT or output.is_relative_to(PROJECT/'data') or output.is_relative_to(PROJECT/'.runtime/simple'):
        raise ValueError('Output must be a separate project directory')
    jobs = inventory(PROJECT)
    snapshot = {identity: {'inputs': entry['inputs'], 'record_hash': digest(entry['row'])} for identity,entry in jobs.items()}
    before = crawler_fingerprints(PROJECT)
    output.mkdir(parents=True, exist_ok=True)
    with Lock(output):
        manifest_path = output/'manifest.json'
        manifest=read_json(manifest_path) if manifest_path.is_file() else {}
        if manifest and manifest.get('input_snapshot') != snapshot:
            raise ValueError('Input snapshot differs from the existing enrichment run')
        dump(manifest_path, {'version': VERSION, 'input_snapshot': snapshot,
            'crawler_fingerprints': before, 'created_at':manifest.get('created_at',now()), 'prompt_version': digest(PROMPT),
            'retrospective_only_for_historical_publication_backfill': True})
        if args.preview:
            print(json.dumps({'unique_records':len(jobs), 'physical_inputs':sum(len(x['inputs']) for x in jobs.values()),
                'missing_title':sum(not x['row'].get('title') for x in jobs.values()),
                'matching_audits':sum(bool(x['audit']) for x in jobs.values()), 'output':str(output)},ensure_ascii=False))
            return 0
        cfg = load_config(PROJECT/'config.yaml')
        manifest=read_json(manifest_path)
        manifest['ollama_configuration']={'model':cfg.ollama.model,'num_ctx':cfg.ollama.num_ctx,
            'num_predict':min(800,cfg.ollama.num_predict),'think':False,'temperature':0,'seed':42}
        dump(manifest_path,manifest)
        model_root = output/'.work'
        model_root.mkdir(exist_ok=True)
        model = Qwen(cfg.ollama, model_root)
        stats = Counter(); failures = []; started = time.monotonic()
        try:
            # Publish a complete format-only view first. Already sufficient,
            # verified metadata needs no additional model inference.
            for identity,entry in jobs.items():
                if completed(output,identity,entry):
                    continue
                baseline=legacy_analysis(entry)
                ready=all(baseline[key] for key in ['securities','industries','impact_scope'])
                save_result(PROJECT,output,identity,entry,baseline,None,None,'completed' if ready else 'pending')
                stats['audit_only']+=ready
            print(f"[legacy enrichment] format_ready={len(jobs)} audit_only={stats['audit_only']} starting_qwen",flush=True)
            for identity, entry in jobs.items():
                if completed(output, identity, entry):
                    stats['reused']+=1
                    continue
                if args.limit and stats['attempted']>=args.limit:
                    break
                stats['attempted']+=1
                for item in entry['inputs']:
                    if sha(PROJECT/item['path']) != item['sha256']:
                        raise ValueError('Input file changed during enrichment')
                model.calls = 0
                try:
                    baseline=legacy_analysis(entry)
                    needs_model=not all(baseline[key] for key in ['securities','industries','impact_scope'])
                    if needs_model:
                        analysis, inferred, chunks = infer(model, entry['row'])
                    else:
                        analysis,inferred,chunks=baseline,None,0
                        stats['audit_only']+=1
                    audit = save_result(PROJECT, output, identity, entry, analysis, model, inferred, 'completed')
                    stats['completed']+=1
                    stats['useful']+=bool(analysis['useful'])
                    stats['chunks']+=chunks
                except ModelServiceError as error:
                    stats['service_pause']+=1
                    stats['model_calls']+=model.calls
                    failures.append({'source_id':identity,'type':'model_service_error','reason':str(error)[:180]})
                    break
                except (ValueError, OSError, KeyError) as error:
                    audit = save_result(PROJECT, output, identity, entry, legacy_analysis(entry), model, None, 'needs_review', type(error).__name__)
                    stats['needs_review']+=1
                    failures.append({'source_id':identity,'type':type(error).__name__,'reason':str(error)[:180]})
                stats['model_calls']+=model.calls
                stats['model_cache_hits']=model.cache_hits
                if stats['attempted']%25==0:
                    elapsed=time.monotonic()-started
                    print(f"[legacy enrichment] attempted={stats['attempted']} reused={stats['reused']} total={len(jobs)} completed={stats['completed']} review={stats['needs_review']} elapsed={elapsed:.0f}s",flush=True)
        finally:
            model.close()
            changed_inputs=[item['path'] for entry in jobs.values() for item in entry['inputs']
                            if not (PROJECT/item['path']).is_file() or sha(PROJECT/item['path'])!=item['sha256']]
            changed_code=[name for name, expected in before.items() if not (PROJECT/name).is_file() or sha(PROJECT/name)!=expected]
            summary={'finished_at':now(),'run_counts':dict(stats),'input_files_changed':changed_inputs,
                'crawler_files_changed':changed_code,'failures':failures,'total_unique_inputs':len(jobs),
                'elapsed_seconds':time.monotonic()-started,'output':str(output)}
            summary.update(totals(output,jobs))
            summary['status']='partial' if summary['status_counts'].get('pending') or summary['status_counts'].get('missing') or stats['service_pause'] else 'completed'
            if summary['status']=='completed' and summary['status_counts'].get('needs_review'):
                summary['status']='completed_with_review'
            dump(output/'report.json',summary)
    print(json.dumps(summary,ensure_ascii=False))
    return 1 if changed_inputs or changed_code else 2 if summary['status']=='partial' else 0


if __name__=='__main__':
    raise SystemExit(main())

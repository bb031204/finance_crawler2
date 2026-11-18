"""Read-only acceptance checks for the independent legacy enrichment outputs."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from offline_enrichment.enrich_legacy import FIELDS, sha, read_json
from finance_agent.core import bounded, digest, dump, now
from record_files import Dataset
from analysis_metadata import validate_public_analysis


def verify(output):
    manifest = read_json(output / 'manifest.json')
    errors = []
    counts = Counter()
    flags = Counter()
    status = Counter()
    coverage_before = Counter()
    coverage_after = Counter()
    samples = []
    targets = set()

    def check(condition, message):
        if not condition:
            errors.append(message)

    for identity, snapshot in manifest['input_snapshot'].items():
        counts['unique_articles'] += 1
        audit = read_json(output / 'analysis' / (identity + '.json'))
        status[audit['status']] += 1
        flags.update(audit['quality_flags'])
        check(audit['source_id'] == identity, identity + ': sidecar identity')
        check(audit['input_hash'] == digest(snapshot['inputs']), identity + ': input manifest')
        check(audit['original_record_hash'] == snapshot['record_hash'], identity + ': original record hash')
        check(audit['code_version'] == sha(PROJECT / 'offline_enrichment/enrich_legacy.py'), identity + ': script version')
        check(audit['prompt_version'] == manifest['prompt_version'], identity + ': prompt version')
        if audit.get('refinement'):
            from offline_enrichment.refine_metadata import PROMPT as REVIEW_PROMPT, ReviewMetadata
            refinement = audit['refinement']
            check(refinement['status'] == 'completed', identity + ': incomplete refinement')
            check(refinement['code_version'] == sha(PROJECT / 'offline_enrichment/refine_metadata.py'), identity + ': refinement script version')
            check(refinement['prompt_version'] == digest(REVIEW_PROMPT), identity + ': refinement prompt version')
            check(refinement['schema_hash'] == digest(ReviewMetadata.model_json_schema()), identity + ': refinement schema version')
            check(refinement['llm_inferred_at'] == audit['llm_inferred_at'], identity + ': refinement inference time')
            counts['targeted_refinements'] += 1
        check(audit['source_verification'] == 'original_webpage_not_revisited', identity + ': provenance')
        original = None
        for item in snapshot['inputs']:
            path = bounded(PROJECT / item['path'], PROJECT / 'data')
            check(sha(path) == item['sha256'], identity + ': original bytes changed')
            source = read_json(path)
            check(digest(source) == snapshot['record_hash'], identity + ': original JSON changed')
            check(original is None or source == original, identity + ': conflicting source copies')
            original = source
            counts['original_files'] += 1

        check(audit['original_time_information'] == original.get('\u65f6\u95f4\u4fe1\u606f'), identity + ': legacy times')
        check(audit['published_at'] == original['\u65f6\u95f4'], identity + ': publication time')
        if audit.get('matching_audit_hash'):
            path = PROJECT / '.runtime/simple/audit' / (identity + '.json')
            check(sha(path) == audit['matching_audit_hash'], identity + ': matching legacy audit changed')
        copies = []
        expected = {str(Path(item['market']) / item['filename']) for item in snapshot['inputs']}
        check({item['path'] for item in audit['outputs']} == expected, identity + ': output paths')
        for item in audit['outputs']:
            path = bounded(output / item['path'], output)
            check(str(path) not in targets, identity + ': duplicate output path')
            targets.add(str(path))
            check(sha(path) == item['sha256'], identity + ': output bytes')
            copies.append(path.read_bytes())
            row = read_json(path)
            Dataset.validate(path.parent.name, row)
            validate_public_analysis(row, strict_codes=True)
            check(set(row) == set(FIELDS), identity + ': eleven-field schema')
            check(digest(row) == audit['record_hash'], identity + ': output record hash')
            for key in ['source_id', 'url', '\u65f6\u95f4', '\u5206\u7c7b', '\u5173\u952e\u8bcd', '\u4e3b\u8981\u5185\u5bb9']:
                check(row[key] == original[key], identity + ': changed ' + key)
            times = row['\u65f6\u95f4\u4fe1\u606f']
            check(times['feature_available_at'] == audit['feature_available_at'] == audit['commit_verified_at'], identity + ': availability stamp')
            available = datetime.fromisoformat(audit['feature_available_at'])
            check(available.tzinfo is not None, identity + ': missing availability timezone')
            for stamp in [times.get('first_seen_at'), times.get('fetched_at'), audit.get('llm_inferred_at')]:
                if stamp:
                    check(datetime.fromisoformat(stamp) <= available, identity + ': available before processing')
            counts['output_files'] += 1
        check(len(set(copies)) == 1, identity + ': GLOBAL and US copy mismatch')
        for label, key in [('entities', '\u80a1\u7968\u5b9e\u4f53'), ('industries', '\u6d89\u53ca\u884c\u4e1a'), ('scope', '\u5f71\u54cd\u8303\u56f4'), ('title', 'title')]:
            coverage_before[label] += bool(original.get(key))
            coverage_after[label] += bool(row.get(key))
        counts[audit['analysis']['method']] += 1
        counts['useful_true'] += audit['analysis'].get('useful') is True
        counts['useful_false'] += audit['analysis'].get('useful') is False
        if audit['analysis']['method'] == 'local_qwen_legacy_summary':
            check(bool(audit['llm_inferred_at']) and bool(audit['model_digest']), identity + ': missing model provenance')
            if len(samples) < 6 and row['\u80a1\u7968\u5b9e\u4f53']:
                samples.append({'source_id': identity, 'path': audit['outputs'][0]['path'],
                    'entities': row['\u80a1\u7968\u5b9e\u4f53'], 'industries': row['\u6d89\u53ca\u884c\u4e1a']})

    for name, expected in manifest['crawler_fingerprints'].items():
        check(sha(PROJECT / name) == expected, 'crawler file changed: ' + name)
    actual = {str(path) for market in ['US', 'GLOBAL'] for path in (output / market).glob('*.json')}
    check(actual == targets, 'untracked or missing output JSON')
    check(set(status) == {'completed'}, 'unfinished or failed articles')
    result = {'validated_at': now(), 'passed': not errors, 'counts': dict(counts),
        'status_counts': dict(status), 'coverage_before': dict(coverage_before),
        'coverage_after': dict(coverage_after), 'quality_flags': dict(flags),
        'crawler_files_checked': len(manifest['crawler_fingerprints']),
        'errors': errors, 'samples': samples,
        'limitations': ['Offline evidence only; original webpages were not revisited.',
            'Unknown original titles are null; unknown stock identities are not guessed.',
            'Legacy summaries and source publication times were preserved, not fact-reverified.',
            'Current enrichment availability cannot be backdated to historical publication.']}
    dump(output / 'validation.json', result)
    print(json.dumps({key: value for key, value in result.items() if key != 'samples'}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(verify(PROJECT / 'data_enriched'))

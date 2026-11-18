"""Versioned dictionary selection and bounded article evidence; no model or ticker inference."""
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone

SCHEMA='industry_dictionary_v1'
STOP={'and','the','of','for','in','to','a','an','with','other','services','service','nec','not','elsewhere','classified'}
GENERIC={'industry','industries','company','companies','business','manufacturing','manufacturers','related','miscellaneous'}

def words(value):
    return {w[:-1] if w.endswith('s') and len(w)>4 else w for w in re.findall(r'[a-z0-9]+',value.casefold()) if w not in STOP}

def compact(value):return re.sub(r'\s+',' ',value).strip()

def parse_json(value):
    def reject(value):raise ValueError('Nonfinite industry dictionary JSON')
    return json.loads(value,parse_constant=reject)

class IndustryDictionary:
    def __init__(self,directory,manifest_name='manifest.json',dictionary_name='industry_dictionary.jsonl',verify_sources=None):
        self.directory=Path(directory).resolve()
        if any(Path(n).name!=n for n in (manifest_name,dictionary_name)):
            raise ValueError('Dictionary filenames must be local basenames')
        raw=(self.directory/dictionary_name).read_bytes()
        self.sha256=hashlib.sha256(raw).hexdigest()
        self.manifest=parse_json((self.directory/manifest_name).read_text(encoding='utf-8'))
        if self.manifest.get('schema_version')!=SCHEMA or self.manifest.get('dictionary_sha256')!=self.sha256:
            raise ValueError('Industry dictionary version/hash mismatch')
        self.rows=[parse_json(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        self.by_key={r['key']:r for r in self.rows}
        if len(self.by_key)!=len(self.rows) or len(self.rows)!=self.manifest['entries']:
            raise ValueError('Duplicate industry identity or dictionary count mismatch')
        for row in self.rows:
            if not all(isinstance(row.get(k),str) and row[k] for k in ('key','industry_standard','industry_version','industry_code','name','source','source_sha256')):
                raise ValueError('Incomplete dictionary provenance')
            prefix={'SEC-SIC':'SEC-SIC','TRSR-INDUSTRY':'TRSR','SP500-SECTOR':'SP500-SECTOR'}.get(row['industry_standard'])
            if prefix is None or row['key']!=prefix+':'+row['industry_code']:
                raise ValueError('Dictionary key/code namespace mismatch')
            if re.fullmatch('[a-f0-9]{64}',row['source_sha256']) is None:
                raise ValueError('Invalid dictionary source SHA256')
            if not isinstance(row.get('aliases',[]),list) or any(not isinstance(s,str) or not s.strip() for s in row.get('aliases',[])):
                raise ValueError('Invalid dictionary aliases')
            if row.get('membership_basis') not in ('historical_verified_only','undated_reference_only'):
                raise ValueError('Unknown dictionary membership policy')
        self.by_code={(r['industry_standard'],r['industry_version'],r['industry_code']):r for r in self.rows}
        if len(self.by_code)!=len(self.rows):raise ValueError('Duplicate standard/version/industry code')
        try:created=datetime.fromisoformat(self.manifest['created_at'].replace('Z','+00:00'))
        except (KeyError,TypeError,ValueError):raise ValueError('Invalid dictionary creation time') from None
        if created.tzinfo is None or created>datetime.now(timezone.utc):raise ValueError('Dictionary creation time is unzoned or future')
        if verify_sources is None:verify_sources=manifest_name=='manifest.json'
        if verify_sources:
            files=self.manifest.get('files')
            if not isinstance(files,dict) or 'industry_dictionary.jsonl' not in files:
                raise ValueError('Dictionary source file manifest is missing')
            prefix=dictionary_name[:-len('industry_dictionary.jsonl')]
            for filename,expected in files.items():
                if Path(filename).name!=filename or re.fullmatch('[a-f0-9]{64}',str(expected)) is None:
                    raise ValueError('Invalid dictionary evidence file manifest')
                source=(self.directory/(prefix+filename)).resolve()
                try:source.relative_to(self.directory)
                except ValueError:raise ValueError('Dictionary evidence escaped its directory') from None
                if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
                    raise ValueError('Dictionary source evidence SHA mismatch: '+filename)

    def selection_valid(self,key,name):
        if key not in self.by_key or not isinstance(name,str):return False
        row=self.by_key[key]
        if compact(name).casefold()==compact(row['name']).casefold():return True
        return bool((words(name)-GENERIC) & (words(row['name']+' '+' '.join(row.get('aliases',[])))-GENERIC))

    def evidence_matches(self,key,quote):
        return self.selection_valid(key,quote)

    def candidates(self,source,limit=12,standards=None,headline=''):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=32:raise ValueError('Invalid dictionary candidate limit')
        tokens=words(source)-GENERIC;title=words(headline)-GENERIC;scored=[]
        allowed=[r for r in self.rows if standards is None or r['industry_standard'] in standards]
        termsets={r['key']:words(r['name']+' '+' '.join(r.get('aliases',[])))-GENERIC for r in allowed}
        frequency=Counter(t for ts in termsets.values() for t in ts)
        for row in allowed:
            terms=termsets[row['key']]
            overlap=tokens & terms
            score=sum(1+math.log((1+len(allowed))/(1+frequency[t])) for t in overlap)/math.sqrt(max(1,len(terms)))
            score+=sum(4*(1+math.log((1+len(allowed))/(1+frequency[t]))) for t in title & terms)
            if overlap:scored.append((-score,row['key'],row))
        return [{'key':r['key'],'name':r['name'],'standard':r['industry_standard'],
                 'membership_basis':r['membership_basis']} for _,_,r in self.balanced(scored,limit,standards)]

    @staticmethod
    def balanced(scored,limit,standards):
        ordered=sorted(scored)
        if not standards or len(standards)<2:return ordered[:limit]
        selected=[]
        groups={s:[v for v in ordered if v[2]['industry_standard']==s] for s in standards}
        while len(selected)<limit and any(groups.values()):
            for s in standards:
                if groups[s] and len(selected)<limit:selected.append(groups[s].pop(0))
        return selected

    def ground(self,note,source,candidates,minimum=.75):
        """Publish only validated selections; old labels are kept in separate raw/audit snapshots."""
        allowed={r['key'] for r in candidates};flags=list(note.get('relation_quality_flags',[]));impacts=[]
        summary=note.get('content','')
        for original in note.get('industry_impacts',[]):
            item=dict(original);key=item.get('industry_key')
            if key is None:
                impacts.append(item);continue  # explicitly unresolved, never guessed by Python
            if key not in allowed or key not in self.by_key:
                flags.append('industry_dictionary_key_not_offered');continue
            if not self.selection_valid(key,item.get('industry_name_original')):
                flags.append('industry_dictionary_name_code_conflict');continue
            quote=item.get('quote');score=item.get('confidence')
            if not isinstance(quote,str) or len(compact(quote))<8 or compact(quote) not in compact(source):
                flags.append('industry_dictionary_quote_unverified');continue
            if not self.evidence_matches(key,quote):
                flags.append('industry_dictionary_code_quote_conflict');continue
            scope_quote=item.get('industry_scope_quote')
            if not isinstance(scope_quote,str) or len(compact(scope_quote))<8 or compact(scope_quote) not in compact(source):
                flags.append('industry_scope_quote_unverified');continue
            if isinstance(score,bool) or not isinstance(score,(float,int)) or not math.isfinite(score) or not 0<=score<=1:
                flags.append('industry_dictionary_confidence_missing');continue
            row=self.by_key[key]
            item.update(industry_standard=row['industry_standard'],industry_version=row['industry_version'],
                industry_code=row['industry_code'],industry_name_standard=row['name'],
                dictionary_sha256=self.sha256,dictionary_version=self.manifest['version'],
                original_evidence_quote=quote,original_evidence_verified=True,
                evidence_basis='original_article_quote_verified_before_raw_cleanup',
                summary_evidence_quote=quote if compact(quote) in compact(summary) else None,
                industry_scope_status='candidate_not_human_approved')
            from market_scope import industry_scope_candidate
            approved=industry_scope_candidate(scope_quote) and score>=minimum
            item.update(propagation_approved=approved,
                industry_scope_status='automated_evidence_gate_passed_not_human_reviewed' if approved else 'not_approved')
            impacts.append(item)
        note['industry_impacts']=impacts
        topics=[]
        for original in note.get('industries',[]):
            item=dict(original);key=item.get('industry_key')
            if (key is not None and key in allowed and self.selection_valid(key,item.get('name'))
                    and item.get('quote') and compact(item['quote']) in compact(source)
                    and self.evidence_matches(key,item['quote'])):
                row=self.by_key[key]
                item.update(industry_standard=row['industry_standard'],industry_version=row['industry_version'],
                    industry_code=row['industry_code'],industry_name_standard=row['name'],dictionary_sha256=self.sha256,
                    dictionary_version=self.manifest['version'],mapping_status='dictionary_selected_not_stock_membership',
                    evidence_basis='original_article_quote_verified_before_raw_cleanup',original_evidence_verified=True,
                    original_evidence_quote=item['quote'],summary_evidence_quote=item['quote'] if compact(item['quote']) in compact(summary) else None)
                topics.append(item)
            else:
                flags.append('industry_mention_not_dictionary_grounded')
        for impact in impacts:
            if not impact.get('industry_code'):continue
            if not any(x.get('industry_key')==impact['industry_key'] for x in topics):
                topics.append(dict(impact,name=impact['industry_name_original'],basis=impact.get('evidence_type','inferred'),
                    mapping_status='dictionary_selected_not_stock_membership'))
        note['industries']=topics
        note['relation_quality_flags']=sorted(set(flags))
        note['industry_dictionary']={'schema_version':SCHEMA,'version':self.manifest['version'],'sha256':self.sha256,
            'source_snapshot':self.manifest.get('source_url'),'candidate_keys':sorted(allowed),
            'selection_method':'qwen_same_content_request','confidence_basis':'model_self_report_not_calibrated'}
        return note

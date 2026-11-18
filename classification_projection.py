"""Replace classifications using freshly grounded claims, never union legacy labels."""
from copy import deepcopy
import math
from finance_agent.core import digest
from market_scope import normalized

CONTRACT='dictionary-scope-replacement-1'
SCOPES=('company','industry','market')


def ground_classification(note,source,basis,minimum):
    flags=list(note.get('relation_quality_flags',[]));accepted=[];evidence=[]
    offered=note.get('impact_scope',[])
    if (not isinstance(offered,list) or len(offered)>3 or len(set(offered))!=len(offered)
            or not set(offered)<=set(SCOPES)):
        raise ValueError('impact_scope_invalid')
    claims=note.get('impact_scope_evidence',[])
    if not isinstance(claims,list) or len(claims)>3:raise ValueError('impact_scope_evidence_invalid')
    for scope in SCOPES:
        if scope not in offered:continue
        rows=[r for r in claims if isinstance(r,dict) and r.get('scope')==scope]
        if not rows:
            flags.append('impact_scope_evidence_missing');continue
        # Multiple corroborating quotes are allowed. Pick a valid quote deterministically;
        # repetition must neither invalidate a supported scope nor rescue a fabricated quote.
        def valid_quote(item):
            q=item.get('quote');s=item.get('confidence');r=item.get('rationale')
            return (isinstance(q,str) and 8<=len(q)<=180 and normalized(q) in normalized(source)
                and isinstance(r,str) and 8<=len(r)<=300 and not isinstance(s,bool)
                and isinstance(s,(int,float)) and math.isfinite(s) and minimum<=s<=1)
        valid=[r for r in rows if valid_quote(r)]
        if not valid:
            flags.append('impact_scope_evidence_unverified');continue
        rows=sorted(valid,key=lambda r:(-r['confidence'],r['quote']))
        item=rows[0];quote=item.get('quote');score=item.get('confidence');reason=item.get('rationale')
        if (not isinstance(quote,str) or not 8<=len(quote)<=180 or normalized(quote) not in normalized(source)
                or not isinstance(reason,str) or not 8<=len(reason)<=300
                or isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score)
                or not minimum<=score<=1):
            flags.append('impact_scope_evidence_unverified');continue
        if scope=='industry' and not any(i.get('propagation_approved') is True for i in note.get('industry_impacts',[])):
            flags.append('industry_scope_without_approved_impact');continue
        if scope=='market' and not note.get('market_scope'):
            flags.append('market_scope_without_typed_scope');continue
        accepted.append(scope);evidence.append(dict(item,evidence_basis=basis,
            original_evidence_verified=basis.startswith('original_article'),
            original_evidence_quote=quote if basis.startswith('original_article') else None,
            summary_evidence_quote=quote if normalized(quote) in normalized(note.get('content','')) else None))
    if 'market' not in accepted:
        note.update(market_scope=[],scope_evidence=[])
    if 'industry' not in accepted:
        for item in note.get('industry_impacts',[]):
            item.update(propagation_approved=False,industry_scope_status='not_approved')
    note.update(impact_scope=accepted,impact_scope_evidence=evidence,
        classification_contract=CONTRACT,relation_quality_flags=sorted(set(flags)))
    return note


def public_industries(audit):
    """Public names are dictionary names; exact keys and quotes remain in the sidecar."""
    result=[]
    for item in audit.get('industries',[])+audit.get('industry_impacts',[]):
        name=item.get('industry_name_standard')
        if not (item.get('industry_key') and name and item.get('dictionary_sha256')):continue
        basis=item.get('basis',item.get('evidence_type','inferred'))
        if basis not in ('explicit','inferred'):continue
        row={'名称':name,'依据':basis}
        if not any(x['名称']==name for x in result):result.append(row)
    return result


def replace_classification(original,audit):
    if audit.get('classification_contract')!=CONTRACT:
        raise ValueError('fresh_classification_required_no_legacy_fallback')
    row=deepcopy(original);audit=deepcopy(audit)
    scopes=audit.get('impact_scope',[])
    if (not isinstance(scopes,list) or scopes!=[s for s in SCOPES if s in scopes]):
        raise ValueError('impact_scope_invalid')
    if ('market' in scopes)!=bool(audit.get('market_scope')):
        raise ValueError('typed_market_scope_inconsistent')
    if 'industry' in scopes and not any(i.get('propagation_approved') is True for i in audit.get('industry_impacts',[])):
        raise ValueError('industry_scope_without_approved_impact')
    # Explicit replacement, including empty arrays. Old labels survive only in audit/raw snapshots.
    audit['classification_replacement']={'policy':CONTRACT,'source_record_hash':digest(original),
        'previous_industries':deepcopy(original.get('涉及行业',[])),
        'previous_impact_scope':deepcopy(original.get('影响范围',[])),
        'previous_labels_used_for_classification':False}
    row['涉及行业']=public_industries(audit)
    row['影响范围']=list(scopes)
    return row,audit

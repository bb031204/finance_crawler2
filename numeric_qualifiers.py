"""Light consistency check for explicit quantitative bounds, without exporting a facts table."""
import re
from collections import defaultdict

PATTERN=re.compile(r'(?<![A-Za-z\d.])(?P<bound>approximately|around|about|more than|over|at least|greater than|less than|under|below|at most|up to|超过|高于|至少|不足|低于|至多|约|大约)?\s*'
    r'(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?:[名位个]\s*)?(?P<unit>employees|workers|staff|people|percent|%|million|billion|trillion|万人|亿元|万元|员工)(?![A-Za-z])',re.I)


def quantities(text):
    result=defaultdict(set)
    for match in PATTERN.finditer(text):
        value=match['number'].replace(',','');unit=match['unit'].lower()
        unit={'percent':'%','workers':'employees','staff':'employees','people':'employees'}.get(unit,unit)
        bound=(match['bound'] or '').lower()
        if bound in {'approximately','around','about','约','大约'}:bound='approximate'
        elif bound in {'more than','over','greater than','超过','高于'}:bound='lower_bound'
        elif bound in {'at least','至少'}:bound='inclusive_lower_bound'
        elif bound in {'less than','under','below','不足','低于'}:bound='upper_bound'
        elif bound in {'at most','up to','至多'}:bound='inclusive_upper_bound'
        else:bound='unqualified'
        result[(value,unit)].add(bound)
    return result


def validate_numeric_qualifiers(summary,source):
    originals=quantities(source)
    for key,bounds in quantities(summary).items():
        # Ambiguous repeated values cannot be resolved by this light check alone.
        previous=originals.get(key,set())
        if len(previous)==1 and previous!={'unqualified'} and bounds!=previous:
            raise ValueError(f'数值限定词须与原文一致：{key[0]} {key[1]}，保留约/超过/不足及相应含义')

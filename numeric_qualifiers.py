"""Source checks for numerical values, units and explicit local claim context."""
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

NUMERIC_EVIDENCE_VERSION = 'numeric-evidence-1'

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


# Keep the old qualifier API for existing callers. The extraction path also
# checks values and their context, using Decimal to avoid conversion noise.
NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
CURRENCY = r'US\$|HK\$|USD|CNY|RMB|EUR|GBP|人民币|美元|港元|港币|[€£$]'
SCALE = r'(?:trillion|billion|million|thousand|bn|mn|[mb])\b|万亿|亿|万|千'
UNIT = (r'percentage\s+points?|percent(?:age)?|basis\s+points?|bps|百分点|基点|[%％]|'
        r'employees|workers|staff|people|员工|shares|股|美元|人民币|港元|港币|元|U[.]?S[.]?\s+dollars|USD|CNY|RMB|EUR|GBP|dollars|euros|pounds')
QUANTITY = re.compile(
    rf'(?<![A-Za-z\d.])(?P<negative>\()?\s*(?P<prefix_sign>[+\-−])?(?P<currency>{CURRENCY})?\s*'
    rf'(?P<number>[+\-−]?{NUMBER}(?:\s*/\s*\d+)?)\s*'
    rf'(?P<scale>{SCALE})?\s*(?:[名位个]\s*)?(?P<unit>{UNIT})?(?P<close>\))?', re.I)
SCALES = {'thousand': Decimal(1000), 'million': Decimal(10)**6,
          'billion': Decimal(10)**9, 'trillion': Decimal(10)**12,
          'mn': Decimal(10)**6, 'm': Decimal(10)**6, 'bn': Decimal(10)**9, 'b': Decimal(10)**9,
          '千': Decimal(1000), '万': Decimal(10000), '亿': Decimal(10)**8, '万亿': Decimal(10)**12}
METRICS = {
    'revenue': r'\b(?:revenues?|sales|turnover)\b|营业收入|营收|销售收入',
    'net_income': r'\bnet (?:income|profit|earnings|loss(?:es)?)\b|净利润|净收益|净亏损',
    'operating_income': r'\boperating (?:income|profit|earnings)\b|营业利润|经营利润',
    'eps': r'\b(?:EPS|(?:earnings|loss) per (?:diluted |basic )?share)\b|每股(?:收益|盈利|亏损)',
    'cash_flow': r'\b(?:cash flow|cash flows)\b|现金流',
    'margin': r'\b(?:gross|operating|profit|net) margin\b|毛利率|利润率',
    'headcount': r'\b(?:employees|workers|headcount|staff)\b|员工',
    'interest_rate': r'\b(?:interest rate|federal funds rate|policy rate)\b|利率',
    'inflation': r'\b(?:inflation|CPI|PCE)\b|通胀|物价指数',
}
METRIC_PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in METRICS.items()]
PERIOD = re.compile(r'(?<!\d)(?:19|20)\d{2}(?!\d)', re.I)
QUARTER = re.compile(r'Q\s*([1-4])(?!\d)|\b(first|second|third|fourth)[ -]+(?:fiscal[ -]+)?quarter\b|第([一二三四1234])季度',re.I)
CHANGE = re.compile(r'\b(?:grew|growth|rose|increased?|rising|higher|up|fell|declined?|decreased?|lower|down|dropped?)\b|增长|上升|增加|下降|减少|下滑', re.I)
NUMBER_WORDS = {word: str(number) for number, word in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split())}
WORD_PATTERN = re.compile(r'\b(?:'+'|'.join(NUMBER_WORDS)+r')\b(?=\s*(?:'+SCALE+'|'+UNIT+r'|[- ](?:years?|months?|days?)\b))', re.I)


@dataclass(frozen=True)
class NumericalClaim:
    value: Decimal
    kind: str
    quantum: Decimal
    start: int
    end: int
    metric: str | None
    period: str | None
    quarter: str | None
    direction: int | None
    entity: str | None
    bound: str


def _nearest(patterns, text, start, end, entity_context=False):
    """Prefer a label immediately preceding a value in the same clause."""
    left = max(text.rfind(mark, 0, start) + 1 for mark in '.;。；\n')
    left = max(0, start - 240) if entity_context else max(left, start - 140)
    labels = [(match.end(), name) for name, pattern in patterns
              for match in pattern.finditer(text, left, start)]
    if labels:
        return max(labels)[1]
    if entity_context:
        return None
    right = min(len(text), end + 35)
    following = [(match.start(), name) for name, pattern in patterns
                 for match in pattern.finditer(text, end, right)]
    return min(following)[1] if following else None


def numerical_claims(text, entities=()):
    # Canonicalize common explicit number words, not inferred/computed ratios.
    text = WORD_PATTERN.sub(lambda match: NUMBER_WORDS[match[0].lower()], text)
    text = re.sub(r'(\d{4})/(\d{1,2})/(\d{1,2})',r'\1-\2-\3',text)
    entity_patterns = []
    for item in entities:
        name = item.get('name') if isinstance(item, dict) else item
        if isinstance(name, str) and len(name.strip()) >= 2:
            entity_patterns.append((name.casefold(), re.compile(re.escape(name), re.I)))
    result = []
    for match in QUANTITY.finditer(text):
        raw = match['number'].replace(',', '').replace('−', '-')
        # Hyphens in a date or numeric range are separators, not negative signs.
        if raw.startswith('-') and match.start('number') and text[match.start('number') - 1].isdigit():
            raw = raw[1:]
        pieces = re.split(r'\s*/\s*', raw)
        value = Decimal(pieces[0])
        if match['prefix_sign'] in {'-','−'} and not (match.start('prefix_sign') and text[match.start('prefix_sign')-1].isdigit()):
            value = -abs(value)
        if len(pieces) == 2:
            if Decimal(pieces[1]) == 0:
                continue
            value /= Decimal(pieces[1])
        if match['negative'] and match['close']:
            value = -abs(value)
        scale = SCALES.get((match['scale'] or '').lower(), Decimal(1))
        unit = (match['unit'] or '').lower()
        # A range can carry its unit once, after the second endpoint.
        if not unit:
            endpoint = re.match(rf'\s*(?:to|至|到|[-–])\s*{NUMBER}(?:\s*/\s*\d+)?\s*(?P<unit>{UNIT})',text[match.end():],re.I)
            if endpoint:unit=endpoint['unit'].lower()
        currency = (match['currency'] or '').upper()
        currencies = {'$': 'USD', 'US$': 'USD', 'USD': 'USD', '美元': 'USD', 'DOLLARS': 'USD',
                      'U.S. DOLLARS': 'USD', 'US DOLLARS': 'USD',
                      'CNY': 'CNY', 'RMB': 'CNY', '人民币': 'CNY', '元': 'CNY',
                      'HK$': 'HKD', '港元': 'HKD', '港币': 'HKD',
                      '€': 'EUR', 'EUR': 'EUR', 'EUROS': 'EUR', '£': 'GBP', 'GBP': 'GBP', 'POUNDS': 'GBP'}
        kind = currencies.get(currency) or currencies.get(unit.upper()) or 'number'
        if unit in {'%', '％', 'percent', 'percentage'}:
            kind = 'percent'
        elif unit in {'百分点', '基点', 'bps'} or unit.startswith(('percentage point', 'basis point')):
            kind = 'percentage_points'
            if unit in {'基点', 'bps'} or unit.startswith('basis point'):
                scale /= 100
        elif unit in {'employees', 'workers', 'staff', 'people', '员工'}:
            kind = 'employees'
        elif unit in {'shares', '股'}:
            kind = 'shares'
        value *= scale
        metric = _nearest(METRIC_PATTERNS, text, match.start(), match.end())
        if metric in {'net_income','eps'} and re.search(r'net loss(?:es)?|loss per (?:diluted |basic )?share|净亏损|每股亏损',text[max(0,match.start()-65):match.start()],re.I):
            value = -abs(value)
        quantum = (Decimal(10) ** Decimal(pieces[0]).as_tuple().exponent * scale).normalize()
        clause_start=max(text.rfind(mark,0,match.start())+1 for mark in '.;。；\n')
        period_context = text[max(clause_start, match.start()-120):match.start()]
        periods = list(PERIOD.finditer(period_context))
        period = re.search(r'\d{4}', periods[-1][0])[0] if periods else None
        quarters=list(QUARTER.finditer(period_context))
        quarter=None
        if quarters:
            found=quarters[-1]
            if found[1]:
                quarter=found[1]
            elif found[2]:
                quarter={'first':'1','second':'2','third':'3','fourth':'4'}[found[2].lower()]
            else:
                quarter={'一':'1','二':'2','三':'3','四':'4'}.get(found[3],found[3])
        change = _nearest([('change', CHANGE)], text, match.start(), match.end())
        direction = None
        if change:
            local = text[max(0, match.start()-65):match.start()]
            changes = list(CHANGE.finditer(local))
            if changes:
                direction = -1 if re.search(r'fell|declin|decreas|lower|down|drop|下降|减少|下滑', changes[-1][0], re.I) else 1
        prefix=text[max(0,match.start()-25):match.start()]
        bound='exact'
        for category,pattern in [
            ('approximate',r'approximately|around|about|约|大约'),
            ('lower',r'more than|over|greater than|超过|高于'),
            ('inclusive_lower',r'at least|至少'),
            ('upper',r'less than|under|below|不足|低于'),
            ('inclusive_upper',r'at most|up to|至多'),
        ]:
            if re.search(r'(?<![A-Za-z])(?:'+pattern+r')\s*$',prefix,re.I):bound=category;break
        result.append(NumericalClaim(value, kind, quantum, match.start(), match.end(),
            metric, period, quarter, direction,
            _nearest(entity_patterns, text, match.start(), match.end(),entity_context=True),
            bound))
    return result


def validate_numeric_evidence(summary, source, entities=()):
    """Reject unsupported numbers and conflicting explicit contexts without new LLM calls.

    Unit conversion and explicitly approximate rounding are allowed. Context
    matching only uses source labels that are actually present, never inferred
    company identities or a guessed table header. This is not a semantic proof.
    """
    originals = numerical_claims(source, entities)
    for claim in numerical_claims(summary, entities):
        candidates = [item for item in originals if item.kind == claim.kind and
                      (item.value == claim.value or (claim.bound=='approximate' and item.bound in {'exact','approximate'} and
                       (item.value/claim.quantum).to_integral_value(rounding=ROUND_HALF_UP)*claim.quantum == claim.value))]
        if not candidates:
            raise ValueError(f'数字缺少同单位原文证据：{claim.value} {claim.kind}；核对数值/单位，不自行计算或补猜')
        # A bare year has no monetary/percentage claim context. Plain financial
        # values (e.g. EPS without a currency marker) still need attribution.
        if claim.kind == 'number' and 1900<=claim.value<=2100 and claim.value==claim.value.to_integral_value():
            continue
        for attribute, label in [('metric', '财务指标'), ('period', '报告年份'), ('quarter','报告季度'), ('entity', '公司主体'), ('direction', '变化方向')]:
            expected = getattr(claim, attribute)
            known = [item for item in candidates if getattr(item, attribute) is not None]
            if expected is not None and known:
                candidates = [item for item in known if getattr(item, attribute) == expected]
                if not candidates:
                    raise ValueError(f'数字的{label}与原文证据不对应：{claim.value} {claim.kind}；保留正确归属和比较基准')
        if not any(item.bound==claim.bound or (claim.bound=='approximate' and item.bound=='exact') for item in candidates):
            raise ValueError(f'数值限定词须与原文证据一致：{claim.value} {claim.kind}；保留约/超过/不足及相应含义')

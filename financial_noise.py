"""Remove narrowly identified filing boilerplate, not economic/legal risk information."""
import re
from summary_quality import trim_routine_sentences

NOISE_VERSION='financial_sentences_v2_strict_templates'
ABBREVIATION=re.compile(r'\b(?:Inc|Ltd|Corp|Co|U\.S|U\.K|Mr|Ms|Dr|L\.P|S\.A)\.',re.I)
MATERIAL=re.compile(
    r'[$€£￥¥]\s*\d|(?:USD|EUR|RMB|CNY)\s*\d|\d[\d,.]*\s*(?:%|percent|million|billion|trillion|basis points?|bps|shares?|亿元|万元|基点|股)|'
    r'\b(?:fraud|penalt\w*|fined?|lawsuit|settlement|investigat\w*|default\w*|covenant\w*|'
    r'restat\w*|resign\w*|appoint\w*|bankrupt\w*|acquisit\w*|merger\w*|divest\w*|buyback\w*|repurchas\w*|refinanc\w*|contract\w*|strategic review|delay\w*|deadline\w*|extension\w*|going concern|material weaknesses?|qualified opinion|change of auditor|auditor change)\b|'
    r'违约|处罚|诉讼|财务重述|辞职|任命|持续经营|重大缺陷|附保留意见',re.I)
WRAPPER=re.compile(
    r'\b(?:issued|furnished|filed|announced|releas\w*|attached|included|incorporat\w*|available|posted|referenc\w*|signed|certif\w*)\b|'
    r'发布|披露|提交|附录|附件|签署',re.I)
REPORT_EVENT=re.compile(r'financial results|annual report|quarterly report|form\s+(?:8-k|40-f|20-f|10-[kq])|investor presentation|财报|年度报告|季度报告|投资者演示',re.I)
CHANGE=re.compile(r'\b(?:increas\w*|decreas\w*|growth|declin\w*|loss\w*|profit\w*|revenue\w*|cash|margin\w*|orders?|demand|supply|restructur\w*|layoff\w*|halt\w*|dividend\w*|guidance|forecast\w*|expects?|warn\w*|breach\w*|weaken\w*|improv\w*|record|surpris\w*|reveal\w*|indicat\w*|identif\w*)\b|增长|下降|亏损|现金|分红|需求|订单|重组|裁员|减值|恶化|利润',re.I)
ONLY_WRAPPER=re.compile(
    r'^(?:on\s+\w+\s+\d{1,2},?\s+\d{4},?\s+)?.{1,180}?\b(?:issued|filed|furnished|announced|released)\b.*\b(?:press release|financial results|annual report|quarterly report|investor presentation|form\s+(?:8-k|40-f|20-f|10-[kq]))\b',re.I)


def sentences(text):
    # Protect corporate abbreviations; offsets remain identical to the original text.
    marker=next(chr(code) for code in range(0xE000,0xF8FF) if chr(code) not in text)
    masked=ABBREVIATION.sub(lambda match:match[0].replace('.',marker),text)
    masked=re.sub(r'(?<![\w-])[A-Z]\.(?=\s+[A-Z])',lambda match:match[0].replace('.',marker),masked)
    start=0
    for match in re.finditer(r'[。！？]|[.!?](?=\s|$)',masked):
        yield text[start:match.end()];start=match.end()
    if start<len(text):yield text[start:]


def noise_kind(sentence):
    """Only positively identified whole templates are removable; unknown clauses stay."""
    text=sentence.strip().replace('“','"').replace('”','"').replace('’',"'")
    if not text or MATERIAL.search(text) or CHANGE.search(text):return None
    legal=(r'(?:The|Such) information(?: (?:provided|disclosed|furnished))?(?: in (?:this|the) (?:report|filing))? '
        r'(?:is|shall be) not (?:deemed|considered) ["\']?filed["\']? (?:for purposes of|under) '
        r'Section 18 of the Securities Exchange Act of 1934(?:, as amended)?[.]?')
    attachment=(r'(?:The|A) press release (?:is|was) (?:included|attached) as Exhibit [0-9]+(?:[.][0-9]+)? '
        r'(?:to|in) (?:this|the) (?:Current Report on )?Form 8-K[.]?')
    role=r'(?:Chief Financial Officer|Chief Executive Officer|Senior Vice President and Chief Financial Officer)'
    person=r"(?:[A-Z][A-Za-z'-]*|[A-Z]\.)(?:\s+(?:[A-Z][A-Za-z'-]*|[A-Z]\.)){1,5}"
    signature=rf'The (?:report|document|filing) (?:is|was) signed by (?:the )?(?:{role}|{person}, {role})[.]?'
    visit=r'(?:For more information|To learn more|Please visit)[, ]+(?:visit )?(?:our (?:website|site)|https?://[^\s]+)[.]?'
    empty=r"(?:This|The) (?:article|report|filing|release|document) (?:provides|offers) (?:valuable|important) insights for investors[.]?"
    empty_filing=r"The press release provides details on the company's financial performance for the specified period[.]?"
    for label,pattern in [('filing_legal_boilerplate',legal),('filing_attachment_reference',attachment),
            ('filing_signature',signature),('visit_promotion',visit),('empty_evaluation',empty),('empty_filing_summary',empty_filing)]:
        if re.fullmatch(pattern,text,re.I):return label
    return None


def trim_financial_noise(text):
    original=text;text,tags=trim_routine_sentences(text);kept=[]
    for sentence in sentences(text):
        label=noise_kind(sentence)
        if label:tags.append(label)
        else:kept.append(sentence)
    result=''.join(kept).strip()
    # Preserve an uncertainty statement when only its closing promotional clause is noise.
    result,number=re.subn(r'\s+and (?:provides?|offers?) (?:valuable|important) insights? (?:for|to) investors[.!]?$', '.',result,flags=re.I)
    if number:tags.append('trailing_empty_evaluation')
    return (result if tags else original),list(dict.fromkeys(tags))


def only_filing_wrapper(text):
    parts=[sentence.strip() for sentence in sentences(text) if sentence.strip()]
    return bool(parts) and all(not MATERIAL.search(part) and not CHANGE.search(part) and REPORT_EVENT.search(part)
        and WRAPPER.search(part) and ONLY_WRAPPER.search(part) for part in parts)

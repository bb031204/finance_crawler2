"""Remove narrowly identified filing boilerplate, not economic/legal risk information."""
import re
from summary_quality import trim_routine_sentences

NOISE_VERSION='financial_sentences_v1'
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
    text=sentence.strip()
    if not text or MATERIAL.search(text) or CHANGE.search(text):return None
    profile=(re.search(r'\b(?:based|headquartered) in\b',text,re.I)
        and re.search(r'\b\d[\d,]*\s+employees\b',text,re.I)
        and re.search(r'\b\d[\d,]*\s+countries\b',text,re.I))
    if profile and not re.search(r'\b(?:new|changed|moved|relocat\w*|open\w*|clos\w*|expan\w*|reduc\w*|cut|hir\w*|strike|tariff\w*|sanction\w*|exposure|geopolit\w*|lockdown)\b',text,re.I):
        return 'static_company_profile'
    if (re.search(r'\b(?:not (?:be )?(?:deemed|considered)|not deemed|shall not be deemed|not incorporated|incorporated by reference)\b',text,re.I)
            and re.search(r'Section\s+18|Securities (?:Exchange )?Act|Exchange Act|registration statement|filing',text,re.I)):
        return 'filing_legal_boilerplate'
    if re.match(r'^(?:the |such |a )?(?:press release|presentation|slides|report|document|information|exhibit)',text,re.I):
        if re.search(r'\b(?:attached|included|incorporated by reference)\b',text,re.I) and re.search(r'\b(?:exhibit|form\s+8-k|registration statements?)\b',text,re.I):
            return 'filing_attachment_reference'
        if re.search(r'\b(?:was signed by|is signed by|includes a signature|confirming the filing)\b',text,re.I):
            return 'filing_signature'
    if re.match(r'^(?:for more information|to learn more|please visit|更多信息请|详情请访问)',text,re.I):
        return 'visit_promotion'
    if re.fullmatch(r"The press release provides details on the company['’]s financial performance for the specified period\.?",text,re.I):
        return 'empty_filing_summary'
    if re.match(r'^(?:this|the)\s+(?:article|report|filing|release|document)\s+(?:provides?|offers?|demonstrates?|reflects?)\b',text,re.I):
        if re.search(r'valuable insights|reference value|transparency|worth watching',text,re.I):return 'empty_evaluation'
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

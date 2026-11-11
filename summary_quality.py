"""Conservative summary checks; request a model repair rather than truncating facts."""
import re

EMPTY_EVALUATION=re.compile(
    r'\b(?:provides?|offers?)\s+(?:valuable|important)\s+insights?\b|'
    r'\b(?:reflects?|demonstrates?|enhances?|improves?)\s+(?:regulatory\s+|corporate\s+)?transparency\b|'
    r'\btrailblazing\b|\bpoised to grow exponentially\b|'
    r'\bis expected to influence market dynamics significantly\b|'
    r'(?:本次披露|本公告|该报告|本文)[^。！？\n]{0,24}(?:参考价值|提高透明度|提升透明度)',re.I)

ROUTINE_SENTENCES=[
    ('unanimous_votes',re.compile(r'董事会(?:表决)?(?:结果为?)?[：:]?(?:同意|赞成)\d+票[，,、](?:反对0票[，,、]弃权0票|弃权0票[，,、]反对0票)')),
    ('committee_flow',re.compile(r'(?:董事会提名与薪酬考核)?委员会已事前审议[，,]同意提交董事会')),
    ('signature',re.compile(r'董事长签署本公告(?:[，,]公司保证不存在虚假记载和重大遗漏)?')),
    ('guarantee',re.compile(r'公司保证不存在虚假记载和重大遗漏')),
]


def trim_routine_sentences(text):
    """Remove only full, known procedural sentences; never truncate a mixed fact sentence."""
    kept=[];removed=[]
    for sentence in re.split(r'(?<=[。！？])',text):
        literal=re.sub(r'\s+','',sentence).rstrip('。！？')
        label=next((label for label,pattern in ROUTINE_SENTENCES if pattern.fullmatch(literal)),None)
        if label:removed.append(label)
        else:kept.append(sentence)
    return ''.join(kept).strip(),removed





def validate_summary_quality(text,language):
    if language=='zh':
        too_long=len(re.findall(r'[\u4e00-\u9fff]',text))>380
    else:
        too_long=len(re.findall(r"\b[\w]+(?:[-'][\w]+)*\b",text))>200
    match=EMPTY_EVALUATION.search(text)
    issues=[]
    if match:issues.append('含营销或空泛评价“'+match[0]+'”')
    unanimous=(re.search(r'(?:同意|赞成)\s*[:：]?\s*\d+\s*票',text)
        and re.search(r'反对\s*[:：]?\s*0\s*票',text) and re.search(r'弃权\s*[:：]?\s*0\s*票',text))
    if unanimous or '董事长签署本公告' in text:
        issues.append('含例行票数/签署流程，请删这些及委员会送审套话，保留实质决定与批准条件')
    if too_long:issues.append('主要内容偏长')
    if issues:
        raise ValueError('；'.join(issues)+'；删套话和冗余，保留主体/动作/对象、关键数据与条件、观点归属；中文200至350字，英文120至190词，短讯不凑数')

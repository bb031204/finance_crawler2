"""Public market-analysis metadata, with evidence retained only in internal audits."""
import json
from finance_agent.core import bounded, digest, now

ANALYSIS_FIELDS = {"股票实体", "涉及行业", "影响范围"}
SCOPES = ["company", "industry", "market"]
BASES = {"explicit", "inferred", "legacy_metadata"}


def validate_industries(items, source_text):
    from record_quality import compact
    result = []
    for item in items:
        name, basis, quote = item["name"].strip(), item["basis"], item.get("quote", "").strip()
        if not name or basis not in {"explicit", "inferred"}: raise ValueError("行业须有名称并区分explicit/inferred")
        if not quote or compact(quote) not in compact(source_text): raise ValueError("行业依据quote须逐字来自本文")
        if basis == "explicit" and compact(name) not in compact(quote):
            raise ValueError("explicit行业名称须在引文中出现；语义归类使用inferred")
        normalized = {"name": name, "basis": basis, "quote": quote}
        if normalized not in result: result.append(normalized)
    return result


def grounded_industries(items, source_text):
    from record_quality import compact
    result=[]
    for item in items:
        item=dict(item)
        if item.get("basis")=="explicit" and compact(item["name"]) not in compact(item.get("quote", "")):
            item["basis"]="inferred"
        try:result.extend(validate_industries([item],source_text))
        except ValueError:continue
    return result


def validate_scopes(values):
    if not isinstance(values, list) or any(value not in SCOPES for value in values):
        raise ValueError("影响范围仅允许company/industry/market列表；未知留空")
    return [scope for scope in SCOPES if scope in values]


def preserve_analysis(final, notes, source_text):
    industries = validate_industries([item for note in notes + [final] for item in note.get("industries", [])], source_text)
    selected = {}
    for item in industries:
        key = item["name"].casefold()
        if key not in selected or item["basis"] == "explicit": selected[key] = item
    final["industries"] = list(selected.values())
    final["impact_scope"] = validate_scopes([scope for note in notes + [final] for scope in note.get("impact_scope", [])])
    return final


def public_analysis(note):
    entities = []
    for item in note.get("securities", []):
        if not item.get('ticker'):continue
        entity = {"名称": item.get("name"), "股票代码": item.get("ticker"), "交易所": item.get("exchange")}
        if entity not in entities: entities.append(entity)
    industries = []
    for item in note.get("industries", []):
        industry = {"名称": item["name"], "依据": item["basis"]}
        if industry not in industries: industries.append(industry)
    return {"股票实体": entities, "涉及行业": industries, "影响范围": validate_scopes(note.get("impact_scope", []))}


def validate_public_analysis(row, strict_codes=False):
    if not ANALYSIS_FIELDS <= set(row): raise ValueError("正式记录缺少股票实体、涉及行业或影响范围")
    for key in ANALYSIS_FIELDS:
        if not isinstance(row[key], list): raise ValueError(key + "必须为列表，未知使用[]")
    for item in row["股票实体"]:
        if not isinstance(item, dict) or set(item) != {"名称", "股票代码", "交易所"}:
            raise ValueError("股票实体须包含名称、股票代码、交易所")
        if not (item["名称"] or item["股票代码"]): raise ValueError("股票实体不能仅有交易所或全部为空")
        for value in item.values():
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
                raise ValueError("股票实体属性须为非空字符串或null")
        if strict_codes and item["股票代码"] is not None:
            from security_grounding import is_security_code
            if not is_security_code(item["股票代码"]):raise ValueError("正式股票代码格式不正确，不能使用公司全名")
    for item in row["涉及行业"]:
        if (not isinstance(item, dict) or set(item) != {"名称", "依据"} or not isinstance(item["名称"], str)
                or not item["名称"].strip() or len(item["名称"]) > 100 or not isinstance(item["依据"], str) or item["依据"] not in BASES):
            raise ValueError("涉及行业须有名称及explicit/inferred/legacy_metadata依据")
    if validate_scopes(row["影响范围"]) != row["影响范围"]: raise ValueError("影响范围不能重复且须按company/industry/market排列")
    return row


def upgrade_analysis(root, row, industry, original=None):
    """Recover existing metadata only; never fabricate stock mappings for old summaries."""
    if ANALYSIS_FIELDS <= set(row): return validate_public_analysis(row), None
    if ANALYSIS_FIELDS & set(row): raise ValueError("旧记录的分析字段仅部分存在，需人工核对")
    from record_quality import grounded_securities
    original = original or row
    audit_path = bounded(root / ".runtime/simple/audit" / (row["source_id"] + ".json"), root)
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    matched = audit.get("record_hash") in {digest(row), digest(original)}
    securities, ignored = [], 0
    if matched:
        for item in audit.get("securities", []):
            try:
                recovered = grounded_securities([item], item.get("quote", ""))
                securities.extend(recovered)
                if not recovered or any(recovered[0].get(key)!=item.get(key) for key in ['name','ticker','exchange']): ignored += 1
            except (ValueError, TypeError, KeyError): ignored += 1
    sectors = []
    if industry and industry.casefold() not in {"unspecified", "unknown", "未明确", "未知"}:
        sectors = [{"name": industry, "basis": "legacy_metadata"}]
    metadata = public_analysis({"securities": securities, "industries": sectors,
                                "impact_scope": ["company"] if securities else []})
    upgraded = dict(row, **metadata)
    stamp = now()
    upgraded["时间信息"] = dict(row["时间信息"], feature_available_at=stamp, availability_basis="migration")
    # Preserve original extraction evidence; explicitly label the metadata-only upgrade.
    updated_audit = dict(audit) if matched else {}
    updated_audit.update(source_id=row["source_id"], url=row["url"], record_hash=digest(upgraded),
        source_published_at=row["时间"], feature_available_at=stamp,
        analysis_schema_version=1, analysis_migrated_at=stamp,
        analysis_migration={"previous_record_hash": digest(original), "entities_from_matching_audit": matched,
            "industry_basis": "legacy_metadata" if sectors else "unknown", "ignored_unverified_entities": ignored,
            "scope_basis": "recovered_company_entities" if securities else "unknown"})
    return validate_public_analysis(upgraded), updated_audit

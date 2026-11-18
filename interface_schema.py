"""Versioned, bounded schemas for market relations and downstream exports."""
from typing import Literal
from datetime import datetime,timezone
from pydantic import BaseModel,ConfigDict,Field

SCHEMA_VERSION='1.1'
EXTRACTION_VERSION='market-relations-1'
EVENT_TYPES=('earnings','guidance','corporate_action','product_business','regulation_policy',
    'monetary_fiscal','macro_economy','industry_supply_demand','market_structure','risk_incident','other','unknown')
EventType=Literal['earnings','guidance','corporate_action','product_business','regulation_policy',
    'monetary_fiscal','macro_economy','industry_supply_demand','market_structure','risk_incident','other','unknown']
EntityType=Literal['company','issuer','subsidiary','brand','organization','unknown']
EntityRole=Literal['issuer','subject','customer','supplier','partner','competitor','mentioned','unknown']
ImpactRole=Literal['subject','potential_beneficiary','cost_exposure','demand_exposure','supply_exposure','regulatory_exposure','mentioned_only','unknown']
Direction=Literal['positive_conditional','negative_conditional','mixed','neutral','uncertain']


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)


class DataInterfaceConfig(Strict):
    schema_version:Literal['1.1']='1.1'
    export_parquet:bool=True
    preserve_legacy_json:Literal[True]=True
    directory:str='structured'
    export_every_records:int=Field(default=50,ge=1,le=10000)
    export_interval_seconds:int=Field(default=300,ge=10,le=3600)


class IndustryDictionaryConfig(Strict):
    min_confidence:float=Field(default=.75,ge=.5,le=1)
    enabled: bool = False
    directory: str = 'industry_data'
    candidate_limit: int = Field(default=12,ge=1,le=32)
    active_standards: list[Literal['SEC-SIC','TRSR-INDUSTRY','SP500-SECTOR']] = Field(default_factory=lambda:['SEC-SIC'],min_length=1)


class MarketInformationConfig(Strict):
    scope_min_confidence:float=Field(default=0.75,ge=0.75,le=1)
    multi_label_scope:Literal[True]=True
    allowed_scopes:list[Literal['company','industry','market']]=Field(default_factory=lambda:['company','industry','market'],min_length=1)
    preserve_unmapped_entities:Literal[True]=True
    allow_null_ticker:Literal[True]=True
    require_evidence_for_inferred_industry:Literal[True]=True
    industry_standard:str|None=None
    industry_standard_version:str|None=None
    industry_mapping_enabled:Literal[False]=False
    industry_dictionary:IndustryDictionaryConfig=Field(default_factory=IndustryDictionaryConfig)


class RawContentConfig(Strict):
    retain_after_success:Literal[False]=False
    delete_after_verified_commit:Literal[True]=True
    pending_cache_ttl_hours:float=Field(default=24,gt=0,le=24)
    delete_full_prompt_cache_after_success:Literal[True]=True
    audit_cleanup_result:Literal[True]=True


class EventNote(Strict):
    event_type:EventType='unknown'
    subject:str|None=Field(default=None,max_length=160)
    action:str|None=Field(default=None,max_length=160)
    object:str|None=Field(default=None,max_length=160)
    key_period:str|None=Field(default=None,max_length=100)
    key_values:str|None=Field(default=None,max_length=160)
    conditions:str|None=Field(default=None,max_length=180)
    evidence_type:Literal['explicit','inferred']='explicit'
    quote:str=Field(min_length=1,max_length=180)


class IndustryImpactNote(Strict):
    industry_key:str|None=Field(default=None,max_length=80)
    industry_scope_quote:str|None=Field(default=None,max_length=180)
    industry_name_original:str=Field(min_length=1,max_length=100)
    event_index:int|None=Field(default=None,ge=0)
    impact_role:ImpactRole='unknown'
    direction:Direction='uncertain'
    mechanism:str=Field(min_length=1,max_length=220)
    horizon:Literal['short','medium','long','unknown']='unknown'
    evidence_type:Literal['explicit','inferred']='inferred'
    confidence:float|None=Field(default=None,ge=0,le=1)
    quote:str=Field(min_length=1,max_length=180)


def public_schema_version(row):
    if 'title' in row and '股票实体' in row:return 'public-title-11'
    if 'title' in row:return 'legacy-title-time-8' if '时间信息' in row else 'legacy-title-7'
    if '股票实体' in row:return 'legacy-analysis-10'
    return 'legacy-time-7' if '时间信息' in row else 'legacy-6'


def feature_is_available(row,cutoff):
    """A publication date never substitutes for observed feature availability."""
    value=row.get('feature_available_at') or row.get('时间信息',{}).get('feature_available_at')
    if not value:return False
    def stamp(x):
        if isinstance(x,str):x=datetime.fromisoformat(x.replace('Z','+00:00'))
        if not isinstance(x,datetime) or x.tzinfo is None:raise ValueError('cutoff和可用时间须为带时区时刻')
        return x.astimezone(timezone.utc)
    return stamp(value)<stamp(cutoff)


class MarketScopeEvidence(Strict):
    scope:Literal['US_EQUITY','NASDAQ_LISTED','NYSE_LISTED']
    evidence_quote:str=Field(min_length=8,max_length=350)
    rationale:str=Field(min_length=8,max_length=300)
    confidence:float=Field(ge=0,le=1)
    evidence_basis:str|None=None
    original_evidence_verified:bool=False
    original_evidence_quote:str|None=None
    summary_evidence_quote:str|None=None
    scope_status:str|None=None
    confidence_interpretation:str|None=None

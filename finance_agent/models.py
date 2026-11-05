from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Query(Strict):
    query: str = Field(min_length=2, max_length=240)
    market: Literal["CN", "US", "GLOBAL"]
    direction: str
    reason: str = Field(max_length=200)

class Plan(Strict):
    queries: list[Query] = Field(max_length=10)

class Decision(Strict):
    candidate_id: str
    relevant: bool
    fetch_full_text: bool
    category: Literal["announcement","policy","earnings","news","opinion","other"]
    reason: str = Field(max_length=300)
    needs_review: bool

class Decisions(Strict):
    decisions: list[Decision] = Field(max_length=10)

class Mention(Strict):
    name: str = Field(min_length=1, max_length=160)
    symbol: str | None
    market: Literal["CN","US","UNKNOWN"]
    evidence_ids: list[StrictInt] = Field(min_length=1, max_length=4)

class Fact(Strict):
    name: str = Field(min_length=1, max_length=100)
    value_raw: str = Field(min_length=1, max_length=180)
    currency: str | None
    unit: str | None
    reporting_period: str | None
    comparison_basis: Literal["yoy","qoq","other","not_applicable","unknown"]
    evidence_ids: list[StrictInt] = Field(min_length=1, max_length=4)

class Event(Strict):
    event_type: Literal["earnings","investment","merger","buyback","shareholding_change",
                        "dividend","policy","penalty","operations","governance","opinion",
                        "third_party_forecast","other"]
    stage: Literal["proposed","approved","ongoing","completed","terminated","reported","unknown"]
    subject: str | None
    summary: str = Field(min_length=1,max_length=400)
    event_time_raw: str | None
    mentions: list[Mention] = Field(max_length=5)
    facts: list[Fact] = Field(max_length=5)
    evidence_ids: list[StrictInt] = Field(min_length=1,max_length=6)

class Extraction(Strict):
    events: list[Event] = Field(max_length=3)

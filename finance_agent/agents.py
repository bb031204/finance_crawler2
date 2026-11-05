from .models import Plan,Decisions
from .core import digest,now

PLANNER="""根据给定监测配置提出简洁搜索词。只输出queries，数量不超过limit；market和direction必须选给定枚举。
query应包含方向词或指定关键词/股票；可增加中英文表达，不提出股票预测，不返回URL或命令。"""
FILTER="""对候选标题与摘要进行相关性初筛，返回每个candidate_id恰好一个decision。
相关性仅表示是否属于监测范围，不判断利好利空或预测股价。
摘要不完整的公告/政策/财报应保守保留。reason解释采集范围；候选不是已核实事实。"""

def plan_queries(llm,cfg,repo,start,end,round_no=0,previous=None,remaining=None):
    limit=remaining or cfg.collection.max_queries_per_cycle
    inputs={"markets":cfg.monitoring.markets+["GLOBAL"],"directions":cfg.monitoring.directions,
            "include_keywords":cfg.monitoring.include_keywords,"direction_aliases":cfg.monitoring.direction_aliases,
            "securities":cfg.monitoring.securities,
            "start_date":start,"end_date":end,"limit":limit,
            "previous_queries":previous or [],"round":round_no}
    def validate(value):
        queries=value["queries"]
        if not queries or len(queries)>limit:raise ValueError("查询数不在预算内")
        seen=set()
        required=cfg.monitoring.directions+cfg.monitoring.include_keywords+cfg.monitoring.securities
        required+=[x for terms in cfg.monitoring.direction_aliases.values() for x in terms]
        for q in queries:
            if q["direction"] not in cfg.monitoring.directions or q["market"] not in cfg.monitoring.markets+["GLOBAL"]:
                raise ValueError("规划越出监测范围")
            if not any(term.casefold() in q["query"].casefold() for term in required):
                raise ValueError("查询没有配置方向或关键词")
            if q["query"] in seen or q["query"] in (previous or []):raise ValueError("重复查询")
            seen.add(q["query"])
        return value
    return llm.complete("planner",PLANNER,inputs,Plan,validate)["queries"]

def screen(llm,cfg,candidates):
    inputs={"markets":cfg.monitoring.markets,"securities":cfg.monitoring.securities,
            "directions":cfg.monitoring.directions,"include_keywords":cfg.monitoring.include_keywords,
            "exclude_keywords":cfg.monitoring.exclude_keywords,"candidates":[
                {"candidate_id":c["candidate_id"],"title":c["title"][:120],
                 "snippet":c["snippet"][:180],"source":c["source_name"]} for c in candidates]}
    expected={c["candidate_id"] for c in candidates}
    def validate(value):
        ids=[d["candidate_id"] for d in value["decisions"]]
        if len(ids)!=len(set(ids)) or set(ids)!=expected:raise ValueError("筛选结果必须覆盖且仅覆盖本批候选")
        return value
    return llm.complete("relevance_filter",FILTER,inputs,Decisions,validate)["decisions"]

import json
import re
import unicodedata
from decimal import Decimal,InvalidOperation
from pathlib import Path
from .core import digest,now,bounded
from .llm import tokens,BudgetExceeded,EvidenceValidationError
from .models import Extraction
from .progress import log, short

PROMPT="""依据当前编号段落提炼少量金融事件，输出events；没有实质事件可以为空。
只提原文事实与归属明确的观点，不自行推断股价方向、利好利空或收益。
summary应是一件事，保留拟/计划/预计/已完成等状态；不要单独提取会议流程、法律套话、风险免责声明。
stage=reported用于披露业绩、生产快报、预计统计数；completed仅用于原文明确已实际完成的事项。
摘要包含拟/计划/预计时禁止stage=completed，可用proposed/ongoing/reported/unknown。
subject必须逐字复制所选证据中连续出现的主体名称，找不到则填null。不能将“银行不在少数”改写为主体“多家银行”。mentions.name也必须逐字复制，缺失为空数组。symbol仅在原文明确出现时填写，否则null。若代码在第1段而公司名在第5段，该mention的evidence_ids必须同时包含1和5；找不到含代码的证据就将symbol设为null。
event_time_raw只复制该事件原文的时间表达，如“11月1日”“周一”“近日”；禁止补全年份或转换为ISO日期，无法确认填null，禁止把文章发布日期当作事件日期。
facts.value_raw复制含数字及单位的最短原文，currency仅填写CNY/USD/HKD/EUR或null，unit填写原文单位，期间和同比环比不确定填null/unknown。
招聘、岗位扩充属于operations；earnings用于财务业绩，investment用于投资事项。
每项evidence_ids只选当前段落编号，证据必须覆盖数字、主体与时间；不依据标题补全段落没有的名称。
不得修复或猜测OCR数字。第三方预测必须明确归属原文作者，不是系统预测。每批至多1至2个核心事件。"""

def compact(s):
    return re.sub(r"\s+","",unicodedata.normalize("NFKC",s))

def numbers(s):
    return {x.replace(",","") for x in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?",compact(s))}

def grounded(value,evidence):
    def quote(ids):
        if len(ids)!=len(set(ids)) or any(i not in evidence for i in ids):raise ValueError("证据编号不存在或重复")
        return "\n".join(evidence[i]["text"] for i in ids)
    for event_index, event in enumerate(value["events"]):
        text=quote(event["evidence_ids"])
        if not numbers(event["summary"]).issubset(numbers(text)):raise ValueError("摘要含无证据数字")
        if event["subject"] and compact(event["subject"]) not in compact(text):
            raise ValueError(f"events[{event_index}].subject={event['subject'][:80]!r}缺少逐字证据；复制原文连续名称，无法确认填null")
        if event["event_time_raw"] and compact(event["event_time_raw"]) not in compact(text):
            raise ValueError(f"events[{event_index}].event_time_raw={event['event_time_raw'][:80]!r}缺少逐字证据；复制原文时间表达，禁止补全年份或转ISO日期，无法确认填null")
        if event["event_type"]=="third_party_forecast" and (not event["subject"] or not re.search(
                r"预计|预测|预期|看涨|看跌|forecast|expect|predict",text,re.I)):
            raise ValueError("第三方预测缺少作者/主体或明确预测证据")
        if event["stage"]=="completed" and re.search(r"拟|计划|预计|propos|plans? to",event["summary"],re.I):
            raise ValueError("计划或预计事项不能标为已完成")
        for m in event["mentions"]:
            # Complete exact, explicitly present cross-paragraph references; never invent a field value.
            for field in ["name","symbol"]:
                literal=m[field]
                if not literal:continue
                selected=compact(quote(m["evidence_ids"])).casefold()
                if compact(literal).casefold() in selected:continue
                match=next((i for i,ref in evidence.items() if compact(literal).casefold() in compact(ref["text"]).casefold()),None)
                if match is not None and len(m["evidence_ids"])<4:
                    m["evidence_ids"].append(match)
                    event.setdefault("evidence_repairs",[]).append({"field":"mentions."+field,
                        "added_evidence_id":match,"method":"exact_literal_in_current_batch; relation_still_requires_review"})
            q=compact(quote(m["evidence_ids"]))
            if compact(m["name"]) not in q:raise ValueError("公司名称未在所选证据出现")
            if m["symbol"] and compact(m["symbol"]).casefold() not in q.casefold():raise ValueError("证券代码未在所选证据出现")
        for f in event["facts"]:
            q=compact(quote(f["evidence_ids"]))
            if compact(f["value_raw"]) not in q:raise ValueError("事实数值原文未在证据中出现")
            if f["unit"] and compact(f["unit"]) not in q:raise ValueError("单位未在证据中出现")
            if f["reporting_period"] and compact(f["reporting_period"]) not in q:raise ValueError("报告期缺少证据")
            if f["currency"]:
                patterns={"CNY":r"人民币|CNY|RMB|(?<!港|美|欧|日)元|¥","USD":r"美元|USD|US\$","HKD":r"港元|港币|HKD","EUR":r"欧元|EUR|€"}
                if f["currency"] not in patterns or not re.search(patterns[f["currency"]],q,re.I):
                    raise ValueError("币种缺少证据")
            if f["comparison_basis"]=="yoy" and not re.search(r"同比|year.over.year|yoy",q,re.I):raise ValueError("同比缺少证据")
            if f["comparison_basis"]=="qoq" and not re.search(r"环比|quarter.over.quarter|qoq",q,re.I):raise ValueError("环比缺少证据")
    return value

def batches(paragraphs,budget=650):
    out=[];batch={};size=0
    for p in paragraphs:
        start=0
        while start<len(p["text"]):
            end=min(start+500,len(p["text"]))
            part=p["text"][start:end]
            while tokens(part)>budget:
                end=start+max(40,int((end-start)*.8));part=p["text"][start:end]
            if batch and size+tokens(part)+25>budget:out.append(batch);batch={};size=0
            batch[len(batch)+1]={"text":part,"paragraph_id":p["id"],"span":[start,end],
                                 "page_start":p.get("page_start"),"page_end":p.get("page_end")}
            size+=tokens(part)+25
            if end==len(p["text"]):break
            start=max(start+1,end-40)
    if batch:out.append(batch)
    return out

def normalize_fact(fact):
    raw=unicodedata.normalize("NFKC",fact["value_raw"])
    values=re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?",raw)
    value=None
    if len(values)==1:
        try:
            n=Decimal(values[0].replace(",",""))
            # Do not guess conversions for mixed or unsupported units.
            factor=Decimal("100000000") if "亿" in raw else Decimal("10000") if "万" in raw else Decimal(1)
            value=format(n*factor,"f")
        except InvalidOperation:pass
    unit=fact["unit"]
    factor_applied="100000000" if "亿" in raw else "10000" if "万" in raw else "1"
    if value is not None and unit:unit=unit.replace("亿","").replace("万","")
    return dict(fact,value=value,unit=unit,unit_raw=fact["unit"],scale_applied=factor_applied,
                normalization="decimal_scale_only" if value is not None else "unresolved")

def map_security(mention,master,at_date):
    matches=[]
    for m in master:
        if mention["symbol"]:
            match=m["symbol"].casefold()==mention["symbol"].casefold()
        else:match=mention["name"] in [m["company_name"],*m.get("aliases",[])]
        if not match or (mention["market"]!="UNKNOWN" and m["market"]!=mention["market"]):continue
        # Historical validity must be known; absent intervals stay unresolved.
        if not at_date or not m.get("valid_from") or m["valid_from"]>at_date:continue
        if m.get("valid_to") and m["valid_to"]<at_date:continue
        matches.append(m)
    unique={m["stock_id"]:m for m in matches}
    result={"company_name":mention["name"],"symbol":mention["symbol"],"stock_id":None,
            "market":mention["market"],"mapping_status":"ambiguous" if len(unique)>1 else "unresolved",
            "mention_type":"ticker_explicit" if mention["symbol"] else "company_name_explicit",
            "evidence_ids":mention["evidence_ids"]}
    if len(unique)==1:
        m=next(iter(unique.values()))
        result.update(stock_id=m["stock_id"],symbol=m["symbol"],market=m["market"],
                      mapping_status="verified",mapping_source=m["source"])
    return result

class Extractor:
    def __init__(self,cfg,llm,repo,root):
        self.cfg,self.llm,self.repo,self.root=cfg,llm,repo,Path(root)
    def process(self,doc):
        if doc["quality"].get("status")=="needs_ocr":raise ValueError("needs_ocr：正文缺页，先修复")
        if digest(bounded(self.root/doc["raw_path"],self.root).read_bytes())!=doc["content_hash"]:
            raise ValueError("原件哈希不一致")
        path=bounded(self.root/self.cfg.output.security_master,self.root)
        master=json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not self.llm.model_digest:
            self.llm.check()
        groups=batches(doc["paragraphs"])
        log("正文分块", f"{doc['candidate_id'][:16]} | {short(doc['title'])} | {len(doc['paragraphs'])}段，分为{len(groups)}块")
        accepted=[];new=0;cached=0
        for index,evidence in enumerate(groups):
            key=digest({"doc":doc["version_id"],"evidence":evidence,"schema":Extraction.model_json_schema(),
                        "prompt":PROMPT,"validator_version":"grounding_v3_partial_events","model":self.llm.model_digest,"options":self.cfg.ollama.model_dump()})
            result=self.repo.chunk(key)
            if result is not None:
                try:
                    if not isinstance(result,dict) or not isinstance(result.get("events"),list):
                        raise ValueError("旧缓存格式不合法")
                    result=grounded(result,evidence)
                except (ValueError,KeyError,TypeError):
                    result=None
            if result is not None:
                cached += 1
                log("分块缓存", f"{doc['candidate_id'][:16]} | {index+1}/{len(groups)}块 | 命中已校验证据缓存")
            if result is None:
                if new>=self.cfg.fetch.max_chunks_per_document_cycle:
                    log("分块预算", f"本篇已新增处理{new}块，剩余{len(groups)-index}块保留续处理")
                    break
                log("语义提取", f"{doc['candidate_id'][:16]} | {index+1}/{len(groups)}块 | 输入{len(evidence)}个编号片段")
                try:
                    result=self.llm.complete("extractor",PROMPT,{"title":doc["title"],
                        "quality":doc["quality"].get("status"),
                        "paragraphs":[dict(id=i,**p) for i,p in evidence.items()]},
                        Extraction,lambda value:grounded(value,evidence))
                except EvidenceValidationError as exc:
                    result = {"events": [], "rejected_events": []}
                    for event_index, event in enumerate(exc.candidate["events"]):
                        try:
                            validated = grounded({"events": [event]}, evidence)
                        except (ValueError, KeyError, TypeError) as error:
                            result["rejected_events"].append({
                                "event_index": event_index, "error": str(error), "candidate": event
                            })
                        else:
                            result["events"].extend(validated["events"])
                log("分块结果", f"{doc['candidate_id'][:16]} | {index+1}/{len(groups)}块 | 合格事件{len(result['events'])}条，待复核{len(result.get('rejected_events', []))}条")
                self.repo.chunk(key,result);new+=1
            accepted.append((index,evidence,result))
        if len(accepted)<len(groups):
            return {"status":"partial_pending","completed_chunks":len(accepted),"total_chunks":len(groups),
                    "chunks_cached":cached,"chunks_processed":new,"events":[]}
        rejected_events = [
            dict(rejected, chunk=chunk)
            for chunk, evidence, result in accepted
            for rejected in result.get("rejected_events", [])
        ]
        processed=now();events=[];seen=set()
        for chunk,evidence,result in accepted:
            for n,event in enumerate(result["events"]):
                all_ids=sorted(set(event["evidence_ids"]+[i for m in event["mentions"] for i in m["evidence_ids"]]+[i for f in event["facts"] for i in f["evidence_ids"]]))
                refs=[dict(id=i,**evidence[i]) for i in all_ids]
                bodykey=digest([event["event_type"],compact(event["summary"]),event["stage"]])
                if bodykey in seen:continue
                seen.add(bodykey)
                securities=[map_security(m,master,doc["published_date"]) for m in event["mentions"]]
                markets=sorted({m["market"] for m in securities if m["market"]!="UNKNOWN"})
                event=dict(event,schema_version="financial_event_v1",event_id="E_"+digest([doc["version_id"],bodykey,self.llm.model_digest,digest(PROMPT),digest(master)]),
                    document_version=doc["version_id"],source_record_id=doc["candidate_id"],
                    source={"source_name":doc["source_name"],"source_url":doc["source_url"],
                            "content_hash":doc["content_hash"],"raw_path":doc["raw_path"]},
                    title=doc["title"],directions=doc.get("directions",[]),market=markets[0] if len(markets)==1 else "GLOBAL",
                    securities=securities,facts=[normalize_fact(f) for f in event["facts"]],
                    source_evidence=refs,chunk=chunk,
                    time={k:doc.get(k) for k in ["published_at_raw","published_at","published_date","time_precision",
                                               "first_seen_at","fetched_at","document_available_at"]},
                    extraction={"model":self.cfg.ollama.model,"model_digest":self.llm.model_digest,
                                "status":"grounded_candidate","needs_human_review":True,
                                "requires_visual_review":doc["quality"].get("requires_visual_review",False)},
                    semantic_features_ready=False)
                identity=[event["event_type"],event["stage"],event["subject"],compact(event["summary"]),
                          doc["published_date"],[(f["name"],compact(f["value_raw"]),f["reporting_period"]) for f in event["facts"]]]
                event["event_group_id"]="G_"+digest(identity) if event["subject"] and event["facts"] else event["event_id"]
                event["time"].update(processed_at=processed,extraction_available_at=processed,
                                     available_at=max(doc["document_available_at"],processed),
                                     time_usable_for_historical_alignment=bool(doc["published_date"]),
                                     requires_time_alignment=doc["time_precision"] in {"date","unknown"})
                events.append(event)
        status = "needs_review" if rejected_events else "grounded_candidate"
        queue_status = "needs_review" if rejected_events else "done"
        self.repo.save_events(doc,events,status=queue_status)
        result = {"status":status,"completed_chunks":len(groups),"total_chunks":len(groups),
                  "chunks_cached":cached,"chunks_processed":new,"events":events}
        if rejected_events:
            result["rejected_events"] = rejected_events
        return result

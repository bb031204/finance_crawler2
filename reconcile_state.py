"""Reconcile state.json with committed JSON and archive backups; state repair is explicit."""
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from finance_agent.core import bounded, digest, dump, now
from record_files import MARKETS


def _load_state(root):
    path = bounded(root/".runtime/simple/state.json", root)
    if not path.is_file():
        return {"records": {}, "pending": {}, "plans": {}}, path
    return json.loads(path.read_text(encoding="utf-8")), path


def archive_index(root, identities):
    """Index archive zips by source_id: newest zip first, all replicas plus in-zip audits."""
    index = {}
    archive_dir = bounded(root/"archive", root)
    if not archive_dir.is_dir():
        return index
    for path in sorted(archive_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with zipfile.ZipFile(path) as archive:
                replicas, audits = {}, {}
                for name in archive.namelist():
                    try:
                        row = json.loads(archive.read(name))
                    except (ValueError, OSError, KeyError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    parts = name.replace("\\", "/").split("/")
                    # 审计sidecar以文件名stem标识身份；正式JSON以内容source_id标识。
                    stem = parts[-1][:-5] if parts[-1].endswith(".json") else None
                    identity = row.get("source_id") or (stem if ".runtime" in parts and "audit" in parts else None)
                    if identity not in identities:
                        continue
                    market = next((m for m in MARKETS if m in parts), None)
                    if market is not None:
                        replicas.setdefault(identity, []).append((name, market, row))
                    elif ".runtime" in parts and "audit" in parts:
                        audits[identity] = row
                for identity, files in replicas.items():
                    index.setdefault(identity, []).append((path, files, audits.get(identity)))
        except (OSError, zipfile.BadZipFile):
            continue
    return index


def choose_restoration(entries, disk_audit):
    """Newest snapshot whose content matches an audit; disk audit takes precedence."""
    for path, files, zip_audit in entries:  # newest zip first
        row = files[0][2]
        if disk_audit is not None:
            if disk_audit.get("record_hash") == digest(row):
                return path, files, "verified_by_current_audit"
            continue  # 当前审计存在但不匹配此版本：继续找更旧的备份快照
        if zip_audit is not None:
            if zip_audit.get("record_hash") == digest(row):
                return path, files, "verified_by_archive_audit"
            continue  # 备份快照内部不一致：继续找更旧版本
        return path, files, "needs_audit"  # 无任何可用审计：按最新备份恢复，报告待补审计
    return None, None, "audit_mismatch"


def restore_files(root, output_dir, files):
    """Write archive replicas back to market folders; never overwrite existing files."""
    written, existing = [], []
    for name, market, row in files:
        target = bounded(root/output_dir/market/Path(name).name, root)
        if target.exists():
            existing.append(str(target.relative_to(root)))
            continue
        dump(target, row)
        written.append(str(target.relative_to(root)))
    return written, existing


def _origin_market(cfg, url):
    if not url:
        return None
    host = (urlsplit(url).hostname or "").lower()
    for market in cfg.monitoring.markets:
        for domain in cfg.search.market_domains.get(market, []):
            if host == domain or host.endswith("." + domain):
                return market
    return None


def reconcile_state(root, cfg, apply=False, requeue_lost=False):
    """Dry-run by default; --apply repairs state, restores archived JSON, rebuilds Parquet."""
    root = Path(root).resolve()
    output_dir = cfg.output_dir
    # 1) 归档守卫：data/{market}.zip 未恢复时对账会把整目录误判为缺失。
    for market in MARKETS:
        zip_path = bounded(root/output_dir/(market + ".zip"), root)
        if not zip_path.is_file():
            continue
        with zipfile.ZipFile(zip_path) as archive:
            inner = sum(1 for name in archive.namelist() if name.endswith(".json"))
        folder = bounded(root/output_dir/market, root)
        disk = len(list(folder.glob("*.json"))) if folder.is_dir() else 0
        if inner > disk:
            raise ValueError(f"{market}目录归档未恢复（zip内{inner}条 > 磁盘{disk}条）；请先恢复正式JSON再对账")
    # 2) 库快照（只读；不构造Dataset，避免恢复outbox等副作用）。
    from structured_export import snapshot
    rows, _fingerprints = snapshot(root, output_dir)
    library = {}
    for market, row, audit in rows:
        library[row["source_id"]] = (market, row, audit)
    # 3) state 与分歧分类。
    state, state_path = _load_state(root)
    records = state.setdefault("records", {})
    pending = state.setdefault("pending", {})
    saved_ids = {i for i, r in records.items() if isinstance(r, dict) and r.get("status") == "saved"}
    fixed_state, conflicts, alias_entries = [], [], []
    for identity, (market, row, audit) in library.items():
        rec = records.get(identity)
        if isinstance(rec, dict) and rec.get("status") == "saved":
            continue  # 一致
        if isinstance(rec, dict) and rec.get("record_id") in library and rec.get("record_id") != identity:
            continue  # 重定向别名指向库内另一记录，健康
        if audit.get("record_hash"):
            fixed_state.append(identity)
        else:
            conflicts.append(identity)  # 磁盘有JSON但无匹配审计：不冒充已验证保存，只报告
    missing = []
    for identity in saved_ids:
        target = records[identity].get("record_id") or identity
        if target in library:
            if target != identity:
                alias_entries.append(identity)
            continue
        missing.append(identity)
    # 备份按目标记录的source_id索引；别名条目（record_id≠identity）要恢复其目标。
    index = archive_index(root, {records[i].get("record_id") or i for i in missing}) if missing else {}
    restored, restore_details, audit_mismatch, lost = [], [], [], []
    for identity in missing:
        target = records[identity].get("record_id") or identity
        if target in restored:
            continue  # 同一目标记录被多个别名引用：恢复一次即可
        entries = index.get(target)
        if not entries:
            lost.append(identity)
            continue
        disk_audit_path = bounded(root/".runtime/simple/audit"/(target + ".json"), root)
        disk_audit = None
        if disk_audit_path.is_file():
            try:
                disk_audit = json.loads(disk_audit_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                disk_audit = None
        path, files, mode = choose_restoration(entries, disk_audit)
        if files is None:
            audit_mismatch.append(identity)  # 现存审计与所有备份内容都不符，不能证明归属
            continue
        restored.append(target)
        detail = {"source_id": target, "via_alias": identity if identity != target else None,
            "archive": path.name, "mode": mode,
            "replicas": [(market, Path(name).name) for name, market, _row in files]}
        if apply:
            written, existing = restore_files(root, output_dir, files)
            detail.update(written=written, existing=existing)
            if mode == "verified_by_archive_audit":
                zip_audit = next((audit for p, _f, audit in entries if p == path and audit is not None), None)
                if zip_audit is not None and not disk_audit_path.is_file():
                    dump(disk_audit_path, zip_audit)  # 恢复与内容匹配的备份审计；不凭空造审计
            previous = records.get(target) or {}
            if previous.get("status") != "saved":  # 目标记录回到库中，state同步登记
                records[target] = {"status": "saved",
                    "reason": "对账恢复：从archive备份找回正式JSON", "previous_status": previous.get("status"),
                    "at": now(), "record_id": target, "reconciled": True}
        restore_details.append(detail)
    # lost 中的别名条目：目标记录无备份，别名本身也不再计为saved（重新采集用request_url）。
    lost_aliases = [i for i in lost if records[i].get("record_id") and records[i]["record_id"] != i]
    audit_dir = bounded(root/".runtime/simple/audit", root)
    audit_orphans = sorted(p.stem for p in audit_dir.glob("*.json") if p.stem not in library) if audit_dir.is_dir() else []
    requeued = []
    if apply:
        stamp = now()
        for identity in fixed_state:
            _market, row, _audit = library[identity]
            previous = records.get(identity) or {}
            records[identity] = {"status": "saved",
                "reason": "对账修复：磁盘正式JSON与匹配审计存在而state未登记为saved",
                "previous_status": previous.get("status"), "at": stamp,
                "record_id": identity, "request_url": row["url"], "resolved_url": row["url"], "reconciled": True}
            pending.pop(identity, None)  # 已保存记录的候选不再需要
        for identity in lost:
            previous = records[identity]
            records[identity] = {**previous, "status": "lost",
                "reason": "正式JSON与archive备份中均不存在；不再计为saved", "lost_at": stamp}
        if requeue_lost:
            from simple_crawler import collection_scope_digest
            scope = collection_scope_digest(cfg)
            for identity in lost:
                previous = records[identity]
                if identity in pending or identity in library:
                    continue
                url = previous.get("request_url") or previous.get("resolved_url")
                origin = _origin_market(cfg, url)
                if url and origin:
                    pending[identity] = {"url": url, "title": url, "snippet": "对账发现记录丢失，重新采集",
                        "origin_market": origin, "collection_scope": scope, "first_seen_at": previous.get("at")}
                    requeued.append(identity)
        dump(state_path, state, compact=True)
    parquet = None
    if apply and any(d.get("written") for d in restore_details):
        from structured_export import export_or_defer
        parquet = export_or_defer(root, output_dir, cfg.data_interface.directory)  # 恢复了正式JSON后重建投影
    return {"at": now(), "apply": apply, "requeue_lost": requeue_lost, "status": "ok",
        "library_records": len(library), "state_saved": len(saved_ids),
        "fixed_state": sorted(fixed_state), "fixed_state_count": len(fixed_state),
        "restored": sorted(restored), "restored_count": len(restored), "restore_details": restore_details,
        "lost": sorted(lost), "lost_count": len(lost), "lost_aliases": lost_aliases,
        "audit_mismatch": sorted(audit_mismatch), "conflicts": sorted(conflicts), "conflict_count": len(conflicts),
        "audit_orphans": audit_orphans, "audit_orphan_count": len(audit_orphans),
        "requeued": requeued, "parquet": parquet,
        "notes": ["conflicts=磁盘有JSON但无匹配审计，未改state；可用--reprocess-saved补审计后重新对账",
                  "audit_mismatch=现存审计与所有备份内容均不符，未恢复文件",
                  "lost=无备份可恢复；--requeue-lost可把URL重新入队采集"]}

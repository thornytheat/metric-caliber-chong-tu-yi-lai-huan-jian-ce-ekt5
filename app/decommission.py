"""指标版本撤场（decommission）：把「直接下线」做成可审查、可恢复、幂等的过渡。

状态机（落 SQLite decommissions 表，单向前进）：

    （无记录） --受理--> transitioning（撤场过渡中） --待迁清单清空--> decommissioned（已撤场）

过渡期的三条硬规则：
1) 新引用立即被拒 —— 登记/发布/迁移门禁都会拦截指向过渡中或已撤场版本的引用；
2) 已发布公式仍按原版本取数 —— 过渡中目标版本保持 published，依赖图解析不变；
3) 待迁清单清空才允许一次性切换 —— 清单 = 「最新发布版本仍传递依赖目标版本」的指标名，
   由依赖图实时重算，不靠人工勾选。

可恢复与幂等：
- 受理/迁移/切换各自是一个 SQLite 事务，中途失败整体回滚，重试从断点继续；
- decommissions.UNIQUE(name, version) + ON CONFLICT DO NOTHING：受理重试不重复生成记录；
- finalize 的 UPDATE ... WHERE state='transitioning'：切换重试不会重复写事件；
- snapshot 保存受理时的指标行与待迁清单（旧版结果），重启后过渡现场完整可查。
"""

from __future__ import annotations

import sqlite3

from . import expr, gate, graph as graph_mod, registry, report, store

STATE_TRANSITIONING = "transitioning"
STATE_DECOMMISSIONED = "decommissioned"
ACTIVE_STATES = (STATE_TRANSITIONING, STATE_DECOMMISSIONED)


class DecommissionError(ValueError):
    """撤场流程错误基类。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TargetNotFound(DecommissionError):
    """指标版本不存在 → 404。"""


class RecordNotFound(DecommissionError):
    """撤场记录不存在 → 404。"""


class NotTransitioning(DecommissionError):
    """记录不在过渡中（已撤场）→ 409。"""

    def __init__(self, message: str, record: dict) -> None:
        super().__init__(message)
        self.record = record


class PendingNotEmpty(DecommissionError):
    """待迁清单未清空，不允许切换 → 409。"""

    def __init__(self, message: str, record: dict, pending: list[dict]) -> None:
        super().__init__(message)
        self.record = record
        self.pending = pending


class NotInPendingList(DecommissionError):
    """迁移对象不在待迁清单 → 400。"""


class MigrationRejected(DecommissionError):
    """迁移表达式引用了不可用版本（撤场中/已撤场/未发布/不存在）→ 400。"""

    def __init__(self, message: str, problems: list[dict]) -> None:
        super().__init__(message)
        self.problems = problems


class MigrationBlocked(DecommissionError):
    """迁移版本触发发布门禁（环等，口径冲突除外）→ 409。"""

    def __init__(self, message: str, blockers: list[dict]) -> None:
        super().__init__(message)
        self.blockers = blockers


def decom_id_of(name: str, version: int) -> str:
    """确定性的撤场记录 ID：重试天然命中同一条记录。"""
    return f"dec_{name}_{int(version)}"


# --------------------------------------------------------------------------- 纯函数：待迁清单与状态标注


def compute_pending(metrics: list[dict], name: str, version: int) -> list[dict]:
    """待迁清单：最新发布版本仍（传递）依赖目标版本的指标名。

    判定走依赖图真算：目标版本的全部传递下游里，保留「该指标名的最新已发布版本」
    仍在下游集合中的条目 —— 这些就是仍在按原版本取数、必须迁移的已发布公式。
    迁移完成的标志是：该指标名发布了不再依赖目标版本的新版本（裸引用随之解析走）。
    """
    key = graph_mod.node_key(name, version)
    graph = graph_mod.build_graph(metrics)
    downstream = graph_mod.downstream(graph, key)
    if not downstream:
        return []
    depth_by_key = {item["key"]: item["depth"] for item in downstream}
    latest = registry.latest_published(metrics)
    pending = []
    for metric_name, metric in latest.items():
        metric_key = graph_mod.node_key(metric_name, metric["version"])
        if metric_key not in depth_by_key:
            continue
        pending.append(
            {
                "name": metric_name,
                "key": metric_key,
                "version": int(metric["version"]),
                "owner": metric.get("owner", ""),
                "depth": depth_by_key[metric_key],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
            }
        )
    pending.sort(key=lambda item: (item["depth"], item["key"]))
    return pending


def compute_draft_refs(
    metrics: list[dict], name: str, version: int, exclude_names: set[str] | None = None
) -> list[dict]:
    """仍引用目标版本的未发布草稿（不参与取数，仅提示：它们发布时会被门禁拦截）。"""
    graph = graph_mod.build_graph(metrics)
    key = graph_mod.node_key(name, version)
    exclude = set(exclude_names or ()) | {name}
    drafts: dict[str, dict] = {}
    for item in graph_mod.downstream(graph, key):
        if item.get("published"):
            continue
        if item["name"] in exclude:
            continue
        current = drafts.get(item["name"])
        if current is None or item["depth"] < current["depth"]:
            drafts[item["name"]] = {
                "name": item["name"],
                "key": item["key"],
                "depth": item["depth"],
                "normalized": item.get("normalized", ""),
            }
    return sorted(drafts.values(), key=lambda entry: (entry["depth"], entry["key"]))


def annotate_metrics(metrics: list[dict], records: list[dict]) -> list[dict]:
    """把撤场状态标注到指标行上（decom_state/decom_id），门禁与页面统一读取。"""
    by_key = {(record["name"], int(record["version"])): record for record in records}
    annotated = []
    for metric in metrics:
        record = by_key.get((metric["name"], int(metric["version"])))
        enriched = dict(metric)
        enriched["decom_state"] = record["state"] if record else None
        enriched["decom_id"] = record["decom_id"] if record else None
        annotated.append(enriched)
    return annotated


def enrich_record(record: dict, metrics: list[dict]) -> dict:
    """把撤场记录补全成可审查视图：实时待迁清单、已迁清单、迁移日志、草稿引用。"""
    enriched = dict(record)
    snapshot = record.get("snapshot") or {}
    enriched["key"] = graph_mod.node_key(record["name"], record["version"])
    if record["state"] == STATE_TRANSITIONING:
        pending = compute_pending(metrics, record["name"], record["version"])
    else:
        pending = []
    accept_pending = list(snapshot.get("pending_at_accept") or [])
    live_names = {item["name"] for item in pending}
    accept_names = {item["name"] for item in accept_pending}

    pending_items = []
    for item in accept_pending:
        pending_items.append(
            {**item, "cleared": item["name"] not in live_names, "source": "accept"}
        )
    for item in pending:
        if item["name"] not in accept_names:
            # 受理后新出现的在用在引（经由其他指标传递），同样要迁
            pending_items.append({**item, "cleared": False, "source": "new"})

    enriched["pending"] = pending
    enriched["pending_count"] = len(pending)
    enriched["accept_pending_count"] = len(accept_pending)
    enriched["cleared"] = [item for item in pending_items if item["cleared"]]
    enriched["cleared_count"] = sum(1 for item in pending_items if item["cleared"])
    enriched["pending_items"] = pending_items
    enriched["draft_refs"] = compute_draft_refs(
        metrics, record["name"], record["version"], exclude_names=live_names
    )
    enriched["migrations"] = list(snapshot.get("migrations") or [])
    enriched["finalizable"] = record["state"] == STATE_TRANSITIONING and not pending
    return enriched


# --------------------------------------------------------------------------- 事务动作：受理 / 迁移 / 切换


def accept(
    conn: sqlite3.Connection,
    metrics: list[dict],
    name: str,
    version: int,
    reason: str = "",
    operator: str = "",
    now: str | None = None,
) -> tuple[dict, bool]:
    """受理撤场：写记录 + 事件，同一事务。重试命中 UNIQUE 约束，返回既有记录。

    返回 (record, created)：created=False 表示本次是幂等重试，没有生成新记录。
    """
    index = registry.index_by_key(metrics)
    target = index.get((name, int(version)))
    if target is None:
        raise TargetNotFound(f"指标版本不存在：{name}@{version}")

    existing = store.get_decommission(conn, name, int(version))
    if existing is not None:
        return existing, False

    now = now or report.now_iso()
    pending = compute_pending(metrics, name, version)
    snapshot = {
        "metric": {
            "key": graph_mod.node_key(name, version),
            "name": name,
            "version": int(version),
            "expression": target["expression"],
            "normalized": registry.normalized_of(target),
            "aggregation": target.get("aggregation", ""),
            "dimensions": list(target.get("dimensions") or []),
            "unit": target.get("unit", ""),
            "owner": target.get("owner", ""),
            "published": bool(target.get("published")),
            "description": target.get("description", ""),
        },
        "pending_at_accept": pending,
        "migrations": [],
    }
    record = {
        "decom_id": decom_id_of(name, version),
        "name": name,
        "version": int(version),
        "state": STATE_TRANSITIONING,
        "reason": reason or "",
        "operator": operator or "",
        "snapshot": snapshot,
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
    }
    inserted = store.insert_decommission(conn, record)
    if not inserted:
        # 并发下另一个请求先写入了：返回既有记录，不重复写事件
        return store.get_decommission(conn, name, int(version)), False
    store.add_event(
        conn,
        "decommission_accept",
        f"受理撤场 {name}@{version}：新引用立即拒绝，待迁清单 {len(pending)} 项",
        {
            "decom_id": record["decom_id"],
            "key": graph_mod.node_key(name, version),
            "pending_count": len(pending),
            "pending": [item["key"] for item in pending],
            "reason": reason or "",
            "operator": operator or "",
        },
        now,
    )
    return record, True


def migrate(
    conn: sqlite3.Connection,
    metrics: list[dict],
    target_name: str,
    target_version: int,
    metric_name: str,
    expression: str,
    aggregation: str | None = None,
    dimensions: list[str] | None = None,
    unit: str | None = None,
    owner: str | None = None,
    now: str | None = None,
) -> dict:
    """迁移并发布：为待迁指标登记一个不再引用目标版本的新版本并直接发布。

    整个动作是一个事务：登记新版本（published=1）+ 迁移日志写入撤场记录 + 事件。
    口径冲突门禁在此被豁免（迁移本身就是换口径），其余发布门禁（未发布引用 /
    缺失引用 / 环 / 撤场引用）照常拦截。已迁过的指标名重试返回 already_migrated，
    不会重复登记版本。
    """
    record = store.get_decommission(conn, target_name, int(target_version))
    if record is None:
        raise RecordNotFound(f"{target_name}@{target_version} 没有撤场记录，先受理再迁移")
    if record["state"] != STATE_TRANSITIONING:
        raise NotTransitioning(
            f"{target_name}@{target_version} 已撤场，迁移窗口已关闭", record
        )
    now = now or report.now_iso()

    pending = compute_pending(metrics, target_name, target_version)
    live_names = {item["name"] for item in pending}
    accept_names = {
        item["name"] for item in (record["snapshot"].get("pending_at_accept") or [])
    }
    if metric_name not in live_names:
        if metric_name in accept_names:
            # 已迁过：幂等返回，不重复登记版本
            return {
                "ok": True,
                "already_migrated": True,
                "new_key": None,
                "record": enrich_record(record, metrics),
            }
        raise NotInPendingList(
            f"{metric_name} 不在 {target_name}@{target_version} 的待迁清单中"
        )

    item = next(entry for entry in pending if entry["name"] == metric_name)
    index = registry.index_by_key(metrics)
    old = index.get((metric_name, item["version"])) or {}
    new_version = registry.next_version(metrics, metric_name)
    candidate = registry.validate_metric(
        {
            "name": metric_name,
            "version": new_version,
            "expression": expression,
            "aggregation": aggregation or old.get("aggregation") or "sum",
            "dimensions": dimensions if dimensions is not None else old.get("dimensions") or [],
            "unit": unit if unit is not None else old.get("unit", ""),
            "owner": owner if owner is not None else old.get("owner", ""),
            "description": f"撤场迁移：不再引用 {target_name}@{target_version}",
            "published": False,
        }
    )

    problems = []
    for finding in gate.reference_findings(candidate, metrics):
        state = finding.get("decom_state")
        if state in ACTIVE_STATES:
            problems.append(
                {
                    "target": finding["target"],
                    "reason": "decommissioning_reference",
                    "state": state,
                }
            )
        elif finding["published"] is False:
            problems.append({"target": finding["target"], "reason": "unpublished_reference"})
        elif finding["reason"] in ("version_not_found", "metric_not_found", "no_published_version"):
            problems.append({"target": finding["target"], "reason": finding["reason"]})
    if problems:
        raise MigrationRejected(
            "迁移后的表达式仍引用不可用版本：" + "、".join(p["target"] for p in problems),
            problems,
        )

    publish_result = gate.publish_check(metrics + [candidate], metric_name, new_version)
    blockers = [b for b in publish_result["blockers"] if b["code"] != "caliber_conflict"]
    if blockers:
        raise MigrationBlocked(
            "迁移版本触发发布门禁：" + "；".join(b["detail"] for b in blockers),
            blockers,
        )

    candidate["published"] = True
    store.insert_metric(conn, candidate, now)

    snapshot = record["snapshot"]
    migrations = list(snapshot.get("migrations") or [])
    migrations.append(
        {
            "name": metric_name,
            "from_key": item["key"],
            "to_key": graph_mod.node_key(metric_name, new_version),
            "from_normalized": item["normalized"],
            "to_normalized": candidate["normalized"],
            "at": now,
        }
    )
    snapshot["migrations"] = migrations
    store.update_decommission_snapshot(conn, record["decom_id"], snapshot, now)
    store.add_event(
        conn,
        "decommission_migrate",
        f"撤场迁移 {metric_name}：{item['key']} → {metric_name}@{new_version}（脱离 {target_name}@{target_version}）",
        {
            "decom_id": record["decom_id"],
            "name": metric_name,
            "from_key": item["key"],
            "to_key": graph_mod.node_key(metric_name, new_version),
        },
        now,
    )
    refreshed = store.get_decommission(conn, target_name, int(target_version))
    return {
        "ok": True,
        "already_migrated": False,
        "new_key": graph_mod.node_key(metric_name, new_version),
        "record": enrich_record(refreshed, metrics + [candidate]),
    }


def finalize(
    conn: sqlite3.Connection,
    metrics: list[dict],
    name: str,
    version: int,
    now: str | None = None,
) -> tuple[dict, bool]:
    """一次性切换为已撤场：确认待迁清单为空后，单事务完成状态翻转 + 旧版本下线。

    返回 (record, flipped)：flipped=False 表示记录本来就是已撤场（幂等重试），
    不会重复下线、不重复写事件。
    """
    record = store.get_decommission(conn, name, int(version))
    if record is None:
        raise RecordNotFound(f"{name}@{version} 没有撤场记录")
    if record["state"] == STATE_DECOMMISSIONED:
        return record, False

    pending = compute_pending(metrics, name, version)
    if pending:
        raise PendingNotEmpty(
            f"待迁清单还有 {len(pending)} 项，清空后才能切换为已撤场",
            record,
            pending,
        )

    now = now or report.now_iso()
    snapshot = record["snapshot"]
    snapshot["final"] = {
        "finished_at": now,
        "pending_at_finish": [],
        "note": "待迁清单已清空，一次性切换为已撤场；旧版本停止取数，历史快照保留",
    }
    flipped = store.finalize_decommission(conn, record["decom_id"], snapshot, now, now)
    if flipped:
        store.set_published(conn, name, int(version), False, now)
        store.add_event(
            conn,
            "decommission_finalize",
            f"{name}@{version} 已撤场：待迁清单清空，旧版本停止取数（历史快照与体检可回看）",
            {
                "decom_id": record["decom_id"],
                "key": graph_mod.node_key(name, version),
            },
            now,
        )
    return store.get_decommission(conn, name, int(version)), flipped

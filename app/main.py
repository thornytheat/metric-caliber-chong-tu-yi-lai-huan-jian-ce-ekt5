"""FastAPI 路由（薄）：页面渲染 + JSON API，规则全部在 app/expr|graph|conflict|gate。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import conflict, decommission, expr, gate, graph as graph_mod, registry, report, seed, settings, store

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

@asynccontextmanager
async def lifespan(application: FastAPI):
    with store.session() as conn:
        application.state.seed_result = seed.seed(conn)
    yield


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def load_metrics() -> list[dict]:
    with store.session() as conn:
        store.init_schema(conn)
        metrics = store.all_metrics(conn)
        records = store.list_decommissions(conn)
    metrics = decommission.annotate_metrics(metrics, records)
    return [registry.with_normalized(metric) for metric in metrics]


def base_context(request: Request, **extra: Any) -> dict:
    metrics = load_metrics()
    overview = report.overview(metrics)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "error": None,
        "notice": None,
    }
    context.update(extra)
    return context


# --------------------------------------------------------------------------- 请求模型


class ParseRequest(BaseModel):
    expression: str = Field(default="")
    strict: bool = False


class ImpactRequest(BaseModel):
    name: str
    version: int | None = None
    expression: str
    aggregation: str | None = None
    dimensions: list[str] | None = None


class PublishRequest(BaseModel):
    name: str
    version: int


class RegistryRequest(BaseModel):
    name: str
    expression: str
    version: int | None = None
    aggregation: str = "sum"
    dimensions: list[str] = Field(default_factory=list)
    unit: str = ""
    owner: str = ""
    description: str = ""
    published: bool = False
    allow_missing_reference: bool = False


class DecommissionRequest(BaseModel):
    name: str
    version: int
    reason: str = ""
    operator: str = ""


class DecommissionMigrateRequest(BaseModel):
    target_name: str
    target_version: int
    name: str
    expression: str
    aggregation: str | None = None
    dimensions: list[str] | None = None
    unit: str | None = None
    owner: str | None = None


class DecommissionFinalizeRequest(BaseModel):
    name: str
    version: int


# --------------------------------------------------------------------------- 页面


@app.get("/health")
def health() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        rows = store.count_metrics(conn)
    return {"status": "ok", "app": settings.APP_SHORT, "metrics": rows}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    overview = report.overview(metrics)
    with store.session() as conn:
        store.init_schema(conn)
        recent_checks = store.list_checks(conn, limit=6)
    top = sorted(
        overview["conflicts"], key=lambda item: item["variant_count"], reverse=True
    )[:3]
    cycles = overview["cycles"]
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "top_conflicts": top,
        "cycles": cycles,
        "seed_result": getattr(app.state, "seed_result", {"seeded": False, "reason": "启动中"}),
        "recent_checks": recent_checks,
        "error": None,
        "notice": None,
        "page": "index",
        "active": "index",
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    names = registry.names(metrics)
    versions = {
        name: [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "unit": metric["unit"],
                "published": metric["published"],
                "owner": metric["owner"],
                "decom_state": metric.get("decom_state"),
            }
            for metric in registry.versions_of(metrics, name)
        ]
        for name in names
    }
    focus = registry.versions_of(metrics, "gmv_daily")
    focus_metric = focus[0] if focus else None
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "names": names,
        "versions": versions,
        "focus": focus_metric,
        "focus_key": graph_mod.node_key(focus_metric["name"], focus_metric["version"]) if focus_metric else "",
        "focus_rows": graph_mod.dependency_rows(
            graph_mod.build_graph(metrics),
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "focus_downstream": graph_mod.downstream(
            graph_mod.build_graph(metrics),
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "error": None,
        "notice": None,
        "page": "workbench",
        "active": "workbench",
    }
    return templates.TemplateResponse(request, "workbench.html", context)


@app.get("/result/{check_id}", response_class=HTMLResponse)
def result_page(request: Request, check_id: str) -> HTMLResponse:
    with store.session() as conn:
        payload = store.load_check(conn, check_id)
    metrics = load_metrics()
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "check": payload,
        "check_id": check_id,
        "missing": payload is None,
        "error": None if payload else f"体检记录 {check_id} 不存在，请回到首页重新体检",
        "notice": None,
        "page": "result",
        "active": "result",
    }
    return templates.TemplateResponse(request, "result.html", context)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    with store.session() as conn:
        latest = store.latest_check(conn)
        impact = store.latest_impact(conn)
        events = store.list_events(conn, limit=25)
        checks = store.list_checks(conn, limit=8)
        decom_records = store.list_decommissions(conn)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "latest": latest,
        "impact": impact,
        "events": events,
        "checks": checks,
        "decom_records": [decommission.enrich_record(record, metrics) for record in decom_records],
        "error": None,
        "notice": None,
        "page": "review",
        "active": "review",
    }
    return templates.TemplateResponse(request, "review.html", context)


# --------------------------------------------------------------------------- 注册表


@app.get("/api/registry")
def api_registry() -> dict:
    metrics = load_metrics()
    overview = report.overview(metrics)
    return {
        "app": settings.APP_SHORT,
        "stats": {
            "metric_entries": overview["metric_entries"],
            "metric_names": overview["metric_names"],
            "published_entries": overview["published_entries"],
            "unpublished_entries": overview["unpublished_entries"],
            "conflict_count": overview["conflict_count"],
            "cycle_count": overview["cycle_count"],
            "publish_blocked_count": overview["publish_blocked_count"],
            "unresolved_count": overview["unresolved_count"],
            "cross_version_count": overview["cross_version_count"],
            "decommissioning_count": overview["decommissioning_count"],
            "decommissioned_count": overview["decommissioned_count"],
        },
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "unit": metric["unit"],
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "owner": metric["owner"],
                "published": metric["published"],
                "decom_state": metric.get("decom_state"),
            }
            for metric in metrics
        ],
    }


@app.post("/api/registry", status_code=201)
def api_register(payload: RegistryRequest) -> JSONResponse:
    metrics = load_metrics()
    version = payload.version if payload.version else registry.next_version(metrics, payload.name)
    if store_metric_exists(metrics, payload.name, version):
        raise HTTPException(
            status_code=409,
            detail=f"{payload.name}@{version} 已存在，请递增 version 后再登记",
        )
    try:
        metric = registry.validate_metric(
            {
                "name": payload.name,
                "version": version,
                "expression": payload.expression,
                "aggregation": payload.aggregation,
                "dimensions": payload.dimensions,
                "unit": payload.unit,
                "owner": payload.owner,
                "description": payload.description,
                "published": payload.published,
            }
        )
    except registry.RegistryError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.message, "field": exc.field})

    analysis = expr.analyze_expression(payload.expression)
    unknown_dims = _unknown_dimensions(analysis, metrics)
    findings = gate.reference_findings(metric, metrics)
    decommissioning_refs = [
        {"target": finding["target"], "state": finding["decom_state"]}
        for finding in findings
        if finding.get("decom_state") in decommission.ACTIVE_STATES
    ]
    missing_refs = [
        finding["target"]
        for finding in findings
        if finding["reason"] in ("version_not_found", "metric_not_found", "no_published_version")
    ]
    unpublished_refs = [
        finding["target"]
        for finding in findings
        if finding["published"] is False and not finding.get("decom_state")
    ]
    if decommissioning_refs:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "表达式引用了撤场过渡中/已撤场的版本，新引用一律拒绝",
                "decommissioning_references": decommissioning_refs,
                "field": "expression",
            },
        )
    if missing_refs and not payload.allow_missing_reference:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "表达式引用了不存在或没有已发布版本的指标",
                "missing_references": missing_refs,
                "field": "expression",
            },
        )
    if unpublished_refs and payload.published:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "引用了未发布版本，不能直接登记为已发布",
                "unpublished_references": unpublished_refs,
                "field": "expression",
            },
        )

    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.insert_metric(conn, metric, now)
        store.add_event(
            conn,
            "register",
            f"登记指标 {metric['name']}@{metric['version']}（{metric['normalized']}）",
            {
                "key": f"{metric['name']}@{metric['version']}",
                "normalized": metric["normalized"],
                "published": metric["published"],
            },
            now,
        )
    conflicts = conflict.caliber_groups(metrics + [metric], metric["name"])
    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "key": f"{metric['name']}@{metric['version']}",
            "name": metric["name"],
            "version": metric["version"],
            "expression": metric["expression"],
            "normalized": metric["normalized"],
            "aggregation": metric["aggregation"],
            "dimensions": metric["dimensions"],
            "published": metric["published"],
            "unknown_dims": unknown_dims,
            "missing_references": missing_refs,
            "unpublished_references": unpublished_refs,
            "caliber_group_count": len(conflicts),
            "caliber_conflict": len(conflicts) > 1,
        },
    )


def store_metric_exists(metrics: list[dict], name: str, version: int) -> bool:
    return any(
        metric["name"] == name and int(metric["version"]) == int(version) for metric in metrics
    )


def _unknown_dimensions(analysis: dict, metrics: list[dict]) -> list[str]:
    known = set()
    for metric in metrics:
        known.update(metric.get("dimensions") or [])
    return [ref["name"] for ref in analysis["dims"] if ref["name"] not in known]


# --------------------------------------------------------------------------- 解析 / 体检 / 影响面 / 发布


@app.post("/api/parse")
def api_parse(payload: ParseRequest = Body(default=ParseRequest())) -> JSONResponse:
    analysis = expr.analyze_expression(payload.expression)
    if not analysis["ok"]:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": analysis["error"],
                "position": analysis["position"],
                "normalized": None,
            },
        )
    metrics = load_metrics()
    index = registry.index_by_key(metrics)
    published = registry.latest_published(metrics)
    unresolved = []
    decommissioning = []
    for ref in analysis["metrics"]:
        resolved = gate._resolve(ref, metrics, index, published)
        if resolved.get("decom_state") in decommission.ACTIVE_STATES:
            decommissioning.append(
                {
                    "target": resolved["target"],
                    "state": resolved["decom_state"],
                }
            )
        if resolved["reason"] or resolved["published"] is False:
            unresolved.append(
                {
                    "target": resolved["target"],
                    "reason": resolved["reason"] or "unpublished",
                    "published": resolved["published"],
                }
            )
    if (unresolved or decommissioning) and payload.strict:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "表达式引用了不可解析或已撤场的指标",
                "unresolved": unresolved,
                "decommissioning": decommissioning,
            },
        )
    return JSONResponse(
        content={
            "ok": True,
            "raw": analysis["raw"],
            "normalized": analysis["normalized"],
            "changed": analysis["raw"] != analysis["normalized"],
            "references": analysis["references"],
            "metrics": [ref["name"] for ref in analysis["metrics"]],
            "dims": [ref["name"] for ref in analysis["dims"]],
            "functions": analysis["functions"],
            "unresolved": unresolved,
            "decommissioning": decommissioning,
        }
    )


def _run_and_store_check() -> dict:
    metrics = load_metrics()
    with store.session() as conn:
        store.init_schema(conn)
        records = store.list_decommissions(conn)
    decom_list = [decommission.enrich_record(record, metrics) for record in records]
    payload = report.build_check(metrics, decommissions=decom_list)
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.save_check(conn, payload)
        store.add_event(
            conn,
            "check",
            f"执行注册表体检：冲突 {payload['stats']['conflict_count']} 组 / 环 {payload['stats']['cycle_count']} 条 / 发布阻断 {payload['stats']['publish_blocked_count']} 个",
            {"check_id": payload["check_id"], "verdict": payload["verdict"]},
            now,
        )
    return payload


@app.post("/api/check")
def api_check() -> dict:
    return _run_and_store_check()


@app.post("/check")
def check_form() -> RedirectResponse:
    payload = _run_and_store_check()
    return RedirectResponse(url=f"/result/{payload['check_id']}", status_code=303)


@app.get("/api/check/{check_id}")
def api_check_detail(check_id: str) -> dict:
    with store.session() as conn:
        payload = store.load_check(conn, check_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"体检记录 {check_id} 不存在")
    return payload


@app.post("/api/impact")
def api_impact(payload: ImpactRequest) -> dict:
    metrics = load_metrics()
    if not any(metric["name"] == payload.name for metric in metrics):
        raise HTTPException(status_code=404, detail=f"指标 {payload.name} 未注册")
    version = payload.version
    if not version:
        latest = registry.latest_version(metrics, payload.name)
        version = latest["version"] if latest else 1
    try:
        impact = report.build_impact(
            metrics,
            name=payload.name,
            version=int(version),
            expression=payload.expression,
            aggregation=payload.aggregation,
            dimensions=payload.dimensions,
        )
    except gate.ExpressionInvalid as exc:
        raise HTTPException(
            status_code=400, detail={"error": exc.message, "position": exc.position}
        )
    except gate.TargetNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": exc.message})
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.save_impact(conn, impact)
        store.add_event(
            conn,
            "impact",
            f"评估 {impact['target']['key']} 变更：下游 {impact['downstream_count']} 个，breaking {impact['breaking_count']} 个",
            {"impact_id": impact["impact_id"], "key": impact["target"]["key"]},
            now,
        )
    return impact


@app.post("/api/publish")
def api_publish(payload: PublishRequest) -> JSONResponse:
    metrics = load_metrics()
    try:
        result = gate.publish_check(metrics, payload.name, int(payload.version))
    except gate.GateError as exc:
        raise HTTPException(status_code=404, detail={"error": exc.message})
    if not result["ok"]:
        return JSONResponse(status_code=409, content=result)
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.set_published(conn, payload.name, int(payload.version), True, now)
        store.add_event(
            conn,
            "publish",
            f"发布指标 {result['key']}（{result['normalized']}）",
            {"key": result["key"], "normalized": result["normalized"]},
            now,
        )
    result["published"] = True
    return JSONResponse(status_code=200, content=result)


# --------------------------------------------------------------------------- 撤场（可审查过渡）


@app.post("/api/decommission", status_code=201)
def api_decommission_accept(payload: DecommissionRequest) -> JSONResponse:
    """受理撤场：进入过渡中。幂等 —— 同版本重试返回既有记录，不重复生成。"""
    metrics = load_metrics()
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        try:
            record, created = decommission.accept(
                conn,
                metrics,
                payload.name,
                int(payload.version),
                reason=payload.reason,
                operator=payload.operator,
                now=now,
            )
        except decommission.TargetNotFound as exc:
            raise HTTPException(status_code=404, detail={"error": exc.message})
        enriched = decommission.enrich_record(record, metrics)
        if record["state"] == decommission.STATE_DECOMMISSIONED:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": f"{payload.name}@{payload.version} 已撤场，撤场记录不可重复生成",
                    "record": enriched,
                },
            )
        return JSONResponse(
            status_code=201 if created else 200,
            content={"ok": True, "deduplicated": not created, "record": enriched},
        )


@app.get("/api/decommission")
def api_decommission_list() -> dict:
    """全部撤场记录（含实时待迁清单）：中断/重启后的过渡现场从这里恢复。"""
    metrics = load_metrics()
    with store.session() as conn:
        store.init_schema(conn)
        records = store.list_decommissions(conn)
    enriched = [decommission.enrich_record(record, metrics) for record in records]
    return {"count": len(enriched), "records": enriched}


@app.post("/api/decommission/migrate", status_code=201)
def api_decommission_migrate(payload: DecommissionMigrateRequest) -> JSONResponse:
    """迁移并发布：为待迁指标登记不再引用目标版本的新版本。单事务，重试幂等。"""
    metrics = load_metrics()
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        try:
            result = decommission.migrate(
                conn,
                metrics,
                payload.target_name,
                int(payload.target_version),
                payload.name,
                payload.expression,
                aggregation=payload.aggregation,
                dimensions=payload.dimensions,
                unit=payload.unit,
                owner=payload.owner,
                now=now,
            )
        except decommission.RecordNotFound as exc:
            raise HTTPException(status_code=404, detail={"error": exc.message})
        except decommission.NotTransitioning as exc:
            raise HTTPException(status_code=409, detail={"error": exc.message})
        except decommission.NotInPendingList as exc:
            raise HTTPException(status_code=400, detail={"error": exc.message})
        except registry.RegistryError as exc:
            raise HTTPException(status_code=400, detail={"error": exc.message, "field": exc.field})
        except decommission.MigrationRejected as exc:
            raise HTTPException(
                status_code=400, detail={"error": exc.message, "problems": exc.problems}
            )
        except decommission.MigrationBlocked as exc:
            raise HTTPException(
                status_code=409, detail={"error": exc.message, "blockers": exc.blockers}
            )
        return JSONResponse(
            status_code=200 if result["already_migrated"] else 201, content=result
        )


@app.post("/api/decommission/finalize")
def api_decommission_finalize(payload: DecommissionFinalizeRequest) -> JSONResponse:
    """待迁清单清空后一次性切换为已撤场。重试返回既有终态，不重复写记录。"""
    metrics = load_metrics()
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        try:
            record, flipped = decommission.finalize(
                conn, metrics, payload.name, int(payload.version), now=now
            )
        except decommission.RecordNotFound as exc:
            raise HTTPException(status_code=404, detail={"error": exc.message})
        except decommission.PendingNotEmpty as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": exc.message,
                    "pending": exc.pending,
                    "record": decommission.enrich_record(exc.record, metrics),
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "already": not flipped,
                "record": decommission.enrich_record(record, metrics),
            },
        )


@app.get("/api/decommission/{name}/{version}")
def api_decommission_detail(name: str, version: int) -> dict:
    """单条撤场记录：快照（旧版结果）+ 实时待迁清单 + 迁移日志。"""
    metrics = load_metrics()
    with store.session() as conn:
        store.init_schema(conn)
        record = store.get_decommission(conn, name, int(version))
    if record is None:
        raise HTTPException(status_code=404, detail=f"{name}@{version} 没有撤场记录")
    return decommission.enrich_record(record, metrics)


@app.get("/api/metrics")
def api_metrics() -> dict:
    metrics = load_metrics()
    return {
        "count": len(metrics),
        "names": registry.names(metrics),
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "published": metric["published"],
                "decom_state": metric.get("decom_state"),
            }
            for metric in metrics
        ],
    }

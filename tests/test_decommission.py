"""撤场过渡：受理 → 新引用拒绝 → 待迁清单 → 迁移 → 一次性切换 → 恢复与幂等。

用一条独立的 legacy_* 指标链（不碰样例数据），按定义顺序共享同一个库：
legacy_base@1（已发布）← legacy_mid@1 ← legacy_top@1，外加未发布草稿 legacy_draft@1。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import decommission, gate, store
from app.main import app

STATE: dict = {}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _events(kind: str) -> list[dict]:
    with store.session() as conn:
        store.init_schema(conn)
        return [event for event in store.list_events(conn, limit=300) if event["kind"] == kind]


def _decom_records() -> list[dict]:
    with store.session() as conn:
        store.init_schema(conn)
        return store.list_decommissions(conn)


# --------------------------------------------------------------------------- 准备指标链


def test_setup_chain(client):
    chain = [
        {"name": "legacy_base", "expression": "sum(dim:legacy_amt)", "published": True},
        {"name": "legacy_mid", "expression": "metric:legacy_base * 2", "published": True},
        {"name": "legacy_top", "expression": "metric:legacy_mid / metric:order_count", "published": True},
        {"name": "legacy_draft", "expression": "metric:legacy_base + metric:order_count", "published": False},
    ]
    for item in chain:
        response = client.post(
            "/api/registry",
            json={**item, "aggregation": "sum", "dimensions": ["dt"], "owner": "测试组"},
        )
        assert response.status_code == 201, response.text
    # 撤场前先做一次体检，后面验证历史体检可回看
    check = client.post("/api/check")
    assert check.status_code == 200
    STATE["check_id"] = check.json()["check_id"]


# --------------------------------------------------------------------------- 受理


def test_accept_unknown_version_returns_404(client):
    response = client.post("/api/decommission", json={"name": "legacy_base", "version": 99})
    assert response.status_code == 404


def test_accept_creates_transition(client):
    response = client.post(
        "/api/decommission",
        json={"name": "legacy_base", "version": 1, "reason": "口径由新指标替代", "operator": "指标体系组"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True
    assert body["deduplicated"] is False
    record = body["record"]
    assert record["decom_id"] == "dec_legacy_base_1"
    assert record["state"] == "transitioning"
    # 待迁清单 = 最新发布版本仍依赖 legacy_base@1 的指标名（未发布草稿不算）
    assert {item["name"] for item in record["pending"]} == {"legacy_mid", "legacy_top"}
    assert record["pending_count"] == 2
    assert {item["name"] for item in record["draft_refs"]} == {"legacy_draft"}
    # 旧版结果快照：受理时的指标行原样保留
    assert record["snapshot"]["metric"]["published"] is True
    assert record["snapshot"]["metric"]["normalized"] == "sum(dim:legacy_amt)"
    assert len(record["snapshot"]["pending_at_accept"]) == 2
    STATE["transition"] = record


def test_accept_retry_deduplicates(client):
    response = client.post(
        "/api/decommission",
        json={"name": "legacy_base", "version": 1, "reason": "重复提交", "operator": "别人"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deduplicated"] is True
    assert body["record"]["decom_id"] == "dec_legacy_base_1"
    # 重试没有重复生成记录，也没有重复写受理事件
    assert len(_decom_records()) == 1
    assert len(_events("decommission_accept")) == 1


# --------------------------------------------------------------------------- 过渡期：新引用拒绝 + 旧公式照常取数


def test_new_reference_rejected_at_register(client):
    bare = client.post(
        "/api/registry",
        json={"name": "legacy_new", "expression": "metric:legacy_base + metric:order_count", "aggregation": "sum"},
    )
    assert bare.status_code == 400
    detail = bare.json()["detail"]
    assert detail["decommissioning_references"] == [
        {"target": "legacy_base@1", "state": "transitioning"}
    ]

    explicit = client.post(
        "/api/registry",
        json={"name": "legacy_new", "expression": "metric:legacy_base@1 + metric:order_count", "aggregation": "sum"},
    )
    assert explicit.status_code == 400
    assert explicit.json()["detail"]["decommissioning_references"]

    names = client.get("/api/metrics").json()["names"]
    assert "legacy_new" not in names


def test_parse_warns_about_transitioning_reference(client):
    response = client.post("/api/parse", json={"expression": "metric:legacy_base * 2"})
    assert response.status_code == 200
    assert response.json()["decommissioning"] == [
        {"target": "legacy_base@1", "state": "transitioning"}
    ]
    strict = client.post("/api/parse", json={"expression": "metric:legacy_base * 2", "strict": True})
    assert strict.status_code == 400


def test_published_formulas_still_fetch_original_version(client):
    registry = client.get("/api/registry").json()
    entry = next(item for item in registry["metrics"] if item["key"] == "legacy_base@1")
    assert entry["published"] is True  # 过渡中不下线
    assert entry["decom_state"] == "transitioning"
    assert registry["stats"]["decommissioning_count"] == 1

    check = client.post("/api/check").json()
    assert check["stats"]["decommissioning_count"] == 1
    assert check["decommissions"][0]["pending_count"] == 2
    metric_row = next(item for item in check["metrics"] if item["key"] == "legacy_base@1")
    assert metric_row["published"] is True


def test_publish_gate_blocks_draft_referencing_transitioning(client):
    # 受理前登记的草稿，过渡期内不允许发布（新引用立即被拒）
    response = client.post("/api/publish", json={"name": "legacy_draft", "version": 1})
    assert response.status_code == 409
    codes = {blocker["code"] for blocker in response.json()["blockers"]}
    assert "decommissioning_reference" in codes


def test_review_page_shows_transition(client):
    response = client.get("/review")
    assert response.status_code == 200
    assert "撤场过渡与待迁清单" in response.text
    assert "dec_legacy_base_1" in response.text
    assert "撤场中" in response.text


# --------------------------------------------------------------------------- 迁移


def test_finalize_blocked_while_pending(client):
    response = client.post("/api/decommission/finalize", json={"name": "legacy_base", "version": 1})
    assert response.status_code == 409
    body = response.json()
    assert {item["name"] for item in body["pending"]} == {"legacy_mid", "legacy_top"}
    # 状态未被改变
    detail = client.get("/api/decommission/legacy_base/1").json()
    assert detail["state"] == "transitioning"
    assert _events("decommission_finalize") == []


def test_migrate_rejects_bad_requests(client):
    unknown = client.post(
        "/api/decommission/migrate",
        json={"target_name": "no_such", "target_version": 1, "name": "legacy_mid", "expression": "sum(dim:x)"},
    )
    assert unknown.status_code == 404

    not_pending = client.post(
        "/api/decommission/migrate",
        json={"target_name": "legacy_base", "target_version": 1, "name": "order_amount", "expression": "sum(dim:x)"},
    )
    assert not_pending.status_code == 400

    still_refs = client.post(
        "/api/decommission/migrate",
        json={"target_name": "legacy_base", "target_version": 1, "name": "legacy_mid", "expression": "metric:legacy_base@1"},
    )
    assert still_refs.status_code == 400
    assert still_refs.json()["detail"]["problems"][0]["reason"] == "decommissioning_reference"

    bad_expr = client.post(
        "/api/decommission/migrate",
        json={"target_name": "legacy_base", "target_version": 1, "name": "legacy_mid", "expression": "metric:a +"},
    )
    assert bad_expr.status_code == 400


def test_migrate_mid_success_cascades_via_bare_reference(client):
    response = client.post(
        "/api/decommission/migrate",
        json={
            "target_name": "legacy_base",
            "target_version": 1,
            "name": "legacy_mid",
            "expression": "sum(dim:legacy_mid_amt)",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["already_migrated"] is False
    assert body["new_key"] == "legacy_mid@2"
    record = body["record"]
    # legacy_mid@2 不再依赖 legacy_base@1；legacy_top@1 的裸引用随之解析到 @2，级联清空
    assert record["pending"] == []
    assert record["finalizable"] is True
    assert {item["name"] for item in record["cleared"]} == {"legacy_mid", "legacy_top"}
    assert record["migrations"][0]["from_key"] == "legacy_mid@1"
    assert record["migrations"][0]["to_key"] == "legacy_mid@2"

    metrics = client.get("/api/metrics").json()
    versions = sorted(
        item["version"] for item in metrics["metrics"] if item["name"] == "legacy_mid"
    )
    assert versions == [1, 2]


def test_migrate_retry_is_idempotent(client):
    response = client.post(
        "/api/decommission/migrate",
        json={
            "target_name": "legacy_base",
            "target_version": 1,
            "name": "legacy_mid",
            "expression": "sum(dim:legacy_mid_amt)",
        },
    )
    assert response.status_code == 200
    assert response.json()["already_migrated"] is True
    # 没有重复登记 legacy_mid@3
    metrics = client.get("/api/metrics").json()
    versions = sorted(
        item["version"] for item in metrics["metrics"] if item["name"] == "legacy_mid"
    )
    assert versions == [1, 2]
    assert len(_events("decommission_migrate")) == 1


# --------------------------------------------------------------------------- 一次性切换


def test_finalize_success(client):
    response = client.post("/api/decommission/finalize", json={"name": "legacy_base", "version": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["already"] is False
    assert body["record"]["state"] == "decommissioned"
    assert body["record"]["finished_at"]

    registry = client.get("/api/registry").json()
    entry = next(item for item in registry["metrics"] if item["key"] == "legacy_base@1")
    assert entry["published"] is False  # 切换后旧版本停止取数
    assert entry["decom_state"] == "decommissioned"
    assert registry["stats"]["decommissioned_count"] == 1
    assert len(_events("decommission_finalize")) == 1


def test_finalize_retry_no_duplicate(client):
    response = client.post("/api/decommission/finalize", json={"name": "legacy_base", "version": 1})
    assert response.status_code == 200
    assert response.json()["already"] is True
    assert len(_decom_records()) == 1
    assert len(_events("decommission_finalize")) == 1


def test_reference_to_decommissioned_rejected(client):
    explicit = client.post(
        "/api/registry",
        json={"name": "legacy_late", "expression": "metric:legacy_base@1 + metric:order_count", "aggregation": "sum"},
    )
    assert explicit.status_code == 400
    refs = explicit.json()["detail"]["decommissioning_references"]
    assert refs == [{"target": "legacy_base@1", "state": "decommissioned"}]

    # 裸引用：legacy_base 已无已发布版本，按缺失引用拒绝
    bare = client.post(
        "/api/registry",
        json={"name": "legacy_late", "expression": "metric:legacy_base + metric:order_count", "aggregation": "sum"},
    )
    assert bare.status_code == 400
    assert bare.json()["detail"]["missing_references"] == ["legacy_base"]


def test_accept_after_decommission_returns_409(client):
    response = client.post("/api/decommission", json={"name": "legacy_base", "version": 1})
    assert response.status_code == 409
    assert len(_decom_records()) == 1
    assert len(_events("decommission_accept")) == 1


# --------------------------------------------------------------------------- 中断恢复与旧版结果回看


def test_transition_scene_and_old_results_recoverable(client):
    # 模拟重启：换一个全新的 TestClient（同一 SQLite 文件），过渡现场完整恢复
    with TestClient(app) as fresh:
        detail = fresh.get("/api/decommission/legacy_base/1")
        assert detail.status_code == 200
        record = detail.json()
        assert record["state"] == "decommissioned"
        assert record["snapshot"]["metric"]["published"] is True  # 旧版结果：受理时的取数口径
        assert record["snapshot"]["metric"]["normalized"] == "sum(dim:legacy_amt)"
        assert len(record["snapshot"]["pending_at_accept"]) == 2
        assert len(record["migrations"]) == 1

        listing = fresh.get("/api/decommission").json()
        assert listing["count"] == 1
        assert listing["records"][0]["decom_id"] == "dec_legacy_base_1"

        # 历史体检仍可回看原版本（撤场前的体检快照里 legacy_base@1 是已发布）
        old = fresh.get(f"/api/check/{STATE['check_id']}")
        assert old.status_code == 200
        row = next(item for item in old.json()["metrics"] if item["key"] == "legacy_base@1")
        assert row["published"] is True


# --------------------------------------------------------------------------- 纯函数：待迁清单与门禁（不碰数据库）


def test_compute_pending_on_sample_graph(metrics):
    pending = decommission.compute_pending(metrics, "gmv_daily", 1)
    assert {item["name"] for item in pending} == {
        "gmv_7d",
        "gmv_index",
        "arpu",
        "revenue",
        "pay_gmv_share",
        "uv_per_gmv",
    }
    depth = {item["name"]: item["depth"] for item in pending}
    assert depth["gmv_7d"] == 1
    assert depth["revenue"] == 2  # revenue 经由 arpu 传递依赖


def test_publish_check_blocks_decommissioning_reference(metrics_copy):
    for metric in metrics_copy:
        if (metric["name"], metric["version"]) == ("uv", 2):
            metric["decom_state"] = "transitioning"
    result = gate.publish_check(metrics_copy, "uv_per_gmv", 1)
    codes = {blocker["code"] for blocker in result["blockers"]}
    assert "decommissioning_reference" in codes


def test_change_impact_flags_decommissioning_refs(metrics_copy):
    for metric in metrics_copy:
        if (metric["name"], metric["version"]) == ("uv", 2):
            metric["decom_state"] = "transitioning"
    result = gate.change_impact(
        metrics_copy,
        name="gmv_daily",
        version=1,
        new_expression="metric:uv@2 / metric:user_count",
    )
    assert result["decommissioning_refs"] == ["uv@2"]
    assert {item["code"] for item in result["breaking"]} == {"decommissioning_reference"}
    assert any("撤场" in note for note in result["notes"])

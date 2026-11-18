"""端到端：样例 → 判定 → 对账 → 导出，页面与接口一致。"""

from __future__ import annotations

import csv
import io

from tests.fixtures import expected

PAGES = [
    ("/", "规格单"),
    ("/types", "类型与兼容"),
    ("/mapping", "字段映射"),
    ("/impact", "影响面"),
    ("/batches", "版本历史"),
    ("/reconciliation", "生效日复核"),
    ("/spec", "迁移单"),
]


def test_health_and_home(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["runs"] == 1
    assert body["window_semantics"] == "[start, end)"

    home = client.get("/")
    assert home.status_code == 200
    assert "规格单" in home.text


def test_all_pages_render(client):
    for path, marker in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, path
        assert "/static/theme.css" in response.text
        assert "http://" not in response.text.split("<main")[0]


def test_full_flow_matches_ledger(client):
    reconcile = client.get("/api/reconcile").json()
    fields = reconcile["fields"]
    assert fields["old_total"] == expected.OLD_TOTAL
    assert fields["new_total"] == expected.NEW_TOTAL
    assert fields["old_gap"] == 0 and fields["new_gap"] == 0
    assert reconcile["refs"]["balanced"] is True
    assert reconcile["balanced"]["all"] is True

    rows = list(csv.DictReader(io.StringIO(client.get("/api/export/catalog.csv").text)))
    assert len(rows) == expected.OLD_TOTAL + 4
    assert sum(1 for row in rows if row["verdict"] == "BREAKING") == 6


def test_quarantine_csv_export(client):
    response = client.get("/api/schemas/quarantine.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == expected.QUARANTINE_BLOCKED + expected.QUARANTINE_WARNINGS
    reasons = {row["reason"] for row in rows}
    assert {"LINE_MALFORMED", "DUPLICATE_COLUMN", "BOM_STRIPPED"} <= reasons
    assert sum(1 for row in rows if row["blocking"] == "Y") == expected.QUARANTINE_BLOCKED


def test_home_renders_field_rows(client):
    home = client.get("/")
    assert "order_code" in home.text
    assert "pay_channel" in home.text
    assert "破坏" in home.text


def test_reconciliation_lifecycle_overlap_signoff_lock(client):
    """端到端：原批 → 重叠更正 → 复核选新批 → 签收原批 → 锁定回查。"""
    day = "2026-09-15"

    # 晚到更正：同区间、不同参数，产生新批次并与原批重叠
    correction = client.post("/api/runs", json={
        "effective_from": "2026-09-10T00:00:00Z",
        "params": {"wave": "month-end-correction"},
    })
    assert correction.status_code == 200
    correction_id = correction.json()["run_id"]

    # 复核按生效日选批：重叠取最新批次，并说清缘由与被替换的原批
    recon = client.get(f"/api/reconciliation/by-day?effective_day={day}")
    assert recon.status_code == 200
    body = recon.json()
    assert body["run_id"] == correction_id
    assert body["selection"]["reason"] == "WINDOW_LATEST_WINS"
    assert len(body["selection"]["superseded"]) == 1

    # 签收原批次，锁定历史口径
    original_id = body["selection"]["superseded"][0]["run_id"]
    signoff = client.post("/api/reconciliation/signoff", json={
        "effective_day": day, "run_id": original_id, "note": "月底签收",
    })
    assert signoff.status_code == 200

    # 再查同一天：锁定到原批次
    locked = client.get(f"/api/reconciliation/by-day?effective_day={day}").json()
    assert locked["run_id"] == original_id
    assert locked["selection"]["reason"] == "WINDOW_SIGNED_OFF_LOCKED"

    # 已签收对账可按原批次回查
    signed = client.get(f"/api/reconciliation/signed/{day}?run_id={original_id}")
    assert signed.status_code == 200
    assert signed.json()["run_id"] == original_id

    # 同一份更正重跑：批次与复核记录都不新增（UPSERT 键 = 生效日 + 批次）
    runs_before = client.get("/api/batches").json()
    client.post("/api/runs", json={
        "effective_from": "2026-09-10T00:00:00Z",
        "params": {"wave": "month-end-correction"},
    })
    runs_after = client.get("/api/batches").json()
    assert len(runs_after["runs"]) == len(runs_before["runs"])
    before_rows = client.get("/api/reconciliation/overview").json()["reconciliations"]
    client.get(f"/api/reconciliation/by-day?effective_day={day}")
    after_rows = client.get("/api/reconciliation/overview").json()["reconciliations"]
    assert len(after_rows) == len(before_rows)
    # 当天两条复核记录：更正批一条、签收锁定的原批一条，各自重跑不重复
    day_rows = [row for row in after_rows if row["effective_day"] == day]
    assert {row["run_id"] for row in day_rows} == {original_id, correction_id}
    assert all(row["reused"] in (True, False) for row in day_rows)


def test_correction_diff_endpoint(client):
    original = client.get("/api/batches").json()["runs"][0]["run_id"]
    correction = client.post("/api/runs", json={
        "effective_from": "2026-09-10T00:00:00Z",
        "params": {"wave": "diff-correction"},
    }).json()["run_id"]
    resp = client.get(f"/api/reconciliation/correction/{original}/{correction}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["original_run_id"] == original
    assert body["correction_run_id"] == correction
    assert "verdict_changes" in body

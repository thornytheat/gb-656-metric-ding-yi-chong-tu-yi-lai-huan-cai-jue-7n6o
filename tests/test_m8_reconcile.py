"""M8 生效日复核：按日选批、重叠取新、签收锁定、幂等重跑、更正差异。"""

from __future__ import annotations

import pytest

from app import config, errors, store
from app.modules.m6_batches import service as batch_service
from app.modules.m8_reconcile import correction, point, service as recon_service


SEED_DAY = "2026-09-15"


def _run(client, **overrides):
    body = {
        "effective_from": "2026-09-01T00:00:00Z",
        "effective_to": None,
        "params": {"wave": "base"},
    }
    body.update(overrides)
    if body.get("effective_to") is None:
        body.pop("effective_to")
    resp = client.post("/api/runs", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 选批纯规则 ----------

def test_seed_window_covers_september_day(run):
    day = point.parse_day(SEED_DAY)
    selection = point.select_for_day(day)
    assert selection["run_id"] == run["run_id"]
    assert selection["reason"] == "WINDOW_COVERS_DAY"
    assert selection["signed_off"] is False


def test_day_before_any_window_has_no_coverage(run):
    selection = point.select_for_day(point.parse_day("2026-01-02"))
    assert selection["reason"] == "WINDOW_NO_COVERAGE"
    assert selection["run_id"] is None


def test_half_open_end_boundary_excludes_end_day(run):
    """末端开区间：effective_to 当天不再被该批次覆盖。"""
    closed = point.select_for_day(
        point.parse_day("2026-10-01"),
        runs=[_fake_run("fm-closed", 1, "2026-09-01T00:00:00+00:00",
                        "2026-10-01T00:00:00+00:00")],
    )
    assert closed["reason"] == "WINDOW_NO_COVERAGE"
    inside = point.select_for_day(
        point.parse_day("2026-09-30"),
        runs=[_fake_run("fm-closed", 1, "2026-09-01T00:00:00+00:00",
                        "2026-10-01T00:00:00+00:00")],
    )
    assert inside["run_id"] == "fm-closed"


def test_overlapping_windows_latest_seq_wins(run):
    """晚到更正与原批区间重叠：当天取版本序号最大的批次，原批进替换清单。"""
    original = _fake_run("fm-original", 2, "2026-09-01T00:00:00+00:00", None)
    corrected = _fake_run("fm-correction", 5, "2026-09-10T00:00:00+00:00", None)
    selection = point.select_for_day(point.parse_day(SEED_DAY), runs=[original, corrected])
    assert selection["run_id"] == "fm-correction"
    assert selection["reason"] == "WINDOW_LATEST_WINS"
    assert [item["run_id"] for item in selection["superseded"]] == ["fm-original"]
    assert set(selection["candidates"]) == {"fm-original", "fm-correction"}


def _fake_run(run_id, seq, start, end):
    return {
        "run_id": run_id,
        "version_seq": seq,
        "effective_from": start,
        "effective_to": end,
    }


# ---------- 复核记录与幂等 ----------

def test_reconcile_day_returns_ledger_and_reason(client):
    record = recon_service.reconcile_day(SEED_DAY)
    assert record["effective_day"] == SEED_DAY
    assert record["run_id"] == store.latest_run()["run_id"]
    assert record["selection"]["reason"] == "WINDOW_COVERS_DAY"
    assert record["reconcile"]["balanced"]["all"] is True
    assert record["reused"] is False


def test_same_reconciliation_rerun_does_not_duplicate(client):
    first = recon_service.reconcile_day(SEED_DAY)
    again = recon_service.reconcile_day(SEED_DAY)
    assert again["reconcile_id"] == first["reconcile_id"]
    assert again["reused"] is True
    assert again["created_at"] == first["created_at"]
    rows = [row for row in store.list_reconciliations()
            if row["effective_day"] == SEED_DAY]
    assert len(rows) == 1


def test_reconcile_no_coverage_raises_404(client):
    with pytest.raises(errors.NotFound) as caught:
        recon_service.reconcile_day("2026-01-02")
    assert caught.value.detail["reason"] == "WINDOW_NO_COVERAGE"


def test_preview_mode_does_not_persist(client):
    before = len(store.list_reconciliations())
    record = recon_service.reconcile_day(SEED_DAY, persist=False)
    assert record["reused"] is False
    assert len(store.list_reconciliations()) == before


# ---------- 签收锁定 ----------

def test_signoff_locks_original_batch_against_later_correction(client):
    original = store.latest_run()

    # 先来一个同区间、不同参数（即不同输入摘要）的更正批次
    _run(client, params={"wave": "correction-september"})
    corrected = store.latest_run()
    assert corrected["run_id"] != original["run_id"]

    # 签收前：裸选批规则在重叠区间上取版本序号最大的更正批
    before = point.select_for_day(point.parse_day(SEED_DAY))
    assert before["run_id"] == corrected["run_id"]
    assert before["reason"] == "WINDOW_LATEST_WINS"

    # 显式签收原批次，保留历史口径
    signed = recon_service.sign_off(SEED_DAY, original["run_id"], "月底复核签收")
    assert signed["run_id"] == original["run_id"]
    assert signed["signed_off"] is True

    # 签收后：选批与复核都锁回原批，更正批不再替换
    locked = recon_service.reconcile_day(SEED_DAY)
    assert locked["run_id"] == original["run_id"]
    assert locked["selection"]["reason"] == "WINDOW_SIGNED_OFF_LOCKED"
    assert locked["signed_off"] is True
    assert corrected["run_id"] in locked["selection"]["candidates"]


def test_signed_reconciliation_queryable_by_original_batch(client):
    original = store.latest_run()
    recon_service.sign_off(SEED_DAY, None, "锁定原批")
    _run(client, params={"wave": "correction-after-signoff"})

    fetched = recon_service.signed_reconciliation(SEED_DAY, original["run_id"])
    assert fetched["run_id"] == original["run_id"]
    assert fetched["signoff"]["note"] == "锁定原批"

    # 没签收过的批次查不到
    assert recon_service.signed_reconciliation(SEED_DAY, "fm-never-signed") is None


def test_signoff_explicit_original_batch_then_cannot_switch(client):
    original = store.latest_run()
    _run(client, params={"wave": "newer-correction"})
    corrected = store.latest_run()

    # 显式签收（较旧的）原批次，只要它的区间仍覆盖当天就允许
    record = recon_service.sign_off(SEED_DAY, original["run_id"], "保留历史口径")
    assert record["run_id"] == original["run_id"]

    # 同一天改签到另一批次必须拒绝
    with pytest.raises(errors.Conflict) as caught:
        recon_service.sign_off(SEED_DAY, corrected["run_id"], "想换批")
    assert caught.value.detail["reason"] == "SIGNOFF_CONFLICT"


def test_repeated_signoff_same_batch_updates_without_duplicate(client):
    recon_service.sign_off(SEED_DAY, None, "第一次备注")
    recon_service.sign_off(SEED_DAY, None, "第二次备注")
    assert len(store.list_signoffs()) == 1
    row = store.list_signoffs()[0]
    assert row["note"] == "第二次备注"


def test_signoff_rejects_batch_not_covering_day(client):
    run = store.latest_run()
    with pytest.raises(errors.BadInput) as caught:
        recon_service.sign_off("2026-01-02", run["run_id"], None)
    assert caught.value.detail["reason"] == "WINDOW_NO_COVERAGE"


def test_signoff_note_length_guard(client):
    from app.modules.m8_reconcile import handler

    with pytest.raises(errors.BadInput):
        handler._note({"note": "x" * 201})


# ---------- 更正批次与不重复登记 ----------

def test_rerun_same_correction_reuses_batch_and_reconciliation(client):
    _run(client, params={"wave": "correction-rerun"})
    runs_after_first = store.count_runs()
    # 完全相同的输入再跑一次：批次键命中，不新增批次
    _run(client, params={"wave": "correction-rerun"})
    assert store.count_runs() == runs_after_first

    recon_service.reconcile_day(SEED_DAY)
    recon_service.reconcile_day(SEED_DAY)
    day_rows = [row for row in store.list_reconciliations()
                if row["effective_day"] == SEED_DAY]
    assert len(day_rows) == 1


def test_correction_diff_lists_verdict_changes(client, tmp_path, monkeypatch):
    original = store.latest_run()
    # 把整组样例搬进临时目录，再改写旧版文件模拟上游更正
    for name in ("schema_v2026_06.sql", "schema_v2026_09.sql",
                 "schema_v2026_03.sql", "refs_downstream.json"):
        (tmp_path / name).write_bytes((config.SAMPLE_DIR / name).read_bytes())
    variant = tmp_path / "schema_v2026_06_variant.sql"
    variant.write_text(
        'CREATE TABLE dim_user (\n'
        '  `user_id` BIGINT NOT NULL, -- fid=3001\n'
        '  `user_name` VARCHAR(40) NOT NULL, -- fid=3002\n'
        '  `created_at` DATE NULL -- fid=3003\n'
        ');\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SAMPLE_DIR", tmp_path)
    corrected_run = batch_service.execute(
        old_file="schema_v2026_06_variant.sql",
        params={"wave": "upstream-file-updated"},
    )
    assert corrected_run["run_id"] != original["run_id"]
    assert corrected_run["version_seq"] > original["version_seq"]

    diff = recon_service.explain_correction(original["run_id"], corrected_run["run_id"])
    assert diff["original_run_id"] == original["run_id"]
    assert diff["correction_run_id"] == corrected_run["run_id"]
    assert diff["input_sha_before"] != diff["input_sha_after"]
    assert diff["changed_count"] >= 1
    assert all(item["verdict_before"] != item["verdict_after"]
               for item in diff["verdict_changes"])


def test_overlap_pairs_detect_correction_overlap(client):
    _run(client, effective_from="2026-09-10T00:00:00Z",
         params={"wave": "late-correction"})
    pairs = correction.overlap_pairs()
    assert pairs, "种子批次与更正批次区间重叠，应被识别"
    pair = pairs[0]
    assert pair["reason"] == "WINDOW_CORRECTION_REPLACES"
    assert pair["correction_run_id"] != pair["original_run_id"]


# ---------- HTTP 接口 ----------

def test_api_reconcile_by_day(client):
    resp = client.get(f"/api/reconciliation/by-day?effective_day={SEED_DAY}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["effective_day"] == SEED_DAY
    assert body["selection"]["reason"] == "WINDOW_COVERS_DAY"
    assert body["reconcile"]["balanced"]["all"] is True


def test_api_reconcile_by_day_bad_date(client):
    resp = client.get("/api/reconciliation/by-day?effective_day=not-a-date")
    assert resp.status_code == 400


def test_api_reconcile_by_day_no_coverage(client):
    resp = client.get("/api/reconciliation/by-day?effective_day=2026-01-02")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "WINDOW_NO_COVERAGE"


def test_api_signoff_and_signed_lookup_flow(client):
    created = client.post("/api/reconciliation/signoff", json={
        "effective_day": SEED_DAY, "note": "接口签收",
    })
    assert created.status_code == 200
    assert created.json()["signed_off"] is True

    lookup = client.get(f"/api/reconciliation/signed/{SEED_DAY}")
    assert lookup.status_code == 200
    assert lookup.json()["signoff"]["note"] == "接口签收"

    missing = client.get("/api/reconciliation/signed/2026-01-02")
    assert missing.status_code == 404


def test_api_select_preview_does_not_persist(client):
    before = len(store.list_reconciliations())
    resp = client.get(f"/api/reconciliation/select/{SEED_DAY}")
    assert resp.status_code == 200
    assert "explanation" in resp.json()
    assert len(store.list_reconciliations()) == before


def test_api_correction_diff_unknown_run(client):
    resp = client.get("/api/reconciliation/correction/fm-missing/fm-also-missing")
    assert resp.status_code == 404


def test_reconciliation_page_renders(client):
    resp = client.get("/reconciliation")
    assert resp.status_code == 200
    assert "生效日复核" in resp.text
    assert "/static/theme.css" in resp.text


def test_overview_lists_signoffs_and_overlaps(client):
    recon_service.sign_off(SEED_DAY, None, "总览用签收")
    overview = recon_service.overview()
    assert overview["current_version_seq"] >= 1
    assert any(item["effective_day"] == SEED_DAY for item in overview["signoffs"])


def test_parse_day_accepts_date_and_full_instant():
    from datetime import timezone

    from_date = point.parse_day("2026-09-15")
    from_instant = point.parse_day("2026-09-15T12:30:00+08:00")
    assert from_date.tzinfo == timezone.utc
    # 时刻按其 UTC 归属日期参与选批：+08:00 当天仍是 9-15
    assert from_instant.date().isoformat() == "2026-09-15"
    assert point.select_for_day(from_instant)["effective_day"] == "2026-09-15"


def test_parse_day_rejects_blank_and_malformed():
    with pytest.raises(errors.BadInput):
        point.parse_day("")
    with pytest.raises(errors.BadInput):
        point.parse_day("15/09/2026")


def test_signoff_default_targets_current_selection(client):
    """不传 run_id 时，签收选批规则当前选中的批次。"""
    record = recon_service.sign_off(SEED_DAY, None, "默认签收当前批")
    assert record["run_id"] == store.latest_run()["run_id"]
    assert record["signoff"]["note"] == "默认签收当前批"


def test_signoff_unknown_run_returns_404(client):
    with pytest.raises(errors.NotFound):
        recon_service.sign_off(SEED_DAY, "fm-does-not-exist", None)


def test_reconcile_id_deterministic():
    left = recon_service.reconcile_id_for("2026-09-15", "fm-abc")
    right = recon_service.reconcile_id_for("2026-09-15", "fm-abc")
    other_day = recon_service.reconcile_id_for("2026-09-16", "fm-abc")
    other_run = recon_service.reconcile_id_for("2026-09-15", "fm-xyz")
    assert left == right and left.startswith("rc-")
    assert left != other_day and left != other_run

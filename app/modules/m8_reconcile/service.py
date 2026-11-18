"""生效日复核编排：选批 → 对账 → 落复核记录 → 签收。

对外承诺四条口径：

1. 复核按生效日查询，重叠区间由 ``point`` 模块裁决当天采用哪批并附缘由；
2. 已签收的对账锁定原批次，永远可按原批次复查，后续更正不替换；
3. 上游文件更新后重跑批次会得到新判定，复核读的是选批结果里的新批次；
4. 同一份更正重跑命中批次键，且同一天同一批次的复核走 UPSERT，不重复登记。
"""

from __future__ import annotations

import hashlib

from app import config, errors, store
from app.modules.m6_batches import history
from app.modules.m8_reconcile import correction, point


def reconcile_id_for(day_text: str, run_id: str) -> str:
    digest = hashlib.sha256(f"{day_text}|{run_id}".encode("utf-8")).hexdigest()[:12]
    return f"rc-{digest}"


def reconcile_day(day_text: str, persist: bool = True) -> dict:
    """按生效日出一份复核结论；默认落库，纯查询场景可 ``persist=False``。"""
    day = point.parse_day(day_text)
    selection = point.select_for_day(day)
    if selection["run"] is None:
        raise errors.NotFound(
            selection["explanation"],
            detail={"reason": selection["reason"], "effective_day": selection["effective_day"]},
        )

    run = selection["run"]
    reconcile_id = reconcile_id_for(selection["effective_day"], run["run_id"])
    ledger_reconcile = history.reconcile(run)

    prior = store.find_reconciliation(selection["effective_day"], run["run_id"])
    reused = prior is not None
    record = {
        "reconcile_id": reconcile_id,
        "effective_day": selection["effective_day"],
        "run_id": run["run_id"],
        "version_seq": run["version_seq"],
        "selection": selection_view(selection),
        "reconcile": ledger_reconcile,
        "reused": reused,
        "signed_off": selection["signed_off"],
        "signoff": selection["signoff"],
        "created_at": (prior or {}).get("created_at") if reused
        else config.now_utc().isoformat(),
        "updated_at": config.now_utc().isoformat(),
    }
    if persist:
        store.save_reconciliation(record)
    return record


def selection_view(selection: dict) -> dict:
    """选批结论对外只暴露可解释字段，不带整批 payload。"""
    return {
        "effective_day": selection["effective_day"],
        "run_id": selection["run_id"],
        "version_seq": selection["version_seq"],
        "window": selection["window"],
        "reason": selection["reason"],
        "explanation": selection["explanation"],
        "signed_off": selection["signed_off"],
        "candidates": selection["candidates"],
        "superseded": selection["superseded"],
    }


def sign_off(day_text: str, run_id: str | None, note: str | None) -> dict:
    """签收某天的对账。

    * 不传 ``run_id``：签收选批规则当前为那天选中的批次；
    * 显式传 ``run_id``：允许签收原批次（保留历史口径），但若该天已签收别的
      批次则拒绝，避免一天挂两份互相矛盾的签收。
    """
    day = point.parse_day(day_text)
    day_text = day.date().isoformat()
    runs = store.all_runs()
    by_id = {run["run_id"]: run for run in runs}

    selection = point.select_for_day(day, runs)
    target_id = (run_id or selection.get("run_id") or "").strip()
    if not target_id:
        raise errors.BadInput("缺少可签收的批次",
                              detail={"reason": selection["reason"]})
    target = by_id.get(target_id)
    if target is None:
        raise errors.NotFound("批次不存在或已被清理", detail={"run_id": target_id})
    if not point.covers(target, day):
        raise errors.BadInput(
            "该批次的生效区间不覆盖签收日",
            detail={"reason": "WINDOW_NO_COVERAGE", "run_id": target_id,
                    "effective_day": day_text,
                    "window": {"from": target["effective_from"],
                               "to": target.get("effective_to")}},
        )

    existing = _day_signoff(day_text)
    if existing is not None and existing["run_id"] != target_id:
        raise errors.Conflict(
            f"{day_text} 已签收批次 {existing['run_id']}，不能改签到 {target_id}",
            detail={"reason": "SIGNOFF_CONFLICT", "effective_day": day_text,
                    "signed_run_id": existing["run_id"], "requested_run_id": target_id},
        )

    record = {
        "reconcile_id": reconcile_id_for(day_text, target_id),
        "effective_day": day_text,
        "run_id": target_id,
        "version_seq": target["version_seq"],
        "note": (note or "").strip(),
        "signed_at": config.now_utc().isoformat(),
    }
    store.save_signoff(record)
    # 签收同时确保复核记录存在，之后可直接按原批次回查
    if store.find_reconciliation(day_text, target_id) is None:
        store.save_reconciliation({
            "reconcile_id": record["reconcile_id"],
            "effective_day": day_text,
            "run_id": target_id,
            "version_seq": target["version_seq"],
            "selection": {
                "effective_day": day_text, "run_id": target_id,
                "version_seq": target["version_seq"],
                "window": {"from": target["effective_from"],
                           "to": target.get("effective_to"),
                           "semantics": config.WINDOW_SEMANTICS},
                "reason": "WINDOW_SIGNED_OFF_QUERY",
                "explanation": "签收时按指定批次登记的复核记录",
                "signed_off": True, "candidates": [target_id], "superseded": [],
            },
            "reconcile": history.reconcile(target),
            "reused": False,
            "signed_off": True,
            "signoff": {
                "reconcile_id": record["reconcile_id"], "run_id": target_id,
                "note": record["note"], "signed_at": record["signed_at"],
            },
            "created_at": record["signed_at"],
            "updated_at": record["signed_at"],
        })
    return get_reconciliation(record["reconcile_id"])


def get_reconciliation(reconcile_id: str) -> dict:
    record = store.signoff_by_id(reconcile_id)
    reconciliation = store.reconciliation_by_id(reconcile_id)
    if reconciliation is None:
        raise errors.NotFound("复核记录不存在", detail={"reconcile_id": reconcile_id})
    if record is not None:
        reconciliation["signed_off"] = True
        reconciliation["signoff"] = {
            "reconcile_id": record["reconcile_id"], "run_id": record["run_id"],
            "note": record.get("note") or "", "signed_at": record["signed_at"],
        }
    return reconciliation


def _day_signoff(day_text: str) -> dict | None:
    return store.day_signoff(day_text)


def signed_reconciliation(day_text: str, run_id: str | None = None) -> dict | None:
    """按原批次回查已签收对账；不传批次则返回该天唯一签收。"""
    if run_id:
        row = store.signoff_for(day_text, run_id)
        if row is None:
            return None
        return get_reconciliation(row["reconcile_id"])
    row = _day_signoff(day_text)
    return get_reconciliation(row["reconcile_id"]) if row else None


def explain_correction(original_run_id: str, correction_run_id: str) -> dict:
    original = store.run_by_id(original_run_id)
    corrected = store.run_by_id(correction_run_id)
    if original is None or corrected is None:
        raise errors.NotFound(
            "批次不存在",
            detail={"original_run_id": original_run_id, "correction_run_id": correction_run_id},
        )
    return correction.compare_runs(original, corrected)


def overview() -> dict:
    """复核页总览：最近复核、签收台账、重叠批次对。"""
    return {
        "reconciliations": store.list_reconciliations(),
        "signoffs": store.list_signoffs(),
        "overlaps": correction.overlap_pairs(),
        "current_version_seq": store.current_version_seq(),
    }

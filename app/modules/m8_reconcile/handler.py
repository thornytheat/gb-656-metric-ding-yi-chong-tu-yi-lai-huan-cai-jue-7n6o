"""M8 入口：生效日复核、签收、按原批次回查、更正差异。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import errors, store, web
from app.modules.m8_reconcile import point, service

router = APIRouter()

MAX_NOTE = 200


def _note(body: dict) -> str:
    note = body.get("note") or ""
    if not isinstance(note, str):
        raise errors.BadInput("note 必须是字符串", detail={"field": "note"})
    note = note.strip()
    if len(note) > MAX_NOTE:
        raise errors.BadInput(f"note 最多 {MAX_NOTE} 字", detail={"length": len(note)})
    return note


@router.get("/reconciliation", response_class=HTMLResponse)
def reconciliation_page(request: Request):
    run = store.latest_run()
    return web.page_or_404(
        request,
        "reconciliation.html",
        "reconciliation",
        run,
        overview=service.overview(),
    )


@router.get("/api/reconciliation/by-day")
def reconcile_by_day(effective_day: str, persist: bool = True):
    """按生效日取对账：说清当天采用哪批、缘由、是否被签收锁定。"""
    return service.reconcile_day(effective_day, persist=persist)


@router.get("/api/reconciliation/overview")
def reconciliation_overview():
    return service.overview()


@router.get("/api/reconciliation/select/{effective_day}")
def select_preview(effective_day: str):
    """只看选批结论与缘由，不产生复核记录。"""
    day = point.parse_day(effective_day)
    selection = point.select_for_day(day)
    return service.selection_view(selection)


@router.get("/api/reconciliation/signed/{effective_day}")
def signed_query(effective_day: str, run_id: str | None = None):
    """已签收对账回查：默认取当天签收录，可指定原批次。"""
    record = service.signed_reconciliation(effective_day, run_id)
    if record is None:
        raise errors.NotFound(
            "当天没有已签收的对账",
            detail={"effective_day": effective_day, "run_id": run_id},
        )
    return record


@router.get("/api/reconciliation/correction/{original_run_id}/{correction_run_id}")
def correction_diff(original_run_id: str, correction_run_id: str):
    return service.explain_correction(original_run_id, correction_run_id)


@router.get("/api/reconciliation/{reconcile_id}")
def reconciliation_detail(reconcile_id: str):
    return service.get_reconciliation(reconcile_id)


@router.post("/api/reconciliation/signoff")
def create_signoff(payload: dict | None = None):
    body = payload or {}
    day = body.get("effective_day")
    if not day:
        raise errors.BadInput("effective_day 不能为空", detail={"field": "effective_day"})
    run_id = str(body["run_id"]).strip() if body.get("run_id") else None
    return service.sign_off(str(day), run_id, _note(body))

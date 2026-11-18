"""生效日选批：把"某一天该采信哪一批"收敛成一个纯函数。

规则（与人工复核口径一致，全部落到原因码上）：

1. 签收优先：当天若有已签收复核，永远返回签收时那批，即使后来的更正批次
   区间也覆盖当天。已签收的对账不随后续批次变样。
2. 区间覆盖：半开区间 ``[effective_from, effective_to)`` 覆盖当天才算候选，
   ``effective_to`` 为空视为无穷远。
3. 重叠取新：多个批次区间都覆盖当天（晚到更正与原批重叠即此情形），取
   ``version_seq`` 最大的批次，并把被盖过的原批列入替换清单。
4. 没有任何批次覆盖当天 → ``WINDOW_NO_COVERAGE``，交由调用方决定 404 还是空态。
"""

from __future__ import annotations

from datetime import datetime

from app import config, errors, store
from app.modules.m6_batches import window as window_mod


def parse_day(value: str, field: str = "effective_day") -> datetime:
    """接受 ``YYYY-MM-DD`` 或完整时刻；日期按 UTC 零点解释，返回 UTC 时刻。"""
    raw = (value or "").strip()
    if not raw:
        raise errors.BadInput("生效日不能为空", detail={"field": field})
    if len(raw) == 10:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=config.UTC)
        except ValueError:
            raise errors.BadInput("生效日格式无法识别，应为 YYYY-MM-DD",
                                  detail={"field": field, "value": raw})
    return window_mod.parse_instant(raw, field)


def covers(run: dict, day: datetime) -> bool:
    """批次的半开生效区间是否覆盖给定时刻所在的生效日。"""
    start = window_mod.parse_instant(run["effective_from"], "effective_from")
    if day < start:
        return False
    if run.get("effective_to"):
        end = window_mod.parse_instant(run["effective_to"], "effective_to")
        if day >= end:
            return False
    return True


def _window_text(run: dict) -> str:
    return window_mod.describe({
        "from": run["effective_from"],
        "to": run.get("effective_to"),
    })


def select_for_day(day: datetime, runs: list[dict] | None = None) -> dict:
    """返回选批结论；``runs`` 可注入以便单测，默认读库。"""
    runs = store.all_runs() if runs is None else runs
    day_text = day.date().isoformat()

    signed = _signed_run(day_text, runs)
    if signed is not None:
        run, signoff_row = signed
        return {
            "effective_day": day_text,
            "run_id": run["run_id"],
            "version_seq": run["version_seq"],
            "window": {"from": run["effective_from"], "to": run.get("effective_to"),
                       "semantics": config.WINDOW_SEMANTICS},
            "reason": "WINDOW_SIGNED_OFF_LOCKED",
            "explanation": (
                f"当天已在 {signoff_row['signed_at']} 签收批次 {run['run_id']}，"
                "签收结果锁定，后续更正批次不替换"
            ),
            "signed_off": True,
            "signoff": _signoff_brief(signoff_row),
            "candidates": [r["run_id"] for r in runs if covers(r, day)],
            "superseded": [],
            "run": run,
        }

    candidates = [run for run in runs if covers(run, day)]
    if not candidates:
        return {
            "effective_day": day_text,
            "run_id": None,
            "version_seq": None,
            "window": None,
            "reason": "WINDOW_NO_COVERAGE",
            "explanation": f"{day_text} 没有落在任何批次的生效区间内",
            "signed_off": False,
            "signoff": None,
            "candidates": [],
            "superseded": [],
            "run": None,
        }

    chosen = max(candidates, key=lambda item: int(item["version_seq"]))
    older = [item for item in candidates if item["run_id"] != chosen["run_id"]]
    if older:
        reason = "WINDOW_LATEST_WINS"
        explanation = (
            f"{len(candidates)} 个批次的生效区间同时覆盖 {day_text}，"
            f"按版本序号取最新批次 {chosen['run_id']}（序号 {chosen['version_seq']}）；"
            f"被替换的原批：{'、'.join(item['run_id'] for item in older)}"
        )
    else:
        reason = "WINDOW_COVERS_DAY"
        explanation = (
            f"批次 {chosen['run_id']} 的生效区间 {_window_text(chosen)} "
            f"覆盖 {day_text}，且无重叠批次"
        )
    return {
        "effective_day": day_text,
        "run_id": chosen["run_id"],
        "version_seq": chosen["version_seq"],
        "window": {"from": chosen["effective_from"], "to": chosen.get("effective_to"),
                   "semantics": config.WINDOW_SEMANTICS},
        "reason": reason,
        "explanation": explanation,
        "signed_off": False,
        "signoff": None,
        "candidates": [item["run_id"] for item in candidates],
        "superseded": [
            {"run_id": item["run_id"], "version_seq": item["version_seq"],
             "window": _window_text(item)}
            for item in older
        ],
        "run": chosen,
    }


def _signed_run(day_text: str, runs: list[dict]) -> tuple[dict, dict] | None:
    """当天签收记录对应的批次仍在库中才认；批次被物理清理则签收失效。"""
    row = store.day_signoff(day_text)
    if row is None:
        return None
    by_id = {run["run_id"]: run for run in runs}
    run = by_id.get(row["run_id"])
    return (run, row) if run is not None else None


def _signoff_brief(signoff_row: dict) -> dict:
    return {
        "reconcile_id": signoff_row["reconcile_id"],
        "run_id": signoff_row["run_id"],
        "note": signoff_row.get("note") or "",
        "signed_at": signoff_row["signed_at"],
    }

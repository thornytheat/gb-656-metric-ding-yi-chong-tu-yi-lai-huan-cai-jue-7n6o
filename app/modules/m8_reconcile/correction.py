"""更正识别：上游文件更新后，新旧批次的判定差异要逐条说清。

批次键由输入摘要 + 生效区间 + 参数 + 规则集推导，因此：

* 同一份更正（文件内容没变）重跑，批次键不变，命中已有批次，不会新增批次；
* 文件内容一旦变化，摘要变化 → 新批次键 → 新 ``version_seq`` 的更正批次；
* 更正批次与原批的生效区间重叠时，由 ``point`` 模块在选批阶段裁决替换关系。

本模块只做只读比对，不重算判定；重算入口仍是 ``m6_batches.service.execute``。
"""

from __future__ import annotations

from app import store


VERDICT_ORDER = ("COMPATIBLE", "COMPATIBLE_BY_MAPPING", "BREAKING")


def _verdict_index(run: dict) -> dict[str, dict]:
    return {str(row["field_id"]): row for row in (run.get("diff") or [])}


def compare_runs(original: dict, correction: dict) -> dict:
    """逐字段比对两批判定，给出兼容判定变化与原因码级别的差异。"""
    old_rows = _verdict_index(original)
    new_rows = _verdict_index(correction)
    changed: list[dict] = []
    for field_id in sorted(set(old_rows) | set(new_rows)):
        before = old_rows.get(field_id)
        after = new_rows.get(field_id)
        before_verdict = (before or {}).get("verdict")
        after_verdict = (after or {}).get("verdict")
        if before_verdict == after_verdict:
            continue
        changed.append({
            "field_id": field_id,
            "table": (after or before).get("table"),
            "old_name": (before or {}).get("old_name"),
            "new_name": (after or {}).get("new_name"),
            "verdict_before": before_verdict,
            "verdict_after": after_verdict,
            "bucket_before": (before or {}).get("bucket"),
            "bucket_after": (after or {}).get("bucket"),
            "reason_before": (before or {}).get("primary_reason"),
            "reason_after": (after or {}).get("primary_reason"),
        })

    before_counts = (original.get("ledger") or {}).get("counts") or {}
    after_counts = (correction.get("ledger") or {}).get("counts") or {}
    return {
        "original_run_id": original["run_id"],
        "correction_run_id": correction["run_id"],
        "original_version_seq": original["version_seq"],
        "correction_version_seq": correction["version_seq"],
        "input_sha_before": original.get("input_sha256"),
        "input_sha_after": correction.get("input_sha256"),
        "verdict_changes": changed,
        "changed_count": len(changed),
        "counts_before": {key: before_counts.get(key, 0) for key in VERDICT_ORDER
                          if key in before_counts or key in after_counts},
        "counts_after": {key: after_counts.get(key, 0) for key in VERDICT_ORDER
                         if key in before_counts or key in after_counts},
        "breaking_before": int(before_counts.get("breaking", 0)),
        "breaking_after": int(after_counts.get("breaking", 0)),
    }


def overlap_pairs(runs: list[dict] | None = None) -> list[dict]:
    """列出区间互相重叠的批次对，供复核页提示更正与原批的重叠关系。"""
    from app.modules.m6_batches import window as window_mod

    runs = store.all_runs() if runs is None else runs
    pairs: list[dict] = []
    for i, left in enumerate(runs):
        left_window = {"from": left["effective_from"], "to": left.get("effective_to")}
        for right in runs[i + 1:]:
            right_window = {"from": right["effective_from"], "to": right.get("effective_to")}
            if window_mod.overlaps(left_window, right_window):
                newer, older = sorted(
                    (left, right), key=lambda item: int(item["version_seq"]), reverse=True
                )
                pairs.append({
                    "original_run_id": older["run_id"],
                    "correction_run_id": newer["run_id"],
                    "original_window": left_window if left is older else right_window,
                    "correction_window": right_window if right is newer else left_window,
                    "reason": "WINDOW_CORRECTION_REPLACES",
                })
    return pairs

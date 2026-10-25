"""Record lab report JSON as research evidence, compacted so the ledger stays small and agents
are not handed equity curves or trade lists they could overfit to by eye."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lab.ledger import Ledger
from lab.report import to_jsonable

MAX_LIST_ITEMS = 50  # longer lists (equity points, trades, null paths) become {"n": len}


def compact_report(value: Any) -> Any:
    """``value`` as JSON-safe data with every list longer than :data:`MAX_LIST_ITEMS`
    replaced by ``{"n": <length>}`` (recursively). Scalars and short lists are kept."""
    value = to_jsonable(value)
    if isinstance(value, Mapping):
        return {str(k): compact_report(v) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            return {"n": len(value)}
        return [compact_report(v) for v in value]
    return value


def record_report(
    ledger: Ledger, run_id: str, plan_hash: str, kind: str, report: Mapping[str, Any]
) -> int:
    """Store :func:`compact_report` of ``report`` as evidence ``kind`` for ``plan_hash``."""
    payload = json.dumps(compact_report(report), sort_keys=True, allow_nan=False)
    return ledger.record_evidence(run_id, plan_hash, kind, payload)

"""
Admin *Overview* tab: the anomaly findings (list, acknowledge, resolve) and
the usage exports.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session, select

from app.core.database import get_session
from app.models.schema import AnomalyEvent
from app.routers.admin.common import router
from app.services.analytics import build_monthly_report, iter_months, parse_ym, user_backend_costs
from app.services.usage_export import build_workbook


# ── Anomaly findings (populated by scripts/scan_anomalies.py) ──

@router.get("/api/anomalies")
def list_anomalies(
    status: str = "new",
    limit: int = 50,
    session: Session = Depends(get_session),
):
    """List anomaly findings. `status` = new | acknowledged | resolved | all."""
    stmt = select(AnomalyEvent).order_by(AnomalyEvent.created_at.desc())  # type: ignore[attr-defined]
    if status != "all":
        stmt = stmt.where(AnomalyEvent.status == status)
    rows = session.exec(stmt.limit(max(1, min(limit, 200)))).all()
    return JSONResponse([
        {
            "id": e.id, "scope": e.scope, "rule": e.rule, "severity": e.severity,
            "user_id": e.user_id, "model": e.model,
            "window_start": e.window_start.isoformat(), "window_end": e.window_end.isoformat(),
            "observed": e.observed, "baseline": e.baseline, "ratio": e.ratio,
            "detail": e.detail, "status": e.status,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ])


def _set_anomaly_status(session: Session, event_id: int, status: str) -> JSONResponse:
    ev = session.get(AnomalyEvent, event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Anomaly event not found.")
    ev.status = status
    ev.updated_at = datetime.now(timezone.utc)
    session.add(ev)
    session.commit()
    return JSONResponse({"ok": True, "id": event_id, "status": status})


@router.post("/api/anomalies/{event_id}/ack")
def ack_anomaly(event_id: int, session: Session = Depends(get_session)):
    return _set_anomaly_status(session, event_id, "acknowledged")


@router.post("/api/anomalies/{event_id}/resolve")
def resolve_anomaly(event_id: int, session: Session = Depends(get_session)):
    return _set_anomaly_status(session, event_id, "resolved")


# ── Usage Export ──

_MAX_EXPORT_MONTHS = 24


@router.get("/api/export/usage.xlsx")
def export_usage_report(
    session: Session = Depends(get_session),
    from_: str = Query(..., alias="from", description="Start month YYYY-MM (inclusive)"),
    to: str = Query(..., description="End month YYYY-MM (inclusive)"),
):
    """Export a 4-sheet xlsx usage report for the given closed month range."""
    try:
        parse_ym(from_)
        parse_ym(to)
        months = iter_months(from_, to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if len(months) > _MAX_EXPORT_MONTHS:
        raise HTTPException(
            status_code=400,
            detail=f"Range too wide: {len(months)} months (max {_MAX_EXPORT_MONTHS}).",
        )

    report = build_monthly_report(session, from_, to)
    content = build_workbook(report)
    filename = f"usage_{from_}_to_{to}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/export/user-backend-costs.csv")
def export_user_backend_costs_csv(
    session: Session = Depends(get_session),
    from_: str = Query(..., alias="from", description="Start month YYYY-MM (inclusive)"),
    to: str = Query(..., description="End month YYYY-MM (inclusive)"),
):
    """Export per-account cost split by serving backend as CSV.

    One row per (month × account) with separate On-prem (vLLM) / Azure /
    AWS Bedrock cost columns — the flat-file counterpart of the xlsx
    "Cost by Backend" sheet, for feeding billing/chargeback systems.
    Includes both human users and service accounts (see the `type` column).
    """
    import csv
    import io

    try:
        parse_ym(from_)
        parse_ym(to)
        months = iter_months(from_, to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if len(months) > _MAX_EXPORT_MONTHS:
        raise HTTPException(
            status_code=400,
            detail=f"Range too wide: {len(months)} months (max {_MAX_EXPORT_MONTHS}).",
        )

    months_rows = user_backend_costs(session, from_, to)

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM so Excel opens CJK correctly
    writer = csv.writer(buf)
    writer.writerow([
        "month", "username", "display_name", "org_code", "type",
        "requests", "input_tokens", "output_tokens",
        "vllm_cost_usd", "azure_cost_usd", "bedrock_cost_usd", "total_cost_usd",
    ])
    for month, rows in months_rows:
        for u in rows:
            writer.writerow([
                month,
                u["username"],
                u["display_name"],
                u["org_code"],
                "service_account" if u["is_service_account"] else "user",
                u["requests"],
                u["input_tokens"],
                u["output_tokens"],
                f"{u['vllm_cost_usd']:.6f}",
                f"{u['azure_cost_usd']:.6f}",
                f"{u['bedrock_cost_usd']:.6f}",
                f"{u['total_cost_usd']:.6f}",
            ])

    filename = f"user_backend_costs_{from_}_to_{to}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

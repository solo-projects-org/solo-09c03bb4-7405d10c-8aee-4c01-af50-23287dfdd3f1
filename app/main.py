"""FastAPI web surface for the fault-code classification service."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import domain
from .db import Store

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("FAULT_DB", str(BASE_DIR.parent / "data" / "ops.db"))

store = Store(DB_PATH)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    domain.purge_stale_current(store)
    domain.purge_stale_qc_current(store)
    yield


app = FastAPI(title="风电机组故障码类别发布台", lifespan=lifespan)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def render(request: Request, name: str, **ctx):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, name, ctx)


def flash_redirect(url: str, message: str, ok: bool = True) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(
        f"{url}{sep}msg={quote(message)}&ok={'1' if ok else '0'}",
        status_code=303,
    )


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@app.get("/")
def dashboard(request: Request, msg: str | None = None, ok: str = "1"):
    with store.read() as conn:
        days = []
        for unit, day in domain.days_with_events(conn):
            days.append(domain.day_view(conn, unit, day))
        current = domain.available_days(conn)
        recent = domain.event_history(conn)[-8:][::-1]
    return render(
        request,
        "index.html",
        days=days,
        current=current,
        recent=recent,
        msg=msg,
        ok=ok == "1",
    )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

@app.get("/events")
def events_page(request: Request, msg: str | None = None, ok: str = "1"):
    with store.read() as conn:
        history = domain.event_history(conn)[::-1]
    return render(request, "events.html", history=history, msg=msg, ok=ok == "1")


@app.post("/events")
def events_create(
    request: Request,
    event_no: str = Form(...),
    revision: int = Form(...),
    unit: str = Form(...),
    firmware_version: str = Form(...),
    fault_code: str = Form(...),
    occurred_at: str = Form(...),
):
    try:
        ev = domain.normalise_event_input(
            event_no, revision, unit, firmware_version, fault_code, occurred_at
        )
        domain.add_event_revision(store, ev)
    except domain.DomainError as exc:
        with store.read() as conn:
            history = domain.event_history(conn)[::-1]
        return render(
            request,
            "events.html",
            history=history,
            msg=str(exc),
            ok=False,
            form=dict(
                event_no=event_no,
                revision="",
                unit=unit,
                firmware_version=firmware_version,
                fault_code=fault_code,
                occurred_at=occurred_at,
            ),
        )
    return flash_redirect(
        "/events",
        f"已登记事件 {ev.event_no} 修订 {ev.revision}；相关日档现行资格已撤销",
    )


# --------------------------------------------------------------------------- #
# Mappings
# --------------------------------------------------------------------------- #

@app.get("/mappings")
def mappings_page(request: Request, msg: str | None = None, ok: str = "1"):
    with store.read() as conn:
        history = domain.mapping_history(conn)
    return render(
        request,
        "mappings.html",
        history=history,
        msg=msg,
        ok=ok == "1",
    )


@app.post("/mappings")
def mappings_create(
    request: Request,
    firmware_version: str = Form(...),
    fault_code: str = Form(...),
    revision: int = Form(...),
    category: str = Form(...),
):
    try:
        domain.add_mapping_revision(
            store, firmware_version, fault_code, revision, category
        )
    except domain.DomainError as exc:
        with store.read() as conn:
            history = domain.mapping_history(conn)
        return render(
            request,
            "mappings.html",
            history=history,
            msg=str(exc),
            ok=False,
            form=dict(
                firmware_version=firmware_version,
                fault_code=fault_code,
                revision="",
                category=category,
            ),
        )
    return flash_redirect(
        "/mappings",
        f"已登记映射 {firmware_version}/{fault_code} 修订 {revision} → {category}；"
        "受影响日档现行资格已撤销",
    )


# --------------------------------------------------------------------------- #
# Daily files
# --------------------------------------------------------------------------- #

@app.get("/days/{unit}/{day}")
def day_page(request: Request, unit: str, day: str, msg: str | None = None, ok: str = "1"):
    with store.snapshot() as conn:
        view = domain.day_view(conn, unit, day)
        items = domain.publish_items(conn, view.publish["id"]) if view.publish else []
    return render(
        request,
        "day.html",
        view=view,
        items=items,
        revision_set=view.revision_token,
        recipe=view.recipe,
        msg=msg,
        ok=ok == "1",
    )


@app.post("/days/{unit}/{day}/publish")
def day_publish(
    request: Request,
    unit: str,
    day: str,
    revision_set: str = Form(...),
    recipe: str = Form(...),
):
    try:
        domain.publish_day(store, unit, day, revision_set, recipe)
    except domain.DomainError as exc:
        return flash_redirect(f"/days/{unit}/{day}", str(exc), ok=False)
    return flash_redirect(f"/days/{unit}/{day}", "已发布完整日档，现行资格生效")


@app.post("/days/{unit}/{day}/revoke")
def day_revoke(request: Request, unit: str, day: str):
    n = domain.revoke_day(store, unit, day, "操作员手动撤销")
    msg = "已撤销当前日档的现行资格" if n else "该日没有现行日档"
    return flash_redirect(f"/days/{unit}/{day}", msg, ok=bool(n))


# --------------------------------------------------------------------------- #
# Read API for the analysis group
# --------------------------------------------------------------------------- #

@app.get("/api/days")
def api_days():
    with store.read() as conn:
        return {"days": domain.available_days(conn)}


@app.get("/api/days/{unit}/{day}")
def api_day(unit: str, day: str):
    with store.snapshot() as conn:
        view = domain.day_view(conn, unit, day)
        if view.publish is None:
            reasons = []
            if view.missing:
                reasons.append("缺少类别映射")
            if not view.events:
                reasons.append("该日无事件")
            if view.revoked and not reasons:
                reasons.append("现行资格已撤销，等待重算发布")
            return JSONResponse(
                status_code=409,
                content={
                    "unit": unit,
                    "day": day,
                    "available": False,
                    "reasons": reasons or ["该日尚未发布"],
                    "missing_mappings": [
                        {
                            "event_no": e["event_no"],
                            "revision": e["revision"],
                            "firmware_version": e["firmware_version"],
                            "fault_code": e["fault_code"],
                        }
                        for e in view.missing
                    ],
                },
            )
        items = [
            {
                "event_no": str(i["event_no"]),
                "revision": i["revision"],
                "unit": i["unit"],
                "fault_code": i["fault_code"],
                "category": i["category"],
                "occurred_at": i["occurred_at"],
                "mapping_revision": i["map_revision"],
            }
            for i in domain.publish_items(conn, view.publish["id"])
        ]
        return {
            "unit": unit,
            "day": day,
            "available": True,
            "publish_id": view.publish["id"],
            "published_at": view.publish["created_at"],
            "event_revisions": view.publish["event_revisions"],
            "recipe": view.publish["recipe"],
            "events": items,
        }


@app.get("/api/days/{unit}/{day}/export.csv")
def api_day_csv(unit: str, day: str):
    """Same content as the JSON endpoint, as a CSV download for downstream jobs."""
    import csv
    import io

    from fastapi.responses import Response

    with store.snapshot() as conn:
        view = domain.day_view(conn, unit, day)
        if view.publish is None:
            reasons = []
            if view.missing:
                reasons.append("缺少类别映射")
            if view.revoked:
                reasons.append("现行资格已撤销，等待重算发布")
            return JSONResponse(
                status_code=409,
                content={"unit": unit, "day": day, "available": False,
                         "reasons": reasons or ["该日没有现行日档"]},
            )
        items = domain.publish_items(conn, view.publish["id"])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["unit", "utc_day", "event_no", "revision", "firmware_version",
             "fault_code", "category", "occurred_at", "mapping_revision",
             "publish_id", "recipe"]
        )
        for i in items:
            writer.writerow(
                [unit, day, i["event_no"], i["revision"], i["firmware_version"],
                 i["fault_code"], i["category"], i["occurred_at"],
                 i["map_revision"], view.publish["id"], view.publish["recipe"]]
            )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{unit}_{day}.csv"'
        },
    )


@app.get("/healthz")
def healthz():
    with store.read() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Adjacent-day quality check
# --------------------------------------------------------------------------- #

@app.get("/qc")
def qc_page(
    request: Request,
    unit: str | None = None,
    day: str | None = None,
    msg: str | None = None,
    ok: str = "1",
):
    view = None
    error = None
    with store.snapshot() as conn:
        units = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT unit FROM events ORDER BY unit"
            ).fetchall()
        ]
        if unit and day:
            try:
                view = domain.qc_view(conn, unit, day)
            except domain.DomainError as exc:
                error = str(exc)
    return render(
        request,
        "qc.html",
        view=view,
        units=units,
        unit=unit or "",
        day=day or "",
        error=error,
        msg=msg,
        ok=ok == "1",
    )


@app.post("/qc/{unit}/{start_day}/recompute")
def qc_recompute(
    request: Request,
    unit: str,
    start_day: str,
    source_token: str = Form(""),
):
    try:
        domain.qc_recompute(store, unit, start_day, source_token)
    except domain.DomainError as exc:
        return flash_redirect(
            f"/qc?unit={quote(unit)}&day={quote(start_day)}", str(exc), ok=False
        )
    return flash_redirect(
        f"/qc?unit={quote(unit)}&day={quote(start_day)}",
        "已按当前两日现行日档重算质检结论",
    )


@app.get("/qc/reports/{report_id}")
def qc_report_page(request: Request, report_id: int):
    """Read-only review of a past conclusion, including revoked ones."""
    with store.snapshot() as conn:
        report = domain.qc_report(conn, report_id)
        if report is None:
            return render(
                request,
                "qc_report.html",
                report=None,
                pairs=[],
                current=False,
                error=f"质检结论 #{report_id} 不存在",
                msg=None,
                ok=True,
            )
        pairs = domain.qc_pairs(conn, report_id)
        live = domain.qc_current_report(conn, report["unit"], report["start_day"])
        current = live is not None and live["id"] == report_id
    return render(
        request,
        "qc_report.html",
        report=report,
        pairs=pairs,
        current=current,
        error=None,
        msg=None,
        ok=True,
    )


@app.get("/api/qc/{unit}/{start_day}")
def api_qc(unit: str, start_day: str):
    with store.snapshot() as conn:
        try:
            view = domain.qc_view(conn, unit, start_day)
        except domain.DomainError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if view.report is None:
            return JSONResponse(
                status_code=409,
                content={
                    "unit": unit,
                    "start_day": start_day,
                    "day_a": view.day_a,
                    "day_b": view.day_b,
                    "available": False,
                    "reasons": view.reasons or ["尚未生成质检结论"],
                },
            )
        pairs = [
            {
                "firmware_version": p["firmware_version"],
                "fault_code": p["fault_code"],
                "early": {
                    "event_no": p["early_event_no"],
                    "revision": p["early_revision"],
                    "occurred_at": p["early_at"],
                    "publish_id": p["source_publish_early"],
                },
                "late": {
                    "event_no": p["late_event_no"],
                    "revision": p["late_revision"],
                    "occurred_at": p["late_at"],
                    "publish_id": p["source_publish_late"],
                },
                "delta_seconds": p["delta_us"] / 1_000_000,
            }
            for p in view.pairs
        ]
        return {
            "unit": unit,
            "start_day": start_day,
            "day_a": view.day_a,
            "day_b": view.day_b,
            "available": True,
            "report_id": view.report["id"],
            "created_at": view.report["created_at"],
            "source_token": view.report["source_token"],
            "publish_a_id": view.report["publish_a_id"],
            "publish_b_id": view.report["publish_b_id"],
            "pair_count": view.report["pair_count"],
            "pairs": pairs,
        }


@app.get("/status")
def status():
    """Operator-facing sanity: what is current, and what is waiting for recompute."""
    with store.read() as conn:
        current = domain.available_days(conn)
        waiting = []
        for unit, day in domain.days_with_events(conn):
            view = domain.day_view(conn, unit, day)
            if view.publish is None:
                waiting.append(
                    {
                        "unit": unit,
                        "day": day,
                        "missing_mappings": len(view.missing),
                        "revoked_publishes": len(view.revoked),
                    }
                )
        recent_log = [
            dict(r)
            for r in conn.execute(
                "SELECT at, action, detail FROM operation_log ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
    return {
        "current": [(d["unit"], d["day"], d["event_count"]) for d in current],
        "waiting_for_recompute": waiting,
        "recent_operations": recent_log,
    }

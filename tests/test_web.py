from __future__ import annotations

import os
import tempfile
from urllib.parse import unquote

import pytest

# The web module binds its Store at import time, so point it at a throwaway
# database before anything imports app.main.
_TMP = tempfile.mkdtemp(prefix="fault-ops-tests-")
os.environ["FAULT_DB"] = os.path.join(_TMP, "web.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture()
def client():
    main.store.init()
    with TestClient(main.app) as c:
        yield c
    with main.store.write() as conn:
        for table in ("qc_pairs", "qc_reports", "publish_items", "publishes",
                      "mappings", "events", "operation_log"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")


def revision_form(client, unit, day, **overrides):
    page = client.get(f"/days/{unit}/{day}").text
    data = {
        "revision_set": _hidden(page, "revision_set"),
        "recipe": _hidden(page, "recipe"),
    }
    data.update(overrides)
    return data


def _hidden(page, name):
    import re

    m = re.search(rf'name="{name}" value="([^"]*)"', page)
    return m.group(1) if m else ""


def post_event(client, **kw):
    data = dict(
        event_no="EVT-1",
        revision=1,
        unit="WTG-01",
        firmware_version="FW-1.4",
        fault_code="E-1042",
        occurred_at="2026-03-01T10:00:00Z",
    )
    data.update(kw)
    return client.post("/events", data=data, follow_redirects=False)


def test_pages_render(client):
    for path in ("/", "/events", "/mappings", "/api/days", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_event_create_via_form_and_validation_error_shown(client):
    r = post_event(client, revision=2)
    assert r.status_code == 200
    assert "修订号必须为 1" in r.text

    r = post_event(client)
    assert r.status_code == 303
    assert "已登记事件" in unquote(r.headers["location"])

    r = post_event(client, revision=3)
    assert r.status_code == 200
    assert "修订号必须为 2" in r.text

    page = client.get("/events").text
    assert "EVT-1" in page


def test_publish_flow_over_http(client):
    post_event(client)
    r = client.post(
        "/mappings",
        data={
            "firmware_version": "FW-1.4",
            "fault_code": "E-1042",
            "revision": 1,
            "category": "变桨系统",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    page = client.get("/days/WTG-01/2026-03-01").text
    assert "按现行修订重算并发布" in page

    form = revision_form(client, "WTG-01", "2026-03-01")
    assert form["revision_set"] == "EVT-1@1"
    r = client.post(
        "/days/WTG-01/2026-03-01/publish", data=form, follow_redirects=False
    )
    assert r.status_code == 303
    assert "ok=1" in r.headers["location"]

    api = client.get("/api/days/WTG-01/2026-03-01")
    assert api.status_code == 200
    body = api.json()
    assert body["available"] is True
    assert body["events"][0]["category"] == "变桨系统"

    # Correct the event: the day immediately becomes unavailable.
    post_event(client, revision=2, occurred_at="2026-03-01T12:00:00Z")
    api = client.get("/api/days/WTG-01/2026-03-01")
    assert api.status_code == 409
    assert api.json()["available"] is False

    # Stale token from before the correction is rejected; the page offers fresh one.
    r = client.post(
        "/days/WTG-01/2026-03-01/publish",
        data={"revision_set": "EVT-1@1", "recipe": form["recipe"]},
        follow_redirects=False,
    )
    assert "ok=0" in r.headers["location"]
    assert client.get("/api/days/WTG-01/2026-03-01").status_code == 409

    r = client.post(
        "/days/WTG-01/2026-03-01/publish",
        data=revision_form(client, "WTG-01", "2026-03-01"),
        follow_redirects=False,
    )
    assert "ok=1" in r.headers["location"]
    assert client.get("/api/days/WTG-01/2026-03-01").json()["event_revisions"] == "EVT-1@2"


def test_missing_mapping_blocks_publish_and_is_listed(client):
    post_event(client)
    r = client.post(
        "/days/WTG-01/2026-03-01/publish",
        data=revision_form(client, "WTG-01", "2026-03-01"),
        follow_redirects=False,
    )
    assert "ok=0" in r.headers["location"]
    assert "缺少类别映射" in unquote(r.headers["location"])

    page = client.get("/days/WTG-01/2026-03-01").text
    assert "缺少映射的事件" in page
    assert "EVT-1" in page
    assert client.get("/api/days").json() == {"days": []}


def test_api_unavailable_reasons(client):
    post_event(client)
    body = client.get("/api/days/WTG-01/2026-03-01").json()
    assert body["available"] is False
    assert "缺少类别映射" in body["reasons"]
    assert body["missing_mappings"][0]["fault_code"] == "E-1042"


# --------------------------------------------------------------------------- #
# Adjacent-day quality check over HTTP
# --------------------------------------------------------------------------- #

def add_mapping(client, fw="FW-1.4", code="E-1042", revision=1, category="变桨系统"):
    return client.post(
        "/mappings",
        data={
            "firmware_version": fw,
            "fault_code": code,
            "revision": revision,
            "category": category,
        },
        follow_redirects=False,
    )


def publish_day(client, unit, day):
    form = revision_form(client, unit, day)
    r = client.post(f"/days/{unit}/{day}/publish", data=form, follow_redirects=False)
    assert "ok=1" in r.headers["location"], r.headers["location"]


def qc_page(client, unit, day):
    return client.get(f"/qc?unit={unit}&day={day}")


def qc_token(client, unit, day):
    return _hidden(qc_page(client, unit, day).text, "source_token")


def test_qc_page_renders(client):
    r = client.get("/qc")
    assert r.status_code == 200
    assert "相邻两日质检" in r.text


def test_qc_flow_over_http_including_cross_midnight(client):
    add_mapping(client)
    post_event(client, event_no="EVT-1", occurred_at="2026-03-01T23:59:40Z")
    post_event(client, event_no="EVT-2", occurred_at="2026-03-02T00:00:20Z")
    publish_day(client, "WTG-01", "2026-03-01")
    publish_day(client, "WTG-01", "2026-03-02")

    page = qc_page(client, "WTG-01", "2026-03-01").text
    assert "尚未生成质检结论" in page
    assert "2026-03-01 仅" not in page

    token = qc_token(client, "WTG-01", "2026-03-01")
    assert token
    r = client.post(
        "/qc/WTG-01/2026-03-01/recompute",
        data={"source_token": token},
        follow_redirects=False,
    )
    assert "ok=1" in r.headers["location"]

    page = qc_page(client, "WTG-01", "2026-03-01").text
    assert "EVT-1" in page and "EVT-2" in page
    assert "40.000 秒" in page

    api = client.get("/api/qc/WTG-01/2026-03-01")
    assert api.status_code == 200
    body = api.json()
    assert body["available"] is True
    assert body["pair_count"] == 1
    pair = body["pairs"][0]
    assert (pair["early"]["event_no"], pair["late"]["event_no"]) == ("EVT-1", "EVT-2")
    assert pair["delta_seconds"] == 40.0
    assert pair["early"]["publish_id"] != pair["late"]["publish_id"]

    # Correcting an event on the second day revokes that publish and with it the
    # conclusion; the API must not serve the old "all clear".
    post_event(
        client,
        event_no="EVT-2",
        revision=2,
        occurred_at="2026-03-02T00:00:20Z",
    )
    assert client.get("/api/qc/WTG-01/2026-03-01").status_code == 409
    page = qc_page(client, "WTG-01", "2026-03-01").text
    assert "已失去现行资格" in page or "现行资格已撤销" in page


def test_qc_no_verdict_when_a_day_is_missing(client):
    add_mapping(client)
    post_event(client, event_no="EVT-1", occurred_at="2026-03-01T23:59:40Z")
    post_event(client, event_no="EVT-2", occurred_at="2026-03-02T00:00:20Z")
    publish_day(client, "WTG-01", "2026-03-01")

    page = qc_page(client, "WTG-01", "2026-03-01")
    assert "无法给出「无疑点」结论" in page.text
    assert qc_token(client, "WTG-01", "2026-03-01") == ""
    body = client.get("/api/qc/WTG-01/2026-03-01").json()
    assert body["available"] is False
    assert any("2026-03-02" in r for r in body["reasons"])

    r = client.post(
        "/qc/WTG-01/2026-03-01/recompute",
        data={"source_token": ""},
        follow_redirects=False,
    )
    assert "ok=0" in r.headers["location"]
    assert client.get("/api/qc/WTG-01/2026-03-01").status_code == 409


def test_qc_stale_token_rejected_over_http(client):
    add_mapping(client)
    post_event(client, event_no="EVT-1", occurred_at="2026-03-01T23:59:40Z")
    post_event(client, event_no="EVT-2", occurred_at="2026-03-02T00:00:20Z")
    publish_day(client, "WTG-01", "2026-03-01")
    publish_day(client, "WTG-01", "2026-03-02")
    token = qc_token(client, "WTG-01", "2026-03-01")

    # Republish one day: the token now refers to a superseded publish id.
    publish_day(client, "WTG-01", "2026-03-02")
    r = client.post(
        "/qc/WTG-01/2026-03-01/recompute",
        data={"source_token": token},
        follow_redirects=False,
    )
    assert "ok=0" in r.headers["location"]
    assert client.get("/api/qc/WTG-01/2026-03-01").status_code == 409

    r = client.post(
        "/qc/WTG-01/2026-03-01/recompute",
        data={"source_token": qc_token(client, "WTG-01", "2026-03-01")},
        follow_redirects=False,
    )
    assert "ok=1" in r.headers["location"]


def test_qc_bad_day_shows_error(client):
    r = client.get("/qc?unit=WTG-01&day=2026-02-30")
    assert r.status_code == 200
    assert "不是有效日期" in r.text


def test_qc_revoked_conclusion_remains_reviewable(client):
    add_mapping(client)
    post_event(client, event_no="EVT-1", occurred_at="2026-03-01T23:59:40Z")
    post_event(client, event_no="EVT-2", occurred_at="2026-03-02T00:00:20Z")
    publish_day(client, "WTG-01", "2026-03-01")
    publish_day(client, "WTG-01", "2026-03-02")
    client.post(
        "/qc/WTG-01/2026-03-01/recompute",
        data={"source_token": qc_token(client, "WTG-01", "2026-03-01")},
        follow_redirects=False,
    )
    report_id = client.get("/api/qc/WTG-01/2026-03-01").json()["report_id"]

    post_event(client, event_no="EVT-2", revision=2,
               occurred_at="2026-03-02T00:00:20Z")

    page = client.get(f"/qc/reports/{report_id}")
    assert page.status_code == 200
    assert "已失去现行资格" in page.text
    assert "EVT-1" in page.text and "EVT-2" in page.text
    assert "事件 EVT-2 修订 2 更正" in page.text

    assert client.get("/qc/reports/999999").status_code == 200

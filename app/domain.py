"""Domain rules.

The domain is deliberately split from HTTP so it can be tested directly and so
that every rule is checked under the same transaction that mutates state.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .db import Store, log, utc_now_iso

EVENT_NO_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._: -]{1,64}$")
CODE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")
CATEGORY_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DomainError(Exception):
    """A rule violation that should be shown to the operator as-is."""


class Conflict(DomainError):
    """Stale/duplicate revision or concurrent publish attempt."""


@dataclass(frozen=True)
class EventRevision:
    event_no: str
    revision: int
    unit: str
    firmware_version: str
    fault_code: str
    occurred_at: datetime


def parse_utc(value: str) -> datetime:
    """Accept ISO-8601 with an explicit UTC offset and normalise to UTC.

    A naive timestamp is rejected: the operator was asked for a UTC instant and
    guessing would silently shift events across day boundaries.
    """
    raw = (value or "").strip()
    if not raw:
        raise DomainError("UTC 时刻不能为空")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise DomainError(f"无法解析 UTC 时刻: {raw!r}") from None
    if dt.tzinfo is None:
        raise DomainError("时刻必须带 UTC 时区标记（例如 2026-01-01T08:00:00Z）")
    if dt.utcoffset() is not None and dt.utcoffset().total_seconds() != 0:
        raise DomainError("时刻必须是 UTC（偏移为 +00:00）")
    return dt.astimezone(timezone.utc)


def canonical_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require(value: str, label: str, pattern: re.Pattern[str]) -> str:
    cleaned = (value or "").strip()
    if not pattern.match(cleaned):
        raise DomainError(f"{label} 格式不合法: {value!r}")
    return cleaned


def normalise_event_input(
    event_no: str,
    revision: int,
    unit: str,
    firmware_version: str,
    fault_code: str,
    occurred_at: str,
) -> EventRevision:
    return EventRevision(
        event_no=_require(event_no, "事件号", EVENT_NO_RE),
        revision=int(revision),
        unit=_require(unit, "机组", TOKEN_RE),
        firmware_version=_require(firmware_version, "固件版本", TOKEN_RE),
        fault_code=_require(fault_code, "故障码", CODE_RE),
        occurred_at=parse_utc(occurred_at),
    )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

def add_event_revision(store: Store, ev: EventRevision) -> None:
    """Record an event revision.  Revision numbers must increase by exactly 1.

    Every affected (unit, day) - the ones the event used to occupy *and* the ones
    it now occupies - loses its current publish in the same transaction.  That is
    the "原日档与新日档都立即失去现行资格" rule.
    """
    if ev.revision < 1:
        raise DomainError("修订号必须从 1 开始递增")
    occurred = canonical_utc(ev.occurred_at)
    with store.write() as conn:
        prior = conn.execute(
            "SELECT id, revision, unit, occurred_at FROM events"
            " WHERE event_no = ? ORDER BY revision DESC LIMIT 1",
            (ev.event_no,),
        ).fetchone()
        expected = 1 if prior is None else prior["revision"] + 1
        if ev.revision != expected:
            raise Conflict(
                f"事件 {ev.event_no} 的修订号必须为 {expected}，收到 {ev.revision}"
                "（不允许跳号或重复提交）"
            )

        now = utc_now_iso()
        conn.execute(
            "INSERT INTO events (event_no, revision, unit, firmware_version,"
            " fault_code, occurred_at, revision_created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.event_no,
                ev.revision,
                ev.unit,
                ev.firmware_version,
                ev.fault_code,
                occurred,
                now,
            ),
        )

        affected: set[tuple[str, str]] = {(ev.unit, occurred[:10])}
        if prior is not None:
            affected.add((prior["unit"], prior["occurred_at"][:10]))
            # A correction may change any field of the event; the old publish for
            # the old day was built from the old revision, so it must go too even
            # if unit/day happen to be unchanged.
        for unit, day in sorted(affected):
            revoke_day_locked(
                conn,
                unit,
                day,
                f"事件 {ev.event_no} 修订 {ev.revision} 更正",
            )
        log(
            conn,
            "event.revision",
            f"{ev.event_no} r{ev.revision} unit={ev.unit} day={occurred[:10]}",
        )


def current_event_revisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Latest revision of every event number."""
    return conn.execute(
        "SELECT e.* FROM events e"
        " JOIN (SELECT event_no, MAX(revision) AS r FROM events GROUP BY event_no) m"
        "   ON m.event_no = e.event_no AND m.r = e.revision"
        " ORDER BY e.occurred_at, e.event_no"
    ).fetchall()


def event_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events ORDER BY event_no, revision"
    ).fetchall()


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #

def add_mapping_revision(
    store: Store,
    firmware_version: str,
    fault_code: str,
    revision: int,
    category: str,
) -> None:
    fw = _require(firmware_version, "固件版本", TOKEN_RE)
    code = _require(fault_code, "故障码", CODE_RE)
    cat = _require(category, "类别", CATEGORY_RE)
    if revision < 1:
        raise DomainError("映射修订号必须从 1 开始递增")

    with store.write() as conn:
        prior = conn.execute(
            "SELECT revision FROM mappings WHERE firmware_version = ? AND fault_code = ?"
            " ORDER BY revision DESC LIMIT 1",
            (fw, code),
        ).fetchone()
        expected = 1 if prior is None else prior["revision"] + 1
        if revision != expected:
            raise Conflict(
                f"映射 {fw}/{code} 的修订号必须为 {expected}，收到 {revision}"
            )
        conn.execute(
            "INSERT INTO mappings (firmware_version, fault_code, revision, category,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (fw, code, revision, cat, utc_now_iso()),
        )
        affected = _days_using_mapping_locked(conn, fw, code)
        for unit, day in sorted(affected):
            revoke_day_locked(
                conn,
                unit,
                day,
                f"映射 {fw}/{code} 新增修订 {revision}",
            )
        log(
            conn,
            "mapping.revision",
            f"{fw}/{code} r{revision} -> {cat}; revoked {len(affected)} day(s)",
        )


def mapping_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM mappings ORDER BY firmware_version, fault_code, revision"
    ).fetchall()


def effective_mapping(
    conn: sqlite3.Connection, firmware_version: str, fault_code: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mappings WHERE firmware_version = ? AND fault_code = ?"
        " ORDER BY revision DESC LIMIT 1",
        (firmware_version, fault_code),
    ).fetchone()


def _days_using_mapping_locked(
    conn: sqlite3.Connection, fw: str, code: str
) -> set[tuple[str, str]]:
    """(unit, day) pairs whose data is affected by a (firmware, fault code) mapping.

    A mapping revision changes the answer for every day whose events include that
    pair, regardless of which mapping revision the old publish consumed, so we
    look at both live publishes and the current event set.
    """
    days: set[tuple[str, str]] = set()
    rows = conn.execute(
        "SELECT DISTINCT p.unit, p.day FROM publishes p"
        " WHERE p.status = 'CURRENT' AND p.id IN ("
        "   SELECT publish_id FROM publish_items"
        "   WHERE firmware_version = ? AND fault_code = ?"
        " )"
        " UNION"
        " SELECT DISTINCT e.unit, substr(e.occurred_at, 1, 10) FROM events e"
        " JOIN (SELECT event_no, MAX(revision) AS r FROM events GROUP BY event_no) m"
        "   ON m.event_no = e.event_no AND m.r = e.revision"
        " WHERE e.firmware_version = ? AND e.fault_code = ?",
        (fw, code, fw, code),
    ).fetchall()
    for row in rows:
        days.add((row[0], row[1]))
    return days


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #

@dataclass
class DayView:
    unit: str
    day: str
    events: list[sqlite3.Row]
    missing: list[sqlite3.Row]           # events without a mapping
    publish: sqlite3.Row | None          # CURRENT publish or None
    revoked: list[sqlite3.Row]           # historical revoked publishes
    categories: dict[str, str] = field(default_factory=dict)
    map_revisions: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def missing_ids(self) -> set[str]:
        return {ev["event_no"] for ev in self.missing}

    @property
    def revision_token(self) -> str:
        """Canonical, unambiguous snapshot of the event revisions on this day.

        ``event_no@revision`` pairs are used because bare revision numbers collide
        across event numbers (every event starts at 1).
        """
        return revision_token(self.events)

    @property
    def recipe(self) -> str:
        """Everything the publish depends on: event revisions *and* mapping revisions."""
        pairs = sorted(
            f"{fw}/{code}@{rev}" for (fw, code), rev in self.map_revisions.items()
        )
        return f"E[{revision_token(self.events)}]|M[{','.join(pairs)}]"


def revoke_day_locked(
    conn: sqlite3.Connection, unit: str, day: str, reason: str
) -> int:
    cur = conn.execute(
        "UPDATE publishes SET status = 'REVOKED', revoked_at = ?, revoked_reason = ?"
        " WHERE unit = ? AND day = ? AND status = 'CURRENT'",
        (utc_now_iso(), reason, unit, day),
    )
    # Any quality-check conclusion that consumed this day as a source loses its
    # current status in the very same transaction.  A conclusion is only valid
    # for the exact published files it was computed from, so it must never
    # survive a revocation and later be mistaken for a fresh result.
    revoke_qc_for_day_locked(conn, unit, day, f"来源日档 {unit} {day} 变动：{reason}")
    return cur.rowcount


def revoke_day(store: Store, unit: str, day: str, reason: str) -> int:
    with store.write() as conn:
        n = revoke_day_locked(conn, unit, day, reason)
        if n:
            log(conn, "publish.revoke", f"{unit} {day}: {reason}")
        return n


def day_view(conn: sqlite3.Connection, unit: str, day: str) -> DayView:
    events = conn.execute(
        "SELECT e.* FROM events e"
        " JOIN (SELECT event_no, MAX(revision) AS r FROM events GROUP BY event_no) m"
        "   ON m.event_no = e.event_no AND m.r = e.revision"
        " WHERE e.unit = ? AND substr(e.occurred_at, 1, 10) = ?"
        " ORDER BY e.occurred_at, e.event_no",
        (unit, day),
    ).fetchall()
    missing = [
        ev for ev in events
        if effective_mapping(conn, ev["firmware_version"], ev["fault_code"]) is None
    ]
    categories: dict[str, str] = {}
    map_revisions: dict[tuple[str, str], int] = {}
    for ev in events:
        key = (ev["firmware_version"], ev["fault_code"])
        mp = effective_mapping(conn, *key)
        if mp is not None:
            categories[ev["event_no"]] = mp["category"]
            map_revisions[key] = mp["revision"]
    publish = conn.execute(
        "SELECT * FROM publishes WHERE unit = ? AND day = ? AND status = 'CURRENT'",
        (unit, day),
    ).fetchone()
    revoked = conn.execute(
        "SELECT * FROM publishes WHERE unit = ? AND day = ? AND status = 'REVOKED'"
        " ORDER BY created_at DESC",
        (unit, day),
    ).fetchall()
    return DayView(
        unit, day, events, missing, publish, revoked, categories, map_revisions
    )


def revision_token(events: Iterable[sqlite3.Row]) -> str:
    """Stable signature of a set of event revisions (order-independent)."""
    return ",".join(
        sorted(f"{ev['event_no']}@{ev['revision']}" for ev in events)
    )


def parse_revision_token(token: str) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for part in (token or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "@" not in part:
            raise DomainError(f"事件修订快照格式不合法: {part!r}")
        event_no, _, rev = part.rpartition("@")
        try:
            pairs.append((event_no.strip(), int(rev)))
        except ValueError:
            raise DomainError(f"事件修订快照格式不合法: {part!r}") from None
    return sorted(pairs)


def days_with_events(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT unit, substr(occurred_at, 1, 10) AS day FROM events"
        " ORDER BY day DESC, unit"
    ).fetchall()
    return [(r["unit"], r["day"]) for r in rows]


def publish_day(
    store: Store,
    unit: str,
    day: str,
    revision_token_in: str,
    recipe_in: str,
) -> int:
    """Publish a complete daily file for (unit, day) under one coherent revision set.

    ``revision_token_in`` / ``recipe_in`` are the snapshots the operator reviewed on
    the page (event revisions, and event+mapping revisions).  Both are mandatory:
    if either no longer matches live data the publish is refused rather than
    silently publishing something else.  A failure anywhere rolls the whole
    transaction back, leaving the prior revocation - and therefore the prior
    publish's non-current status - intact.

    The revocation of whatever was current happens before insertion, so a
    re-publish never needs to overwrite a CURRENT row.  The partial unique index is
    a second guard.
    """
    with store.write() as conn:
        view = day_view(conn, unit, day)
        if not view.events:
            raise DomainError(f"{unit} {day} 没有任何事件，无法发布日档")

        live_token = revision_token(view.events)
        requested = parse_revision_token(revision_token_in)
        live_pairs = sorted((ev["event_no"], ev["revision"]) for ev in view.events)
        if requested != live_pairs:
            raise Conflict(
                "重算所依据的事件修订已过期：页面提交的事件修订集合与当前数据不一致，"
                "请重新核对后再发布"
            )
        if recipe_in != view.recipe:
            raise Conflict(
                "重算所依据的修订已过期：期间事件或类别映射有新的修订，"
                "请重新核对后再发布"
            )

        if view.missing:
            missing_desc = ", ".join(
                f"{ev['event_no']}({ev['firmware_version']}/{ev['fault_code']})"
                for ev in view.missing
            )
            raise DomainError(
                f"{unit} {day} 缺少类别映射，整日不能供数：{missing_desc}"
            )

        # Sanity: no revision may be ambiguous between two units/days.
        for ev in view.events:
            if ev["unit"] != unit or ev["occurred_at"][:10] != day:
                raise Conflict("事件集合在事务内发生变化，请重试")

        revoke_day_locked(conn, unit, day, "重新发布取代旧日档")
        rows = []
        for ev in view.events:
            mp = effective_mapping(conn, ev["firmware_version"], ev["fault_code"])
            assert mp is not None
            rows.append(
                {
                    "event_no": ev["event_no"],
                    "revision": ev["revision"],
                    "unit": ev["unit"],
                    "firmware_version": ev["firmware_version"],
                    "fault_code": ev["fault_code"],
                    "category": mp["category"],
                    "occurred_at": ev["occurred_at"],
                    "map_revision": mp["revision"],
                }
            )
        cur = conn.execute(
            "INSERT INTO publishes (unit, day, event_revisions, recipe, event_count,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'CURRENT', ?)",
            (
                unit,
                day,
                live_token,
                view.recipe,
                len(rows),
                utc_now_iso(),
            ),
        )
        publish_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO publish_items (publish_id, event_no, revision, unit,"
            " firmware_version, fault_code, category, occurred_at, map_revision)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    publish_id,
                    r["event_no"],
                    r["revision"],
                    r["unit"],
                    r["firmware_version"],
                    r["fault_code"],
                    r["category"],
                    r["occurred_at"],
                    r["map_revision"],
                )
                for r in rows
            ],
        )
        log(conn, "publish.create", f"{unit} {day} id={publish_id} n={len(rows)}")
        return publish_id


def publish_items(conn: sqlite3.Connection, publish_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM publish_items WHERE publish_id = ? ORDER BY occurred_at, event_no",
        (publish_id,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Adjacent-day quality check (suspect retransmissions)
# --------------------------------------------------------------------------- #

QC_WINDOW_US = 60 * 1_000_000


@dataclass(frozen=True)
class QcPair:
    firmware_version: str
    fault_code: str
    early_event_no: str
    early_revision: int
    early_at: str
    late_event_no: str
    late_revision: int
    late_at: str
    delta_us: int
    source_publish_early: int
    source_publish_late: int


@dataclass
class QcDaySource:
    day: str
    publish: sqlite3.Row | None
    revoked: list[sqlite3.Row]
    reasons: list[str]


@dataclass
class QcView:
    unit: str
    start_day: str
    day_a: str
    day_b: str
    source_a: QcDaySource
    source_b: QcDaySource
    report: sqlite3.Row | None = None       # CURRENT and still coherent
    invalid_report: sqlite3.Row | None = None  # CURRENT row that fails coherence
    history: list[sqlite3.Row] = field(default_factory=list)
    pairs: list[sqlite3.Row] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.report is not None

    @property
    def source_token(self) -> str:
        """Token the browser must echo back; empty while either source is absent."""
        if self.source_a.publish is None or self.source_b.publish is None:
            return ""
        return qc_source_token(self.source_a.publish, self.source_b.publish)

    @property
    def reasons(self) -> list[str]:
        out = list(self.source_a.reasons) + list(self.source_b.reasons)
        if self.invalid_report is not None:
            out.append("现行结论与来源日档不再一致，已失去现行资格")
        return out


def parse_day(value: str) -> str:
    raw = (value or "").strip()
    if not DAY_RE.match(raw):
        raise DomainError(f"UTC 日格式不合法: {value!r}（须为 YYYY-MM-DD）")
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise DomainError(f"UTC 日不是有效日期: {value!r}") from None
    return raw


def next_day(day: str) -> str:
    try:
        return (date.fromisoformat(parse_day(day)) + timedelta(days=1)).isoformat()
    except OverflowError:
        raise DomainError(f"起始日 {day} 之后没有可用日期（超出可表示范围）") from None


def _qc_day_source(conn: sqlite3.Connection, unit: str, day: str) -> QcDaySource:
    publish = conn.execute(
        "SELECT * FROM publishes WHERE unit = ? AND day = ? AND status = 'CURRENT'",
        (unit, day),
    ).fetchone()
    revoked = conn.execute(
        "SELECT * FROM publishes WHERE unit = ? AND day = ? AND status = 'REVOKED'"
        " ORDER BY created_at DESC",
        (unit, day),
    ).fetchall()
    reasons: list[str] = []
    if publish is not None:
        # Defence in depth: the boot guard already demotes such rows, but a
        # conclusion must never be computed from a file that does not match the
        # live revision set (e.g. a hand-edited database).
        view = day_view(conn, unit, day)
        if (
            view.missing
            or view.recipe != publish["recipe"]
            or revision_token(view.events) != publish["event_revisions"]
        ):
            publish = None
            reasons.append(f"{day} 的现行日档与当前数据不一致，须重新发布")
    if publish is None and not reasons:
        has_events = conn.execute(
            "SELECT 1 FROM events WHERE unit = ? AND substr(occurred_at, 1, 10) = ?"
            " LIMIT 1",
            (unit, day),
        ).fetchone()
        missing = conn.execute(
            "SELECT COUNT(*) FROM events e"
            " JOIN (SELECT event_no, MAX(revision) AS r FROM events GROUP BY event_no) m"
            "   ON m.event_no = e.event_no AND m.r = e.revision"
            " WHERE e.unit = ? AND substr(e.occurred_at, 1, 10) = ?"
            "   AND NOT EXISTS (SELECT 1 FROM mappings mp"
            "     WHERE mp.firmware_version = e.firmware_version"
            "       AND mp.fault_code = e.fault_code)",
            (unit, day),
        ).fetchone()[0]
        if not has_events:
            reasons.append(f"{day} 当天无可供质检的事件")
        elif missing:
            reasons.append(f"{day} 存在缺少类别映射的事件，整日尚未发布")
        elif revoked:
            reasons.append(f"{day} 现行资格已撤销，等待重算发布")
        else:
            reasons.append(f"{day} 尚未发布")
    return QcDaySource(day, publish, revoked, reasons)


def qc_source_token(publish_a: sqlite3.Row, publish_b: sqlite3.Row) -> str:
    return "|".join(
        f"{p['id']}:{p['event_revisions']}:{p['recipe']}"
        for p in (publish_a, publish_b)
    )


def qc_pair_rows(
    conn: sqlite3.Connection, publish_a: sqlite3.Row, publish_b: sqlite3.Row
) -> list[QcPair]:
    """All event pairs from the two published files sharing firmware + fault code
    whose timestamps are within 60 s.  Both intra-day and cross-midnight pairs are
    included because both files' items are pooled before pairing."""
    rows = conn.execute(
        "SELECT event_no, revision, firmware_version, fault_code, occurred_at,"
        " publish_id FROM publish_items WHERE publish_id IN (?, ?)",
        (publish_a["id"], publish_b["id"]),
    ).fetchall()

    groups: dict[tuple[str, str], list[tuple[int, sqlite3.Row]]] = defaultdict(list)
    for row in rows:
        ts = parse_utc(row["occurred_at"])
        delta = ts - datetime(1970, 1, 1, tzinfo=timezone.utc)
        ts_us = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        groups[(row["firmware_version"], row["fault_code"])].append((ts_us, row))

    pairs: list[QcPair] = []
    for (fw, code), items in groups.items():
        items.sort(key=lambda t: (t[0], t[1]["event_no"]))
        for i, (early_us, early) in enumerate(items):
            j = i + 1
            while j < len(items):
                late_us, late = items[j]
                delta = late_us - early_us
                if delta > QC_WINDOW_US:
                    break
                # Identical event numbers should never appear in two day files;
                # if data is corrupt, never pair an event with itself.
                if early["event_no"] != late["event_no"]:
                    pairs.append(
                        QcPair(
                            firmware_version=fw,
                            fault_code=code,
                            early_event_no=early["event_no"],
                            early_revision=early["revision"],
                            early_at=early["occurred_at"],
                            late_event_no=late["event_no"],
                            late_revision=late["revision"],
                            late_at=late["occurred_at"],
                            delta_us=delta,
                            source_publish_early=early["publish_id"],
                            source_publish_late=late["publish_id"],
                        )
                    )
                j += 1
    pairs.sort(
        key=lambda p: (p.early_at, p.early_event_no, p.late_at, p.late_event_no)
    )
    return pairs


def _revoke_qc_locked(conn: sqlite3.Connection, report_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE qc_reports SET status = 'REVOKED', revoked_at = ?, revoked_reason = ?"
        " WHERE id = ? AND status = 'CURRENT'",
        (utc_now_iso(), reason, report_id),
    )


def revoke_qc_for_day_locked(
    conn: sqlite3.Connection, unit: str, day: str, reason: str
) -> int:
    """Revoke every current conclusion that consumed (unit, day) as a source.

    Called from ``revoke_day_locked`` so the source publish and the conclusion
    that depended on it change state in one transaction: there is no instant at
    which a revoked daily file still has a "current" quality-check result.
    """
    rows = conn.execute(
        "SELECT id FROM qc_reports WHERE unit = ? AND status = 'CURRENT'"
        " AND (day_a = ? OR day_b = ?)",
        (unit, day, day),
    ).fetchall()
    for row in rows:
        _revoke_qc_locked(conn, row["id"], reason)
    return len(rows)


def qc_current_report(
    conn: sqlite3.Connection, unit: str, start_day: str
) -> sqlite3.Row | None:
    """The CURRENT conclusion, only if both source publishes are still CURRENT
    and unchanged.  A stale row is reported as not current, never upgraded."""
    report = conn.execute(
        "SELECT * FROM qc_reports WHERE unit = ? AND start_day = ? AND status = 'CURRENT'",
        (unit, start_day),
    ).fetchone()
    if report is None:
        return None
    a = conn.execute(
        "SELECT * FROM publishes WHERE id = ? AND status = 'CURRENT'",
        (report["publish_a_id"],),
    ).fetchone()
    b = conn.execute(
        "SELECT * FROM publishes WHERE id = ? AND status = 'CURRENT'",
        (report["publish_b_id"],),
    ).fetchone()
    if a is None or b is None:
        return None
    if (
        a["unit"] != report["unit"]
        or b["unit"] != report["unit"]
        or a["day"] != report["day_a"]
        or b["day"] != report["day_b"]
    ):
        return None
    if qc_source_token(a, b) != report["source_token"]:
        return None
    return report


def qc_view(conn: sqlite3.Connection, unit: str, start_day: str) -> QcView:
    start = parse_day(start_day)
    day_a, day_b = start, next_day(start)
    source_a = _qc_day_source(conn, unit, day_a)
    source_b = _qc_day_source(conn, unit, day_b)
    history = conn.execute(
        "SELECT * FROM qc_reports WHERE unit = ? AND start_day = ?"
        " ORDER BY created_at DESC, id DESC",
        (unit, start),
    ).fetchall()
    current_row = next((r for r in history if r["status"] == "CURRENT"), None)
    report = qc_current_report(conn, unit, start)
    invalid = current_row if current_row is not None and report is None else None
    pairs = (
        conn.execute(
            "SELECT * FROM qc_pairs WHERE report_id = ? ORDER BY early_at,"
            " early_event_no, late_at, late_event_no",
            (report["id"],),
        ).fetchall()
        if report is not None
        else []
    )
    return QcView(
        unit=unit,
        start_day=start,
        day_a=day_a,
        day_b=day_b,
        source_a=source_a,
        source_b=source_b,
        report=report,
        invalid_report=invalid,
        history=list(history),
        pairs=list(pairs),
    )


def qc_recompute(
    store: Store, unit: str, start_day: str, source_token_in: str
) -> int:
    """Recompute a two-day quality check from the *currently published* files.

    The whole verify-compute-insert runs under one ``BEGIN IMMEDIATE``
    transaction, and the token shown to the operator pins both publish ids and
    their exact event/mapping revision basis.  If an event correction, mapping
    revision or republish interleaves, either this transaction saw the new state
    (and the token check fails) or the later writer revokes the conclusion we
    just created - never a result stitched from two different publish sets.
    """
    unit = _require(unit, "机组", TOKEN_RE)
    start = parse_day(start_day)
    day_b = next_day(start)

    with store.write() as conn:
        source_a = _qc_day_source(conn, unit, start)
        source_b = _qc_day_source(conn, unit, day_b)
        reasons = source_a.reasons + source_b.reasons
        if reasons:
            raise DomainError(
                "两天日档不齐备，不能给出质检结论：" + "；".join(reasons)
            )
        assert source_a.publish is not None and source_b.publish is not None
        live_token = qc_source_token(source_a.publish, source_b.publish)
        if (source_token_in or "").strip() != live_token:
            raise Conflict(
                "质检所依据的日档已变化（期间有更正、映射修订或重新发布），"
                "请重新核对后再重算"
            )

        pairs = qc_pair_rows(conn, source_a.publish, source_b.publish)

        # Replacing a conclusion is revocation first, then insert: a failure to
        # insert rolls everything back and the older conclusion stays REVOKED
        # (it was revoked when its source changed, and is never promoted back).
        old = conn.execute(
            "SELECT id FROM qc_reports WHERE unit = ? AND start_day = ?"
            " AND status = 'CURRENT'",
            (unit, start),
        ).fetchone()
        if old is not None:
            _revoke_qc_locked(conn, old["id"], "被新的质检重算取代")

        cur = conn.execute(
            "INSERT INTO qc_reports (unit, start_day, day_a, day_b, publish_a_id,"
            " publish_b_id, source_token, pair_count, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CURRENT', ?)",
            (
                unit,
                start,
                start,
                day_b,
                source_a.publish["id"],
                source_b.publish["id"],
                live_token,
                len(pairs),
                utc_now_iso(),
            ),
        )
        report_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO qc_pairs (report_id, firmware_version, fault_code,"
            " early_event_no, early_revision, early_at, late_event_no,"
            " late_revision, late_at, delta_us, source_publish_early,"
            " source_publish_late) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    report_id,
                    p.firmware_version,
                    p.fault_code,
                    p.early_event_no,
                    p.early_revision,
                    p.early_at,
                    p.late_event_no,
                    p.late_revision,
                    p.late_at,
                    p.delta_us,
                    p.source_publish_early,
                    p.source_publish_late,
                )
                for p in pairs
            ],
        )
        log(
            conn,
            "qc.create",
            f"{unit} {start}~{day_b} id={report_id} pairs={len(pairs)}"
            f" pubs={source_a.publish['id']},{source_b.publish['id']}",
        )
        return report_id


def qc_pairs(conn: sqlite3.Connection, report_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM qc_pairs WHERE report_id = ? ORDER BY early_at,"
        " early_event_no, late_at, late_event_no",
        (report_id,),
    ).fetchall()


def qc_reports(conn: sqlite3.Connection, unit: str, start_day: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM qc_reports WHERE unit = ? AND start_day = ?"
        " ORDER BY created_at DESC, id DESC",
        (unit, start_day),
    ).fetchall()


def qc_report(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    """One conclusion by id, current or revoked, for review."""
    return conn.execute(
        "SELECT * FROM qc_reports WHERE id = ?", (report_id,)
    ).fetchone()


def purge_stale_qc_current(store: Store) -> int:
    """Boot guard: demote qc conclusions whose source publishes are no longer
    current or no longer match the recorded basis.  Never promotes anything."""
    with store.write() as conn:
        rows = conn.execute(
            "SELECT * FROM qc_reports WHERE status = 'CURRENT'"
        ).fetchall()
        revoked = 0
        for row in rows:
            a = conn.execute(
                "SELECT * FROM publishes WHERE id = ? AND status = 'CURRENT'",
                (row["publish_a_id"],),
            ).fetchone()
            b = conn.execute(
                "SELECT * FROM publishes WHERE id = ? AND status = 'CURRENT'",
                (row["publish_b_id"],),
            ).fetchone()
            if (
                a is None
                or b is None
                or qc_source_token(a, b) != row["source_token"]
                or a["unit"] != row["unit"]
                or b["unit"] != row["unit"]
                or a["day"] != row["day_a"]
                or b["day"] != row["day_b"]
            ):
                _revoke_qc_locked(conn, row["id"], "启动校验：质检来源日档已不一致")
                revoked += 1
        if revoked:
            log(conn, "startup.qc_revoke", f"{revoked} stale qc conclusion(s) revoked")
        return revoked



def all_publishes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM publishes ORDER BY created_at DESC, id DESC"
    ).fetchall()


def purge_stale_current(store: Store) -> int:
    """Boot-time guard.

    Nothing in normal operation can leave a CURRENT publish that contradicts the
    data, but a crash between a write and its revocation (or a hand-edited DB)
    could.  On startup we revoke any CURRENT publish that no longer matches the
    live revision set or has lost a mapping, so a restart can never *promote* a
    stale file - it can only demote one.
    """
    with store.write() as conn:
        rows = conn.execute(
            "SELECT id, unit, day, event_revisions, recipe FROM publishes"
            " WHERE status = 'CURRENT'"
        ).fetchall()
        revoked = 0
        for row in rows:
            view = day_view(conn, row["unit"], row["day"])
            if (
                view.missing
                or not view.events
                or row["event_revisions"] != revision_token(view.events)
                or row["recipe"] != view.recipe
            ):
                revoke_day_locked(
                    conn,
                    row["unit"],
                    row["day"],
                    "启动校验：现行日档与当前数据不一致",
                )
                revoked += 1
        if revoked:
            log(conn, "startup.revoke", f"{revoked} stale current publish(es) revoked")
        return revoked


def available_days(conn: sqlite3.Connection) -> list[dict]:
    """Daily files that are currently available for downstream consumption."""
    rows = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM publish_items i WHERE i.publish_id = p.id)"
        " AS items FROM publishes p WHERE p.status = 'CURRENT'"
        " ORDER BY p.day DESC, p.unit"
    ).fetchall()
    return [dict(r) for r in rows]

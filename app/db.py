"""SQLite persistence layer for the calibration tool."""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set

from . import solver

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id             INTEGER PRIMARY KEY,
    sample_id      TEXT NOT NULL UNIQUE,
    street         TEXT NOT NULL,
    housing_type   TEXT NOT NULL,
    initial_weight INTEGER NOT NULL CHECK (initial_weight > 0),
    current_weight INTEGER NOT NULL CHECK (current_weight > 0),
    row_order      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_households_street ON households (street, row_order);
CREATE INDEX IF NOT EXISTS idx_households_type ON households (housing_type, row_order);

CREATE TABLE IF NOT EXISTS street_locks (
    street          TEXT PRIMARY KEY,
    locked_at       TEXT NOT NULL,
    household_count INTEGER NOT NULL CHECK (household_count > 0),
    total_weight    INTEGER NOT NULL CHECK (total_weight > 0)
);

CREATE TABLE IF NOT EXISTS locked_weights (
    household_id INTEGER PRIMARY KEY REFERENCES households (id) ON DELETE CASCADE,
    street       TEXT NOT NULL,
    weight       INTEGER NOT NULL CHECK (weight > 0)
);
CREATE INDEX IF NOT EXISTS idx_locked_weights_street ON locked_weights (street);

CREATE TABLE IF NOT EXISTS publications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at        TEXT NOT NULL,
    household_count     INTEGER NOT NULL,
    total_weight        INTEGER NOT NULL,
    street_targets_json TEXT NOT NULL,
    type_targets_json   TEXT NOT NULL,
    locked_streets_json TEXT NOT NULL,
    weights_json        TEXT NOT NULL
);

-- One row per household per publication.  This is the immutable snapshot
-- used for release-to-release comparison: comparisons are plain SQL over
-- this table, so two 200k-household snapshots are never loaded at once.
CREATE TABLE IF NOT EXISTS publication_weights (
    publication_id INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    household_id   INTEGER NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    weight         INTEGER NOT NULL CHECK (weight > 0),
    PRIMARY KEY (publication_id, household_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_publication_weights_household
    ON publication_weights (household_id);

-- Per-release street / housing-type totals frozen at publish time.  The
-- comparison page shows these "full release totals" without re-scanning a
-- 200k-row snapshot on every request.
CREATE TABLE IF NOT EXISTS publication_group_totals (
    publication_id  INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    group_kind      TEXT NOT NULL CHECK (group_kind IN ('street', 'housing_type')),
    name            TEXT NOT NULL,
    household_count INTEGER NOT NULL,
    total_weight    INTEGER NOT NULL,
    PRIMARY KEY (publication_id, group_kind, name)
) WITHOUT ROWID;
"""

PUBLICATION_OPTION_LIMIT = 1000
COMPARE_PAGE_SIZE = 100
COMPARE_MAX_PAGE_SIZE = 500

MAX_TARGET = 1_000_000_000

_logger = logging.getLogger(__name__)

_HEADER_ALIASES = {
    "sample_id": {"样本号", "样本编号", "sample_id", "sample id", "id"},
    "street": {"街道", "街道名称", "street"},
    "housing_type": {"住房类型", "房屋类型", "housing_type", "housing type", "type"},
    "initial_weight": {"初始权重", "初始值", "initial_weight", "initial weight"},
}


class ValidationError(ValueError):
    """Raised when user input or the sample file is invalid."""


class PublicationNotFound(LookupError):
    """Raised when a requested publication number does not exist."""


class SnapshotUnavailable(RuntimeError):
    """Raised when a publication has no complete per-household snapshot."""


@dataclass
class LockOutcome:
    street: str
    count: int
    total: int
    already: bool


@dataclass
class PublishOutcome:
    ok: bool
    updated: int = 0
    total: int = 0
    publication_id: Optional[int] = None
    conflict: Optional[List[dict]] = None


@dataclass
class DashboardData:
    households: List[dict] = field(default_factory=list)
    streets: List[dict] = field(default_factory=list)
    types: List[dict] = field(default_factory=list)
    locks: Dict[str, dict] = field(default_factory=dict)
    publications: List[dict] = field(default_factory=list)
    publication_options: List["PublicationInfo"] = field(default_factory=list)
    publication_options_limited: bool = False
    summary: dict = field(default_factory=dict)


@dataclass
class PublicationInfo:
    id: int
    published_at: str
    household_count: int
    total_weight: int


@dataclass
class ComparisonGroup:
    name: str
    changed_count: int
    old_sum: int
    new_sum: int
    old_total: int = 0
    new_total: int = 0


@dataclass
class ComparisonResult:
    old: PublicationInfo
    new: PublicationInfo
    total_households: int
    changed_count: int
    changed_old_sum: int
    changed_new_sum: int
    rows: List[dict]
    streets: List[ComparisonGroup]
    types: List[ComparisonGroup]
    page: int
    page_size: int
    total_pages: int
    added_count: int = 0
    removed_count: int = 0


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _ensure_publication_snapshots(conn: sqlite3.Connection) -> None:
    """Backfill ``publication_weights`` and group totals for legacy publications.

    Older databases only stored the full snapshot as a JSON blob in
    ``publications.weights_json``.  Such a publication is a single completed
    release and is immutable, so expanding it once at start-up is safe.  New
    releases write both representations inside the publishing transaction.
    """
    pending = conn.execute(
        """
        SELECT p.id
        FROM publications p
        WHERE NOT EXISTS (
            SELECT 1 FROM publication_weights w WHERE w.publication_id = p.id
        ) AND p.household_count > 0
        ORDER BY p.id
        """
    ).fetchall()
    for row in pending:
        # Let SQLite expand the JSON blob; this keeps the (possibly 200k
        # entry) legacy snapshot out of the Python heap.
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO publication_weights
                    (publication_id, household_id, weight)
                SELECT ?, CAST(entry.key AS INTEGER), CAST(entry.value AS INTEGER)
                FROM publications p, json_each(p.weights_json) AS entry
                WHERE p.id = ?
                  AND entry.key NOT GLOB '*[^0-9]*'
                  AND entry.key GLOB '[1-9]*'
                  AND CAST(entry.value AS TEXT) GLOB '[1-9]*'
                  AND CAST(entry.value AS TEXT) NOT GLOB '*[^0-9]*'
                  AND EXISTS (
                      SELECT 1 FROM households h
                      WHERE h.id = CAST(entry.key AS INTEGER)
                  )
                """,
                (row["id"], row["id"]),
            )
            _backfill_group_totals(conn, row["id"])
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            _logger.warning("发布记录 #%s 的权重快照迁移失败", row["id"], exc_info=True)

    # Publications created by a build that predates the totals table, or
    # whose totals backfill was interrupted.
    missing_totals = [
        row[0]
        for row in conn.execute(
            """
            SELECT p.id FROM publications p
            WHERE EXISTS (
                SELECT 1 FROM publication_weights w WHERE w.publication_id = p.id
            ) AND NOT EXISTS (
                SELECT 1 FROM publication_group_totals t WHERE t.publication_id = p.id
            )
            ORDER BY p.id
            """
        )
    ]
    for publication_id in missing_totals:
        try:
            conn.execute("BEGIN IMMEDIATE")
            _backfill_group_totals(conn, publication_id)
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            _logger.warning(
                "发布记录 #%s 的分组合计回填失败", publication_id, exc_info=True
            )


def _backfill_group_totals(conn: sqlite3.Connection, publication_id: int) -> None:
    """Derive per-group totals from the snapshot for legacy publications."""
    for group_kind, column in (("street", "street"), ("housing_type", "housing_type")):
        conn.execute(
            f"""
            INSERT OR REPLACE INTO publication_group_totals
                (publication_id, group_kind, name, household_count, total_weight)
            SELECT w.publication_id, ?, h.{column}, COUNT(*), SUM(w.weight)
            FROM publication_weights w
            JOIN households h ON h.id = w.household_id
            WHERE w.publication_id = ?
            GROUP BY h.{column}
            """,
            (group_kind, publication_id),
        )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Sample import
# ---------------------------------------------------------------------------


def _canonical_headers(fieldnames: Optional[Iterable[str]]) -> Dict[str, str]:
    if not fieldnames:
        raise ValidationError("样本 CSV 没有表头")
    mapping: Dict[str, str] = {}
    for raw in fieldnames:
        name = (raw or "").strip()
        for canonical, aliases in _HEADER_ALIASES.items():
            if name in aliases:
                if canonical in mapping:
                    raise ValidationError(f"样本 CSV 表头重复：{name}")
                mapping[canonical] = raw
                break
    missing = [c for c in _HEADER_ALIASES if c not in mapping]
    if missing:
        readable = {
            "sample_id": "样本号",
            "street": "街道",
            "housing_type": "住房类型",
            "initial_weight": "初始权重",
        }
        raise ValidationError(
            "样本 CSV 缺少列：" + "、".join(readable[item] for item in missing)
        )
    return mapping


def parse_sample_csv(path: Path) -> List[dict]:
    if not path.is_file():
        raise ValidationError(f"找不到样本 CSV：{path}")
    rows: List[dict] = []
    seen_ids: Set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = _canonical_headers(reader.fieldnames)
        for line_no, raw in enumerate(reader, start=2):
            sample_id = (raw.get(headers["sample_id"]) or "").strip()
            street = (raw.get(headers["street"]) or "").strip()
            housing_type = (raw.get(headers["housing_type"]) or "").strip()
            weight_text = (raw.get(headers["initial_weight"]) or "").strip()
            if not sample_id:
                raise ValidationError(f"样本 CSV 第 {line_no} 行：样本号为空")
            if sample_id in seen_ids:
                raise ValidationError(f"样本 CSV 第 {line_no} 行：样本号重复（{sample_id}）")
            if not street:
                raise ValidationError(f"样本 CSV 第 {line_no} 行：街道为空")
            if not housing_type:
                raise ValidationError(f"样本 CSV 第 {line_no} 行：住房类型为空")
            if not re.fullmatch(r"[1-9][0-9]*", weight_text):
                raise ValidationError(
                    f"样本 CSV 第 {line_no} 行：初始权重必须是正整数（当前为 {weight_text!r}）"
                )
            seen_ids.add(sample_id)
            rows.append(
                {
                    "sample_id": sample_id,
                    "street": street,
                    "housing_type": housing_type,
                    "initial_weight": int(weight_text),
                }
            )
    if not rows:
        raise ValidationError("样本 CSV 没有任何数据行")
    if len(rows) > 200_000:
        raise ValidationError("样本 CSV 行数过多（上限 200000 行）")
    return rows


def initialize(db_path: Path, sample_csv_path: Path) -> None:
    """Create the schema and seed the fixed sample once."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _ensure_publication_snapshots(conn)
        count = conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]
        if count:
            return
        rows = parse_sample_csv(Path(sample_csv_path))
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock in case another process seeded first.
            if conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]:
                conn.execute("COMMIT")
                return
            conn.executemany(
                """
                INSERT INTO households
                    (sample_id, street, housing_type, initial_weight, current_weight, row_order)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["sample_id"],
                        row["street"],
                        row["housing_type"],
                        row["initial_weight"],
                        row["initial_weight"],
                        index,
                    )
                    for index, row in enumerate(rows)
                ],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _load_locks(conn: sqlite3.Connection) -> Dict[str, dict]:
    locks: Dict[str, dict] = {}
    for row in conn.execute("SELECT * FROM street_locks ORDER BY street"):
        locks[row["street"]] = {
            "street": row["street"],
            "locked_at": row["locked_at"],
            "household_count": row["household_count"],
            "total_weight": row["total_weight"],
            "weights": {},
        }
    for row in conn.execute("SELECT household_id, street, weight FROM locked_weights"):
        if row["street"] in locks:
            locks[row["street"]]["weights"][row["household_id"]] = row["weight"]
    return locks


def _households(conn: sqlite3.Connection) -> List[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, sample_id, street, housing_type, initial_weight, current_weight
            FROM households ORDER BY row_order
            """
        )
    ]


def get_dashboard(db_path: Path) -> DashboardData:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        households = _households(conn)
        locks = _load_locks(conn)
        publication_rows = [
            {
                "id": row["id"],
                "published_at": row["published_at"],
                "household_count": row["household_count"],
                "total_weight": row["total_weight"],
            }
            for row in conn.execute(
                "SELECT id, published_at, household_count, total_weight "
                "FROM publications ORDER BY id DESC LIMIT 5"
            )
        ]
        publication_count = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        publication_options = [
            PublicationInfo(
                id=row["id"],
                published_at=row["published_at"],
                household_count=row["household_count"],
                total_weight=row["total_weight"],
            )
            for row in conn.execute(
                "SELECT id, published_at, household_count, total_weight "
                "FROM publications ORDER BY id DESC LIMIT ?",
                (PUBLICATION_OPTION_LIMIT + 1,),
            )
        ]
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    options_limited = len(publication_options) > PUBLICATION_OPTION_LIMIT
    publication_options = publication_options[:PUBLICATION_OPTION_LIMIT]

    street_groups: Dict[str, dict] = {}
    type_groups: Dict[str, dict] = {}
    for household in households:
        household["locked"] = household["street"] in locks
        street = household["street"]
        if street not in street_groups:
            street_groups[street] = {
                "name": street,
                "households": [],
                "count": 0,
                "initial_sum": 0,
                "current_sum": 0,
                "locked": street in locks,
            }
        group = street_groups[street]
        group["households"].append(household)
        group["count"] += 1
        group["initial_sum"] += household["initial_weight"]
        group["current_sum"] += household["current_weight"]

        housing_type = household["housing_type"]
        if housing_type not in type_groups:
            type_groups[housing_type] = {
                "name": housing_type,
                "households": [],
                "count": 0,
                "initial_sum": 0,
                "current_sum": 0,
            }
        type_group = type_groups[housing_type]
        type_group["households"].append(household)
        type_group["count"] += 1
        type_group["initial_sum"] += household["initial_weight"]
        type_group["current_sum"] += household["current_weight"]

    for name, group in street_groups.items():
        group["lock"] = locks.get(name)
        group["target_value"] = group["current_sum"]
        group["min_target"] = group["count"]
        group["max_target"] = 2 * group["initial_sum"]
        if name in locks:
            group["target_value"] = locks[name]["total_weight"]
            group["min_target"] = locks[name]["total_weight"]
            group["max_target"] = locks[name]["total_weight"]

    for group in type_groups.values():
        group["min_target"] = group["count"]
        group["max_target"] = 2 * group["initial_sum"]

    summary = {
        "household_count": len(households),
        "street_count": len(street_groups),
        "type_count": len(type_groups),
        "total_initial": sum(item["initial_weight"] for item in households),
        "total_current": sum(item["current_weight"] for item in households),
        "locked_streets": len(locks),
        "publication_count": publication_count,
        "last_published_at": publication_rows[0]["published_at"] if publication_rows else None,
    }

    return DashboardData(
        households=households,
        streets=list(street_groups.values()),
        types=list(type_groups.values()),
        locks=locks,
        publications=publication_rows,
        publication_options=publication_options,
        publication_options_limited=options_limited,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Release comparison
# ---------------------------------------------------------------------------


def get_release_overview(db_path: Path) -> DashboardData:
    """Header/selector data for the comparison page.

    Deliberately avoids loading ``households``: the comparison page must stay
    cheap for 200k-household samples.
    """
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        try:
            summary_row = conn.execute(
                """
                SELECT COUNT(*) AS household_count,
                       COALESCE(SUM(current_weight), 0) AS total_current,
                       COALESCE(SUM(initial_weight), 0) AS total_initial,
                       COUNT(DISTINCT street) AS street_count,
                       COUNT(DISTINCT housing_type) AS type_count
                FROM households
                """
            ).fetchone()
            locked_streets = conn.execute("SELECT COUNT(*) FROM street_locks").fetchone()[0]
            publication_count = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
            last_published_at = conn.execute(
                "SELECT MAX(published_at) FROM publications"
            ).fetchone()[0]
            publication_options = [
                PublicationInfo(
                    id=row["id"],
                    published_at=row["published_at"],
                    household_count=row["household_count"],
                    total_weight=row["total_weight"],
                )
                for row in conn.execute(
                    "SELECT id, published_at, household_count, total_weight "
                    "FROM publications ORDER BY id DESC LIMIT ?",
                    (PUBLICATION_OPTION_LIMIT + 1,),
                )
            ]
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    options_limited = len(publication_options) > PUBLICATION_OPTION_LIMIT
    return DashboardData(
        publication_options=publication_options[:PUBLICATION_OPTION_LIMIT],
        publication_options_limited=options_limited,
        summary={
            "household_count": summary_row["household_count"],
            "street_count": summary_row["street_count"],
            "type_count": summary_row["type_count"],
            "total_initial": summary_row["total_initial"],
            "total_current": summary_row["total_current"],
            "locked_streets": locked_streets,
            "publication_count": publication_count,
            "last_published_at": last_published_at,
        },
    )


def _publication_info(conn: sqlite3.Connection, publication_id: int) -> PublicationInfo:
    row = conn.execute(
        "SELECT id, published_at, household_count, total_weight "
        "FROM publications WHERE id = ?",
        (publication_id,),
    ).fetchone()
    if row is None:
        raise PublicationNotFound(f"发布记录 #{publication_id} 不存在")
    return PublicationInfo(
        id=row["id"],
        published_at=row["published_at"],
        household_count=row["household_count"],
        total_weight=row["total_weight"],
    )


def _check_snapshot(conn: sqlite3.Connection, info: PublicationInfo) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM publication_weights WHERE publication_id = ?",
        (info.id,),
    ).fetchone()[0]
    if count != info.household_count:
        raise SnapshotUnavailable(
            f"发布记录 #{info.id} 的逐户权重快照不完整"
            f"（记录 {info.household_count} 户，实际 {count} 户），无法核对"
        )


# ``diff`` is the union of both snapshots, keeping only households whose
# weight differs (including households present in only one snapshot).  The
# comparison is done entirely inside SQLite: neither the 200k-row snapshots
# nor the diff are materialised in Python.
_DIFF_SELECT = """
SELECT old.household_id AS household_id, old.weight AS old_weight,
       new.weight AS new_weight
FROM publication_weights old
LEFT JOIN publication_weights new
       ON new.publication_id = :new_id
      AND new.household_id = old.household_id
WHERE old.publication_id = :old_id
  AND (new.weight IS NULL OR new.weight <> old.weight)
UNION ALL
SELECT new.household_id, NULL, new.weight
FROM publication_weights new
WHERE new.publication_id = :new_id
  AND NOT EXISTS (
      SELECT 1 FROM publication_weights old
      WHERE old.publication_id = :old_id
        AND old.household_id = new.household_id
  )
"""


def compare_publications(
    db_path: Path,
    old_id: int,
    new_id: int,
    page: int = 1,
    page_size: int = COMPARE_PAGE_SIZE,
) -> ComparisonResult:
    """Compare two immutable release snapshots without loading them in memory.

    All reads share one SQLite read transaction, so the result is a consistent
    view of the two selected releases.  Releases are immutable and are
    addressed by id, so a publication committed while the comparison is being
    reviewed cannot change an already selected comparison.
    """
    if page < 1:
        raise ValidationError("页码必须是从 1 开始的整数")
    page_size = max(1, min(int(page_size), COMPARE_MAX_PAGE_SIZE))

    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        try:
            old = _publication_info(conn, old_id)
            new = _publication_info(conn, new_id)
            _check_snapshot(conn, old)
            _check_snapshot(conn, new)
            params = {"old_id": old.id, "new_id": new.id}

            # Materialise the diff as a temporary table inside SQLite.  The
            # 200k-row diff lives in the database engine, not the Python heap,
            # and the page/count/group queries each scan it only once.
            conn.execute("DROP TABLE IF EXISTS temp.release_diff")
            conn.execute(
                "CREATE TEMP TABLE release_diff ("
                "household_id INTEGER PRIMARY KEY, old_weight INTEGER, new_weight INTEGER)"
            )
            conn.execute(
                "INSERT INTO temp.release_diff (household_id, old_weight, new_weight) "
                + _DIFF_SELECT,
                params,
            )

            totals = conn.execute(
                """
                SELECT COUNT(*) AS changed_count,
                       COALESCE(SUM(old_weight), 0) AS old_sum,
                       COALESCE(SUM(new_weight), 0) AS new_sum,
                       COALESCE(SUM(CASE WHEN old_weight IS NULL THEN 1 ELSE 0 END), 0)
                           AS added_count,
                       COALESCE(SUM(CASE WHEN new_weight IS NULL THEN 1 ELSE 0 END), 0)
                           AS removed_count
                FROM temp.release_diff
                """
            ).fetchone()
            changed_count = totals["changed_count"]
            total_pages = max(1, (changed_count + page_size - 1) // page_size)
            page = min(page, total_pages)
            offset = (page - 1) * page_size

            row_cursor = conn.execute(
                """
                SELECT h.sample_id, h.street, h.housing_type,
                       d.old_weight, d.new_weight
                FROM temp.release_diff d
                JOIN households h ON h.id = d.household_id
                ORDER BY h.sample_id
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            )
            rows = [
                {
                    "sample_id": row["sample_id"],
                    "street": row["street"],
                    "housing_type": row["housing_type"],
                    "old_weight": row["old_weight"],
                    "new_weight": row["new_weight"],
                }
                for row in row_cursor
            ]

            street_sums = _group_sums(conn, "street")
            type_sums = _group_sums(conn, "housing_type")
            street_totals = _group_totals(conn, params, "street")
            type_totals = _group_totals(conn, params, "housing_type")

            street_names = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT street FROM households ORDER BY street"
                )
            ]
            type_names = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT housing_type FROM households ORDER BY housing_type"
                )
            ]

            total_households = conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return ComparisonResult(
        old=old,
        new=new,
        total_households=total_households,
        changed_count=changed_count,
        changed_old_sum=totals["old_sum"],
        changed_new_sum=totals["new_sum"],
        rows=rows,
        streets=_merge_groups(street_names, street_sums, street_totals),
        types=_merge_groups(type_names, type_sums, type_totals),
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        added_count=totals["added_count"],
        removed_count=totals["removed_count"],
    )


def _group_sums(conn: sqlite3.Connection, column: str) -> Dict[str, dict]:
    assert column in {"street", "housing_type"}
    cursor = conn.execute(
        f"""
        SELECT h.{column} AS name,
               COUNT(*) AS changed_count,
               COALESCE(SUM(d.old_weight), 0) AS old_sum,
               COALESCE(SUM(d.new_weight), 0) AS new_sum
        FROM temp.release_diff d
        JOIN households h ON h.id = d.household_id
        GROUP BY h.{column}
        """
    )
    return {
        row["name"]: {
            "changed_count": row["changed_count"],
            "old_sum": row["old_sum"],
            "new_sum": row["new_sum"],
        }
        for row in cursor
    }


def _merge_groups(
    names: List[str],
    sums: Mapping[str, dict],
    totals: Mapping[str, dict],
) -> List[ComparisonGroup]:
    groups: List[ComparisonGroup] = []
    for name in names:
        values = sums.get(name, {"changed_count": 0, "old_sum": 0, "new_sum": 0})
        totals_for_name = totals.get(name, {"old_total": 0, "new_total": 0})
        groups.append(
            ComparisonGroup(
                name=name,
                changed_count=values["changed_count"],
                old_sum=values["old_sum"],
                new_sum=values["new_sum"],
                old_total=totals_for_name["old_total"],
                new_total=totals_for_name["new_total"],
            )
        )
    return groups


def _group_totals(
    conn: sqlite3.Connection, params: Mapping[str, int], column: str
) -> Dict[str, dict]:
    """Frozen full-release totals per group for the old and new releases."""
    assert column in {"street", "housing_type"}
    group_kind = "housing_type" if column == "housing_type" else "street"
    cursor = conn.execute(
        """
        SELECT name,
               COALESCE(SUM(CASE WHEN publication_id = :old_id THEN total_weight END), 0)
                   AS old_total,
               COALESCE(SUM(CASE WHEN publication_id = :new_id THEN total_weight END), 0)
                   AS new_total
        FROM publication_group_totals
        WHERE group_kind = :group_kind
          AND publication_id IN (:old_id, :new_id)
        GROUP BY name
        """,
        {**params, "group_kind": group_kind},
    )
    return {
        row["name"]: {"old_total": row["old_total"], "new_total": row["new_total"]}
        for row in cursor
    }


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def lock_street(db_path: Path, street: str) -> LockOutcome:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, current_weight FROM households WHERE street = ? ORDER BY row_order",
                (street,),
            ).fetchall()
            if not rows:
                raise ValidationError(f"街道不存在：{street}")
            existing = conn.execute(
                "SELECT * FROM street_locks WHERE street = ?", (street,)
            ).fetchone()
            if existing:
                conn.execute("COMMIT")
                return LockOutcome(
                    street=street,
                    count=existing["household_count"],
                    total=existing["total_weight"],
                    already=True,
                )
            total = sum(row["current_weight"] for row in rows)
            conn.execute(
                "INSERT INTO street_locks (street, locked_at, household_count, total_weight) "
                "VALUES (?, ?, ?, ?)",
                (street, _now(), len(rows), total),
            )
            conn.executemany(
                "INSERT INTO locked_weights (household_id, street, weight) VALUES (?, ?, ?)",
                [(row["id"], street, row["current_weight"]) for row in rows],
            )
            conn.execute("COMMIT")
            return LockOutcome(street=street, count=len(rows), total=total, already=False)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _solver_households(rows: List[dict]) -> List[solver.Household]:
    return [
        solver.Household(
            id=row["id"],
            sample_id=row["sample_id"],
            street=row["street"],
            housing_type=row["housing_type"],
            initial_weight=row["initial_weight"],
            current_weight=row["current_weight"],
        )
        for row in rows
    ]


def _describe_conflict(
    constraints: List[solver.Constraint],
    street_targets: Mapping[str, int],
    type_targets: Mapping[str, int],
    locks: Mapping[str, dict],
) -> List[dict]:
    described: List[dict] = []
    for kind, name in constraints:
        if kind == "street":
            described.append(
                {
                    "kind": "street",
                    "name": name,
                    "label": f"街道「{name}」目标户数 = {street_targets[name]:,}",
                }
            )
        elif kind == "type":
            described.append(
                {
                    "kind": "type",
                    "name": name,
                    "label": f"住房类型「{name}」目标户数 = {type_targets[name]:,}",
                }
            )
        elif kind == "lock":
            lock = locks[name]
            described.append(
                {
                    "kind": "lock",
                    "name": name,
                    "label": (
                        f"街道「{name}」锁定（{lock['household_count']} 户样本，"
                        f"锁定合计 {lock['total_weight']:,}）"
                    ),
                }
            )
    return described


def publish(
    db_path: Path,
    street_targets: Mapping[str, int],
    type_targets: Mapping[str, int],
) -> PublishOutcome:
    """Atomically calibrate and publish; never partially writes weights."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = _households(conn)
            if not rows:
                raise ValidationError("样本为空，无法校准")
            locks = _load_locks(conn)
            unknown = set(street_targets) - {row["street"] for row in rows}
            if unknown:
                raise ValidationError("目标中出现样本里不存在的街道：" + "、".join(sorted(unknown)))
            # A locked street's weights are frozen, so its total is already
            # implied by the lock; its submitted value is ignored and only the
            # remaining streets are target conditions the statistician adjusts.
            street_targets = {
                name: value for name, value in street_targets.items() if name not in locks
            }
            streets = {row["street"] for row in rows} - set(locks)
            types = {row["housing_type"] for row in rows}
            if set(street_targets) != streets:
                raise ValidationError("街道目标与样本中的街道不一致")
            if set(type_targets) != types:
                raise ValidationError("住房类型目标与样本中的类型不一致")
            for name, value in street_targets.items():
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValidationError(f"街道「{name}」的目标户数必须是非负整数")
                if value > MAX_TARGET:
                    raise ValidationError(f"街道「{name}」的目标户数过大")
            for name, value in type_targets.items():
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValidationError(f"住房类型「{name}」的目标户数必须是非负整数")
                if value > MAX_TARGET:
                    raise ValidationError(f"住房类型「{name}」的目标户数过大")

            # Only the unlocked streets contribute target constraints; the
            # locked ones contribute their frozen per-household weights.
            lock_weights = {
                household_id: weight
                for lock in locks.values()
                for household_id, weight in lock["weights"].items()
            }
            households = _solver_households(rows)
            locked_streets = set(locks)

            result = solver.solve(
                households, street_targets, type_targets, lock_weights, locked_streets
            )
            if not result.feasible:
                constraints = solver.find_conflict(
                    households, street_targets, type_targets, lock_weights, locked_streets
                )
                conflict = (
                    _describe_conflict(constraints, street_targets, type_targets, locks)
                    if constraints
                    else []
                )
                conn.execute("ROLLBACK")
                return PublishOutcome(ok=False, conflict=conflict)

            weights = result.weights or {}
            for street, lock in locks.items():
                for household_id, locked_weight in lock["weights"].items():
                    if weights.get(household_id) != locked_weight:
                        raise RuntimeError("内部错误：求解结果偏离锁定权重")

            updated = len(weights)
            total = sum(weights.values())
            conn.executemany(
                "UPDATE households SET current_weight = ? WHERE id = ?",
                [(weight, household_id) for household_id, weight in weights.items()],
            )
            conn.execute(
                """
                INSERT INTO publications
                    (published_at, household_count, total_weight, street_targets_json,
                     type_targets_json, locked_streets_json, weights_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    updated,
                    total,
                    json.dumps(dict(street_targets), ensure_ascii=False, sort_keys=True),
                    json.dumps(dict(type_targets), ensure_ascii=False, sort_keys=True),
                    json.dumps(sorted(locked_streets), ensure_ascii=False),
                    json.dumps(weights, ensure_ascii=False, sort_keys=True),
                ),
            )
            publication_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Complete, immutable per-household snapshot of the release.
            # INSERT ... SELECT keeps the 200k rows inside SQLite.
            conn.execute(
                """
                INSERT INTO publication_weights (publication_id, household_id, weight)
                SELECT ?, id, current_weight FROM households
                """,
                (publication_id,),
            )
            for group_kind, column in (
                ("street", "street"),
                ("housing_type", "housing_type"),
            ):
                conn.execute(
                    f"""
                    INSERT INTO publication_group_totals
                        (publication_id, group_kind, name, household_count, total_weight)
                    SELECT ?, ?, {column}, COUNT(*), SUM(current_weight)
                    FROM households GROUP BY {column}
                    """,
                    (publication_id, group_kind),
                )
            conn.execute("COMMIT")
            return PublishOutcome(
                ok=True, updated=updated, total=total, publication_id=publication_id
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

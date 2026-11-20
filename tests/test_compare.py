"""Tests for release-to-release snapshot comparison."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from app import db


def write_csv(path: Path, rows, headers=("样本号", "街道", "住房类型", "初始权重")) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    sample = write_csv(
        tmp_path / "sample.csv",
        [
            ("H01", "A", "T1", 2),
            ("H02", "A", "T2", 3),
            ("H03", "B", "T1", 4),
            ("H04", "B", "T2", 1),
        ],
    )
    path = tmp_path / "calibration.sqlite3"
    db.initialize(path, sample)
    return path


def publish(database: Path, street_targets: dict, type_targets: dict) -> int:
    outcome = db.publish(database, street_targets, type_targets)
    assert outcome.ok, outcome.conflict
    return outcome.publication_id


def snapshot(database: Path, publication_id: int) -> dict:
    conn = sqlite3.connect(database)
    try:
        return dict(
            conn.execute(
                "SELECT household_id, weight FROM publication_weights "
                "WHERE publication_id = ?",
                (publication_id,),
            ).fetchall()
        )
    finally:
        conn.close()


def current(database: Path) -> dict:
    return {
        household["sample_id"]: household["current_weight"]
        for household in db.get_dashboard(database).households
    }


def test_publish_stores_complete_snapshot(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    assert len(snapshot(database, first)) == 4
    assert sum(snapshot(database, first).values()) == 14


def test_compare_lists_changed_households_with_old_and_new(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    before = snapshot(database, first)
    after = snapshot(database, second)

    result = db.compare_publications(database, first, second)

    changed_ids = {hid for hid in before if before[hid] != after[hid]}
    assert result.changed_count == len(changed_ids)
    assert result.total_households == 4
    assert {row["sample_id"] for row in result.rows} == {
        household_id_to_sample(database, hid) for hid in changed_ids
    }
    by_sample = {row["sample_id"]: row for row in result.rows}
    for hid in changed_ids:
        sample_id = household_id_to_sample(database, hid)
        assert by_sample[sample_id]["old_weight"] == before[hid]
        assert by_sample[sample_id]["new_weight"] == after[hid]

    # Rows are ordered by sample id, as required for the audit listing.
    sample_ids = [row["sample_id"] for row in result.rows]
    assert sample_ids == sorted(sample_ids)


def household_id_to_sample(database: Path, household_id: int) -> str:
    conn = sqlite3.connect(database)
    try:
        return conn.execute(
            "SELECT sample_id FROM households WHERE id = ?", (household_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_group_summaries_match_snapshots(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    result = db.compare_publications(database, first, second)

    conn = sqlite3.connect(database)
    try:
        for column, groups in (("street", result.streets), ("housing_type", result.types)):
            for group in groups:
                expected = {}
                for publication_id in (first, second):
                    expected[publication_id] = conn.execute(
                        f"""
                        SELECT COALESCE(SUM(w.weight), 0)
                        FROM publication_weights w
                        JOIN households h ON h.id = w.household_id
                        WHERE w.publication_id = ? AND h.{column} = ?
                        """,
                        (publication_id, group.name),
                    ).fetchone()[0]
                assert group.old_total == expected[first]
                assert group.new_total == expected[second]

                changed = conn.execute(
                    f"""
                    SELECT COALESCE(SUM(old.weight), 0), COALESCE(SUM(new.weight), 0),
                           COUNT(*)
                    FROM publication_weights old
                    JOIN publication_weights new
                      ON new.publication_id = ? AND new.household_id = old.household_id
                    JOIN households h ON h.id = old.household_id
                    WHERE old.publication_id = ? AND h.{column} = ?
                      AND old.weight <> new.weight
                    """,
                    (second, first, group.name),
                ).fetchone()
                assert group.changed_count == changed[2]
                assert group.old_sum == changed[0]
                assert group.new_sum == changed[1]
    finally:
        conn.close()


def test_compare_does_not_use_current_weights(database: Path):
    """Snapshots are immutable; later publishes must not alter an old diff."""
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    before = db.compare_publications(database, first, second)

    # A third release happens while the auditor is reviewing; the selected
    # comparison must not move.
    publish(database, {"A": 6, "B": 6}, {"T1": 8, "T2": 4})
    after = db.compare_publications(database, first, second)

    assert before.rows == after.rows
    assert before.changed_count == after.changed_count
    assert [
        (group.name, group.changed_count, group.old_sum, group.new_sum)
        for group in before.streets
    ] == [
        (group.name, group.changed_count, group.old_sum, group.new_sum)
        for group in after.streets
    ]


def test_compare_survives_snapshot_tampering_of_current_table(database: Path):
    """Comparison reads only frozen snapshots, never ``households.current_weight``."""
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    expected = db.compare_publications(database, first, second)

    conn = sqlite3.connect(database)
    try:
        conn.execute("UPDATE households SET current_weight = 99")
        conn.commit()
    finally:
        conn.close()

    actual = db.compare_publications(database, first, second)
    assert actual.rows == expected.rows
    assert actual.changed_count == expected.changed_count


def test_old_release_remains_queryable(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    for street_a, t2 in ((6, 4), (5, 3)):
        publish(database, {"A": street_a, "B": 6}, {"T1": 8, "T2": t2})
    result = db.compare_publications(database, first, second)
    assert result.old.id == first and result.new.id == second
    assert result.changed_count > 0


def test_unknown_publication_raises(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    with pytest.raises(db.PublicationNotFound) as excinfo:
        db.compare_publications(database, first, 999)
    assert "999" in str(excinfo.value)
    with pytest.raises(db.PublicationNotFound):
        db.compare_publications(database, 0, first)


def test_incomplete_snapshot_is_reported(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    conn = sqlite3.connect(database)
    try:
        victim = conn.execute("SELECT MIN(household_id) FROM publication_weights "
                              "WHERE publication_id = ?", (second,)).fetchone()[0]
        conn.execute(
            "DELETE FROM publication_weights WHERE publication_id = ? AND household_id = ?",
            (second, victim),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(db.SnapshotUnavailable):
        db.compare_publications(database, first, second)


def test_legacy_publication_is_backfilled(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    expected = snapshot(database, first)
    conn = sqlite3.connect(database)
    try:
        conn.execute("DELETE FROM publication_weights WHERE publication_id = ?", (first,))
        conn.execute(
            "DELETE FROM publication_group_totals WHERE publication_id = ?", (first,)
        )
        conn.commit()
    finally:
        conn.close()

    db.initialize(database, Path("unused-sample.csv"))
    assert snapshot(database, first) == expected

    conn = sqlite3.connect(database)
    try:
        totals = dict(
            conn.execute(
                "SELECT group_kind || ':' || name, total_weight "
                "FROM publication_group_totals WHERE publication_id = ?",
                (first,),
            ).fetchall()
        )
    finally:
        conn.close()
    assert totals == {"street:A": 8, "street:B": 6, "housing_type:T1": 8, "housing_type:T2": 6}

    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    result = db.compare_publications(database, first, second)
    assert result.old.id == first
    assert result.changed_count > 0
    street_a = next(group for group in result.streets if group.name == "A")
    assert (street_a.old_total, street_a.new_total) == (8, 7)


def test_missing_group_totals_are_backfilled(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "DELETE FROM publication_group_totals WHERE publication_id = ?", (first,)
        )
        conn.commit()
    finally:
        conn.close()

    db.initialize(database, Path("unused-sample.csv"))
    result = db.compare_publications(database, first, second)
    street_a = next(group for group in result.streets if group.name == "A")
    assert (street_a.old_total, street_a.new_total) == (8, 7)


def test_pagination_is_bounded_and_consistent(database: Path, tmp_path: Path):
    # Re-seed with enough households to force several pages.
    sample = write_csv(
        tmp_path / "many.csv",
        [(f"H{i:03d}", "A", "T1", 2) for i in range(120)],
    )
    path = tmp_path / "many.sqlite3"
    db.initialize(path, sample)
    first = publish(path, {"A": 120}, {"T1": 120})
    second = publish(path, {"A": 480}, {"T1": 480})

    full = db.compare_publications(path, first, second, page=1, page_size=500)
    assert full.changed_count == 120
    assert len(full.rows) == 120
    assert full.total_pages == 1

    page_one = db.compare_publications(path, first, second, page=1, page_size=10)
    page_two = db.compare_publications(path, first, second, page=2, page_size=10)
    assert len(page_one.rows) == 10
    assert page_one.total_pages == 12
    assert page_one.rows == full.rows[:10]
    assert page_two.rows == full.rows[10:20]
    assert page_one.changed_count == page_two.changed_count == full.changed_count

    clamped = db.compare_publications(path, first, second, page=10_000, page_size=10)
    assert clamped.page == clamped.total_pages == 12
    assert clamped.rows == full.rows[-10:]


def test_identical_releases_have_empty_diff(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    result = db.compare_publications(database, first, first)
    assert result.changed_count == 0
    assert result.rows == []
    assert result.total_pages == 1


def test_failed_publish_writes_no_snapshot(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    snapshot_before = snapshot(database, first)
    outcome = db.publish(database, {"A": 100, "B": 1}, {"T1": 50, "T2": 50})
    assert not outcome.ok

    conn = sqlite3.connect(database)
    try:
        publication_count = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        snapshot_rows = conn.execute(
            "SELECT COUNT(*) FROM publication_weights"
        ).fetchone()[0]
    finally:
        conn.close()
    assert publication_count == 1
    assert snapshot_rows == len(snapshot_before)
    assert snapshot(database, first) == snapshot_before
    assert current(database) == {
        h["sample_id"]: h["current_weight"]
        for h in db.get_dashboard(database).households
    }


def test_locking_still_freezes_and_is_comparable(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    db.lock_street(database, "A")
    data = db.get_dashboard(database)
    a_before = {
        h["sample_id"]: h["current_weight"] for h in data.households if h["street"] == "A"
    }
    a_t1 = sum(
        h["current_weight"]
        for h in data.households
        if h["street"] == "A" and h["housing_type"] == "T1"
    )
    a_t2 = sum(
        h["current_weight"]
        for h in data.households
        if h["street"] == "A" and h["housing_type"] == "T2"
    )
    second = publish(database, {"A": 1, "B": 3}, {"T1": a_t1 + 1, "T2": a_t2 + 2})

    result = db.compare_publications(database, first, second)
    assert all(row["street"] != "A" for row in result.rows)
    assert {
        h["sample_id"]: h["current_weight"]
        for h in db.get_dashboard(database).households
        if h["street"] == "A"
    } == a_before
    street_a = next(group for group in result.streets if group.name == "A")
    assert street_a.changed_count == 0
    assert street_a.old_total == street_a.new_total == sum(a_before.values())


def test_snapshot_write_failure_rolls_back_weights(database: Path):
    """Snapshot write shares the publish transaction: failure means no change."""
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    before = current(database)
    conn = sqlite3.connect(database)
    try:
        conn.execute("DROP TABLE publication_weights")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        db.publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})

    assert current(database) == before
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 1
    finally:
        conn.close()


def test_households_present_in_only_one_snapshot(database: Path):
    first = publish(database, {"A": 8, "B": 6}, {"T1": 8, "T2": 6})
    second = publish(database, {"A": 7, "B": 6}, {"T1": 8, "T2": 5})
    conn = sqlite3.connect(database)
    try:
        ids = [
            row[0]
            for row in conn.execute("SELECT id FROM households ORDER BY id")
        ]
        # Keep both snapshot counts intact while making the sets differ:
        # household 4 is only in the new release, household 3 only in the old.
        conn.execute(
            "DELETE FROM publication_weights WHERE publication_id = ? AND household_id = ?",
            (first, ids[3]),
        )
        conn.execute(
            "DELETE FROM publication_weights WHERE publication_id = ? AND household_id = ?",
            (second, ids[2]),
        )
        # Keep each publication's recorded counts consistent with its trimmed
        # snapshot, modelling a sample whose membership changed between two
        # releases.
        for publication_id in (first, second):
            count, total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(weight), 0) FROM publication_weights "
                "WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            conn.execute(
                "UPDATE publications SET household_count = ?, total_weight = ? WHERE id = ?",
                (count, total, publication_id),
            )
        conn.commit()
    finally:
        conn.close()

    result = db.compare_publications(database, first, second)
    assert result.added_count == 1
    assert result.removed_count == 1
    by_sample = {row["sample_id"]: row for row in result.rows}
    added = by_sample[household_id_to_sample(database, ids[3])]
    assert added["old_weight"] is None and added["new_weight"] is not None
    removed = by_sample[household_id_to_sample(database, ids[2])]
    assert removed["old_weight"] is not None and removed["new_weight"] is None

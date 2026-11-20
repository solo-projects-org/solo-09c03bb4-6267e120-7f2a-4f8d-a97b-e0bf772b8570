"""End-to-end tests for the release comparison page."""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sample = tmp_path / "sample.csv"
    with sample.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["样本号", "街道", "住房类型", "初始权重"])
        writer.writerows(
            [
                ("H01", "A", "T1", 2),
                ("H02", "A", "T2", 3),
                ("H03", "B", "T1", 4),
                ("H04", "B", "T2", 1),
            ]
        )
    monkeypatch.setenv("CALIBRATION_DB", str(tmp_path / "web.sqlite3"))
    monkeypatch.setenv("CALIBRATION_SAMPLE", str(sample))
    with TestClient(main.app) as test_client:
        yield test_client


def publish(client: TestClient, street_a: int, street_b: int, t1: int, t2: int) -> int:
    response = client.post(
        "/publish",
        data={
            "target__A": str(street_a),
            "target__B": str(street_b),
            "target__T1": str(t1),
            "target__T2": str(t2),
        },
    )
    assert response.status_code == 200, response.text
    match = re.search(r"发布记录 #(\d+)", response.text)
    assert match, response.text
    return int(match.group(1))


def test_index_links_to_compare(client: TestClient):
    page = client.get("/").text
    assert 'href="/compare"' in page
    assert "核对两次发布之间的权重变化" in page


def test_compare_page_defaults_to_latest_two(client: TestClient):
    first = publish(client, 8, 6, 8, 6)
    second = publish(client, 7, 6, 8, 5)
    response = client.get("/compare")
    assert response.status_code == 200, response.text
    assert "发布差异核对" in response.text
    assert f'value="{first}"' in response.text
    assert f'value="{second}"' in response.text
    assert "逐户权重变化" in response.text
    assert "H02" in response.text


def test_compare_page_explicit_ids(client: TestClient):
    first = publish(client, 8, 6, 8, 6)
    publish(client, 7, 6, 8, 5)
    response = client.get(f"/compare?old={first}&new={first}")
    assert response.status_code == 200
    assert "没有住户的权重发生变化" in response.text


def test_compare_page_unknown_publication(client: TestClient):
    first = publish(client, 8, 6, 8, 6)
    response = client.get(f"/compare?old={first}&new=999")
    assert response.status_code == 404
    assert "发布记录 #999 不存在" in response.text
    response = client.get("/compare?old=abc&new=1")
    assert response.status_code == 400
    assert "发布号必须是正整数" in response.text


def test_compare_page_without_publications(client: TestClient):
    response = client.get("/compare")
    assert response.status_code == 200
    assert "尚无成功发布记录" in response.text


def test_compare_page_validates_paging(client: TestClient):
    first = publish(client, 8, 6, 8, 6)
    response = client.get(f"/compare?old={first}&new={first}&page=0")
    assert response.status_code == 400
    assert "页码" in response.text
    response = client.get(f"/compare?old={first}&new={first}&page_size=100000")
    assert response.status_code == 400
    assert "每页行数" in response.text


def test_compare_page_shows_missing_snapshot(client: TestClient):
    first = publish(client, 8, 6, 8, 6)
    second = publish(client, 7, 6, 8, 5)
    import sqlite3

    conn = sqlite3.connect(main._db_path())
    try:
        conn.execute(
            "DELETE FROM publication_weights WHERE publication_id = ? AND household_id = 1",
            (second,),
        )
        conn.commit()
    finally:
        conn.close()
    response = client.get(f"/compare?old={first}&new={second}")
    assert response.status_code == 409
    assert "快照不完整" in response.text


def test_selected_comparison_is_stable_across_new_publications(client: TestClient):
    first = publish(client, 8, 6, 8, 6)
    second = publish(client, 7, 6, 8, 5)
    before = client.get(f"/compare?old={first}&new={second}").text
    publish(client, 6, 6, 8, 4)
    after = client.get(f"/compare?old={first}&new={second}").text

    def result_region(html: str) -> str:
        # Everything after the picker: totals, household rows, group sums.
        # The picker itself legitimately lists the newly created release.
        start = html.index('id="totals"')
        end = html.index("<footer>")
        return html[start:end]

    assert result_region(before) == result_region(after)
    assert "H02" in result_region(after)


def test_compare_page_does_not_break_publish_or_lock(client: TestClient):
    publish(client, 8, 6, 8, 6)
    assert client.post("/lock", data={"street": "A"}).status_code == 200
    page = client.get("/").text
    assert "已锁定" in page

"""FastAPI web application for household energy-survey weight calibration."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Mapping

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class _NoPublications(Exception):
    """Internal: no releases exist yet, so render the empty state."""


def _db_path() -> Path:
    return Path(os.environ.get("CALIBRATION_DB", str(BASE_DIR / "data" / "calibration.sqlite3")))


def _sample_path() -> Path:
    return Path(
        os.environ.get("CALIBRATION_SAMPLE", str(BASE_DIR / "data" / "sample.csv"))
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize(_db_path(), _sample_path())
    yield


app = FastAPI(title="住户能源调查权重校准", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _render(
    request: Request,
    status_code: int = 200,
    errors: List[str] | None = None,
    form: Mapping[str, str] | None = None,
    conflict: List[dict] | None = None,
    conflict_message: str | None = None,
    scroll_to: str | None = None,
) -> HTMLResponse:
    data = db.get_dashboard(_db_path())
    notices: List[str] = []
    notice = request.query_params.get("notice")
    if notice:
        notices.append(notice)
    context = {
        "request": request,
        "data": data,
        "errors": errors or [],
        "notices": notices,
        "form": dict(form or {}),
        "conflict": conflict or [],
        "conflict_message": conflict_message,
        "scroll_to": scroll_to,
    }
    return templates.TemplateResponse(request, "index.html", context, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _render(request)


def _parse_publication_id(raw: str | None, label: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if not text.isdigit():
        raise db.ValidationError(f"{label}发布号必须是正整数：{text!r}")
    value = int(text)
    if value <= 0:
        raise db.ValidationError(f"{label}发布号必须是正整数：{text!r}")
    return value


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request) -> HTMLResponse:
    query = request.query_params
    data = db.get_release_overview(_db_path())
    options = data.publication_options
    errors: List[str] = []

    # Default to the two most recent releases; a single release compares to
    # itself, which is a valid (empty) result.
    default_new = options[0].id if options else None
    default_old = options[1].id if len(options) > 1 else default_new
    old_raw = query.get("old", "" if default_old is None else str(default_old))
    new_raw = query.get("new", "" if default_new is None else str(default_new))
    page_raw = (query.get("page") or "1").strip()
    page_size_raw = (query.get("page_size") or str(db.COMPARE_PAGE_SIZE)).strip()

    if not page_raw.isdigit() or int(page_raw) < 1:
        errors.append(f"页码必须是从 1 开始的整数：{page_raw!r}")
        page = 1
    else:
        page = int(page_raw)
    if not page_size_raw.isdigit() or not (
        1 <= int(page_size_raw) <= db.COMPARE_MAX_PAGE_SIZE
    ):
        errors.append(
            f"每页行数必须是 1–{db.COMPARE_MAX_PAGE_SIZE} 的整数：{page_size_raw!r}"
        )
        page_size = db.COMPARE_PAGE_SIZE
    else:
        page_size = int(page_size_raw)

    result = None
    selected_old = old_raw
    selected_new = new_raw
    status_code = 400 if errors else 200
    try:
        old_id = _parse_publication_id(old_raw, "旧")
        new_id = _parse_publication_id(new_raw, "新")
        if old_id is None or new_id is None:
            if not options and not query.get("old") and not query.get("new"):
                # Nothing to compare yet; render the friendly empty state.
                raise _NoPublications()
            raise db.ValidationError("请选择要比较的两个发布号")
        selected_old, selected_new = str(old_id), str(new_id)
        if not errors:
            result = db.compare_publications(
                _db_path(), old_id, new_id, page=page, page_size=page_size
            )
    except _NoPublications:
        status_code = 400 if errors else 200
    except db.PublicationNotFound as exc:
        errors.append(str(exc) + "。请确认发布号后重试；旧发布只要记录仍在即可查询。")
        status_code = 404
    except db.SnapshotUnavailable as exc:
        errors.append(str(exc) + "。该发布缺少完整快照，无法核对。")
        status_code = 409
    except db.ValidationError as exc:
        errors.append(str(exc))
        status_code = 400

    context = {
        "request": request,
        "data": data,
        "result": result,
        "errors": errors,
        "selected_old": selected_old,
        "selected_new": selected_new,
        "page": result.page if result else page,
        "page_size": result.page_size if result else page_size,
        "max_page_size": db.COMPARE_MAX_PAGE_SIZE,
    }
    return templates.TemplateResponse(
        request, "compare.html", context, status_code=status_code
    )


@app.post("/lock")
def lock(
    request: Request,
    street: str = Form(default=""),
):
    try:
        outcome = db.lock_street(_db_path(), street.strip())
    except db.ValidationError as exc:
        return _render(
            request,
            status_code=400,
            errors=[str(exc)],
            form={"street": street},
            scroll_to="locks",
        )
    message = (
        f"街道「{outcome.street}」此前已锁定，沿用原锁定权重（{outcome.count} 户，合计 {outcome.total:,}）。"
        if outcome.already
        else f"已锁定街道「{outcome.street}」：{outcome.count} 户，合计 {outcome.total:,}。"
    )
    return RedirectResponse(url="/?notice=" + _quote(message) + "#locks", status_code=303)


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _parse_targets(
    form: Mapping[str, str], names: List[str], label: str
) -> Dict[str, int]:
    targets: Dict[str, int] = {}
    problems: List[str] = []
    for name in names:
        raw = (form.get(f"target__{name}") or "").strip()
        if not raw:
            problems.append(f"{label}「{name}」的目标户数未填写")
            continue
        if not raw.isdigit():
            problems.append(f"{label}「{name}」的目标户数必须是非负整数：{raw!r}")
            continue
        value = int(raw)
        if value > db.MAX_TARGET:
            problems.append(f"{label}「{name}」的目标户数过大（上限 {db.MAX_TARGET:,}）")
            continue
        targets[name] = value
    if problems:
        raise db.ValidationError("；".join(problems))
    return targets


def _form_snapshot(form: Mapping[str, str]) -> Dict[str, str]:
    return {key: value for key, value in form.items() if key.startswith("target__")}


@app.post("/publish")
async def publish(request: Request):
    form = await request.form()
    form_dict = _form_snapshot(form)
    data = db.get_dashboard(_db_path())
    # Locked streets have frozen weights and an implied total, so their form
    # value is not a user-editable condition and need not be validated.
    street_names = [street["name"] for street in data.streets if not street["locked"]]
    type_names = [group["name"] for group in data.types]
    try:
        street_targets = _parse_targets(form, street_names, "街道")
        type_targets = _parse_targets(form, type_names, "住房类型")
    except db.ValidationError as exc:
        return _render(
            request,
            status_code=400,
            errors=[str(exc)],
            form=form_dict,
            scroll_to="publish",
        )

    try:
        outcome = db.publish(_db_path(), street_targets, type_targets)
    except db.ValidationError as exc:
        return _render(
            request,
            status_code=400,
            errors=[str(exc)],
            form=form_dict,
            scroll_to="publish",
        )

    if not outcome.ok:
        return _render(
            request,
            status_code=409,
            conflict=outcome.conflict,
            conflict_message="当前目标与锁定条件无解，以下是一组极小冲突（去掉其中任意一项，其余条件即可同时满足）：",
            form=form_dict,
            scroll_to="publish",
        )

    message = (
        f"发布成功：{outcome.updated} 户已更新，现行权重合计 {outcome.total:,}"
        f"（发布记录 #{outcome.publication_id}）。"
    )
    return RedirectResponse(url="/?notice=" + _quote(message) + "#summary", status_code=303)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}

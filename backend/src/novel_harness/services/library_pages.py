"""Bounded scalar projections; never hydrate a material just to list its title."""

import base64
import binascii
import json

from fastapi import HTTPException
from sqlalchemy import String, cast, func, literal, select, tuple_, union_all

from novel_harness.db.models import StoryNode
from novel_harness.services.library import SOURCES


def validate_category(category):
    if category not in {"all", "manuscript", *SOURCES}:
        raise HTTPException(422, detail={"code": "INVALID_MATERIAL_CATEGORY"})


def projection(project_id, *, deleted=False):
    statements = []
    for kind, (model, title_field, content_field) in SOURCES.items():
        table = model.__table__
        content = cast(table.c[content_field], String)
        if kind == "canon":
            content = cast(func.json_extract(table.c[content_field], "$"), String)
        statement = select(
            table.c.id,
            literal(kind).label("type"),
            func.substr(table.c[title_field], 1, 240).label("title"),
            func.substr(content, 1, 160).label("preview"),
            table.c.revision,
            table.c.is_pinned,
            table.c.deleted_at,
            table.c.purge_after,
            (table.c.status if "status" in table.c else literal(None)).label("status"),
            (table.c.origin if "origin" in table.c else literal(None)).label("origin"),
        ).where(
            table.c.project_id == project_id,
            table.c.deleted_at.is_not(None) if deleted else table.c.deleted_at.is_(None),
        )
        if kind == "summary" and not deleted:
            nodes = StoryNode.__table__
            statement = statement.where(
                table.c.status == "valid",
                select(nodes.c.id)
                .where(
                    nodes.c.id == table.c.chapter_id,
                    nodes.c.project_id == project_id,
                    nodes.c.deleted_at.is_(None),
                )
                .exists(),
            )
        statements.append(statement)
    return union_all(*statements).subquery("materials")


def decode_cursor(cursor, scope):
    try:
        if len(cursor) > 4096:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        anchor = value["after"]
        if (
            value["scope"] != scope
            or value.get("v") != 1
            or not isinstance(anchor, list)
            or len(anchor) != 4
        ):
            raise ValueError
        if type(anchor[0]) is not int or anchor[0] not in (-1, 0):
            raise ValueError
        if not all(isinstance(part, str) for part in anchor[1:]):
            raise ValueError
        if len(anchor[1]) > 240 or anchor[2] not in SOURCES or len(anchor[3]) > 100:
            raise ValueError
        return anchor
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(422, detail={"code": "INVALID_LIBRARY_CURSOR"}) from exc


def page(
    session, project_id, *, limit=50, category="all", cursor=None, deleted=False, pinned=False
):
    validate_category(category)
    limit = min(100, max(1, limit))
    scope = [project_id, category, deleted, pinned]
    anchor = decode_cursor(cursor, scope) if cursor else None
    materials = projection(project_id, deleted=deleted)
    counts = {kind: 0 for kind in (*SOURCES, "manuscript")}
    counts.update(
        dict(
            session.execute(select(materials.c.type, func.count()).group_by(materials.c.type)).all()
        )
    )
    statement = select(materials)
    if category != "all":
        statement = statement.where(materials.c.type == category)
    if pinned:
        statement = statement.where(materials.c.is_pinned.is_(True))
    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    sort = (-materials.c.is_pinned, materials.c.title, materials.c.type, materials.c.id)
    if anchor:
        statement = statement.where(tuple_(*sort) > tuple_(*anchor))
    rows = session.execute(statement.order_by(*sort).limit(limit + 1)).mappings().all()
    items = [dict(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        value = {
            "v": 1,
            "scope": scope,
            "after": [-int(last["is_pinned"]), last["title"], last["type"], last["id"]],
        }
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(value, ensure_ascii=False).encode()
        ).decode()
    return {"items": items, "total": total, "counts": counts, "next_cursor": next_cursor}

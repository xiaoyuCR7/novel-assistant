"""Reader-facing exports use saved manuscripts, never model candidates."""

from fastapi import HTTPException

from novel_harness.db.models import ChapterDocument, ChapterVersion
from novel_harness.services.projects import require_project
from novel_harness.services.retrieval import ordered_nodes


def export_manuscript(session, project_id, command):
    project = require_project(session, project_id)
    nodes = [node for node in ordered_nodes(session) if node.project_id == project_id]
    by_id = {node.id: node for node in nodes}
    chapters = [node for node in nodes if node.kind == "chapter"]
    selected = []
    for node_id in command.node_ids:
        node = by_id.get(node_id)
        if node is None or node.kind not in {"chapter", "volume"}:
            raise HTTPException(
                404,
                detail={
                    "code": "MANUSCRIPT_NODE_NOT_FOUND",
                    "message": "所选卷章不存在或已删除，请刷新目录。",
                },
            )
        if node.kind == "chapter":
            selected.append(node.id)
        else:
            for chapter in chapters:
                parent, visited = chapter.parent_id, set()
                while parent and parent not in visited:
                    if parent == node.id:
                        selected.append(chapter.id)
                        break
                    visited.add(parent)
                    ancestor = by_id.get(parent)
                    parent = ancestor.parent_id if ancestor else None
    if not command.node_ids:
        selected = [chapter.id for chapter in chapters]
    selected = list(dict.fromkeys(selected))
    if command.order == "story":
        chosen = set(selected)
        selected = [chapter.id for chapter in chapters if chapter.id in chosen]
    if not selected:
        raise HTTPException(
            422,
            detail={"code": "MANUSCRIPT_EMPTY_SELECTION", "message": "当前范围没有可导出的章节。"},
        )

    def heading(title):
        return str(title).replace("\r", " ").replace("\n", " ").strip()

    parts = [("# " if command.format == "markdown" else "") + heading(project.title)]
    missing = []
    for chapter_id in selected:
        document = session.get(ChapterDocument, chapter_id)
        content = document.content if document else ""
        if command.source == "published":
            version = (
                session.get(ChapterVersion, document.current_version_id)
                if (document and document.current_version_id)
                else None
            )
            if (
                version is None
                or version.project_id != project_id
                or version.chapter_id != chapter_id
            ):
                missing.append({"id": chapter_id, "title": by_id[chapter_id].title})
                continue
            content = version.content
        parts.append(
            ("## " if command.format == "markdown" else "")
            + heading(by_id[chapter_id].title)
            + "\n\n"
            + content
        )
    if missing:
        raise HTTPException(
            409,
            detail={
                "code": "MANUSCRIPT_VERSION_REQUIRED",
                "message": "部分章节尚无正式版本，请先保存版本或选择工作副本。",
                "chapters": missing,
            },
        )
    return "\n\n".join(parts) + "\n"

from fastapi import APIRouter, Request, Response

from novel_harness.services.automatic_backups import BackupPolicy

router = APIRouter(prefix="/projects/{project_id}/automatic-backups", tags=["backups"])


@router.get("")
def report(project_id: str, request: Request):
    return request.app.state.automatic_backups.report(project_id)


@router.put("")
def configure(project_id: str, value: BackupPolicy, request: Request):
    return request.app.state.automatic_backups.configure(project_id, value.model_dump())


@router.post("/run")
def run(project_id: str, request: Request):
    return request.app.state.automatic_backups.run(project_id)


@router.get("/{backup_id}")
def download(project_id: str, backup_id: str, request: Request):
    data = request.app.state.automatic_backups.read(project_id, backup_id)
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="novel-backup-{backup_id}.zip"',
        },
    )

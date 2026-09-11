from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from novel_harness.api.routes.settings import same_origin
from novel_harness.services.spending import Money, SpendingSettings

router = APIRouter(
    prefix="/settings/spending", tags=["spending"], dependencies=[Depends(same_origin)]
)


@router.get("")
def get_spending(request: Request, project_id: str | None = None):
    ledger = request.app.state.model_settings.spending
    return {"settings": ledger.settings(), **ledger.report(project_id)}


@router.put("")
def put_spending(payload: SpendingSettings, request: Request):
    try:
        return request.app.state.model_settings.spending.configure(payload)
    except ValueError:
        raise HTTPException(
            409,
            detail={
                "code": "SPENDING_SETTINGS_CHANGED",
                "message": "费用设置已在其他窗口更新，请刷新后重试。",
            },
        ) from None


class Reconciliation(BaseModel):
    actual_cny: Money
    note: str = Field(min_length=1, max_length=200)


@router.post("/entries/{entry_id}/reconcile")
def reconcile(entry_id: str, payload: Reconciliation, request: Request):
    try:
        request.app.state.model_settings.spending.reconcile(
            entry_id, payload.actual_cny, payload.note
        )
    except KeyError:
        raise HTTPException(404, detail={"message": "费用记录不存在。"}) from None
    except ValueError:
        raise HTTPException(
            409, detail={"message": "请求仍在运行或已完成核对，请刷新记录。"}
        ) from None
    return {"ok": True}

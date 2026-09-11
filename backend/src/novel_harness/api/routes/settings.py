from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from novel_harness.ai.base import (
    PROVIDER_FAILURE_MESSAGES,
    AITextRequest,
    ExecutionLimits,
    ProviderError,
)
from novel_harness.services.model_profiles import (
    ApplyProfile,
    CreateProfile,
    ModelProfiles,
    RenameProfile,
)
from novel_harness.services.model_settings import (
    ModelConfigChanged,
    ModelConfigInput,
    selected_provider,
)


def same_origin(request: Request):
    origin = request.headers.get("origin")
    try:
        host = request.url.hostname
        parsed = urlsplit(origin or "")
    except ValueError:
        raise HTTPException(403, detail={"message": "请求来源无效。"}) from None
    if host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
        raise HTTPException(403, detail={"message": "创作接口仅允许本机访问。"})
    trusted_dev = origin in request.app.state.settings.cors_origin_list and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if (
        origin
        and not trusted_dev
        and (parsed.netloc != request.url.netloc or parsed.scheme != request.url.scheme)
    ):
        raise HTTPException(403, detail={"message": "创作接口不允许跨站访问。"})


router = APIRouter(prefix="/settings/model", tags=["settings"], dependencies=[Depends(same_origin)])


@router.get("")
def get_model(request: Request):
    try:
        return request.app.state.model_settings.public()
    except (ValueError, OSError):
        raise HTTPException(
            503,
            detail={
                "code": "MODEL_CONFIG_UNAVAILABLE",
                "message": "模型配置无法读取，请重试或明确重置配置。",
            },
        ) from None


@router.put("")
def put_model(payload: ModelConfigInput, request: Request):
    try:
        return request.app.state.model_settings.save(payload)
    except ModelConfigChanged as exc:
        raise HTTPException(
            409, detail={'code': 'MODEL_CONFIG_CHANGED', 'message': str(exc)}
        ) from None
    except (ValueError, ProviderError) as exc:
        raise HTTPException(422, detail={"message": str(exc)}) from None
    except OSError:
        raise HTTPException(
            503, detail={"message": "无法保存模型配置，请检查本地目录权限。"}
        ) from None


def profile_action(request, action):
    try:
        return action(ModelProfiles(request.app.state.model_settings))
    except ModelConfigChanged as exc:
        raise HTTPException(
            409, detail={'code': 'MODEL_CONFIG_CHANGED', 'message': str(exc)}
        ) from None
    except (ValueError, ProviderError):
        raise HTTPException(422, detail={'code': 'MODEL_PROFILE_INVALID',
            'message': '模型方案配置不可用，请检查当前模型参数后重新保存方案。'}) from None
    except OSError:
        raise HTTPException(503, detail={'code': 'MODEL_PROFILE_WRITE_FAILED',
            'message': '无法保存模型方案，请检查本机目录权限后重试。'}) from None


@router.get('/profiles')
def list_profiles(request: Request):
    return profile_action(request, lambda profiles: profiles.list())


@router.post('/profiles', status_code=201)
def create_profile(payload: CreateProfile, request: Request):
    return profile_action(request, lambda profiles: profiles.create(payload))


@router.patch('/profiles/{profile_id}')
def rename_profile(profile_id: str, payload: RenameProfile, request: Request):
    return profile_action(request, lambda profiles: profiles.rename(profile_id, payload))


@router.delete('/profiles/{profile_id}')
def delete_profile(profile_id: str, request: Request, expected_revision: int = Query(ge=1)):
    return profile_action(request, lambda profiles: profiles.delete(profile_id, expected_revision))


@router.post('/profiles/{profile_id}/apply')
def apply_profile(profile_id: str, payload: ApplyProfile, request: Request):
    return profile_action(request, lambda profiles: profiles.apply(profile_id, payload))


@router.post("/test")
def test_model(request: Request):
    try:
        result = selected_provider(request).generate_text(
            AITextRequest(
                **ExecutionLimits.model_validate(request.app.state.model_settings.identity()).model_dump(),
                task="connection_test",
                developer_instruction="Reply briefly.",
                user_prompt="Reply OK.",
            )
        )
        return {"ok": True, "provider": result.provider, "model": result.model}
    except ProviderError as exc:
        code = getattr(exc, "code", "PROVIDER_ERROR")
        raise HTTPException(
            502, detail={"code": code, "message": PROVIDER_FAILURE_MESSAGES.get(
                code, "连接测试失败，请检查 API 地址、密钥和模型。"
            )}
        ) from None

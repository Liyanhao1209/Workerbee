"""基础助手端点（AI-01）：线程、消息、整理前文、配置。

认证由 app.py 的中间件统一覆盖（loopback + token），本模块不写任何鉴权代码。
路由只做参数解析、调用 ``services.assistant``、返回已声明的响应模型。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["assistant"])


@router.post(
    "/api/assistant/threads",
    response_model=S.AssistantThreadResponse,
    summary="新建助手对话",
)
async def create_thread(
    services: ServicesDep, req: S.AssistantThreadCreateRequest | None = None
) -> S.AssistantThreadResponse:
    return await services.assistant.create_thread(req or S.AssistantThreadCreateRequest())


@router.get(
    "/api/assistant/threads",
    response_model=S.AssistantThreadListResponse,
    summary="列出助手对话",
)
async def list_threads(services: ServicesDep) -> S.AssistantThreadListResponse:
    return await services.assistant.list_threads()


@router.patch(
    "/api/assistant/threads/{thread_id}",
    response_model=S.AssistantThreadResponse,
    summary="重命名助手对话",
)
async def rename_thread(
    thread_id: str, req: S.AssistantThreadRenameRequest, services: ServicesDep
) -> S.AssistantThreadResponse:
    return await services.assistant.rename_thread(thread_id, req)


@router.get(
    "/api/assistant/threads/{thread_id}/messages",
    response_model=S.AssistantMessageListResponse,
    summary="读取对话历史（重启后原样读回）",
)
async def list_messages(
    thread_id: str, services: ServicesDep
) -> S.AssistantMessageListResponse:
    return await services.assistant.list_messages(thread_id)


@router.post(
    "/api/assistant/threads/{thread_id}/messages",
    response_model=S.AssistantSendResponse,
    summary="发消息（同步返回助手回复）",
)
async def send_message(
    thread_id: str, req: S.AssistantSendRequest, services: ServicesDep
) -> S.AssistantSendResponse:
    """一问一答同步返回完整回复；生成期间的增量经 WS 推 ``assistant_chunk``。"""
    return await services.assistant.send_message(thread_id, req)


@router.post(
    "/api/assistant/threads/{thread_id}/compact",
    response_model=S.AssistantCompactResponse,
    summary="手动整理前文（会额外调用一次模型）",
)
async def compact(
    thread_id: str, services: ServicesDep
) -> S.AssistantCompactResponse:
    return await services.assistant.compact(thread_id)


@router.post(
    "/api/assistant/drafts/{draft_id}/adopt",
    response_model=S.AssistantDraftResponse,
    summary="采用草稿提案（存为草稿修订，不自动发布）",
)
async def adopt_draft(draft_id: str, services: ServicesDep) -> S.AssistantDraftResponse:
    """采用走与手动建图相同的服务层入口；publish 恒为 False，不接受 publish 参数。"""
    return await services.assistant.adopt_draft(draft_id)


@router.post(
    "/api/assistant/drafts/{draft_id}/reject",
    response_model=S.AssistantDraftResponse,
    summary="拒绝草稿提案（留痕，不产生任何实体）",
)
async def reject_draft(draft_id: str, services: ServicesDep) -> S.AssistantDraftResponse:
    return await services.assistant.reject_draft(draft_id)


@router.get(
    "/api/assistant/config",
    response_model=S.AssistantConfigResponse,
    summary="读取助手配置（只含引用，密钥只进不出）",
)
async def get_config(services: ServicesDep) -> S.AssistantConfigResponse:
    return await services.assistant.get_config()


@router.put(
    "/api/assistant/config",
    response_model=S.AssistantConfigResponse,
    summary="更新助手配置",
)
async def put_config(
    services: ServicesDep, req: S.AssistantConfigUpdateRequest
) -> S.AssistantConfigResponse:
    return await services.assistant.update_config(req)

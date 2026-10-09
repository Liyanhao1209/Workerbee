"""Web Chat 端点（v0.03 §5）：会话 CRUD、消息读取与发送。

认证由 app.py 的中间件统一覆盖（loopback + token），本模块不写任何鉴权代码。
路由只做参数解析、调用 ``services.chat``、返回已声明的响应模型。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["chat"])


@router.post(
    "/api/chat/sessions",
    response_model=S.ChatSessionResponse,
    summary="新建对话会话",
)
async def create_session(
    services: ServicesDep, req: S.ChatSessionCreateRequest | None = None
) -> S.ChatSessionResponse:
    return await services.chat.create_session(req or S.ChatSessionCreateRequest())


@router.get(
    "/api/chat/sessions",
    response_model=S.ChatSessionListResponse,
    summary="列出对话会话（可按工作区过滤）",
)
async def list_sessions(
    services: ServicesDep, workspace_id: str | None = Query(default=None)
) -> S.ChatSessionListResponse:
    return await services.chat.list_sessions(workspace_id)


@router.get(
    "/api/chat/sessions/{session_id}",
    response_model=S.ChatSessionResponse,
    summary="读取单个会话",
)
async def get_session(session_id: str, services: ServicesDep) -> S.ChatSessionResponse:
    return await services.chat.get_session(session_id)


@router.patch(
    "/api/chat/sessions/{session_id}",
    response_model=S.ChatSessionResponse,
    summary="重命名对话会话",
)
async def rename_session(
    session_id: str, req: S.ChatSessionRenameRequest, services: ServicesDep
) -> S.ChatSessionResponse:
    return await services.chat.rename_session(session_id, req)


@router.delete(
    "/api/chat/sessions/{session_id}",
    response_model=S.ChatSessionDeleteResponse,
    summary="删除对话会话（含全部消息节点）",
)
async def delete_session(
    session_id: str, services: ServicesDep
) -> S.ChatSessionDeleteResponse:
    return await services.chat.delete_session(session_id)


@router.get(
    "/api/chat/sessions/{session_id}/messages",
    response_model=S.ChatMessageListResponse,
    summary="读取当前分支的消息序列（重启后原样读回）",
)
async def list_messages(
    session_id: str,
    services: ServicesDep,
    leaf_id: str | None = Query(default=None),
) -> S.ChatMessageListResponse:
    return await services.chat.list_messages(session_id, leaf_id)


@router.post(
    "/api/chat/sessions/{session_id}/messages",
    response_model=S.ChatSendResponse,
    summary="发消息（同步返回最终回复；增量经 WS 推 chat_chunk）",
)
async def send_message(
    session_id: str, req: S.ChatSendRequest, services: ServicesDep
) -> S.ChatSendResponse:
    """一轮问答同步返回；工具往返与等待审批期间经 WS 推 ``chat_status``。"""
    return await services.chat.send_message(session_id, req)

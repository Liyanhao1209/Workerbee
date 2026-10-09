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


@router.get(
    "/api/chat/sessions/{session_id}/tree",
    response_model=S.ChatTreeResponse,
    summary="读取会话的完整节点森林（含软删节点，前端自组树）",
)
async def get_tree(session_id: str, services: ServicesDep) -> S.ChatTreeResponse:
    return await services.chat.get_tree(session_id)


@router.post(
    "/api/chat/sessions/{session_id}/purge_deleted",
    response_model=S.ChatPurgeResponse,
    summary="清空会话内全部软删消息（硬删，不可恢复）",
)
async def purge_deleted(session_id: str, services: ServicesDep) -> S.ChatPurgeResponse:
    return await services.chat.purge_deleted(session_id)


@router.post(
    "/api/chat/nodes/{node_id}/fork",
    response_model=S.ChatForkResponse,
    summary="以该节点为分叉点，返回新分支的上下文（实际分叉发生在下一次带 parent_id 的发送）",
)
async def fork_node(node_id: str, services: ServicesDep) -> S.ChatForkResponse:
    return await services.chat.fork_node(node_id)


@router.delete(
    "/api/chat/nodes/{node_id}",
    response_model=S.ChatNodeDeleteResponse,
    summary="级联软删除该节点及其全部后代（D-F；清空前可恢复）",
)
async def delete_node(node_id: str, services: ServicesDep) -> S.ChatNodeDeleteResponse:
    return await services.chat.delete_node(node_id)


@router.post(
    "/api/chat/nodes/{node_id}/move",
    response_model=S.ChatNodeMoveResponse,
    summary="移动/合并子树到新父节点（目标在自己或后代下时 409）",
)
async def move_node(
    node_id: str, req: S.ChatNodeMoveRequest, services: ServicesDep
) -> S.ChatNodeMoveResponse:
    return await services.chat.move_node(node_id, req)


@router.post(
    "/api/chat/nodes/{node_id}/restore",
    response_model=S.ChatNodeRestoreResponse,
    summary="恢复软删子树（按删除批次还原；清空后不可恢复）",
)
async def restore_node(node_id: str, services: ServicesDep) -> S.ChatNodeRestoreResponse:
    return await services.chat.restore_node(node_id)


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


@router.put(
    "/api/chat/sessions/{session_id}/grants",
    response_model=S.ChatSessionResponse,
    summary="授予本会话某类操作的临时授权（本会话不再逐次询问，D-G）",
)
async def grant_session(
    session_id: str, req: S.ChatGrantRequest, services: ServicesDep
) -> S.ChatSessionResponse:
    return await services.chat.grant_session(session_id, req)


@router.delete(
    "/api/chat/sessions/{session_id}/grants/{category}",
    response_model=S.ChatSessionResponse,
    summary="撤销本会话某类操作的临时授权（恢复逐次审批）",
)
async def revoke_session_grant(
    session_id: str, category: str, services: ServicesDep
) -> S.ChatSessionResponse:
    return await services.chat.revoke_session_grant(session_id, category)

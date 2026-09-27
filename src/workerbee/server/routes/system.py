"""系统类端点：健康、状态、事件增量、「需处理」、存储、会话台账。

对应需求：OBS-01/02/05、RES-03、REC-03。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from .. import schemas as S
from ..deps import ServicesDep, allow_remote

router = APIRouter(tags=["system"])


@router.get(
    "/api/health",
    response_model=S.HealthResponse,
    summary="存活探针（免鉴权）",
)
async def health(services: ServicesDep) -> S.HealthResponse:
    return await services.system.health()


@router.get(
    "/api/system/status",
    response_model=S.SystemStatusResponse,
    summary="系统状态（OBS-01）",
)
async def status(
    services: ServicesDep,
    remote_ok: Annotated[bool, Depends(allow_remote)],
) -> S.SystemStatusResponse:
    return await services.system.status(allow_remote=remote_ok)


@router.get(
    "/api/system/events",
    response_model=S.EventPage,
    summary="事件增量（OBS-02）",
)
async def events(
    services: ServicesDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    task_id: str | None = None,
) -> S.EventPage:
    return await services.system.events(after_id=after_id, limit=limit, task_id=task_id)


@router.get(
    "/api/attention",
    response_model=S.AttentionResponse,
    summary="需处理（OBS-05）",
)
async def attention(services: ServicesDep) -> S.AttentionResponse:
    return await services.system.attention()


@router.get(
    "/api/sessions",
    response_model=S.SessionListResponse,
    summary="会话台账（REC-03，只读）",
)
async def sessions(services: ServicesDep) -> S.SessionListResponse:
    return await services.system.sessions()


@router.get(
    "/api/sessions/{session_ref}/attach",
    response_model=S.SessionAttachResponse,
    summary="接入运行中的会话（HUM-02）",
)
async def session_attach(
    session_ref: str, services: ServicesDep
) -> S.SessionAttachResponse:
    """取该会话已累积的输出，以及对它可做哪些操作。

    只对**此刻在运行**的会话有效。已结束的尝试没有可接入的对象，返回的
    ``reason`` 会说明这一点并指向任务详情页。
    """
    return await services.system.attach(session_ref)


@router.post(
    "/api/sessions/{session_ref}/input",
    response_model=S.SessionInputResponse,
    summary="向运行中的会话注入输入（HUM-01）",
)
async def session_input(
    session_ref: str, req: S.SessionInputRequest, services: ServicesDep
) -> S.SessionInputResponse:
    """把一条消息投给正在跑的 agent。

    只投**首轮之后**的消息。首轮输入若已在建会话时交付，重复投递会让同一条
    指令执行两遍。
    """
    return await services.system.send_input(session_ref, req)


@router.get(
    "/api/storage",
    response_model=S.StorageReportResponse,
    summary="存储占用（RES-03）",
)
async def storage(services: ServicesDep) -> S.StorageReportResponse:
    return await services.system.storage()


@router.post(
    "/api/storage/prune",
    response_model=S.PruneResponse,
    summary="手动清理（RES-03，默认 dry-run）",
)
async def prune(services: ServicesDep, req: S.PruneRequest) -> S.PruneResponse:
    return await services.system.prune(req)

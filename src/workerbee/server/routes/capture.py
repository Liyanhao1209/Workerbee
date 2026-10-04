"""流程捕获端点（Graph Capture，WF-03）：捕获任务、草案生成、采用/拒绝。

认证由 app.py 的中间件统一覆盖（loopback + token），本模块不写任何鉴权代码。
路由只做参数解析、调用 ``services.capture``、返回已声明的响应模型。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["capture"])


@router.post(
    "/api/capture/runs",
    response_model=S.CaptureRunResponse,
    summary="新建捕获任务（选基础候选，真实执行一次）",
)
async def create_run(
    services: ServicesDep, req: S.CaptureRunCreateRequest
) -> S.CaptureRunResponse:
    return await services.capture.create_run(req)


@router.post(
    "/api/capture/runs/from_task",
    response_model=S.CaptureRunResponse,
    summary="从既有任务补捕获（不重新执行；任务没有执行记录返回 400）",
)
async def create_run_from_task(
    services: ServicesDep, req: S.CaptureRunFromTaskRequest
) -> S.CaptureRunResponse:
    return await services.capture.create_run_from_task(req)


@router.get(
    "/api/capture/runs",
    response_model=S.CaptureRunListResponse,
    summary="捕获任务列表",
)
async def list_runs(services: ServicesDep) -> S.CaptureRunListResponse:
    return await services.capture.list_runs()


@router.get(
    "/api/capture/runs/{run_id}",
    response_model=S.CaptureRunDetailResponse,
    summary="捕获记录详情（含任务状态与材料汇编摘要）",
)
async def get_run(services: ServicesDep, run_id: str) -> S.CaptureRunDetailResponse:
    return await services.capture.get_run(run_id)


@router.post(
    "/api/capture/runs/{run_id}/drafts",
    response_model=S.CaptureDraftResponse,
    summary="生成流程草案（显式触发的一次模型调用；任务跑完才可用）",
)
async def generate_draft(services: ServicesDep, run_id: str) -> S.CaptureDraftResponse:
    return await services.capture.generate_draft(run_id)


@router.get(
    "/api/capture/drafts/{draft_id}",
    response_model=S.CaptureDraftResponse,
    summary="捕获草案详情",
)
async def get_draft(services: ServicesDep, draft_id: str) -> S.CaptureDraftResponse:
    return await services.capture.get_draft(draft_id)


@router.post(
    "/api/capture/drafts/{draft_id}/adopt",
    response_model=S.CaptureDraftResponse,
    summary="采用捕获草案（存为草稿修订，不自动发布；as_template 存为模板）",
)
async def adopt_draft(
    services: ServicesDep,
    draft_id: str,
    req: Annotated[S.CaptureDraftAdoptRequest | None, Body()] = None,
) -> S.CaptureDraftResponse:
    """采用走与手动建图相同的服务层入口；publish 恒为 False，不接受 publish 参数。"""
    return await services.capture.adopt_draft(draft_id, req)


@router.post(
    "/api/capture/drafts/{draft_id}/reject",
    response_model=S.CaptureDraftResponse,
    summary="拒绝捕获草案（留痕，不产生任何实体）",
)
async def reject_draft(services: ServicesDep, draft_id: str) -> S.CaptureDraftResponse:
    return await services.capture.reject_draft(draft_id)

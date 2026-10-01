"""Workflow 定义与修订（WF-05/06、D-02）以及节点启停（ACT-02、D-01）。"""

from __future__ import annotations

from typing import Annotated, Union

from fastapi import APIRouter, Body, Query

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["workflows"])

#: 启停端点有两种返回形状：预览（不改状态）与执行。同一个路径由 ``preview`` 区分。
ToggleResult = Union[S.TogglePreviewResponse, S.NodeToggleResponse]


@router.get("/api/workflows", response_model=S.WorkflowListResponse, summary="流程列表")
async def list_workflows(
    services: ServicesDep,
    include_deleted: bool = False,
    include_capture: bool = False,
) -> S.WorkflowListResponse:
    """捕获专用的临时 Workflow 默认不出现在列表里；``include_capture=true`` 可见。"""
    return await services.workflows.list_workflows(
        include_deleted=include_deleted, include_capture=include_capture
    )


@router.post("/api/workflows", response_model=S.WorkflowResponse, summary="新建流程")
async def create_workflow(
    services: ServicesDep, req: S.WorkflowCreateRequest
) -> S.WorkflowResponse:
    return await services.workflows.create_workflow(req)


@router.get(
    "/api/workflows/{workflow_id}", response_model=S.WorkflowResponse, summary="流程详情"
)
async def get_workflow(services: ServicesDep, workflow_id: str) -> S.WorkflowResponse:
    return await services.workflows.get_workflow(workflow_id)


@router.patch(
    "/api/workflows/{workflow_id}", response_model=S.WorkflowResponse, summary="更新流程定义"
)
async def patch_workflow(
    services: ServicesDep, workflow_id: str, req: S.WorkflowPatchRequest
) -> S.WorkflowResponse:
    return await services.workflows.patch_workflow(workflow_id, req)


@router.delete(
    "/api/workflows/{workflow_id}",
    response_model=S.WorkflowDeleteResponse,
    summary="删除流程（LIFE-05，三态结论）",
)
async def delete_workflow(services: ServicesDep, workflow_id: str) -> S.WorkflowDeleteResponse:
    return await services.workflows.delete_workflow(workflow_id)


# ---- 修订 ----


@router.get(
    "/api/workflows/{workflow_id}/revisions",
    response_model=S.RevisionListResponse,
    summary="修订列表",
)
async def list_revisions(
    services: ServicesDep,
    workflow_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> S.RevisionListResponse:
    return await services.workflows.list_revisions(workflow_id, limit=limit)


@router.post(
    "/api/workflows/{workflow_id}/revisions",
    response_model=S.RevisionSaveResponse,
    responses={409: {"model": S.ConflictResponse, "description": "CAS 冲突（D-02）"}},
    summary="保存修订（修订不可变；base_revision_seq 为乐观并发 CAS）",
)
async def save_revision(
    services: ServicesDep, workflow_id: str, req: S.RevisionSaveRequest
) -> S.RevisionSaveResponse:
    return await services.workflows.save_revision(workflow_id, req)


@router.get(
    "/api/workflows/{workflow_id}/revisions/{revision_seq}",
    response_model=S.RevisionResponse,
    summary="指定修订",
)
async def get_revision(
    services: ServicesDep, workflow_id: str, revision_seq: int
) -> S.RevisionResponse:
    return await services.workflows.get_revision(workflow_id, revision_seq)


# ---- 校验与启停 ----


@router.post(
    "/api/workflows/{workflow_id}/validate",
    response_model=S.ValidationReport,
    openapi_extra={"x-api-note": "诊断项带 node_id / edge / slot，可直接定位（WF-05）"},
    summary="校验定义图（WF-05）",
)
async def validate_graph(
    services: ServicesDep,
    workflow_id: str,
    req: Annotated[S.ValidateRequest | None, Body()] = None,
) -> S.ValidationReport:
    return await services.workflows.validate_graph(workflow_id, req)


@router.post(
    "/api/workflows/{workflow_id}/nodes/{node_id}/toggle",
    response_model=ToggleResult,
    summary="节点启停：preview=true 只预览（ACT-02），preview=false 才执行（D-01）",
    description=(
        "`preview=true`（默认）返回拓扑差异、按拟议状态校验的报告与受影响任务，"
        "**不改变任何状态**；`preview=false` 需带 `{enable, mode}`，"
        "`mode=drain` 排水（默认，不丢弃已完成工作）/`immediate` 立即撤回。"
        "两种形状由返回字段区分：预览含 `delta`，执行含 `applied`。"
    ),
)
async def toggle_node(
    services: ServicesDep,
    workflow_id: str,
    node_id: str,
    preview: bool = True,
    req: Annotated[S.ToggleRequest | None, Body()] = None,
) -> ToggleResult:
    if preview:
        return await services.workflows.toggle_preview(workflow_id, node_id)
    return await services.workflows.toggle_node(workflow_id, node_id, req)

"""工作区（v0.03 §3、D-B）：组织与删除安全边界。

归档不硬删数据——流程与历史保留，只是冻结新发射（409）并移出资源清理边界；
删除前必须把流程迁走，仍被占用的工作区返回 409。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["workspaces"])


@router.get("/api/workspaces", response_model=S.WorkspaceListResponse, summary="工作区列表")
async def list_workspaces(services: ServicesDep) -> S.WorkspaceListResponse:
    """含已归档的工作区（前端要展示归档标注）；``current_workspace_id``
    是最近一次 serve 启动匹配/注册的工作区，供前端首次打开时定位。"""
    return await services.workspaces.list_workspaces()


@router.post(
    "/api/workspaces",
    response_model=S.WorkspaceResponse,
    responses={409: {"model": S.ErrorResponse, "description": "根目录已被注册"}},
    summary="注册工作区",
)
async def create_workspace(
    services: ServicesDep, req: S.WorkspaceCreateRequest
) -> S.WorkspaceResponse:
    return await services.workspaces.create_workspace(req)


@router.get(
    "/api/workspaces/{workspace_id}",
    response_model=S.WorkspaceResponse,
    summary="工作区详情",
)
async def get_workspace(services: ServicesDep, workspace_id: str) -> S.WorkspaceResponse:
    return await services.workspaces.get_workspace(workspace_id)


@router.patch(
    "/api/workspaces/{workspace_id}",
    response_model=S.WorkspaceResponse,
    summary="更新工作区（改名 / 归档 / 取消归档）",
)
async def patch_workspace(
    services: ServicesDep, workspace_id: str, req: S.WorkspacePatchRequest
) -> S.WorkspaceResponse:
    return await services.workspaces.patch_workspace(workspace_id, req)


@router.post(
    "/api/workspaces/{workspace_id}/archive",
    response_model=S.WorkspaceResponse,
    summary="归档工作区（冻结新发射，不删任何数据）",
)
async def archive_workspace(services: ServicesDep, workspace_id: str) -> S.WorkspaceResponse:
    return await services.workspaces.archive_workspace(workspace_id)


@router.delete(
    "/api/workspaces/{workspace_id}",
    response_model=S.WorkspaceDeleteResponse,
    responses={409: {"model": S.ErrorResponse, "description": "工作区下仍有流程，需先迁移"}},
    summary="删除工作区（先迁移流程，再删除）",
)
async def delete_workspace(
    services: ServicesDep, workspace_id: str
) -> S.WorkspaceDeleteResponse:
    return await services.workspaces.delete_workspace(workspace_id)

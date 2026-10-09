"""工作区文件系统端点（v0.03 §5.4）：目录列举、读、写、建目录、移动、删除。

全部路径先过 confinement（``workerbee.chat.fs``，与 chat 工具循环同一份
边界实现）；越界与敏感文件返回 403，乐观并发冲突返回 409。
用户在界面上发起的写操作不再过审批——点按钮本身就是批准；
审批只约束模型经工具循环发起的写。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["fs"])


@router.get(
    "/api/fs/list",
    response_model=S.FsListResponse,
    summary="列一层目录（懒加载，不递归）",
)
async def list_dir(
    services: ServicesDep,
    path: str = Query(default=""),
    workspace_id: str | None = Query(default=None),
) -> S.FsListResponse:
    return await services.fs.list_dir(path, workspace_id)


@router.get(
    "/api/fs/read",
    response_model=S.FsReadResponse,
    summary="读文本文件（默认截断 100KB 并如实标注；二进制拒读）",
)
async def read_file(
    services: ServicesDep,
    path: str = Query(...),
    workspace_id: str | None = Query(default=None),
    max_bytes: int | None = Query(default=None),
) -> S.FsReadResponse:
    return await services.fs.read_file(path, workspace_id, max_bytes)


@router.put(
    "/api/fs/write",
    response_model=S.FsWriteResponse,
    summary="写文件（覆盖已存在文件须带 expected_mtime，否则 409）",
)
async def write_file(req: S.FsWriteRequest, services: ServicesDep) -> S.FsWriteResponse:
    return await services.fs.write_file(req)


@router.post(
    "/api/fs/mkdir",
    response_model=S.FsOpResponse,
    summary="建目录（幂等：已存在不报错）",
)
async def make_dir(req: S.FsMkdirRequest, services: ServicesDep) -> S.FsOpResponse:
    return await services.fs.make_dir(req)


@router.post(
    "/api/fs/move",
    response_model=S.FsOpResponse,
    summary="移动/改名（目标已存在则拒绝，不覆盖）",
)
async def move_entry(req: S.FsMoveRequest, services: ServicesDep) -> S.FsOpResponse:
    return await services.fs.move_entry(req)


@router.delete(
    "/api/fs/delete",
    response_model=S.FsOpResponse,
    summary="删除文件或空目录（不提供递归删除）",
)
async def delete_entry(req: S.FsDeleteRequest, services: ServicesDep) -> S.FsOpResponse:
    return await services.fs.delete_entry(req)

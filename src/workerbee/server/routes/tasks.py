"""任务：发射、查询与控制（RUN-02、LIFE-01–06、§11.3）。

控制类响应的形状由 ``TriStateOutcome`` 固定：**已接受／执行已停止／资源清理完成**
三个结论并列，绝不合成一个布尔值（LIFE-06）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from .. import schemas as S
from ..deps import ServicesDep
from ...core.domain import TaskState

router = APIRouter(tags=["tasks"])


@router.post(
    "/api/workflows/{workflow_id}/tasks",
    response_model=S.SubmitResponse,
    status_code=202,
    responses={
        422: {
            "model": S.SubmitResponse,
            "description": "内容不可执行：响应体是完整校验报告（WF-05）",
        }
    },
    summary="发射任务（校验失败返回 422 + 完整报告）",
)
async def submit_task(
    services: ServicesDep, workflow_id: str, req: S.TaskSubmitRequest
) -> S.SubmitResponse:
    return await services.tasks.submit(workflow_id, req)


@router.get("/api/tasks", response_model=S.TaskListResponse, summary="任务列表")
async def list_tasks(
    services: ServicesDep,
    workflow_id: str | None = None,
    state: TaskState | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> S.TaskListResponse:
    return await services.tasks.list_tasks(
        workflow_id=workflow_id, state=state, limit=limit, offset=offset
    )


@router.get(
    "/api/tasks/{task_id}", response_model=S.TaskDetailResponse, summary="任务详情（OBS-03）"
)
async def task_detail(services: ServicesDep, task_id: str) -> S.TaskDetailResponse:
    return await services.tasks.detail(task_id)


@router.post(
    "/api/tasks/{task_id}/pause",
    response_model=S.PauseResponse,
    summary="暂停任务（LIFE-01/02，三态结论）",
)
async def pause_task(
    services: ServicesDep, task_id: str, req: S.PauseRequest | None = None
) -> S.PauseResponse:
    return await services.tasks.pause(task_id, req)


@router.post(
    "/api/tasks/{task_id}/resume",
    response_model=S.ResumeResponse,
    summary="恢复任务（LIFE-03，不重跑已成功阶段）",
)
async def resume_task(
    services: ServicesDep, task_id: str, req: S.ResumeRequest | None = None
) -> S.ResumeResponse:
    return await services.tasks.resume(task_id, req)


@router.delete(
    "/api/tasks/{task_id}",
    response_model=S.DeleteTaskResponse,
    summary="删除任务（LIFE-04，保留已成功阶段的真实结果）",
)
async def delete_task(
    services: ServicesDep, task_id: str, req: S.DeleteTaskRequest | None = None
) -> S.DeleteTaskResponse:
    return await services.tasks.delete(task_id, req)


@router.post(
    "/api/tasks/{task_id}/stages/{node_id}/resume",
    response_model=S.StageResumeResponse,
    summary="从某阶段断点续跑（§11.3）",
)
async def resume_stage(
    services: ServicesDep, task_id: str, node_id: str
) -> S.StageResumeResponse:
    return await services.tasks.resume_stage(task_id, node_id)


@router.get(
    "/api/tasks/{task_id}/events", response_model=S.EventPage, summary="任务事件流（OBS-03）"
)
async def task_events(
    services: ServicesDep,
    task_id: str,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> S.EventPage:
    return await services.tasks.events(task_id, after_id=after_id, limit=limit)


@router.get(
    "/api/tasks/{task_id}/attempts/{attempt_id}/work",
    response_model=S.AttemptWorkResponse,
    summary="单次执行尝试的工作细节：输入、推理、工具调用、涉及的文件",
)
async def attempt_work(
    services: ServicesDep, task_id: str, attempt_id: str
) -> S.AttemptWorkResponse:
    return await services.tasks.attempt_work(task_id, attempt_id)


@router.get(
    "/api/tasks/{task_id}/artifacts/{artifact_id}/content",
    response_model=S.ArtifactContentResponse,
    summary="产物正文（有界、已脱敏）",
)
async def artifact_content(
    services: ServicesDep, task_id: str, artifact_id: str
) -> S.ArtifactContentResponse:
    return await services.tasks.artifact_content(task_id, artifact_id)

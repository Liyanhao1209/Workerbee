"""审批（§5.5、HUM-03/04、AC-12/14）。

决定端点**幂等**：重复提交不报错，返回的是「实际生效结果」——
用户看到的是真相，而不是第二次点击时的一个 500。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["approvals"])


@router.get(
    "/api/approvals",
    response_model=S.ApprovalListResponse,
    summary="待处理审批（含 undeliverable：决定已产生但未送达）",
)
async def list_approvals(services: ServicesDep) -> S.ApprovalListResponse:
    return await services.approvals.list_open()


@router.get(
    "/api/tasks/{task_id}/approvals",
    response_model=S.ApprovalListResponse,
    summary="某次任务的审批（含历史决定）",
)
async def list_task_approvals(
    services: ServicesDep, task_id: str
) -> S.ApprovalListResponse:
    return await services.approvals.list_for_task(task_id)


@router.post(
    "/api/approvals/{approval_id}/decide",
    response_model=S.DeliveryResponse,
    summary="做出决定并回注（重复决定返回实际生效结果，不报错）",
)
async def decide_approval(
    services: ServicesDep, approval_id: str, req: S.ApprovalDecideRequest
) -> S.DeliveryResponse:
    return await services.approvals.decide(approval_id, req)


@router.post(
    "/api/approvals/{approval_id}/retry-delivery",
    response_model=S.DeliveryResponse,
    summary="重试回注（HUM-04：沿用原决定，不重新征求同意）",
)
async def retry_delivery(services: ServicesDep, approval_id: str) -> S.DeliveryResponse:
    return await services.approvals.retry_delivery(approval_id)

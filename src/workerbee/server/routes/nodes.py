"""节点队列投影与调序（RUN-04、AC-03）。"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["nodes"])


@router.get(
    "/api/nodes/{node_id}/queue",
    response_model=S.NodeQueueResponse,
    summary="节点队列（RUN-04，只读投影）",
)
async def node_queue(services: ServicesDep, node_id: str) -> S.NodeQueueResponse:
    return await services.nodes.queue(node_id)


@router.post(
    "/api/nodes/{node_id}/reorder",
    response_model=S.ReorderResponse,
    summary="调整节点队列顺序（AC-03：返回实际生效顺序）",
)
async def reorder_node(
    services: ServicesDep, node_id: str, req: S.ReorderRequest
) -> S.ReorderResponse:
    return await services.nodes.reorder(node_id, req)

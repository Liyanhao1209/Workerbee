"""模板（TPL-01/02/03）。

模板**结构上装不下凭据**：只有 ``sensitive_slots`` 占位。实例化时未绑定的槽位
如实进入 ``report.missing_bindings``，绝不编造一个凭据顶上（WF-02 的同一条纪律）。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schemas as S
from ..deps import ServicesDep
from ...core.domain import TemplateKind

router = APIRouter(tags=["templates"])


@router.get("/api/templates", response_model=S.TemplateListResponse, summary="模板列表")
async def list_templates(
    services: ServicesDep, kind: TemplateKind | None = None
) -> S.TemplateListResponse:
    return await services.templates.list_templates(kind=kind)


@router.post("/api/templates", response_model=S.Template, summary="新建模板")
async def create_template(services: ServicesDep, req: S.TemplateCreateRequest) -> S.Template:
    return await services.templates.create_template(req)


@router.get("/api/templates/{template_id}", response_model=S.Template, summary="模板详情")
async def get_template(services: ServicesDep, template_id: str) -> S.Template:
    return await services.templates.get_template(template_id)


@router.delete("/api/templates/{template_id}", response_model=int, summary="删除模板")
async def delete_template(services: ServicesDep, template_id: str) -> int:
    return await services.templates.delete_template(template_id)


@router.post(
    "/api/templates/{template_id}/instantiate",
    response_model=S.TemplateInstantiateResponse,
    summary="实例化（缺失绑定如实列出，不编造）",
)
async def instantiate_template(
    services: ServicesDep, template_id: str, req: S.TemplateInstantiateRequest
) -> S.TemplateInstantiateResponse:
    return await services.templates.instantiate(template_id, req)

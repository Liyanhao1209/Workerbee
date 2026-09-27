"""注册表：harness、凭据、Skill、工具（HAR-02、AUTH-01/02、AUTH-04）。

**凭据端点只搬引用。** 请求与响应里都不会出现密钥本体
（``CredentialRef`` 只有 ``secret_locator``），撤销密钥本体走 L5 的 Secret Store。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schemas as S
from ..deps import ServicesDep

router = APIRouter(tags=["registry"])


# ---- harness ----


@router.get(
    "/api/harnesses", response_model=list[S.HarnessRegistration], summary="harness 列表"
)
async def list_harnesses(services: ServicesDep) -> list[S.HarnessRegistration]:
    return await services.registry.list_harnesses()


@router.post(
    "/api/harnesses", response_model=S.HarnessRegistration, summary="登记 harness"
)
async def create_harness(
    services: ServicesDep, req: S.HarnessCreateRequest
) -> S.HarnessRegistration:
    return await services.registry.create_harness(req)


@router.get(
    "/api/harnesses/{harness_id}",
    response_model=S.HarnessRegistration,
    summary="harness 详情",
)
async def get_harness(services: ServicesDep, harness_id: str) -> S.HarnessRegistration:
    return await services.registry.get_harness(harness_id)


@router.patch(
    "/api/harnesses/{harness_id}",
    response_model=S.HarnessRegistration,
    summary="更新 harness",
)
async def patch_harness(
    services: ServicesDep, harness_id: str, req: S.HarnessPatchRequest
) -> S.HarnessRegistration:
    return await services.registry.patch_harness(harness_id, req)


@router.delete("/api/harnesses/{harness_id}", response_model=int, summary="移除 harness")
async def delete_harness(services: ServicesDep, harness_id: str) -> int:
    return await services.registry.delete_harness(harness_id)


@router.post(
    "/api/harnesses/{harness_id}/probe",
    response_model=S.ProbeResponse,
    summary="能力探测（HAR-02：实测优先，如实标注结论来源）",
)
async def probe_harness(services: ServicesDep, harness_id: str) -> S.ProbeResponse:
    return await services.registry.probe_harness(harness_id)


# ---- 凭据（只有引用） ----


@router.get("/api/credentials", response_model=list[S.CredentialRef], summary="凭据引用列表")
async def list_credentials(services: ServicesDep) -> list[S.CredentialRef]:
    return await services.registry.list_credentials()


@router.post("/api/credentials", response_model=S.CredentialRef, summary="登记凭据引用")
async def create_credential(
    services: ServicesDep, req: S.CredentialCreateRequest
) -> S.CredentialRef:
    return await services.registry.create_credential(req)


@router.get(
    "/api/credentials/{credential_id}",
    response_model=S.CredentialRef,
    summary="凭据引用详情",
)
async def get_credential(services: ServicesDep, credential_id: str) -> S.CredentialRef:
    return await services.registry.get_credential(credential_id)


@router.post(
    "/api/credentials/{credential_id}/revoke",
    response_model=S.CredentialRef,
    summary="撤销／恢复凭据的可用性（动作，故用 POST）",
)
async def revoke_credential(
    services: ServicesDep,
    credential_id: str,
    req: S.RevokeRequest | None = None,
) -> S.CredentialRef:
    return await services.registry.set_credential_revoked(
        credential_id, revoked=(req or S.RevokeRequest()).revoked
    )


@router.delete(
    "/api/credentials/{credential_id}", response_model=int, summary="移除凭据引用"
)
async def delete_credential(services: ServicesDep, credential_id: str) -> int:
    return await services.registry.delete_credential(credential_id)


# ---- Skill ----


@router.get("/api/skills", response_model=list[S.SkillDoc], summary="Skill 列表")
async def list_skills(services: ServicesDep) -> list[S.SkillDoc]:
    return await services.registry.list_skills()


@router.post("/api/skills", response_model=S.SkillDoc, summary="新建／更新 Skill")
async def create_skill(services: ServicesDep, req: S.SkillCreateRequest) -> S.SkillDoc:
    return await services.registry.create_skill(req)


@router.delete("/api/skills/{skill_id}", response_model=int, summary="删除 Skill")
async def delete_skill(services: ServicesDep, skill_id: str) -> int:
    return await services.registry.delete_skill(skill_id)


# ---- 工具 ----


@router.get("/api/tools", response_model=list[S.ToolSpec], summary="工具列表")
async def list_tools(services: ServicesDep) -> list[S.ToolSpec]:
    return await services.registry.list_tools()


@router.post("/api/tools", response_model=S.ToolSpec, summary="新建／更新工具")
async def create_tool(services: ServicesDep, req: S.ToolCreateRequest) -> S.ToolSpec:
    return await services.registry.create_tool(req)


@router.delete("/api/tools/{tool_id}", response_model=int, summary="删除工具")
async def delete_tool(services: ServicesDep, tool_id: str) -> int:
    return await services.registry.delete_tool(tool_id)

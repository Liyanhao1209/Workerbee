"""测试辅助：把简写拓扑展开成定义层实体。

约定：测试里直接用节点名当 ``node_id``，让失败信息可读；生产代码一律用 uuid。
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from workerbee.core.domain import (
    CredentialKind,
    CredentialRef,
    Edge,
    EdgeContract,
    ExecutionProfile,
    GraphSpec,
    HarnessRegistration,
    NodeDefinition,
    SkillDoc,
    ToolSpec,
)

__all__ = [
    "node",
    "profile",
    "edge",
    "graph",
    "chain",
    "diamond",
    "registry_with",
]


def node(
    name: str,
    *,
    enabled: bool = True,
    with_profile: bool = True,
    harness: str | None = "h1",
    credential: str | None = None,
    required_inputs: Sequence[str] = (),
    compact_threshold: int | None = None,
    reasoning_effort: str | None = None,
    system_prompt: str | None = None,
    model_name: str = "m1",
) -> NodeDefinition:
    profiles: list[ExecutionProfile] = []
    if with_profile:
        profiles.append(
            profile(
                f"{name}-p1",
                harness=harness,
                credential=credential,
                compact_threshold=compact_threshold,
                reasoning_effort=reasoning_effort,
                model_name=model_name,
            )
        )
    return NodeDefinition(
        node_id=name,
        name=name,
        enabled=enabled,
        profiles=profiles,
        required_inputs=list(required_inputs),
        system_prompt=system_prompt,
    )


def profile(
    profile_id: str,
    *,
    harness: str | None = "h1",
    credential: str | None = None,
    compact_threshold: int | None = None,
    reasoning_effort: str | None = None,
    model_name: str = "m1",
) -> ExecutionProfile:
    return ExecutionProfile(
        profile_id=profile_id,
        model_name=model_name,
        harness_ref=harness,
        credential_ref=credential,
        compact_threshold=compact_threshold,
        reasoning_effort=reasoning_effort,
    )


def edge(
    a: str,
    b: str,
    *,
    outputs: Sequence[str] | None = None,
    fmt: str = "any",
) -> Edge:
    contract = EdgeContract(outputs=list(outputs), format=fmt) if outputs is not None else None
    return Edge(from_node=a, to_node=b, output_contract=contract)


def graph(
    adjacency: Mapping[str, Iterable[str]],
    *,
    enabled: Iterable[str] | None = None,
    nodes: Mapping[str, NodeDefinition] | None = None,
    edges: Sequence[Edge] | None = None,
) -> GraphSpec:
    """从邻接表构造 GraphSpec。

    :param enabled: 停用节点名单。None 表示全部启用。
    """
    names: list[str] = list(adjacency.keys())
    for targets in adjacency.values():
        for t in targets:
            if t not in names:
                names.append(t)

    enabled_set = set(names) if enabled is None else set(names) - set(enabled)

    node_objs = []
    for n in names:
        if nodes and n in nodes:
            template = nodes[n]
            template.enabled = n in enabled_set
            node_objs.append(template)
        else:
            node_objs.append(node(n, enabled=n in enabled_set))

    edge_objs = list(edges) if edges is not None else [
        edge(a, b) for a, targets in adjacency.items() for b in targets
    ]
    return GraphSpec(nodes=node_objs, edges=edge_objs)


def chain(*names: str, **kwargs) -> GraphSpec:
    adj = {a: [b] for a, b in zip(names, names[1:])}
    adj.setdefault(names[-1], [])
    return graph(adj, **kwargs)


def diamond(**kwargs) -> GraphSpec:
    """A → {B, C} → D"""
    return graph({"A": ["B", "C"], "B": ["D"], "C": ["D"]}, **kwargs)


def registry_with(
    *,
    harnesses: Sequence[str] = ("h1",),
    credentials: Sequence[str] = (),
    skills: Sequence[str] = (),
    tools: Sequence[str] = (),
    harness_capabilities: Mapping[str, dict] | None = None,
    revoked: Sequence[str] = (),
):
    from workerbee.core.graph.validate import InMemoryRegistry

    caps = harness_capabilities or {}
    return InMemoryRegistry(
        harnesses=[
            HarnessRegistration(
                harness_id=h,
                name=h,
                adapter_id="mock",
                capabilities_snapshot=caps.get(h),
                last_probe_ok=True,
            )
            for h in harnesses
        ],
        credentials=[
            CredentialRef(
                credential_id=c,
                label=c,
                kind=CredentialKind.API_KEY,
                secret_locator=f"secret://{c}",
                revoked=c in revoked,
            )
            for c in credentials
        ],
        skills=[SkillDoc(skill_id=s, name=s) for s in skills],
        tools=[ToolSpec(tool_id=t, name=t) for t in tools],
    )

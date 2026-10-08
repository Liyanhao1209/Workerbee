"""按资源拆分的路由模块。

每个模块只暴露一个 ``router``；路由函数**只做**参数解析、调用 ``Services``、
返回已声明的响应模型。任何需要判断的分支都属于服务层或内核。
"""

from __future__ import annotations

from . import (
    approvals,
    assistant,
    capture,
    nodes,
    registry,
    system,
    tasks,
    templates,
    workflows,
    workspaces,
)

__all__ = [
    "ROUTERS", "approvals", "assistant", "capture", "nodes", "registry", "system",
    "tasks", "templates", "workflows", "workspaces",
]

#: 注册顺序即匹配顺序：更具体的路径在前。
ROUTERS = [
    system.router,
    workspaces.router,
    workflows.router,
    nodes.router,
    tasks.router,
    registry.router,
    templates.router,
    approvals.router,
    assistant.router,
    capture.router,
]

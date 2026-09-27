"""Workerbee - 多 agent harness 协作 workflow 框架。

分层（架构设计 v0.02 §2）：
    core/       L1 定义层 + L2 运行时内核（纯领域逻辑）
    data/       L4 数据与事件层
    adapters/   L3 适配层
    security/   L5 安全治理层
    supervisor/ Session 托管进程
    server/     API 网关（L6 的后端）
    tui/        可选终端客户端
"""

__version__ = "0.1.0"

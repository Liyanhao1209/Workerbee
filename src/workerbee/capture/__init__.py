"""流程捕获子包（Graph Capture，WF-03、D-12、AC-16）。

用户选一个基础候选（harness + 模型 + 凭据）真实跑一遍任务，系统从这次执行的
**可取得材料**（显式计划、输出、工具调用、产物）合成可复用的流程草案；
草案标注「观察到的 / 推断的」，采用后与手动建图走同一校验与执行路径。

模块分工：

- :mod:`.service`：CaptureService 核心编排（建临时 Workflow + 发射、状态收敛）；
- :mod:`.material`：捕获材料汇编（排除推理链、字符预算、裁剪留痕、脱敏）；
- :mod:`.synthesis`：草案合成（复用助手 LLM 配置与 workerbee-draft 提案协议，
  含 observed/inferred 复核降级）。
"""

from .material import CaptureMaterial, assemble_material
from .service import (
    CAPTURE_NAME_PREFIX,
    CaptureError,
    CaptureLocked,
    CaptureNotConfigured,
    CaptureService,
)
from .synthesis import review_basis

__all__ = [
    "CaptureService",
    "CaptureError",
    "CaptureNotConfigured",
    "CaptureLocked",
    "CaptureMaterial",
    "assemble_material",
    "review_basis",
    "CAPTURE_NAME_PREFIX",
]

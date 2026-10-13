"""路由层：汇总导出各 APIRouter。"""

from __future__ import annotations

from app.routers.config_router import router as config_router
from app.routers.pipeline_router import router as pipeline_router
from app.routers.result_router import router as result_router

__all__ = ["config_router", "pipeline_router", "result_router"]

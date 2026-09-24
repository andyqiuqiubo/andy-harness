"""统一错误格式与异常处理。

错误格式: {code, message, detail, trace_id}
核心插件保护: core: true 插件的 deactivate/unload 返回 400
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from harness.kernel.exceptions import (
    PluginDeactivateError,
    PluginError,
    PluginNotLoadedError,
    PluginValidationError,
    ServiceUnavailable,
)

logger = logging.getLogger("harness.api.errors")


class APIError(Exception):
    """API 统一错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        detail: Any = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)


def _error_response(
    code: str,
    message: str,
    status_code: int,
    detail: Any = None,
    trace_id: str | None = None,
) -> JSONResponse:
    """构建统一错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "detail": detail,
            "trace_id": trace_id or str(uuid.uuid4()),
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        logger.warning("API 错误: %s — %s", exc.code, exc.message)
        return _error_response(
            exc.code, exc.message, exc.status_code, exc.detail
        )

    @app.exception_handler(ServiceUnavailable)
    async def service_unavailable_handler(
        request: Request, exc: ServiceUnavailable
    ) -> JSONResponse:
        return _error_response(
            "SERVICE_UNAVAILABLE",
            str(exc),
            503,
            {"service": exc.service_name},
        )

    @app.exception_handler(PluginDeactivateError)
    async def plugin_deactivate_error_handler(
        request: Request, exc: PluginDeactivateError
    ) -> JSONResponse:
        return _error_response(
            "PLUGIN_DEACTIVATE_FORBIDDEN",
            str(exc),
            400,
            {"plugin_id": exc.plugin_id, "reason": exc.reason},
        )

    @app.exception_handler(PluginValidationError)
    async def plugin_validation_error_handler(
        request: Request, exc: PluginValidationError
    ) -> JSONResponse:
        return _error_response(
            "PLUGIN_VALIDATION_ERROR",
            str(exc),
            400,
            {"plugin_id": exc.plugin_id, "reason": exc.reason},
        )

    @app.exception_handler(PluginError)
    async def plugin_error_handler(
        request: Request, exc: PluginError
    ) -> JSONResponse:
        return _error_response(
            "PLUGIN_ERROR",
            str(exc),
            400,
        )

    @app.exception_handler(PluginNotLoadedError)
    async def plugin_not_found_handler(
        request: Request, exc: PluginNotLoadedError
    ) -> JSONResponse:
        return _error_response(
            "PLUGIN_NOT_FOUND",
            str(exc),
            404,
        )

    @app.exception_handler(Exception)
    async def general_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        trace_id = str(uuid.uuid4())
        logger.error("未处理异常 (trace_id=%s): %s", trace_id, exc, exc_info=True)
        return _error_response(
            "INTERNAL_ERROR",
            "内部服务器错误",
            500,
            {"type": type(exc).__name__},
            trace_id,
        )

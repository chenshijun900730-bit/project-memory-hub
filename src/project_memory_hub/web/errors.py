from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.responses import HTMLResponse


@dataclass(frozen=True, slots=True)
class _ErrorCopy:
    english_title: str
    english_message: str
    chinese_title: str
    chinese_message: str


_GENERIC_ERROR = _ErrorCopy(
    english_title="Operation failed",
    english_message="The console could not complete the operation safely.",
    chinese_title="操作失败",
    chinese_message="控制台无法安全完成该操作。",
)
_ERROR_COPY = MappingProxyType(
    {
        400: _ErrorCopy(
            english_title="Invalid request",
            english_message="The request could not be processed safely.",
            chinese_title="请求无效",
            chinese_message="无法安全处理该请求。",
        ),
        401: _ErrorCopy(
            english_title="Authentication required",
            english_message="Open the console using its local access link.",
            chinese_title="需要身份验证",
            chinese_message="请使用本地访问链接打开控制台。",
        ),
        403: _ErrorCopy(
            english_title="Request denied",
            english_message="This action is not allowed from the current request.",
            chinese_title="请求被拒绝",
            chinese_message="当前请求不允许执行此操作。",
        ),
        404: _ErrorCopy(
            english_title="Page not found",
            english_message="The requested console page does not exist.",
            chinese_title="页面未找到",
            chinese_message="请求的控制台页面不存在。",
        ),
        409: _ErrorCopy(
            english_title="Request conflict",
            english_message="The action conflicts with the current local state.",
            chinese_title="请求冲突",
            chinese_message="该操作与当前本地状态冲突。",
        ),
        413: _ErrorCopy(
            english_title="Request too large",
            english_message="The request exceeds the local size limit.",
            chinese_title="请求体过大",
            chinese_message="请求超过本地大小限制。",
        ),
        422: _ErrorCopy(
            english_title="Invalid request",
            english_message="The request could not be processed safely.",
            chinese_title="请求无效",
            chinese_message="无法安全处理该请求。",
        ),
        500: _GENERIC_ERROR,
    }
)
_ENVIRONMENT = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(("html",)),
)
_TEMPLATE = _ENVIRONMENT.get_template("error.html")


def error_response(status_code: int) -> HTMLResponse:
    """Build a fixed error page selected only by an HTTP status code."""
    response_status = status_code if type(status_code) is int and 400 <= status_code <= 599 else 500
    copy = _ERROR_COPY.get(response_status, _GENERIC_ERROR)
    body = _TEMPLATE.render(
        english_title=copy.english_title,
        english_message=copy.english_message,
        chinese_title=copy.chinese_title,
        chinese_message=copy.chinese_message,
    )
    return HTMLResponse(body, status_code=response_status)

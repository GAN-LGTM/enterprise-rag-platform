"""统一异常定义。业务异常携带 code，前端可直接展示友好文案。"""
from __future__ import annotations


class AppError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500
    user_message = "服务开小差了，请稍后重试"

    def __init__(self, message: str | None = None, **detail):
        super().__init__(message or self.user_message)
        self.detail = detail
        self.message = message or self.user_message


class AuthError(AppError):
    code = "AUTH_FAILED"
    http_status = 401
    user_message = "登录状态已失效，请重新登录"


class PermissionDenied(AppError):
    """无权限。注意：绝不返回目标部门是否存在等信息，避免侧信道泄露。"""

    code = "PERMISSION_DENIED"
    http_status = 403
    user_message = "抱歉，您没有访问该知识的权限。如有需要请联系本部门主管或管理员开通。"


class LLMUnavailable(AppError):
    code = "LLM_UNAVAILABLE"
    http_status = 503
    user_message = "AI 服务暂时繁忙（已触发降级保护），请稍后再试。"


class BadRequest(AppError):
    code = "BAD_REQUEST"
    http_status = 400
    user_message = "请求参数有误"


class NotFound(AppError):
    """资源不存在，或当前部署形态下该接口被关闭（不暴露"是否存在"的差异）。"""

    code = "NOT_FOUND"
    http_status = 404
    user_message = "资源不存在"


class SensitiveHit(AppError):
    code = "SENSITIVE_HIT"
    http_status = 400
    user_message = "您的问题包含敏感内容，换个说法再问问吧。"


class RateLimited(AppError):
    """触发每用户限流。429 + Retry-After，前端给出人性化倒计时提示。"""

    code = "RATE_LIMITED"
    http_status = 429
    user_message = "您提问有点太快啦，休息几秒再问我吧～"

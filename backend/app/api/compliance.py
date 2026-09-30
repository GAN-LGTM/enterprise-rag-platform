"""合规审计：只读视角 + 异常检测 + 审计导出。

企业合规基线（金融 / 医疗 / 政府客户验收必查项）：
  - 谁、在什么时候、查了哪个部门、什么内容，全部可追溯
  - 审计员只能看，不能改（最小权限原则）
  - 不能只倒日志，还要能主动发现"不对劲"（风险规则 R1~R6）
  - 支持导出交给审计部门归档，支持按自然月生成合规报告
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ..core import audit
from ..errors import BadRequest, PermissionDenied
from .deps import current_user

router = APIRouter(prefix="/api/compliance", tags=["合规审计"])

# 常见操作类型，供前端下拉筛选（后端不做白名单限制，仍可按任意 type 精确过滤）
EVENT_TYPES = [
    ("login_ok", "登录成功"), ("login_fail", "登录失败"),
    ("chat", "知识问答"), ("chat_deny", "越权被拦截"),
    ("upload_ok", "文档上传"), ("doc_delete", "文档删除"), ("doc_delete_request", "删除申请"),
    ("doc_rollback", "版本回滚"),
    ("perm_request_create", "权限申请"), ("perm_request_approve", "权限审批通过"),
    ("perm_request_reject", "权限审批驳回"), ("perm_request_cancel", "权限申请撤回"),
    ("grant_dept", "手动授权"), ("revoke_dept", "回收授权"), ("grant_expire", "授权到期失效"),
    ("change_pwd_ok", "修改密码"), ("change_pwd_fail", "改密失败"),
    ("leave_submit", "请假提交"), ("leave_approve", "请假审批"), ("leave_reject", "请假驳回"),
    ("reimburse_submit", "报销提交"), ("reimburse_approve", "报销审批"), ("reimburse_reject", "报销驳回"),
    ("gap_feedback", "知识反馈"), ("cache_invalidate", "缓存清除"),
    ("user_create", "新增账号"), ("user_reset_pwd", "重置密码"),
    ("user_role_change", "调整角色"), ("user_status", "账号启停"),
    ("data_backup", "数据备份"),
]


def _assert_compliance(user) -> None:
    # 仅管理员与合规审核员可读；其余角色一律拒绝（最小权限原则）
    if user.role in ("admin", "compliance_reviewer"):
        return
    raise PermissionDenied()


def _to_ts(value) -> Optional[int]:
    """接受 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD' / 秒级时间戳，不合法返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


@router.get("/event-types", summary="可筛选的操作类型清单")
async def event_types(user=Depends(current_user)):
    _assert_compliance(user)
    return {"items": [{"value": v, "label": l} for v, l in EVENT_TYPES]}


@router.get("/audit-logs", summary="操作审计日志（合规只读，可按时间/用户/类型/部门/风险等级筛选）")
async def audit_logs(
    limit: int = 200, type: str | None = None, user_id: str | None = None,
    dept: str | None = None, since: str | None = None, until: str | None = None,
    risk: str | None = None, user=Depends(current_user),
):
    _assert_compliance(user)
    data = audit.list_items(event_type=type, user_id=user_id, dept=dept,
                            since=_to_ts(since), until=_to_ts(until),
                            risk=risk or None, limit=limit)
    # 顺带给出近 24h 高风险行为数量，让审计员一眼看到"这批日志里有没有事"
    risk = audit.risk_alerts(window_sec=86400)
    data["risk_high_24h"] = risk["high_alerts"]
    return data


def _period_range(period: str | None):
    """把 'month' / 'quarter' 翻译成 (since, until, 中文口径说明)。

    合规报送通常按自然月 / 季度出报告，这里直接给口径，避免审计员手工填日期。
    """
    if not period:
        return None, None, "全部（不限时间）"
    now = datetime.now()
    if period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = f"{start.strftime('%Y 年 %m 月')}（自然月）"
    elif period == "quarter":
        q = (now.month - 1) // 3
        start = datetime(now.year, q * 3 + 1, 1)
        label = f"{now.year} 年第 {q + 1} 季度（自然季度）"
    else:
        raise BadRequest("period 只支持 month / quarter")
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), now.strftime(fmt), label


@router.get("/export", summary="导出审计日志（CSV / JSON / PDF 报告，交审计部门归档）")
async def export_logs(
    format: str = "csv", limit: int = 5000, type: str | None = None,
    user_id: str | None = None, dept: str | None = None, since: str | None = None,
    until: str | None = None, risk: str | None = None, period: str | None = None,
    user=Depends(current_user),
):
    """直接返回文件流，不在服务端落地，避免审计数据落到业务目录。

    format=pdf 出的是**带统计结论的合规报告**（概览 + 类型分布 + 异常检测 + 日志明细），
    不是单纯的表格打印；period=month / quarter 用于按月 / 季度报送。
    """
    _assert_compliance(user)
    fmt = (format or "csv").lower()
    p_since, p_until, period_label = _period_range(period)
    since = p_since or since
    until = p_until or until
    data = audit.list_items(event_type=type, user_id=user_id, dept=dept,
                            since=_to_ts(since), until=_to_ts(until),
                            risk=risk or None, limit=limit)
    items = data.get("items", [])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "pdf":
        try:
            from ..core.report_pdf import ReportUnavailable, build_audit_report_pdf
        except ImportError as e:  # pragma: no cover - 依赖缺失
            raise BadRequest(f"PDF 报告依赖未安装（{e}），请先导出 CSV / JSON") from e

        alerts = audit.risk_alerts(since=_to_ts(since), until=_to_ts(until),
                                   limit=50).get("items", [])
        filters = "、".join(x for x in (
            f"类型={type}" if type else "", f"用户={user_id}" if user_id else "",
            f"部门={dept}" if dept else "", f"风险={risk}" if risk else "") if x) or "无（全量）"
        try:
            pdf = build_audit_report_pdf(items, {
                "exported_by": user.display_name, "period_label": period_label,
                "filters": filters, "risk_alerts": alerts,
            })
        except ReportUnavailable as e:
            raise BadRequest(f"PDF 报告暂不可用：{e}，请先导出 CSV / JSON") from e
        audit.record("compliance_export", user_id=user.user_id, user_name=user.display_name,
                     detail=f"导出 PDF 合规报告（{period_label}，{len(items)} 条）")
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'attachment; filename="compliance_report_{stamp}.pdf"'})

    if fmt == "json":
        payload = json.dumps({"exported_by": user.display_name,
                              "exported_at": datetime.now().isoformat(timespec="seconds"),
                              "total": data.get("total", 0), "items": items},
                             ensure_ascii=False, indent=2)
        return Response(content=payload.encode("utf-8"), media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="audit_logs_{stamp}.json"'})

    csv_text = audit.export_csv(items)
    return Response(
        content=csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit_logs_{stamp}.csv"'},
    )


@router.get("/risk-alerts", summary="合规异常检测（规则 R1~R6，只读）")
async def risk_alerts(
    window: int = 3600, cross_dept: int = 10, fail: int = 3, deletes: int = 5,
    cross_dept_window: int = 60, user=Depends(current_user),
):
    """按规则扫描最近 window 秒的审计日志，返回风险项与判定依据（每条都有原文，便于复核）。

    阈值默认值取自《合规审计异常检测规则》：连续失败 3 次、删除 5 次、60 秒内跨 10 个部门。
    """
    _assert_compliance(user)
    return audit.risk_alerts(
        window_sec=max(60, window),
        cross_dept_threshold=max(2, cross_dept),
        fail_threshold=max(2, fail),
        delete_threshold=max(1, deletes),
        cross_dept_window=max(10, cross_dept_window),
    )


@router.get("/summary", summary="合规概览（日志总数、异常数、活跃用户、按类型分布）")
async def summary(days: int = 7, user=Depends(current_user)):
    _assert_compliance(user)
    data = audit.list_items(limit=5000)
    items = data.get("items", [])
    since = datetime.now().timestamp() - days * 86400
    recent = [i for i in items if float(i.get("ts") or 0) >= since]

    from collections import Counter
    by_type = Counter(i.get("type", "") for i in items)
    active = Counter(i.get("user_id", "") for i in recent if i.get("user_id"))
    risk = audit.risk_alerts(window_sec=max(3600, days * 86400))
    return {
        "total": data.get("total", 0),
        "window_days": days,
        "recent_total": len(recent),
        "by_type": dict(by_type.most_common()),
        "active_users": active.most_common(20),
        "risk_total": risk["total_alerts"],
        "risk_high": risk["high_alerts"],
    }


@router.get("/report", summary="按月生成合规报告（月度/季度合规报送）")
async def compliance_report(month: str | None = None, user=Depends(current_user)):
    """按自然月统计合规指标：总操作数、类型分布、跨部门查询、敏感部门访问、
    权限变更、越权拦截、活跃用户、风险分布与异常项。month 格式 YYYY-MM，缺省当前月。"""
    _assert_compliance(user)
    try:
        data = audit.report(month)
    except ValueError as e:
        raise BadRequest(str(e))
    data["generated_by"] = user.display_name
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return data

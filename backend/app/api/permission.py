"""权限申请接口：员工发起跨部门访问申请，主管 / 高管 / 管理员审批后自动开通。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from ..config import settings
from ..core import audit
from ..core.perm_request_store import cancel, create_request, decide, get_request, list_requests
from ..db.models import (DEPARTMENTS, USERS, approvers_of, dept_name, get_accessible_depts,
                        get_grants, get_grant_meta, grant_department)
from ..errors import BadRequest, PermissionDenied
from .deps import current_user

router = APIRouter(prefix="/api/permission", tags=["权限申请"])

# 权限类型：企业里"能看"和"能把文件带走"是两件事，必须分开申请、分开审批
VALID_PERM_TYPES = ("read", "download")


def _can_approve(user, department_id: str) -> bool:
    """审批资格按组织树判定（approvers_of），而不是"审批人自己能看哪些部门"。

    后者会把「申请访问事业部节点」「申请访问他人子部门」这类单子判成无人可审。
    """
    return user.username in approvers_of(department_id)


@router.post("/request", summary="发起跨部门知识库访问申请")
async def request_permission(body: dict, user=Depends(current_user)):
    department_id = str(body.get("department_id") or "")
    reason = str(body.get("reason") or "").strip()[:500]
    permission_type = str(body.get("permission_type") or "read").strip()
    if department_id not in DEPARTMENTS and department_id != settings.PUBLIC_DEPT_ID:
        raise BadRequest("目标部门不存在")
    if not reason:
        raise BadRequest("请填写申请理由")
    if permission_type not in VALID_PERM_TYPES:
        raise BadRequest("权限类型只能是 read（只读）或 download（可下载）")
    # 有效期（天）：留空 / 0 = 长期有效；到期后权限自动失效，无需人工回收
    expires_at = None
    raw_days = body.get("days")
    if raw_days not in (None, "", 0, "0"):
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            raise BadRequest("有效期天数不合法")
        if days <= 0:
            raise BadRequest("有效期天数必须为正整数（长期有效请留空）")
        expires_at = round(time.time() + days * 86400, 3)
    # 已有权限 / 已有待审批申请则拒绝重复提交
    if department_id in get_accessible_depts(user):
        raise BadRequest("您已具备该部门访问权限，无需重复申请")
    if any(r["status"] == "pending" and r["department_id"] == department_id
           for r in list_requests(username=user.username)):
        raise BadRequest("该部门的申请正在审批中，请勿重复提交")
    approvers = approvers_of(department_id)
    if not approvers & {u.username for u in USERS.values() if u.role == "admin"}:
        # 没有管理员兜底又找不到主管 = 死单，宁可当场拒绝也不要让用户白等
        raise BadRequest("该部门没有可审批的主管，请联系系统管理员开通")
    rec = create_request(user.username, user.display_name, department_id, reason,
                         permission_type=permission_type, expires_at=expires_at)
    rec = {**rec, "approvers": sorted(a for a in approvers if a != user.username)}
    audit.record("perm_request_create", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{department_id} | {permission_type} | {reason[:40]}",
                 dept=department_id, changed=True)
    return {"ok": True, "request_id": rec["id"], "status": rec["status"]}


@router.get("/my", summary="我的权限申请列表")
async def my_requests(user=Depends(current_user)):
    items = list_requests(username=user.username)
    return {"items": items, "total": len(items)}


@router.post("/{rid}/cancel", summary="撤回自己的待审批申请")
async def cancel_request(rid: str, user=Depends(current_user)):
    rec = get_request(rid)
    if rec is None:
        raise BadRequest("申请不存在")
    if rec["username"] != user.username:
        raise PermissionDenied("只能撤回自己提交的申请")
    if rec["status"] != "pending":
        raise BadRequest("该申请已处理，无法撤回")
    cancel(rid, user.username)
    audit.record("perm_request_cancel", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{rec['department_id']}（由申请人撤回）", dept=rec["department_id"],
                 changed=True)
    return {"ok": True, "status": "canceled"}


@router.get("/pending", summary="待我审批的权限申请")
async def pending_requests(user=Depends(current_user)):
    scope = approvers_of("") if user.role == "admin" else _approve_scope(user)
    items = [i for i in list_requests(status="pending") if i["department_id"] in scope]
    return {"items": items, "total": len(items)}


def _approve_scope(user) -> set[str]:
    """当前用户有审批权的 departments 集合（按组织树，非"我能看到哪些库"）。"""
    out: set[str] = set()
    for dept in DEPARTMENTS:
        if user.username in approvers_of(dept):
            out.add(dept)
    return out


@router.get("/history", summary="已处理的权限申请（我审批过的 / 我发起的已结束单）")
async def history(user=Depends(current_user)):
    """已归档的申请单，供主管复盘"我批过什么"、员工回看"我的申请结果"。

    管理员看全部；主管看自己管辖范围内的 + 自己发起的；员工只看自己发起的。
    """
    done = [i for i in list_requests() if i["status"] != "pending"]
    if user.role == "admin":
        items = done
    else:
        scope = _approve_scope(user)
        items = [i for i in done
                 if i["department_id"] in scope or i["username"] == user.username
                 or i.get("decided_by") == user.display_name]
    return {"items": items, "total": len(items)}


@router.post("/{rid}/approve", summary="批准申请并自动开通权限")
async def approve(rid: str, user=Depends(current_user)):
    rec = get_request(rid)
    if rec is None or rec["status"] != "pending":
        raise BadRequest("申请不存在或已处理")
    if not _can_approve(user, rec["department_id"]):
        raise PermissionDenied()
    target = USERS.get(rec["username"])
    if not target:
        raise BadRequest("申请人不存在")
    # 按申请单里的权限类型与有效期开通：审批同意什么就开通什么，不多给
    changed = grant_department(rec["username"], rec["department_id"],
                               expires_at=rec.get("expires_at"),
                               granted_by=user.display_name,
                               permission_type=rec.get("permission_type", "read"))
    decide(rid, "approved", user.display_name)
    exp = rec.get("expires_at")
    exp_txt = time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "长期"
    audit.record("perm_request_approve", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{rec['username']} -> {rec['department_id']}"
                        f"（{rec.get('permission_type', 'read')}，有效期至 {exp_txt}，自动开通）",
                 dept=rec["department_id"], changed=changed)
    from ..core import notification_store
    notification_store.push(
        rec["username"], "permission", "权限申请已通过",
        f"你申请访问「{dept_name(rec['department_id'])}」的权限已由 {user.display_name} 审批通过"
        f"（{'可下载' if rec.get('permission_type') == 'download' else '只读'}，有效期至 {exp_txt}），"
        f"现已可访问该部门知识库。")
    return {"ok": True, "status": "approved",
            "granted_departments": get_grants(rec["username"])}


@router.post("/{rid}/reject", summary="驳回申请")
async def reject(rid: str, body: dict, user=Depends(current_user)):
    rec = get_request(rid)
    if rec is None or rec["status"] != "pending":
        raise BadRequest("申请不存在或已处理")
    if not _can_approve(user, rec["department_id"]):
        raise PermissionDenied()
    note = str(body.get("note") or "").strip()[:300]
    decide(rid, "rejected", user.display_name)
    audit.record("perm_request_reject", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{rec['username']} -> {rec['department_id']} | {note[:40]}",
                 dept=rec["department_id"], changed=True)
    from ..core import notification_store
    notification_store.push(
        rec["username"], "permission", "权限申请被驳回",
        f"你申请访问「{dept_name(rec['department_id'])}」的权限被 {user.display_name} 驳回"
        + (f"：{note}" if note else "。"))
    return {"ok": True, "status": "rejected"}

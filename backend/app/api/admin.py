"""管理接口：部门、统计看板、审计日志（持久化）、缓存管理。"""
from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from ..core import audit
from ..core import backup as backup_core
from ..config import settings
from ..core import doc_deletion_store as del_store
from ..db.models import (DEPARTMENTS, USERS, builtin_departments, dept_name,
                       get_accessible_depts, get_grants, get_grant_meta,
                       grant_department, revoke_department)
from ..errors import BadRequest, PermissionDenied
from ..ingest.version_store import list_versions
from .deps import current_user

router = APIRouter(prefix="/api/admin", tags=["管理后台"])


def _assert_admin(user):
    if user.role != "admin":
        raise PermissionDenied()


@router.get("/departments", summary="部门树（管理员）")
async def departments(user=Depends(current_user)):
    """全量组织树（含层级与父子关系）。

    这是管理员配置界面的数据源，必须限管理员：组织架构 + 层级属于内部信息，
    普通账号没有理由拿到全量结构。非管理员要看部门名请用
    `/api/knowledge/departments`（只返回 id/name 的只读列表）。
    """
    _assert_admin(user)
    return [{"id": d.id, "name": d.name, "parent_id": d.parent_id, "level": d.level}
            for d in DEPARTMENTS.values()]


@router.get("/my-departments", summary="我可访问的部门")
async def my_departments(user=Depends(current_user)):
    return {"role": user.role, "dept_id": user.dept_id,
            "accessible_departments": get_accessible_depts(user)}


# ============================================================
#  跨部门手动授权（FR-PERM：permission_config 的手动配置能力）
# ============================================================
@router.get("/grants", summary="查看某用户的跨部门授权")
async def list_grants(username: str, user=Depends(current_user)):
    _assert_admin(user)
    if username not in USERS:
        raise BadRequest("用户不存在")
    return {"username": username, "granted_departments": get_grants(username)}


@router.post("/grants", summary="授予某用户跨部门访问（手动配置）")
async def grant(body: dict, user=Depends(current_user)):
    _assert_admin(user)
    username = str(body.get("username") or "")
    department_id = str(body.get("department_id") or "")
    target = USERS.get(username)
    if not target:
        raise BadRequest("用户不存在")
    if department_id not in DEPARTMENTS and department_id != settings.PUBLIC_DEPT_ID:
        raise BadRequest(f"部门不存在: {department_id}")
    changed = grant_department(username, department_id)
    audit.record("grant_dept", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{username} -> {department_id}", changed=changed)
    return {"ok": True, "username": username, "department_id": department_id,
            "granted_departments": get_grants(username)}


@router.delete("/grants", summary="撤销某用户的跨部门授权")
async def revoke(username: str, department_id: str, user=Depends(current_user)):
    _assert_admin(user)
    if username not in USERS:
        raise BadRequest("用户不存在")
    changed = revoke_department(username, department_id)
    audit.record("revoke_dept", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{username} -> {department_id}", changed=changed)
    return {"ok": True, "username": username, "department_id": department_id,
            "granted_departments": get_grants(username)}


@router.get("/grants-matrix", summary="全量授权矩阵（管理员给用户配置跨部门权限用）")
async def grants_matrix(user=Depends(current_user)):
    """一次拉取「用户 × 部门」授权现状，供管理端勾选保存，避免逐个用户查询。"""
    _assert_admin(user)
    users = []
    for u in USERS.values():
        users.append({
            "username": u.username,
            "display_name": u.display_name,
            "role": u.role,
            "dept_id": u.dept_id,
            "dept_name": dept_name(u.dept_id),
            "builtin_departments": builtin_departments(u),      # 角色自带，不能在这里回收
            "granted_departments": get_grants(u.username),      # 额外授权（已剔除过期），可被管理员调整
            "accessible_departments": get_accessible_depts(u),  # 实际可见范围（含额外授权）
            "grant_meta": get_grant_meta(u.username),           # 授权明细：授予人/时间/有效期/类型
        })
    return {
        "departments": [{"id": d.id, "name": d.name, "parent_id": d.parent_id}
                        for d in DEPARTMENTS.values()],
        "users": users,
    }


@router.post("/grants/bulk", summary="一次性保存某用户的跨部门授权（勾选即生效）")
async def grants_bulk(body: dict, user=Depends(current_user)):
    """提交该用户「应持有的全部额外授权」，服务端做差量：

    不能回收角色自带的部门权限（如本部门、公共库），只能增减额外授权部分，
    避免管理员一保存就把用户的本部门权限弄丢——这是权限系统的经典事故。
    """
    _assert_admin(user)
    username = str(body.get("username") or "")
    target = USERS.get(username)
    if not target:
        raise BadRequest("用户不存在")
    wanted = {str(d) for d in (body.get("departments") or [])}
    # 部门不存在就明确报错，不要静默忽略（否则管理员以为授权成功，实际什么都没发生）
    bad = [d for d in wanted if d not in DEPARTMENTS and d != settings.PUBLIC_DEPT_ID]
    if bad:
        raise BadRequest("部门不存在：" + "、".join(sorted(bad)))
    wanted.discard(target.dept_id)                       # 本部门属于角色自带，不在此处管理
    # 有效期（天）：留空 / 0 = 长期有效；到期自动失效，避免"授权一次永久有效"的合规风险
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
    permission_type = str(body.get("permission_type") or "read").strip()
    if permission_type not in ("read", "download"):
        raise BadRequest("权限类型只能是 read（只读）或 download（可下载）")
    # 注意：这里必须用「角色自带」（builtin），不能用 get_accessible_depts ——
    # 后者已包含手动授权，用它做差量会导致「清空授权」永远删不干净。
    role_depts = set(builtin_departments(target))
    final = (wanted - role_depts) | (set(get_grants(username)) & role_depts)

    current = set(get_grants(username))
    changes = []
    for d in sorted(current - final):
        if revoke_department(username, d):
            changes.append(f"-{d}")
    for d in sorted(final - current):
        if grant_department(username, d, expires_at=expires_at,
                            granted_by=user.display_name, permission_type=permission_type):
            changes.append(f"+{d}")
    if changes:
        audit.record("grant_bulk", user_id=user.user_id, user_name=user.display_name,
                     detail=f"{username} {' '.join(changes)}", changed=True)
    return {"ok": True, "username": username, "changed": changes,
            "granted_departments": sorted(final)}


@router.get("/audit-logs", summary="操作审计日志（持久化，可按类型/用户过滤）")
async def audit_logs(limit: int = 100, type: str | None = None,
                     user_id: str | None = None, user=Depends(current_user)):
    _assert_admin(user)
    return audit.list_items(event_type=type, user_id=user_id, limit=limit)


@router.post("/cache/invalidate", summary="按部门清除问答缓存")
async def invalidate_cache(department_id: str, request: Request, user=Depends(current_user)):
    _assert_admin(user)
    container = request.app.state.container
    await container.cache.invalidate_department(department_id)
    audit.record("cache_invalidate", user_id=user.user_id, user_name=user.display_name,
                 detail=department_id)
    return {"ok": True, "department_id": department_id}


@router.get("/stats", summary="系统运行统计")
async def stats(request: Request, user=Depends(current_user)):
    _assert_admin(user)
    from .. import session_store as store
    from ..core import feedback_store, gap_store
    from ..leave import leave_store as lstore
    from ..reimburse import reimburse_store as rbstore

    container = request.app.state.container
    fb_stats = feedback_store.feedback_stats()
    hot_total, hot_unique = feedback_store.hot_totals()

    leave_all = _all_requests(lstore)
    rb_all = _all_requests(rbstore)
    # 会话存储结构：{user_id: [session, ...]}，会话总数 = 各用户列表长度之和
    store._load()
    session_total = sum(len(v) for v in store._DATA.values())

    return {
        "users": len(USERS),
        "sessions": session_total,
        "questions": hot_total,
        "unique_questions": hot_unique,
        "feedbacks": fb_stats["total"],
        "satisfaction": fb_stats["satisfaction"],
        "leave_total": len(leave_all),
        "leave_pending": sum(1 for r in leave_all if r.get("status") == "pending"),
        "reimburse_total": len(rb_all),
        "reimburse_pending": sum(1 for r in rb_all if r.get("status") == "pending"),
        "audit_total": audit.counts_by_type(),
        "knowledge_gaps": gap_store.gap_summary(),
        "store_mode": container.store_mode,
        "embedding_backend": container.embedder.backend,
        "llm_backend": container.llm.primary.name,
        "cache_mode": container.cache.mode,
        "graph_engine": container.workflow.engine,
    }


def _all_requests(mod) -> list[dict]:
    """读取模块存储中的全部申请（演示实现直接读内存镜像）。"""
    try:
        mod._load()
        return list(mod._DATA.get("requests", []))
    except Exception:  # noqa: BLE001
        return []


# ============================================================
#  文档删除审批（删除文档需管理员同意）
# ============================================================
# ============================================================
#  知识缺口工单闭环（FR-OPS：从"记录缺口"到"处置缺口"）
# ============================================================
def _gap_scopes(user) -> set[str] | None:
    """工单可见范围：管理员看全部；主管/高管看自己管辖的部门。None = 不限制。"""
    if user.role == "admin":
        return None
    if user.role not in ("executive", "dept_director", "sub_manager") and not user.can_upload:
        raise PermissionDenied()
    return set(get_accessible_depts(user))


def _assert_gap_visible(rec: dict, user) -> None:
    scopes = _gap_scopes(user)
    if scopes is not None and rec.get("department_id") not in scopes:
        raise PermissionDenied()


@router.get("/knowledge-gaps", summary="知识缺口工单列表（问了但答不上来 / 用户反馈的问题）")
async def knowledge_gaps(top: int = 20, department_id: str | None = None,
                         reason: str | None = None, status: str | None = None,
                         user=Depends(current_user)):
    """
    知识缺口工单台账。

    reason（系统自动判定）：
      no_hit        一条片段都没捞到（纯粹缺资料）
      low_evidence  捞到了但都不相关（资料写得太泛，或缺少用户实际用的关键词）
    status（人工处置状态）：
      pending 待处理 / supplemented 已补充 / closed 已关闭

    权限隔离：管理员看全部工单；部门主管/高管只看自己管辖范围内的部门。
    """
    from ..core import gap_store

    scopes = _gap_scopes(user)
    if scopes is not None:
        department_id = None      # 非管理员忽略外部指定，改为按可见范围过滤
    items = gap_store.list_gaps(top=top, department_id=department_id,
                                reason=reason, status=status)
    if scopes is not None:
        items = [i for i in items if i.get("department_id") in scopes]
    for it in items:
        it["status_label"] = gap_store.STATUS_LABELS.get(it.get("status"), "")
    return {"summary": gap_store.gap_summary(), "items": items}


@router.post("/knowledge-gaps/{gap_id}/assign", summary="指派工单给处理人")
async def assign_gap(gap_id: str, body: dict, user=Depends(current_user)):
    from ..core import gap_store

    rec = gap_store.get_gap(gap_id)
    if rec is None:
        raise BadRequest("工单不存在")
    _assert_gap_visible(rec, user)
    assignee = str(body.get("assignee") or "").strip()
    if not assignee:
        raise BadRequest("请指定处理人")
    rec = gap_store.assign(gap_id, assignee, operator=user.display_name)
    audit.record("gap_assign", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{rec.get('question', '')[:60]} -> {assignee}",
                 dept=rec.get("department_id"), changed=True)
    return {"ok": True, "gap_id": gap_id, "assignee": assignee}


@router.post("/knowledge-gaps/{gap_id}/resolve", summary="标记工单已补充（资料已上架）")
async def resolve_gap(gap_id: str, body: dict, user=Depends(current_user)):
    """
    资料补齐后调用：工单转"已补充"，并给所有反馈过的人生成通知，
    让闭环回到提问人那里（他下一次再问同类问题就能拿到答案）。
    """
    from ..core import gap_store

    rec = gap_store.get_gap(gap_id)
    if rec is None:
        raise BadRequest("工单不存在")
    _assert_gap_visible(rec, user)
    raw_docs = body.get("docs") or []
    if isinstance(raw_docs, str):
        # 前端可能传"文档A、文档B"这种字符串，统一拆成列表
        import re as _re

        raw_docs = [d for d in _re.split(r"[、,，;；\n]+", raw_docs)]
    docs = [str(d).strip() for d in raw_docs if str(d).strip()][:20]
    note = str(body.get("note") or "").strip()[:1000]
    if not docs and not note:
        raise BadRequest("请填写补充的文档名或处理说明")

    rec = gap_store.resolve(gap_id, docs=docs, note=note, operator=user.display_name)
    audit.record("gap_resolve", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{rec.get('question', '')[:60]} | {'、'.join(docs)[:80]}",
                 dept=rec.get("department_id"), changed=True)
    return {"ok": True, "gap_id": gap_id, "status": rec.get("status"),
            "notified": len(rec.get("notifications") or [])}


@router.post("/knowledge-gaps/{gap_id}/reopen", summary="把已处理工单拉回待处理")
async def reopen_gap(gap_id: str, user=Depends(current_user)):
    """补充的资料仍然答不上来 / 属于误关时，管理员可重新激活工单。"""
    from ..core import gap_store

    rec = gap_store.get_gap(gap_id)
    if rec is None:
        raise BadRequest("工单不存在")
    _assert_gap_visible(rec, user)
    if rec.get("status") == gap_store.STATUS_PENDING:
        return {"ok": True, "gap_id": gap_id, "status": gap_store.STATUS_PENDING}
    rec = gap_store.reopen(gap_id, operator=user.display_name)
    audit.record("gap_reopen", user_id=user.user_id, user_name=user.display_name,
                 detail=rec.get("question", "")[:60], dept=rec.get("department_id"),
                 changed=True)
    return {"ok": True, "gap_id": gap_id, "status": rec.get("status")}


@router.post("/knowledge-gaps/{gap_id}/close", summary="关闭工单（不纳管 / 已线下解答）")
async def close_gap(gap_id: str, body: dict | None = None, user=Depends(current_user)):
    from ..core import gap_store

    rec = gap_store.get_gap(gap_id)
    if rec is None:
        raise BadRequest("工单不存在")
    _assert_gap_visible(rec, user)
    note = str((body or {}).get("note") or "").strip()[:1000]
    rec = gap_store.close(gap_id, note=note, operator=user.display_name)
    audit.record("gap_close", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{rec.get('question', '')[:60]} | {note[:60]}",
                 dept=rec.get("department_id"), changed=True)
    return {"ok": True, "gap_id": gap_id, "status": rec.get("status")}


@router.get("/doc-deletions", summary="文档删除申请列表（管理员）")
async def doc_deletions(status: str | None = None, limit: int = 100,
                        user=Depends(current_user)):
    _assert_admin(user)
    return {"items": del_store.list_requests(status, limit)}


@router.post("/doc-deletions/{rid}/approve", summary="通过删除申请并执行删除（管理员）")
async def approve_doc_deletion(rid: str, request: Request, user=Depends(current_user)):
    _assert_admin(user)
    req = del_store.get_request(rid)
    if req is None:
        raise BadRequest("删除申请不存在")
    if req["status"] != "pending":
        raise BadRequest(f"该申请已处理（{req['status']}）")
    if not list_versions(req["doc_name"], req["department_id"]):
        del_store.set_status(rid, "approved", user.username)
        raise BadRequest("该文档已不存在，申请自动关闭")

    from .knowledge import _perform_delete
    container = request.app.state.container
    removed = await _perform_delete(
        container, req["doc_name"], req["department_id"], user,
        via_request=rid, requested_by=req["requested_by"])
    del_store.set_status(rid, "approved", user.username)
    from ..core import notification_store
    notification_store.push(
        req["requested_by"], "doc_audit", "文档删除申请已通过",
        f"你申请删除的文档「{req['doc_name']}」（{req['department_id']}）已由 {user.display_name} 审批通过并完成删除。")
    return {"ok": True, "doc_name": req["doc_name"],
            "department_id": req["department_id"], "removed_chunks": removed}


@router.post("/doc-deletions/{rid}/reject", summary="驳回删除申请（管理员）")
async def reject_doc_deletion(rid: str, user=Depends(current_user)):
    _assert_admin(user)
    req = del_store.get_request(rid)
    if req is None:
        raise BadRequest("删除申请不存在")
    if req["status"] != "pending":
        raise BadRequest(f"该申请已处理（{req['status']}）")
    del_store.set_status(rid, "rejected", user.username)
    audit.record("doc_delete_reject", user_id=user.user_id, user_name=user.display_name,
                 detail=req["doc_name"], dept=req["department_id"], request_id=rid,
                 requested_by=req["requested_by"])
    from ..core import notification_store
    notification_store.push(
        req["requested_by"], "doc_audit", "文档删除申请被驳回",
        f"你申请删除的文档「{req['doc_name']}」（{req['department_id']}）被 {user.display_name} 驳回，文档保留。")
    return {"ok": True, "request_id": rid, "status": "rejected"}


# ============================================================
#  用户管理（FR-ADMIN-USERS：交付后甲方自行维护账号，不再依赖改代码）
# ============================================================
@router.get("/users", summary="用户列表（含来源/停用状态）")
async def admin_users(user=Depends(current_user)):
    _assert_admin(user)
    from ..core import user_admin_store
    return {"roles": user_admin_store.VALID_ROLES, "items": user_admin_store.list_users()}


@router.post("/users", summary="新增用户账号")
async def create_user(body: dict, user=Depends(current_user)):
    _assert_admin(user)
    from ..core import user_admin_store
    from ..core.security import hash_password, validate_password

    username = str(body.get("username") or "").strip().lower()
    display_name = str(body.get("display_name") or "").strip()[:50]
    password = str(body.get("password") or "")
    role = str(body.get("role") or "")
    dept_id = str(body.get("dept_id") or "")
    if not username or not username.replace("_", "").replace(".", "").isalnum():
        raise BadRequest("账号只能包含字母、数字、下划线和点")
    if len(username) < 3:
        raise BadRequest("账号长度至少 3 位")
    if not display_name:
        raise BadRequest("请填写姓名")
    if username in USERS:
        raise BadRequest("该账号已存在")
    if role not in user_admin_store.VALID_ROLES:
        raise BadRequest("角色不合法")
    if dept_id not in DEPARTMENTS and dept_id != settings.PUBLIC_DEPT_ID:
        raise BadRequest("部门不存在")
    ok, msg = validate_password(password)
    if not ok:
        raise BadRequest(msg)
    user_admin_store.create_user(username, display_name, hash_password(password),
                                 role, dept_id, operator=user.display_name)
    audit.record("user_create", user_id=user.user_id, user_name=user.display_name,
                 detail=f"新增账号 {username}（{display_name}，{role}，{dept_id}）",
                 dept=dept_id, changed=True)
    return {"ok": True, "username": username}


@router.post("/users/{username}/reset-password", summary="重置用户密码（重置后首次登录须改密）")
async def reset_user_password(username: str, body: dict, user=Depends(current_user)):
    _assert_admin(user)
    from ..core import user_admin_store
    from ..core.security import hash_password, validate_password

    if username not in USERS:
        raise BadRequest("用户不存在")
    password = str(body.get("password") or "")
    ok, msg = validate_password(password)
    if not ok:
        raise BadRequest(msg)
    user_admin_store.reset_password(username, hash_password(password),
                                    operator=user.display_name)
    audit.record("user_reset_pwd", user_id=user.user_id, user_name=user.display_name,
                 detail=f"重置 {username} 的密码", changed=True)
    return {"ok": True, "username": username}


@router.post("/users/{username}/role", summary="调整用户角色 / 所属部门")
async def set_user_role(username: str, body: dict, user=Depends(current_user)):
    _assert_admin(user)
    from ..core import user_admin_store

    if username not in USERS:
        raise BadRequest("用户不存在")
    role = str(body.get("role") or "")
    dept_id = str(body.get("dept_id") or "") or None
    if role not in user_admin_store.VALID_ROLES:
        raise BadRequest("角色不合法")
    if dept_id and dept_id not in DEPARTMENTS and dept_id != settings.PUBLIC_DEPT_ID:
        raise BadRequest("部门不存在")
    if username == user.username and role != "admin":
        raise BadRequest("不能取消自己的管理员角色（防止系统失去最后一个管理员）")
    user_admin_store.set_role(username, role, dept_id, operator=user.display_name)
    audit.record("user_role_change", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{username} 角色调整为 {role}" + (f"，部门 {dept_id}" if dept_id else ""),
                 changed=True)
    return {"ok": True, "username": username, "role": role}


@router.post("/users/{username}/status", summary="启用 / 停用用户账号")
async def set_user_status(username: str, body: dict, user=Depends(current_user)):
    _assert_admin(user)
    from ..core import user_admin_store

    if username not in USERS:
        raise BadRequest("用户不存在")
    if username == user.username:
        raise BadRequest("不能停用自己的账号")
    disabled = bool(body.get("disabled"))
    user_admin_store.set_disabled(username, disabled, operator=user.display_name)
    audit.record("user_status", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{'停用' if disabled else '启用'}账号 {username}", changed=True)
    return {"ok": True, "username": username, "disabled": disabled}


# ============================================================
#  部门管理（组织树视图：部门 + 成员分布；架构调整走部署配置，页面只读）
# ============================================================
@router.get("/org-tree", summary="组织树与成员分布")
async def org_tree(user=Depends(current_user)):
    _assert_admin(user)
    from ..core import user_admin_store
    users = user_admin_store.list_users()
    by_dept: dict[str, list[dict]] = {}
    for u in users:
        by_dept.setdefault(u["dept_id"], []).append(
            {"username": u["username"], "display_name": u["display_name"],
             "role_label": u["role_label"], "disabled": u["disabled"]})
    tree = []
    for d in DEPARTMENTS.values():
        members = by_dept.get(d.id, [])
        tree.append({
            "id": d.id, "name": d.name, "parent_id": d.parent_id, "level": d.level,
            "member_count": len(members), "members": members,
        })
    return {"items": tree}


# ============================================================
#  系统配置（只读展示）+ 数据备份
# ============================================================
@router.get("/system-config", summary="系统运行配置（只读）")
async def system_config(request: Request, user=Depends(current_user)):
    _assert_admin(user)
    container = request.app.state.container
    return {
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "offline_mode": settings.OFFLINE_MODE,
        "store_mode": container.store_mode,
        "cache_mode": container.cache.mode,
        "cache_ttl_seconds": settings.CACHE_TTL_SECONDS,
        "cache_max_items": settings.CACHE_MAX_ITEMS,
        "llm": {
            "backend": settings.LLM_BACKEND,
            "model": settings.CLOUD_LLM_MODEL if settings.LLM_BACKEND == "cloud" else settings.LLM_MODEL,
            "fallback": settings.LLM_FALLBACK_MODEL,
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": settings.LLM_MAX_TOKENS,
            "temperature": settings.LLM_TEMPERATURE,
        },
        "embedding": {
            "backend": settings.EMBEDDING_BACKEND,
            "model": settings.EMBEDDING_MODEL_PATH,
            "dim": settings.EMBEDDING_DIM,
        },
        "ocr_enabled": settings.OCR_ENABLED,
        "sensitive_depts": list(settings.SENSITIVE_DEPTS),
        "dingtalk_mock": settings.DINGTALK_MOCK,
    }


# ============================================================
#  数据看板（运营视角趋势：活跃度 / 问答量 / 知识增长 / 越权拦截）
# ============================================================
@router.get("/analytics", summary="数据看板（DAU/MAU、问答趋势、知识库增长、越权拦截）")
async def analytics(days: int = 14, user=Depends(current_user)):
    """只读统计，数据全部来自审计日志与版本库，不额外埋点。

    days 控制趋势图回溯天数（默认 14 天）；DAU 按自然日、MAU 按近 30 天去重用户。
    """
    _assert_admin(user)
    import time as _time
    from collections import defaultdict

    days = max(3, min(60, int(days or 14)))
    items = audit.list_items(limit=20000).get("items", [])
    now = datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    mau_lo = now.timestamp() - 30 * 86400
    start_lo = today0 - (days - 1) * 86400

    dau: set[str] = set()
    mau: set[str] = set()
    q_by_day: dict[str, int] = defaultdict(int)
    act_by_day: dict[str, set] = defaultdict(set)
    deny_by_day: dict[str, int] = defaultdict(int)

    for i in items:
        ts = float(i.get("ts") or 0)
        uid = str(i.get("user_id") or "")
        if ts >= today0 and uid:
            dau.add(uid)
        if ts >= mau_lo and uid:
            mau.add(uid)
        if ts < start_lo:
            continue
        key = datetime.fromtimestamp(ts).strftime("%m-%d")
        if uid:
            act_by_day[key].add(uid)
        t = i.get("type")
        if t == "chat":
            q_by_day[key] += 1
        elif t == "chat_deny":
            deny_by_day[key] += 1

    # 时间轴补齐空档：没有数据的日子也要画出来，否则趋势线会"跳"
    axis = [datetime.fromtimestamp(start_lo + d * 86400).strftime("%m-%d") for d in range(days)]
    trend = [{"date": d, "questions": q_by_day.get(d, 0),
              "active_users": len(act_by_day.get(d, ())),
              "denied": deny_by_day.get(d, 0)} for d in axis]

    # ---- 知识库增长：版本库里每条 history 就是一次文档入库/更新 ----
    from ..ingest import version_store
    doc_total, doc_by_day = 0, defaultdict(int)
    try:
        version_store._load()
        raw = version_store._STATE or {}
        doc_total = len(raw)
        for _k, rec in raw.items():
            for h in (rec.get("history") or []):
                ts = float(h.get("uploaded_at") or 0)
                if ts >= start_lo:
                    doc_by_day[datetime.fromtimestamp(ts).strftime("%m-%d")] += 1
    except Exception:  # noqa: BLE001 - 看板是只读展示，任一数据源异常不阻断整体
        pass
    doc_trend = [{"date": d, "docs": doc_by_day.get(d, 0)} for d in axis]

    return {
        "days": days,
        "dau": len(dau),
        "mau": len(mau),
        "total_users": len(USERS),
        "questions_total": sum(q_by_day.values()),
        "questions_today": q_by_day.get(axis[-1], 0),
        "denied_total": sum(deny_by_day.values()),
        "docs_total": doc_total,
        "docs_new_window": sum(doc_by_day.values()),
        "trend": trend,
        "doc_trend": doc_trend,
    }


# ============================================================
#  模型管理（运行时模型清单 + 主备切换）
# ============================================================
def _gpu_info() -> dict:
    """尽力而为取 GPU / 显存信息；无 GPU 或未装 torch 时明确说明，不编造数字。"""
    try:
        import torch  # noqa: PLC0415

        if not torch.cuda.is_available():
            return {"available": False, "reason": "未检测到可用 CUDA 设备（当前为 CPU 推理）"}
        used = torch.cuda.memory_allocated() / 1024 ** 3
        total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
        return {"available": True, "device": torch.cuda.get_device_name(0),
                "memory_used_gb": round(used, 1), "memory_total_gb": round(total, 1)}
    except Exception:  # noqa: BLE001
        return {"available": False, "reason": "未安装 torch，无法读取显存占用（不影响推理）"}


@router.get("/models", summary="模型清单与运行状态（模型管理）")
async def list_models(request: Request, user=Depends(current_user)):
    _assert_admin(user)
    container = request.app.state.container
    llm = container.llm
    chain = [{"key": f"{b.name}:{b.model}", "name": b.name, "model": b.model,
              "is_primary": b is llm.primary, "is_fallback": b is llm.fallback}
             for b in llm.chain]
    return {
        "llm": {
            "backend": settings.LLM_BACKEND,
            "primary_key": llm.primary_key,
            "fallback_model": settings.LLM_FALLBACK_MODEL,
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": settings.LLM_MAX_TOKENS,
            "temperature": settings.LLM_TEMPERATURE,
            "chain": chain,
        },
        "embedding": {"backend": settings.EMBEDDING_BACKEND,
                      "model": settings.EMBEDDING_MODEL_PATH,
                      "dim": settings.EMBEDDING_DIM,
                      "runtime": container.embedder.backend},
        "intent": {"model": settings.BERT_MODEL_PATH},
        "ocr": {"enabled": settings.OCR_ENABLED,
                "engine": "PaddleOCR" if settings.OCR_ENABLED else "未启用"},
        "gpu": _gpu_info(),
        # 热更新说明：切换立即生效，但重启后回到环境变量配置（避免"改了又不生效"的误解）
        "switch_note": "切换立即生效（无需重启）；如需永久生效，请同步修改环境变量后重启服务。",
    }


@router.post("/models/switch", summary="切换主模型（热切换，无需重启）")
async def switch_model(body: dict, request: Request, user=Depends(current_user)):
    """把 chain 中任一后端提为主模型，原主模型退为备用。

    只做指针交换，不重新加载权重——所以只在已加载的后端之间切换，
    传了不存在的 key 会明确报错，而不是默默忽略。
    """
    _assert_admin(user)
    key = str((body or {}).get("key") or "").strip()
    if not key:
        raise BadRequest("请指定要切换的模型 key")
    container = request.app.state.container
    llm = container.llm
    target = next((b for b in llm.chain if f"{b.name}:{b.model}" == key), None)
    if target is None:
        raise BadRequest(f"模型 {key} 不在已加载的模型链中")
    if target is llm.primary:
        return {"ok": True, "primary_key": llm.primary_key, "changed": False}
    old_primary = llm.primary
    llm.primary = target
    llm.fallback = old_primary
    audit.record("model_switch", user_id=user.user_id, user_name=user.display_name,
                 detail=f"主模型 {old_primary.name}:{old_primary.model} → {target.name}:{target.model}",
                 changed=True)
    return {"ok": True, "primary_key": llm.primary_key, "changed": True}


@router.post("/backup", summary="手动触发完整备份（PostgreSQL 全量 + data 目录）")
async def trigger_backup(user=Depends(current_user)):
    """完整备份。

    注意：这里**不做静默降级**。数据库备份失败会直接返回 500 并说明原因——
    "以为备份成功其实没备份"比"明确告知备份失败"危险得多。
    """
    _assert_admin(user)
    try:
        mf = backup_core.create_backup(reason="manual")
    except backup_core.BackupError as e:
        audit.record("data_backup_failed", user_id=user.user_id,
                     user_name=user.display_name, detail=str(e)[:300], changed=False)
        raise BadRequest(f"备份失败：{e}") from e
    except Exception as e:  # noqa: BLE001 —— 兜住未知异常，但同样显式报错
        audit.record("data_backup_failed", user_id=user.user_id,
                     user_name=user.display_name, detail=str(e)[:300], changed=False)
        raise BadRequest(f"备份失败：{e}") from e

    audit.record("data_backup", user_id=user.user_id, user_name=user.display_name,
                 detail=f"完整备份 {mf['name']}（{mf['total_kb']} KB，"
                        f"含 {len(mf['parts'])} 部分）", changed=True)
    return {"ok": True, **mf}


@router.get("/backups", summary="备份历史列表（含完整性与可恢复性标记）")
async def list_backups(user=Depends(current_user)):
    _assert_admin(user)
    try:
        items = backup_core.list_backups()
    except Exception as e:  # noqa: BLE001 —— 列表属于只读查询，失败给空列表并说明
        return {"dir": str(backup_core.backup_root()), "items": [], "error": str(e)}
    return {"dir": str(backup_core.backup_root()), "keep": settings.BACKUP_KEEP,
            "items": items}


@router.post("/restore", summary="从指定备份恢复（破坏性操作，需二次确认）")
async def restore_backup(body: dict, user=Depends(current_user)):
    """恢复数据库（顺带恢复 data 目录）。

    安全设计：
      · 恢复前**自动对当前库做一次安全备份**，返回的 revert_to 可用于反悔
      · 安全备份失败则中止恢复，绝不把自己置于"恢复后无法回退"的境地
      · 必须传 confirm=true，防止误点
    """
    _assert_admin(user)
    name = str(body.get("name") or "").strip()
    if not name:
        raise BadRequest("必须指定要恢复的备份 name")
    if body.get("confirm") is not True:
        raise BadRequest("恢复是破坏性操作，请传 confirm=true 二次确认")

    try:
        r = backup_core.restore_database(name, safety_backup=True)
        d = backup_core.restore_data(name)
    except backup_core.BackupError as e:
        audit.record("data_restore_failed", user_id=user.user_id,
                     user_name=user.display_name, detail=str(e)[:300], changed=False)
        raise BadRequest(f"恢复失败：{e}") from e

    if d:
        r["data_restored"] = True
    audit.record("data_restore", user_id=user.user_id, user_name=user.display_name,
                 detail=f"从备份 {name} 恢复（可回退到 {r.get('revert_to')}）", changed=True)
    return r

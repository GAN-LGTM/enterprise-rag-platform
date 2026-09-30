"""
请假 / 审批 REST 接口。

申请人侧：查看我的请假、进行中的草稿、未读结果通知。
审批人侧：查看待我审批、通过 / 驳回（严格校验「当前待审步骤的审批人 == 当前用户」）。

所有接口均经 current_user 鉴权；审批接口越权返回 404（不暴露他人单据存在）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db.models import USERS
from ..logging_setup import get_logger
from .deps import current_user
from ..leave import leave_store as lstore
from ..leave import flow as leave_flow

router = APIRouter(prefix="/api/leave", tags=["请假审批"])
log = get_logger("api.leave")


# ============================================================
#  请求体
# ============================================================
class DecisionRequest(BaseModel):
    comment: str | None = None


# ============================================================
#  申请人侧
# ============================================================
@router.get("/my", summary="我的请假列表")
async def my_leaves(user=Depends(current_user)):
    items = lstore.list_requests_by_applicant(user.user_id)
    return [_public(req) for req in items]


@router.get("/draft", summary="我进行中的请假草稿")
async def my_draft(user=Depends(current_user)):
    d = lstore.get_draft(user.user_id)
    if not d:
        return {"draft": None}
    return {"draft": d}


@router.delete("/draft", summary="放弃进行中的请假草稿")
async def drop_draft(user=Depends(current_user)):
    """
    放弃当前草稿。

    为什么要有这个接口：只要草稿存在，**后续所有聊天消息都会被当成对该草稿的填空**
    （`chat_stream` 里"进行中的草稿优先续答"）。用户中途不想请了却没有任何退出方式，
    会被永久卡在引导流程里，连正常知识问答都用不了。
    """
    existed = lstore.get_draft(user.user_id) is not None
    lstore.delete_draft(user.user_id)
    log.info("leave.draft_dropped", user=user.username, existed=existed)
    return {"ok": True, "dropped": existed}


@router.get("/notify", summary="我的未读审批结果通知（读取即标记已读）")
async def my_notify(user=Depends(current_user)):
    return {"events": lstore.pop_notifications(user.user_id)}


@router.get("/{rid}", summary="请假详情（申请人或审批人可看）")
async def leave_detail(rid: str, user=Depends(current_user)):
    req = lstore.get_request(rid)
    if req is None:
        raise HTTPException(status_code=404, detail="请假单不存在")
    if req["applicant_id"] != user.user_id and not _is_approver(req, user.user_id):
        raise HTTPException(status_code=404, detail="请假单不存在")
    return _public(req)


# ============================================================
#  审批人侧
# ============================================================
@router.get("/pending/me", summary="待我审批列表")
async def pending_me(user=Depends(current_user)):
    items = lstore.list_pending_for_approver(user.user_id)
    return [_public(req) for req in items]


@router.post("/{rid}/approve", summary="通过当前审批步骤")
async def approve(rid: str, body: DecisionRequest, user=Depends(current_user)):
    req = lstore.approve_step(rid, user.user_id, body.comment)
    if req is None:
        raise HTTPException(status_code=404, detail="无权限或单据状态不可审批")
    from ..core import audit
    audit.record("leave_approve", user_id=user.user_id, user_name=user.display_name,
                 detail=f"通过 {req['applicant_name']} 的请假申请", rid=rid)
    return _decision_result(req, user)


@router.post("/{rid}/reject", summary="驳回当前审批步骤")
async def reject(rid: str, body: DecisionRequest, user=Depends(current_user)):
    req = lstore.reject_step(rid, user.user_id, body.comment)
    if req is None:
        raise HTTPException(status_code=404, detail="无权限或单据状态不可审批")
    from ..core import audit
    audit.record("leave_reject", user_id=user.user_id, user_name=user.display_name,
                 detail=f"驳回 {req['applicant_name']} 的请假申请", rid=rid)
    return _decision_result(req, user)


# ============================================================
#  辅助
# ============================================================
def _is_approver(req: dict, user_id: str) -> bool:
    return any(s["approver_id"] == user_id for s in req["chain"])


def _current_step(req: dict) -> dict | None:
    return next((s for s in req["chain"] if s["status"] == "pending"), None)


def _public(req: dict) -> dict:
    """对外裁剪：去掉内部字段（这里基本都安全，统一封装便于以后扩展）。"""
    return dict(req)


def _decision_result(req: dict, user) -> dict:
    cur = _current_step(req)
    if req["status"] == "approved":
        return {"ok": True, "status": "approved",
                "message": f"✅ 审批已全部通过，已通知申请人 {req['applicant_name']}，请假生效。"}
    if req["status"] == "rejected":
        return {"ok": True, "status": "rejected",
                "message": f"已驳回 {req['applicant_name']} 的请假申请。"}
    # 仍在流转
    return {"ok": True, "status": "pending",
            "message": f"✅ 您已通过，流转至下一步：{cur['approver_name']} 待审批。"}

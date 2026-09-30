"""通知中心接口：聚合通用通知 + 知识缺口补充通知，统一已读语义。

请假/报销审批结果走 chat 内 push（pop 语义），不在此聚合，避免两处消费竞争。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core import notification_store
from ..core.gap_store import mark_notification_read, pull_notifications
from .deps import current_user

router = APIRouter(prefix="/api/notifications", tags=["通知中心"])


@router.get("", summary="我的通知（通用 + 知识缺口），含未读数")
async def my_notifications(user=Depends(current_user)):
    general = notification_store.list_for(user.username, limit=50)
    # gap 通知按 display_name 匹配（历史设计如此），拉全部（含已读）以统一展示
    gap_items = pull_notifications(user.display_name, unread_only=False)
    gap_view = [{
        "id": n["id"], "type": "gap", "title": "知识缺口已补充",
        "message": n.get("message", ""), "created_at": n.get("created_at", 0),
        "read": bool(n.get("read")),
    } for n in gap_items]

    items = general["items"] + gap_view
    items.sort(key=lambda x: -x.get("created_at", 0.0))
    unread = general["unread"] + sum(1 for n in gap_view if not n["read"])
    return {"unread": unread, "items": items[:80]}


@router.post("/read", summary="标记已读（ids 为空 = 全部已读）")
async def read_notifications(body: dict, user=Depends(current_user)):
    ids = body.get("ids")
    ids = [str(i) for i in ids] if isinstance(ids, list) else None
    n1 = notification_store.mark_read(user.username, ids)
    # gap 通知只能逐条标记
    n2 = 0
    gap_items = pull_notifications(user.display_name, unread_only=True)
    for n in gap_items:
        if ids and n["id"] not in ids:
            continue
        if mark_notification_read(n["id"], user.display_name):
            n2 += 1
    return {"ok": True, "marked": n1 + n2}

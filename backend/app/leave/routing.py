"""
请假审批路由：根据申请人部门 + 请假类型/天数，生成有序审批链。

规则（演示版，可在生产替换为 HR 系统/钉钉职位体系）：
  · 一级审批人 = 申请人所在部门的负责人
      - 营销事业部各子部门 → 营销总监(u_mkt_d)
      - 技术事业部各子部门 → 技术总监(u_tech_d)
      - 财务综合           → 财务主管(u_fin_m)
      - 人力部 / 法务部    → 总经理(u_ceo)（无独立总监）
      - 公共库 / 管理员     → 系统管理员(u_admin)
  · 二级审批人（满足以下任一条件时追加，作为高管/HR 复核）
      - 请假天数 >= 3 天
      - 请假类型 ∈ {年假, 产假, 工伤假, 婚假, 丧假}
      复核人固定为总经理(u_ceo)；若一级已是 ceo 则不再重复。
"""
from __future__ import annotations

from typing import Any

from ..db.models import USERS, dept_name
from ..logging_setup import get_logger

log = get_logger("leave.routing")

# 部门 → 一级审批人 user_id
DEPT_PRIMARY_APPROVER: dict[str, str] = {
    "mkt.sales": "u_mkt_d",
    "mkt.market": "u_mkt_d",
    "mkt.service": "u_mkt_d",
    "tech.rd": "u_tech_d",
    "tech.qa": "u_tech_d",
    "tech.ops": "u_tech_d",
    "fin.general": "u_fin_m",
    "hr": "u_ceo",
    "legal": "u_ceo",
    "public": "u_admin",
}

# 需要高管/HR 复核的请假类型
SECONDARY_TYPES = {"年假", "产假", "工伤假", "婚假", "丧假"}
SECONDARY_APPROVER = "u_ceo"
LONG_LEAVE_DAYS = 3


def _approver_display(user_id: str) -> str:
    for u in USERS.values():
        if u.user_id == user_id:
            return u.display_name
    return user_id


def build_chain(dept_id: str, leave_type: str, duration_days: float) -> list[dict[str, Any]]:
    """返回有序审批链：[{approver_id, approver_name, step, status, acted_at, comment}]。"""
    primary = DEPT_PRIMARY_APPROVER.get(dept_id, "u_admin")
    ids: list[str] = [primary]

    need_secondary = (
        duration_days >= LONG_LEAVE_DAYS
        or leave_type in SECONDARY_TYPES
    )
    if need_secondary and primary != SECONDARY_APPROVER:
        ids.append(SECONDARY_APPROVER)

    chain = []
    for i, uid in enumerate(ids, 1):
        chain.append({
            "approver_id": uid,
            "approver_name": _approver_display(uid),
            "step": i,
            "status": "pending",       # pending / approved / rejected
            "acted_at": None,
            "comment": "",
        })
    log.info("leave.chain_built", dept=dept_id, type=leave_type,
             days=duration_days, approvers=[c["approver_id"] for c in chain])
    return chain


def approver_ids(dept_id: str, leave_type: str, duration_days: float) -> list[str]:
    return [c["approver_id"] for c in build_chain(dept_id, leave_type, duration_days)]


def primary_approver_name(dept_id: str) -> str:
    return _approver_display(DEPT_PRIMARY_APPROVER.get(dept_id, "u_admin"))

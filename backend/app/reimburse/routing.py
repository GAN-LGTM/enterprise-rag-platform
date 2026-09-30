"""
报销审批路由：根据申请人部门 + 报销金额，生成有序审批链。

规则（演示版，可在生产替换为 HR/财务系统或钉钉职位体系）：
  · 一级审批人 = 申请人所在部门的负责人
      - 营销事业部各子部门 → 营销总监(u_mkt_d)
      - 技术事业部各子部门 → 技术总监(u_tech_d)
      - 财务综合           → 财务主管(u_fin_m)
      - 人力部 / 法务部    → 总经理(u_ceo)（无独立总监）
      - 公共库 / 管理员     → 系统管理员(u_admin)
  · 二级审批人（报销必经，除非一级已是财务负责人）：
      财务复核固定为财务主管(u_fin_m) —— 报销涉及资金，必须经过财务把关。
  · 三级审批人（满足以下任一条件时追加，作为高管终审）：
      - 报销金额 >= 5000 元
      - 申请人本身就是财务综合部门（财务人员的报销需总经理复核，避免自批）
      复核人固定为总经理(u_ceo)；若链尾已是 ceo 则不再重复。
"""
from __future__ import annotations

from typing import Any

from ..db.models import USERS
from ..logging_setup import get_logger

log = get_logger("reimburse.routing")

# 部门 → 一级审批人 user_id（与请假共用同一套部门负责人映射）
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

FINANCE_APPROVER = "u_fin_m"        # 财务复核人
CEO_APPROVER = "u_ceo"              # 总经理（高管终审）
LARGE_AMOUNT = 5000.0              # 大额阈值（元）


def _approver_display(user_id: str) -> str:
    for u in USERS.values():
        if u.user_id == user_id:
            return u.display_name
    return user_id


def build_chain(dept_id: str, rb_type: str, amount: float) -> list[dict[str, Any]]:
    """返回有序审批链：[{approver_id, approver_name, step, status, acted_at, comment}]。"""
    primary = DEPT_PRIMARY_APPROVER.get(dept_id, "u_admin")
    ids: list[str] = [primary]

    # 报销必经财务复核（除非一级审批人本身就是财务负责人）
    if primary != FINANCE_APPROVER:
        ids.append(FINANCE_APPROVER)

    # 大额 或 财务本部门（需总经理终审，避免自批）
    if dept_id == "fin.general" or float(amount or 0) >= LARGE_AMOUNT:
        ids.append(CEO_APPROVER)

    # 去重保序（例如一级=财务时不再重复追加财务）
    dedup: list[str] = []
    for x in ids:
        if x not in dedup:
            dedup.append(x)
    ids = dedup

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
    log.info("reimburse.chain_built", dept=dept_id, type=rb_type,
             amount=amount, approvers=[c["approver_id"] for c in chain])
    return chain


def approver_ids(dept_id: str, rb_type: str, amount: float) -> list[str]:
    return [c["approver_id"] for c in build_chain(dept_id, rb_type, amount)]


def primary_approver_name(dept_id: str) -> str:
    return _approver_display(DEPT_PRIMARY_APPROVER.get(dept_id, "u_admin"))

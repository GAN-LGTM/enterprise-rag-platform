"""
权限校验门 —— 6 条递进式判断规则（对应需求书 FR-PERM-01）。

核心原则：**检索前拦截，而不是检索后过滤**。
检索后过滤意味着已经"碰过"无权限的向量表，在安全审计层面等同越权访问尝试。
所以这里必须放在 LangGraph 的 retrieve 节点之前，判定不通过直接短路到错误分支。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
from ..db.models import Department, DEPARTMENTS, User, dept_name, get_accessible_depts, sub_departments_of
from ..logging_setup import get_logger
from ..metrics import PERM_DENIED

log = get_logger("perm")


@dataclass
class PermissionDecision:
    allowed: bool
    dept_ids: list[str] = field(default_factory=list)   # 允许检索的部门白名单（SQL WHERE IN）
    hit_rule: str = ""                                   # 命中第几条规则（审计留痕）
    reason: str = ""
    user_message: str = ""


_DENY_MSG = "抱歉，您没有访问该知识的权限。如有需要请联系本部门主管或管理员开通。"


def check_permission(user: User, intent: str, target_depts: list[str] | None) -> PermissionDecision:
    accessible = get_accessible_depts(user)
    accessible_set = set(accessible)
    audit = {"user_id": user.user_id, "intent": intent, "target": target_depts or []}

    # ---------- 规则 1：身份有效性 ----------
    if not user or not user.dept_id:
        PERM_DENIED.labels(reason="invalid_identity").inc()
        log.warning("perm.denied", rule="R1", **audit)
        return PermissionDecision(False, hit_rule="R1-身份无效", reason="身份信息缺失",
                                  user_message="登录状态异常，请重新登录。")

    # ---------- 规则 2：管理员全量放行 ----------
    if user.role == "admin":
        log.info("perm.allow", rule="R2", **audit)
        return PermissionDecision(True, list(DEPARTMENTS.keys()), hit_rule="R2-管理员全量",
                                  reason="管理员拥有全部部门访问权")

    # ---------- 规则 3：公共知识库 ----------
    if intent == "public_kb":
        if settings.PUBLIC_DEPT_ID in accessible_set:
            # 公共库全员可见；同时必须保留用户"天然可看"的范围——
            # 本部门永远保留；主管/总监/高管再加其管辖范围。
            # 否则问题被判成"公共类"后，部门数据文档（含管辖部门）会检索不到。
            scope = [settings.PUBLIC_DEPT_ID]
            managed_scope: list[str] = []
            if user.role in ("executive", "dept_director"):
                parents = user.managed_dept_ids if user.role == "executive" else [
                    DEPARTMENTS[user.dept_id].parent_id
                ]
                for parent in parents or []:
                    subs = sub_departments_of(parent)
                    managed_scope.extend(subs if subs else [parent])
            for d in [user.dept_id] + managed_scope:
                if d in accessible_set and d not in scope:
                    scope.append(d)
            log.info("perm.allow", rule="R3", **audit)
            return PermissionDecision(True, scope, hit_rule="R3-公共库",
                                      reason="公共库 + 本部门/管辖范围")
        PERM_DENIED.labels(reason="public_denied").inc()
        log.warning("perm.denied", rule="R3", **audit)
        return PermissionDecision(False, hit_rule="R3", reason="无公共库权限", user_message=_DENY_MSG)

    # ---------- 规则 4：本部门查询 ----------
    if intent == "dept_kb":
        if user.dept_id in accessible_set:
            # 本部门 + 公共库一起查；主管/总监/高管再加管辖范围（均在白名单内）
            scope = [d for d in [user.dept_id, settings.PUBLIC_DEPT_ID] if d in accessible_set]
            if user.role in ("executive", "dept_director"):
                parents = user.managed_dept_ids if user.role == "executive" else [
                    DEPARTMENTS[user.dept_id].parent_id
                ]
                for parent in parents or []:
                    subs = sub_departments_of(parent)
                    for d in (subs if subs else [parent]):
                        if d in accessible_set and d not in scope:
                            scope.append(d)
            log.info("perm.allow", rule="R4", **audit)
            return PermissionDecision(True, scope, hit_rule="R4-本部门",
                                      reason=f"限定 {dept_name(user.dept_id)} 及管辖范围")
        PERM_DENIED.labels(reason="own_dept_denied").inc()
        return PermissionDecision(False, hit_rule="R4", reason="本部门不在白名单", user_message=_DENY_MSG)

    # ---------- 规则 5：跨部门查询（重点）----------
    if intent == "cross_dept":
        targets = target_depts or []
        if not targets:
            # 没点名具体部门 → 收缩到"用户有权访问的全部部门"的最小安全集，而不是全库
            log.info("perm.allow", rule="R5", **audit, note="未点名部门，按白名单收敛")
            return PermissionDecision(True, accessible, hit_rule="R5-跨部门(白名单收敛)",
                                      reason="目标部门未明确，按可访问范围检索")

        illegal = [d for d in targets if d not in accessible_set]
        if illegal:
            PERM_DENIED.labels(reason="cross_dept_illegal").inc()
            log.warning("perm.denied", rule="R5", illegal=illegal, **audit)
            # 注意：拒绝文案里不透露目标部门是否存在（防侧信道探测）
            return PermissionDecision(
                False, hit_rule="R5-跨部门越权",
                reason=f"目标部门越权: {illegal}",
                user_message=_DENY_MSG,
            )
        scope = list(dict.fromkeys(targets + [settings.PUBLIC_DEPT_ID]))
        log.info("perm.allow", rule="R5", **audit)
        return PermissionDecision(True, scope, hit_rule="R5-跨部门放行", reason="权限校验通过")

    # ---------- 规则 6：兜底（闲聊/数据分析/操作型）----------
    if intent in ("chitchat", "operation"):
        # 不走向量库，无需部门范围
        return PermissionDecision(True, [], hit_rule="R6-非检索意图", reason="无需检索知识库")

    if intent == "data_analysis":
        # RAG + SQL 混合；SQL 侧同样带 dept_ids 过滤，向量侧走白名单
        return PermissionDecision(True, accessible, hit_rule="R6-数据分析",
                                  reason="混合检索，SQL 与向量均带部门过滤")

    # 未知意图 → 最小权限原则，只给本部门
    scope = [d for d in [user.dept_id, settings.PUBLIC_DEPT_ID] if d in accessible_set]
    return PermissionDecision(True, scope, hit_rule="R6-默认最小权限", reason="未知意图按最小范围")


def assert_dept_ids(dept_ids: list[str] | None) -> list[str]:
    """
    应用层强制约束：所有检索方法必须显式传 dept_ids，不传直接抛异常。
    —— 三层防护的第一层，防止开发者漏写 WHERE 条件。
    """
    if dept_ids is None:
        raise ValueError("SECURITY: 检索方法必须显式传入 dept_ids，禁止无过滤全表查询")
    if not isinstance(dept_ids, list):
        raise TypeError("SECURITY: dept_ids 必须是 list[str]")
    return dept_ids

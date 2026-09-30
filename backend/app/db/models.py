"""组织、用户、权限与审计的领域模型（内存实现，便于零依赖跑通；生产可平替为 DB 表）。"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings


@dataclass
class Department:
    id: str
    name: str
    parent_id: str | None = None      # None = 事业部层级
    level: int = 1                    # 1=事业部 2=子部门


@dataclass
class User:
    user_id: str
    username: str
    password_hash: str
    display_name: str
    role: str                          # admin/executive/dept_director/sub_manager/sub_employee
    dept_id: str                       # 所属子部门（admin 可为 "*"）
    managed_dept_ids: list[str] = field(default_factory=list)  # executive 管辖范围
    can_upload: bool = False
    must_change_password: bool = False  # 首次登录强制改密（FR-AUTH-01）


# ============================================================
# 组织架构：营销事业部 / 技术事业部 / 财务部 / 人力资源部 / 法务部
# ============================================================
DEPARTMENTS: dict[str, Department] = {
    settings.PUBLIC_DEPT_ID: Department(settings.PUBLIC_DEPT_ID, "公共知识库", None, 0),
    "mkt": Department("mkt", "营销事业部", None, 1),
    "mkt.sales": Department("mkt.sales", "销售部", "mkt", 2),
    "mkt.market": Department("mkt.market", "市场部", "mkt", 2),
    "mkt.service": Department("mkt.service", "客服部", "mkt", 2),
    "tech": Department("tech", "技术事业部", None, 1),
    "tech.rd": Department("tech.rd", "研发部", "tech", 2),
    "tech.qa": Department("tech.qa", "测试部", "tech", 2),
    "tech.ops": Department("tech.ops", "运维部", "tech", 2),
    "fin": Department("fin", "财务部", None, 1),
    "fin.general": Department("fin.general", "财务综合", "fin", 2),
    "hr": Department("hr", "人力资源部", None, 1),
    "legal": Department("legal", "法务部", None, 1),
}

ALL_DEPT_IDS = list(DEPARTMENTS.keys())


def dept_name(dept_id: str) -> str:
    d = DEPARTMENTS.get(dept_id)
    return d.name if d else dept_id


def sub_departments_of(parent_id: str) -> list[str]:
    return [d.id for d in DEPARTMENTS.values() if d.parent_id == parent_id]


def ancestor_chain(dept_id: str) -> list[str]:
    """部门自身的祖先链，根在前、自身在后，例如 tech.rd → ['tech', 'tech.rd']。"""
    chain: list[str] = []
    cur = DEPARTMENTS.get(dept_id)
    while cur:
        chain.append(cur.id)
        cur = DEPARTMENTS.get(cur.parent_id) if cur.parent_id else None
    return list(reversed(chain))


def approvers_of(department_id: str) -> set[str]:
    """谁有资格审批「访问某部门知识库」的申请 —— 按组织树判定，而非按审批人自己能看到哪些部门。

    判定规则（企业常见做法）：
      · admin            → 全域审批权
      · dept_director    → 本事业部（含其下所有子部门与事业部节点本身）
      · executive        → 所管辖事业部及其下子部门
      · sub_manager      → 本子部门（通常是申请人的直属主管）
      · 其余角色          → 无审批权

    兜底：若找不到任何审批人（组织树边缘情况），返回全部 admin，
    避免出现"没人能审、申请永远卡住"的死单。
    """
    if department_id not in DEPARTMENTS and department_id != settings.PUBLIC_DEPT_ID:
        return set()
    chain = ancestor_chain(department_id)
    # 目标部门的管辖范围 = 祖先链上任一节点 + 其全部下级
    scope: set[str] = set(chain)
    for node in chain:
        scope.update(_all_descendants(node))

    out: set[str] = set()
    for u in USERS.values():
        if u.role == "admin":
            out.add(u.username)
        elif u.role == "dept_director":
            if u.dept_id in scope:
                out.add(u.username)
        elif u.role == "executive":
            managed = set(u.managed_dept_ids)
            if (scope & managed) or u.dept_id in scope:
                out.add(u.username)
        elif u.role == "sub_manager" and u.dept_id == department_id:
            out.add(u.username)

    if not out:
        out = {u.username for u in USERS.values() if u.role == "admin"}
    return out


def _all_descendants(parent_id: str) -> list[str]:
    """不含自身的全部子孙部门（广度展开，避免嵌套死循环）。"""
    out: list[str] = []
    queue = [parent_id]
    seen = {parent_id}
    while queue:
        cur = queue.pop(0)
        for sub in sub_departments_of(cur):
            if sub not in seen:
                seen.add(sub)
                out.append(sub)
                queue.append(sub)
    return out


def _u(uid, name, pwd, display, role, dept, managed=None, upload=False,
       must_change=None):
    """构造内置账号（仅演示模式使用；生产账号由 LDAP/SSO 或管理员导入）。"""
    from ..core.security import hash_password

    # 首次登录强制改密开关：演示环境默认关闭以免阻塞体验；
    # 生产建议开启（FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN=true）。
    if must_change is None:
        must_change = settings.FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN
    return User(uid, name, hash_password(pwd), display, role, dept,
                managed or [], upload, must_change)


# 演示账号集合：仅在 DEMO_MODE=true（dev 默认）时注册，生产交付不会加载。
_DEMO_USERS: dict[str, User] = {
    "admin": _u("u_admin", "admin", "admin123", "系统管理员", "admin", "*", ALL_DEPT_IDS, True),
    # 营销事业部
    "mkt_director": _u("u_mkt_d", "mkt_director", "123456", "营销总监-李娜", "dept_director", "mkt.sales", ["mkt"], upload=True),
    "sales_mgr": _u("u_sales_m", "sales_mgr", "123456", "销售主管-王强", "sub_manager", "mkt.sales", upload=True),
    "sales_emp": _u("u_sales_e", "sales_emp", "123456", "销售专员-张三", "sub_employee", "mkt.sales"),
    "market_emp": _u("u_mkt_e", "market_emp", "123456", "市场专员-赵敏", "sub_employee", "mkt.market"),
    # 技术事业部
    "tech_director": _u("u_tech_d", "tech_director", "123456", "技术总监-陈晨", "dept_director", "tech.rd", ["tech"], upload=True),
    "rd_emp": _u("u_rd_e", "rd_emp", "123456", "研发工程师-孙悟空", "sub_employee", "tech.rd"),
    # 财务部
    "fin_mgr": _u("u_fin_m", "fin_mgr", "123456", "财务主管-周敏", "sub_manager", "fin.general", upload=True),
    "hr_emp": _u("u_hr_e", "hr_emp", "123456", "人力专员-嫦娥", "sub_employee", "hr"),
    "legal_emp": _u("u_legal_e", "legal_emp", "123456", "法务专员-包拯", "sub_employee", "legal"),
    # 跨事业部高管
    "ceo": _u("u_ceo", "ceo", "123456", "总经理-黛玉", "executive", "mkt.sales", ["mkt", "tech", "fin", "hr", "legal"], upload=True),
    # 知识审核员（知识治理角色）：可审阅 / 发布本部门文档，但无审批权、无跨域权限
    "kb_reviewer": _u("u_kb_r", "kb_reviewer", "123456", "知识审核员-王芳", "knowledge_reviewer", "hr", upload=True),
    # 合规审核员（合规角色）：跨部门只读审计视角，可见全部部门文档，但不能上传 / 删除
    "compliance_reviewer": _u("u_comp_r", "compliance_reviewer", "123456", "合规审核员-李明", "compliance_reviewer", "legal"),
}

# 运行期账号表：演示模式直接加载内置演示账号；生产模式为空，
# 由启动自检（core/preflight.py）注入唯一初始管理员，其余账号走对接导入。
USERS: dict[str, User] = dict(_DEMO_USERS) if settings.DEMO_MODE else {}


def builtin_departments(user: User) -> list[str]:
    """按角色规则计算的可访问部门，不含任何手动授权。

    和 get_accessible_depts 的区别很关键：后者 = 角色自带 ∪ 手动授权。
    做「授权差量」时必须用本函数，否则会把自己刚给的授权当成角色自带，
    导致清空授权时删不掉（这是配置页最容易踩的坑）。
    """
    dept_ids: list[str] = [settings.PUBLIC_DEPT_ID]

    if user.role == "admin":
        return ALL_DEPT_IDS

    if user.role == "compliance_reviewer":
        # 合规审核员：跨部门只读审计视角，可见全部部门文档，但不能上传 / 删除
        return ALL_DEPT_IDS

    if user.role == "executive":
        for parent in user.managed_dept_ids:
            # 事业部节点本身也要纳入（如 mkt/tech/fin 一级部门），
            # 否则挂在事业部节点上的库内容高管不可见
            dept_ids.append(parent)
            subs = sub_departments_of(parent)
            if subs:
                dept_ids.extend(subs)
            else:
                # 独立一级部门（如人力资源部/法务部）没有子部门，直接纳入自身
                dept_ids.append(parent)
        dept_ids.append(user.dept_id)

    elif user.role == "dept_director":
        parent = DEPARTMENTS.get(user.dept_id)
        parent_id = parent.parent_id if parent else None
        if parent_id:
            dept_ids.extend(sub_departments_of(parent_id))
        dept_ids.append(user.dept_id)

    else:  # sub_manager / sub_employee
        dept_ids.append(user.dept_id)

    return _dedup(dept_ids)


def get_accessible_depts(user: User) -> list[str]:
    """
    用户实际可访问的部门 ID 列表 —— 检索白名单。

      get_accessible_depts = builtin_departments（角色自带）∪ grants（手动授权）

    规则（对应需求书 2.4.2）：
      admin / compliance_reviewer → 全部部门
      executive     → 所管辖事业部下的全部子部门 + 公共库
      dept_director → 本事业部下全部子部门 + 公共库
      sub_manager   → 本子部门 + 公共库
      sub_employee  → 本子部门 + 公共库（同级子部门互不可见）
    额外叠加：管理员手动授予的跨部门访问（FR-PERM）。
    """
    ids = builtin_departments(user)
    # 合并手动授予的跨部门访问（仅限真实存在的部门，防越权配置）
    for g in _load_grants().get(user.username, []):
        if g in DEPARTMENTS or g == settings.PUBLIC_DEPT_ID:
            ids.append(g)
    return _dedup(ids)


def _dedup(ids: list[str]) -> list[str]:
    seen, out = set(), []
    for d in ids:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ============================================================
# 跨部门手动授权（需求书 FR-PERM：跨事业部高管「手动配置」permission_config）
# 生产替换为 permission_config 表（user_id, target_department_id, granted_by）。
# ============================================================
_GRANTS_PATH = (Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))) / "grants.json"
_GRANTS_LOCK = threading.RLock()
_GRANTS: dict[str, list[str]] = {}
_GRANTS_LOADED = False


def _load_grants() -> dict[str, list[str]]:
    global _GRANTS_LOADED
    if _GRANTS_LOADED:
        return _GRANTS
    _GRANTS_LOADED = True
    try:
        if _GRANTS_PATH.exists():
            raw = json.loads(_GRANTS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _GRANTS.update(raw)
    except Exception:  # noqa: BLE001
        pass
    return _GRANTS


def _save_grants() -> None:
    try:
        _GRANTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GRANTS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_GRANTS, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _GRANTS_PATH)
    except Exception:  # noqa: BLE001
        pass


# 授权元数据（授予人 / 授予时间 / 有效期 / 权限类型）。
# 与 grants.json 分开存放：grants.json 保持"用户名 -> 部门列表"的原结构（向后兼容旧数据），
# 有效期等扩展字段放 grant_meta.json，缺失即视为长期有效。
_GRANT_META_PATH = (Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))) / "grant_meta.json"
_GRANT_META: dict[str, dict[str, dict]] = {}
_GRANT_META_LOADED = False


def _load_grant_meta() -> dict[str, dict[str, dict]]:
    global _GRANT_META_LOADED
    if _GRANT_META_LOADED:
        return _GRANT_META
    _GRANT_META_LOADED = True
    try:
        if _GRANT_META_PATH.exists():
            raw = json.loads(_GRANT_META_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _GRANT_META.update(raw)
    except Exception:  # noqa: BLE001
        pass
    return _GRANT_META


def _save_grant_meta() -> None:
    try:
        _GRANT_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GRANT_META_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_GRANT_META, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _GRANT_META_PATH)
    except Exception:  # noqa: BLE001
        pass


def grant_department(username: str, department_id: str, expires_at: float | None = None,
                     granted_by: str = "", permission_type: str = "read") -> bool:
    """授予某用户访问某部门的权限（手动跨域配置 / 审批通过后自动开通）。返回是否发生变更。

    expires_at 为 Unix 秒时间戳，None 表示长期有效。
    过期判定放在"读取时"而不是靠定时任务清理——定时任务漏跑会让过期权限继续生效，
    而读时判定是幂等且零运维的：时间一到，权限自然失效。
    """
    if department_id not in DEPARTMENTS and department_id != settings.PUBLIC_DEPT_ID:
        return False
    with _GRANTS_LOCK:
        _load_grants()
        _load_grant_meta()
        lst = _GRANTS.setdefault(username, [])
        meta = _GRANT_META.setdefault(username, {})
        old = meta.get(department_id) or {}
        if department_id in lst:
            # 已在列表中：只刷新有效期与权限类型（相当于续期 / 改类型），不重复计入
            meta[department_id] = {
                "granted_by": granted_by or old.get("granted_by", ""),
                "granted_at": old.get("granted_at") or round(time.time(), 3),
                "expires_at": expires_at,
                "permission_type": permission_type,
            }
            _save_grant_meta()
            return False
        lst.append(department_id)
        _save_grants()
        meta[department_id] = {
            "granted_by": granted_by,
            "granted_at": round(time.time(), 3),
            "expires_at": expires_at,
            "permission_type": permission_type,
        }
        _save_grant_meta()
        return True


def revoke_department(username: str, department_id: str) -> bool:
    """撤销某用户的某部门授权。返回是否发生变更。"""
    with _GRANTS_LOCK:
        _load_grants()
        _load_grant_meta()
        lst = _GRANTS.get(username)
        if not lst or department_id not in lst:
            return False
        lst.remove(department_id)
        if not lst:
            _GRANTS.pop(username, None)
        meta = _GRANT_META.get(username)
        if meta:
            meta.pop(department_id, None)
            if not meta:
                _GRANT_META.pop(username, None)
        _save_grants()
        _save_grant_meta()
        return True


def effective_grants(username: str) -> list[str]:
    """仍然有效的授权部门（自动剔除已过期的）。"""
    with _GRANTS_LOCK:
        _load_grants()
        _load_grant_meta()
        meta = _GRANT_META.get(username) or {}
        now = time.time()
        out: list[str] = []
        for d in _GRANTS.get(username, []):
            exp = (meta.get(d) or {}).get("expires_at")
            if exp and now >= float(exp):
                continue          # 已过期：等同未授权
            out.append(d)
        return out


def get_grants(username: str) -> list[str]:
    """该用户被授予的部门列表——只返回仍然有效的（过期即失效，无需后台清理）。"""
    return effective_grants(username)


def get_grant_meta(username: str) -> dict[str, dict]:
    """授权明细（授予人 / 授予时间 / 过期时间 / 权限类型），供管理端展示与合规核对。"""
    with _GRANTS_LOCK:
        _load_grant_meta()
        return {k: dict(v) for k, v in (_GRANT_META.get(username) or {}).items()}


def get_user_by_name(username: str) -> User | None:
    return USERS.get(username)

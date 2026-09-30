"""
审计日志（企业合规基线）：JSON 落盘、内存镜像读、按类型/用户过滤。

记录事件：
  chat        问答（含 intent / dept_ids / trace_id）
  chat_deny   权限拦截
  login_ok / login_fail   登录成功 / 失败
  leave_approve / leave_reject / leave_submit       请假审批流
  reimburse_submit / reimburse_approve / reimburse_reject   报销审批流

生产替换：写入 PostgreSQL audit_logs 表或接入公司 SIEM 即可，函数签名不变。
并发安全：RLock + 原子写（tmp + os.replace）；内存保留最近 5000 条防膨胀。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("core.audit")

_STORE_PATH = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data")) / "audit.json"
_LOCK = threading.RLock()
_ITEMS: list[dict] = []
_LOADED = False
_MAX_ITEMS = 5000


def _load() -> None:
    global _LOADED, _ITEMS
    if _LOADED:
        return
    _LOADED = True
    try:
        if _STORE_PATH.exists():
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                _ITEMS = raw[-_MAX_ITEMS:]
            log.info("audit.loaded", items=len(_ITEMS), path=str(_STORE_PATH))
    except Exception as e:  # noqa: BLE001
        log.warning("audit.load_failed", error=str(e))


def _save() -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_ITEMS[-_MAX_ITEMS:], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STORE_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("audit.save_failed", error=str(e))


# 事件分类：用于风险打标与规则检测（集中定义，避免规则散落各处后口径不一致）
_PERM_CHANGE_TYPES = {          # 权限变更（规格 R6）：任何授予 / 撤销都要能被单独捞出来复核
    "grant_dept", "revoke_dept",
    "perm_request_approve", "perm_request_reject", "perm_request_cancel",
}
_DELETE_TYPES = {               # 删除类（规格 R4）
    "doc_delete", "doc_delete_request", "doc_delete_approve",
    "delete_ok", "delete_request",
}


def _is_sensitive_dept(dept_id: str) -> bool:
    """敏感部门判定：命中即命中其所有子部门（如 fin 覆盖 fin.general）。"""
    d = str(dept_id or "")
    return any(d == s or d.startswith(s + ".") for s in settings.SENSITIVE_DEPTS)


def _dept_ids_of(item: dict) -> list[str]:
    v = item.get("dept_ids")
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


def _risk_of(item: dict) -> str:
    """单条日志的固有风险等级（low / medium / high），写库时打标，便于按等级筛选与导出。

    这里只判定"单条事件本身"的风险；聚合型风险（连续失败、短时间扫部门等）
    由 risk_alerts() 查询时动态计算，两者互补，不可互相替代。
    """
    t = item.get("type", "")
    if t == "login_fail":
        return "medium"
    if t in _PERM_CHANGE_TYPES:
        return "medium"
    if t in _DELETE_TYPES:
        return "medium"
    if t == "chat_deny":
        return "medium"                     # 越权被拦截：说明有人在试探权限边界
    if t == "login_ok" and _is_off_hours(float(item.get("ts") or 0)):
        return "medium"
    if _is_sensitive_dept(item.get("dept") or ""):
        return "medium"
    if any(_is_sensitive_dept(d) for d in _dept_ids_of(item)):
        return "medium"
    return "low"


def record(event_type: str, user_id: str = "", user_name: str = "",
           detail: str = "", **extra) -> None:
    """写一条审计日志。extra 里的 None/空值会被剔除，保证日志干净。

    每条日志都会自动补 trace_id（全链路追踪，便于把同一次请求的多条记录串起来）
    与 risk_level（风险等级，便于审计员按等级筛选 / 导出）。
    """
    item = {
        "ts": round(time.time(), 3),
        "type": event_type,
        "user_id": user_id,
        "user_name": user_name,
        "detail": detail[:500],
        **{k: v for k, v in extra.items() if v is not None},
    }
    if not item.get("trace_id"):
        item["trace_id"] = uuid.uuid4().hex[:16]
    item["risk_level"] = item.get("risk_level") or _risk_of(item)
    with _LOCK:
        _load()
        _ITEMS.append(item)
        _save()


def list_items(event_type: str | None = None, user_id: str | None = None,
               dept: str | None = None, since: int | None = None,
               until: int | None = None, risk: str | None = None,
               limit: int = 100) -> dict:
    """查询审计日志（最新在前）。

    type/user_id/dept/since/until/risk 可任意组合过滤。
    since / until 均为 Unix 秒时间戳，前端传 0 会被忽略（视为不限）。
    risk 为 low / medium / high；历史数据若缺 risk_level 字段会即时补算，保证筛选口径一致。
    """
    with _LOCK:
        _load()
        items = _ITEMS
        if event_type:
            items = [i for i in items if i["type"] == event_type]
        if user_id:
            items = [i for i in items if i.get("user_id") == user_id]
        if dept:
            items = [i for i in items if i.get("dept") == dept]
        if since:
            items = [i for i in items if float(i.get("ts") or 0) >= since]
        if until:
            items = [i for i in items if float(i.get("ts") or 0) <= until]
        if risk:
            items = [i for i in items if risk_of_item(i) == risk]
        total = len(items)
        # 返回前统一补齐 risk_level，前端与导出拿到的数据结构恒定
        out = [{**i, "risk_level": risk_of_item(i)} for i in items[-limit:][::-1]]
        return {"total": total, "items": out}


def risk_of_item(item: dict) -> str:
    """取一条日志的风险等级；历史数据缺字段时按当前规则即时补算。"""
    return item.get("risk_level") or _risk_of(item)


# ============================================================
#  合规异常检测（企业交付 P0：审计不是只给日志，要能发现"不对劲"）
# ============================================================
# 非工作时间窗口（规格 R1）：22:00 - 06:00，以及整个周末，视为异常时段
OFF_HOURS_START = 22
OFF_HOURS_END = 6
# 跨部门探测的统计窗口（规格 R3）：同一用户在 60 秒内摸了 N 个不同部门
CROSS_DEPT_WINDOW = 60


def _is_off_hours(ts: float) -> bool:
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts)
    if dt.weekday() >= 5:          # 5=周六 6=周日
        return True
    return dt.hour >= OFF_HOURS_START or dt.hour < OFF_HOURS_END


def _max_distinct_in_window(pairs: list[tuple[float, str]], win: int) -> tuple[set[str], float, float]:
    """在长度为 win 秒的任意滑窗内，找出"不同部门数"最多的那个窗口。

    返回 (部门集合, 窗口起始时间, 窗口结束时间)。
    用滑窗而不是"整个统计窗口累计"，是因为规格要的是"1 分钟内"这种短时突发特征：
    一个人一天里慢慢查了 12 个部门是正常的，60 秒内扫 12 个才是横向探测。
    """
    pairs = sorted(pairs, key=lambda x: x[0])
    best: set[str] = set()
    b_start = b_end = 0.0
    for idx, (ts, dept) in enumerate(pairs):
        acc = {dept}
        end = ts
        for ts2, dept2 in pairs[idx + 1:]:
            if ts2 - ts > win:
                break
            acc.add(dept2)
            end = ts2
        if len(acc) > len(best):
            best, b_start, b_end = acc, ts, end
    return best, b_start, b_end


def risk_alerts(window_sec: int = 3600, cross_dept_threshold: int = 10,
                fail_threshold: int = 3, delete_threshold: int = 5,
                cross_dept_window: int = CROSS_DEPT_WINDOW,
                limit: int = 100,
                since: float | None = None, until: float | None = None) -> dict:
    """
    基于审计日志的规则式异常检测，返回风险项与判定依据。

    规则（6 条，与《合规审计异常检测规则》一致；每条都带 evidence，便于审计部门复核）：
      R1 非工作时间登录       login_ok 发生在周末或 22:00 - 06:00            → medium
      R2 连续登录失败         window 内 login_fail >= 3 次（疑似暴力破解）    → high
      R3 短时间大量跨部门查询 60 秒滑窗内命中 >= 10 个不同部门（横向探测）      → high
      R4 频繁删除文档         window 内删除类事件 >= 5 次                     → high
      R5 敏感部门访问         访问财务 / 人事 / 法务等敏感部门                → medium
      R6 权限变更             任何权限授予 / 撤销 / 审批操作                  → medium

    阈值均可通过参数覆盖，便于不同客户按行业合规强度调整。
    since / until 给合规报告用（按自然月统计），缺省为"最近 window_sec 秒"。
    """
    from collections import defaultdict

    with _LOCK:
        _load()
        items = _ITEMS
    now = time.time()
    lo = float(since) if since else now - window_sec
    hi = float(until) if until else now + 60
    recent = [i for i in items if lo <= float(i.get("ts") or 0) <= hi]

    alerts: list[dict] = []
    login_fails: defaultdict[str, list[dict]] = defaultdict(list)
    chat_hits: defaultdict[str, list[tuple[float, str]]] = defaultdict(list)
    deletes: defaultdict[str, list[dict]] = defaultdict(list)
    sensitive: defaultdict[str, list[tuple[float, str]]] = defaultdict(list)
    perm_changes: defaultdict[str, list[dict]] = defaultdict(list)

    for i in recent:
        uid = str(i.get("user_id") or "")
        t = i.get("type")
        ts = float(i.get("ts") or 0)
        if t == "login_fail":
            login_fails[uid].append(i)
        elif t == "chat":
            # chat 审计记录带 dept_ids 字段（本次问答命中的所有部门）
            for d in _dept_ids_of(i):
                if d == settings.PUBLIC_DEPT_ID:     # 公共库不算跨部门
                    continue
                chat_hits[uid].append((ts, d))
                if _is_sensitive_dept(d):
                    sensitive[uid].append((ts, d))
        elif t in _DELETE_TYPES:
            # 注意：这里必须是纯删除类事件。早期版本把 perm_request_create 也算进来，
            # 导致"提交权限申请"被误报成频繁删除——已在 _DELETE_TYPES 集中定义时修正。
            deletes[uid].append(i)
        elif t in _PERM_CHANGE_TYPES:
            perm_changes[uid].append(i)
        elif t == "login_ok" and _is_off_hours(ts):
            alerts.append({
                "rule": "R1", "rule_name": "非工作时间登录", "level": "medium",
                "user_id": uid, "user_name": i.get("user_name", ""),
                "ts": i.get("ts"), "count": 1,
                "detail": f"登录时间 {_fmt_ts(ts)}（周末或 "
                          f"{OFF_HOURS_START}:00 - {OFF_HOURS_END}:00）",
            })

    for uid, logs in login_fails.items():
        if len(logs) >= fail_threshold:
            alerts.append({
                "rule": "R2", "rule_name": "连续登录失败", "level": "high",
                "user_id": uid, "user_name": logs[0].get("user_name", ""),
                "ts": max(float(l.get("ts") or 0) for l in logs),
                "count": len(logs),
                "detail": f"统计窗口内失败 {len(logs)} 次（阈值 {fail_threshold}），疑似暴力破解",
            })

    for uid, pairs in chat_hits.items():
        depts, s, e = _max_distinct_in_window(pairs, cross_dept_window)
        if len(depts) >= cross_dept_threshold:
            alerts.append({
                "rule": "R3", "rule_name": "短时间大量跨部门查询", "level": "high",
                "user_id": uid, "user_name": "",
                "ts": s, "count": len(depts),
                "detail": f"{cross_dept_window} 秒内访问了 {len(depts)} 个不同部门"
                          f"（阈值 {cross_dept_threshold}），疑似横向探测："
                          f"{', '.join(sorted(depts))}",
            })

    for uid, logs in deletes.items():
        if len(logs) >= delete_threshold:
            alerts.append({
                "rule": "R4", "rule_name": "频繁删除文档", "level": "high",
                "user_id": uid, "user_name": logs[0].get("user_name", ""),
                "ts": max(float(l.get("ts") or 0) for l in logs),
                "count": len(logs),
                "detail": f"统计窗口内删除类操作 {len(logs)} 次（阈值 {delete_threshold}）",
            })

    for uid, pairs in sensitive.items():
        if not pairs:
            continue
        depts = sorted({d for _, d in pairs})
        alerts.append({
            "rule": "R5", "rule_name": "敏感部门访问", "level": "medium",
            "user_id": uid, "user_name": "",
            "ts": max(ts for ts, _ in pairs), "count": len(pairs),
            "detail": f"统计窗口内访问敏感部门 {len(pairs)} 次：{', '.join(depts)}"
                      f"（需确认是否属于岗位职责范围）",
        })

    for uid, logs in perm_changes.items():
        if not logs:
            continue
        kinds = sorted({l.get("type", "") for l in logs})
        alerts.append({
            "rule": "R6", "rule_name": "权限变更", "level": "medium",
            "user_id": uid, "user_name": logs[0].get("user_name", ""),
            "ts": max(float(l.get("ts") or 0) for l in logs),
            "count": len(logs),
            "detail": f"统计窗口内权限变更 {len(logs)} 次（{', '.join(kinds)}），"
                      f"请核对是否均有审批依据",
        })

    level_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: (level_order.get(a["level"], 3), -float(a.get("ts") or 0)))
    return {
        "window_sec": window_sec,
        "scanned": len(recent),
        "total_alerts": len(alerts),
        "high_alerts": sum(1 for a in alerts if a["level"] == "high"),
        "rules": [
            {"code": "R1", "name": "非工作时间登录", "level": "medium",
             "default": f"周末或 {OFF_HOURS_START}:00 - {OFF_HOURS_END}:00 登录"},
            {"code": "R2", "name": "连续登录失败", "level": "high",
             "default": f"统计窗口内失败 ≥ {fail_threshold} 次"},
            {"code": "R3", "name": "短时间大量跨部门查询", "level": "high",
             "default": f"{cross_dept_window} 秒内跨部门 ≥ {cross_dept_threshold} 个"},
            {"code": "R4", "name": "频繁删除文档", "level": "high",
             "default": f"统计窗口内删除 ≥ {delete_threshold} 次"},
            {"code": "R5", "name": "敏感部门访问", "level": "medium",
             "default": "访问 " + "、".join(settings.SENSITIVE_DEPTS) + " 等敏感部门"},
            {"code": "R6", "name": "权限变更", "level": "medium",
             "default": "任何权限授予 / 撤销 / 审批操作"},
        ],
        "items": alerts[:limit],
    }


def _fmt_ts(ts) -> str:
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(ts)


def export_csv(items: list[dict]) -> str:
    """导出为带 BOM 的 UTF-8 CSV（Excel 直接打开不乱码），字段顺序固定便于审计归档。"""
    import csv
    import io

    header = ["时间", "操作类型", "用户ID", "姓名", "目标部门", "详情", "风险等级", "追踪ID"]
    buf = io.StringIO()
    buf.write("﻿")                       # BOM：Excel 识别 UTF-8
    w = csv.writer(buf)
    w.writerow(header)
    RISK_CN = {"high": "高", "medium": "中", "low": "低"}
    for i in items:
        lvl = risk_of_item(i)
        w.writerow([
            _fmt_ts(i.get("ts")), i.get("type", ""), i.get("user_id", ""),
            i.get("user_name", ""), i.get("dept", ""), i.get("detail", ""),
            RISK_CN.get(lvl, lvl), i.get("trace_id", ""),
        ])
    return buf.getvalue()


def counts_by_type() -> dict[str, int]:
    with _LOCK:
        _load()
        out: dict[str, int] = {}
        for i in _ITEMS:
            out[i["type"]] = out.get(i["type"], 0) + 1
        return out


def report(month: str | None = None) -> dict:
    """按自然月生成合规报告统计（对应 /api/compliance/report）。

    month 格式 YYYY-MM，缺省取当前月。输出对齐《合规报告》要求：
    总操作数 / 按类型分布 / 跨部门查询 / 敏感部门访问 / 权限变更 / 越权拦截 /
    活跃用户 / 风险分布 / 异常数。可直接导出归档，满足月度 / 季度合规报送。
    """
    import datetime as _dt

    if month:
        parts = str(month).split("-")
        if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
            raise ValueError("month 格式应为 YYYY-MM")
        y, m = int(parts[0]), int(parts[1])
        if not 1 <= m <= 12:
            raise ValueError("月份应在 01 - 12 之间")
    else:
        _n = _dt.datetime.now()
        y, m = _n.year, _n.month
    start = _dt.datetime(y, m, 1)
    end = _dt.datetime(y + (m == 12), (m % 12) + 1, 1)
    lo, hi = start.timestamp(), end.timestamp()

    with _LOCK:
        _load()
        items = [i for i in _ITEMS if lo <= float(i.get("ts") or 0) < hi]

    by_type: dict[str, int] = {}
    cross_dept = 0
    cross_dept_depts: set[str] = set()
    sensitive_hits = 0
    perm_changes = 0
    denies = 0
    users: set[str] = set()

    for i in items:
        t = i.get("type", "")
        by_type[t] = by_type.get(t, 0) + 1
        uid = str(i.get("user_id") or "")
        if uid:
            users.add(uid)
        if t == "chat":
            ds = [d for d in _dept_ids_of(i) if d != settings.PUBLIC_DEPT_ID]
            if len(ds) > 1:                       # 一次问答同时命中多个部门 = 跨部门查询
                cross_dept += 1
            cross_dept_depts.update(ds)
            if any(_is_sensitive_dept(d) for d in ds):
                sensitive_hits += 1
        elif t == "chat_deny":
            denies += 1
        if t in _PERM_CHANGE_TYPES:
            perm_changes += 1
        if t != "chat" and _is_sensitive_dept(i.get("dept") or ""):
            sensitive_hits += 1

    alerts = risk_alerts(since=lo, until=min(hi, time.time() + 60))
    return {
        "month": f"{y:04d}-{m:02d}",
        "from": _fmt_ts(lo),
        "to": _fmt_ts(min(hi, time.time())),
        "total_operations": len(items),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "cross_dept_queries": cross_dept,
        "cross_dept_departments": sorted(cross_dept_depts),
        "sensitive_access": sensitive_hits,
        "permission_changes": perm_changes,
        "access_denied": denies,
        "active_users": len(users),
        "risk_distribution": {
            "high": sum(1 for i in items if risk_of_item(i) == "high"),
            "medium": sum(1 for i in items if risk_of_item(i) == "medium"),
            "low": sum(1 for i in items if risk_of_item(i) == "low"),
        },
        "alerts_total": alerts["total_alerts"],
        "alerts_high": alerts["high_alerts"],
        "alert_items": alerts["items"][:20],
    }

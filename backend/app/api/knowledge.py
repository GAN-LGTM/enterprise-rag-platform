"""知识库接口：文档导入（单文件/批量）、检索、管理、版本。"""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from ..config import settings
from ..core import audit, gap_store
from ..db.models import DEPARTMENTS, dept_name, get_accessible_depts
from ..errors import BadRequest, PermissionDenied
from ..ingest.pipeline import ingest_document
from ..ingest.version_store import (current_version, delete_versions, get_version,
                                    list_doc_versions, list_versions)
from ..retrieval.hybrid import confidence_of
from ..schemas.dto import SearchRequest
from .deps import active_user, current_user

router = APIRouter(prefix="/api/knowledge", tags=["知识库"])

_ALLOWED = {"pdf", "docx", "doc", "xlsx", "xls", "csv", "txt", "md"}


def _assert_can_upload(user, department_id: str):
    """需求书 2.4.3：仅部门主管及以上可上传，且只能传到自己有权的部门。"""
    if user.role == "admin":
        return
    if not user.can_upload:
        raise PermissionDenied()
    if department_id not in get_accessible_depts(user):
        raise PermissionDenied()


def _check_file(file: UploadFile):
    ext = (file.filename or "").lower().rsplit(".", 1)[-1]
    if ext not in _ALLOWED:
        raise BadRequest(f"不支持的文件类型 .{ext}，仅支持 {sorted(_ALLOWED)}")


def _stored_name(raw_filename: str | None) -> str:
    """把上传的原始文件名转成"只能落在上传目录内"的安全文件名。

    客户端可以任意构造 filename（`../../etc/x.txt`、`C:\\dir\\a.pdf`），
    直接拼进 os.path.join 就能把文件写到上传目录之外，覆盖应用数据文件。
    这里只取最后一段文件名（两种分隔符都切，因为 Windows 上 os.path.basename
    不认 `/`），去掉前导点（防 `.` / `..`），再前置随机前缀避免重名互相覆盖。
    """
    text = (raw_filename or "").replace("\\", "/")
    base = text.rsplit("/", 1)[-1].strip().lstrip(".").strip()
    if not base or base in {".", ".."}:
        base = "upload.bin"
    return f"{uuid.uuid4().hex[:8]}_{base}"


def _resolve_upload_path(name: str) -> str:
    """拼出最终落盘路径，并二次确认它没有越出上传目录（纵深防御）。"""
    root = os.path.realpath(settings.DOC_UPLOAD_DIR)
    path = os.path.realpath(os.path.join(root, name))
    if path != root and not path.startswith(root + os.sep):
        raise BadRequest("文件名不合法")
    return path


@router.get("/uploadable-departments", summary="当前用户可上传文档的部门列表")
async def uploadable_departments(user=Depends(current_user)):
    """文档管理弹窗的部门下拉数据源：只返回该用户有权上传的部门。

    - admin        → 全部部门（全部门上传权限）
    - can_upload   → 其可访问部门（get_accessible_depts，主管及以上）
    - 其余         → 空列表（前端隐藏上传入口）
    与 _assert_can_upload 的校验口径完全一致，前端不再展示无权限部门。
    """
    if user.role == "admin":
        from ..db.models import ALL_DEPT_IDS
        ids = ALL_DEPT_IDS
    elif user.can_upload:
        ids = get_accessible_depts(user)
    else:
        ids = []
    return [{"id": d, "name": dept_name(d)} for d in ids if d in DEPARTMENTS]


@router.get("/departments", summary="全部部门列表（权限申请下拉数据源）")
async def all_departments(user=Depends(current_user)):
    """返回全部部门（含公共知识库），供权限申请弹窗选择目标部门。只读，不泄露敏感信息。"""
    items = [{"id": settings.PUBLIC_DEPT_ID, "name": "公共知识库"}]
    items += [{"id": d, "name": dept_name(d)} for d in DEPARTMENTS]
    return items


# ============================================================
#  知识缺口反馈闭环（用户侧）
# ============================================================
@router.post("/gap", summary="提交知识缺口反馈（生成治理工单）")
async def submit_gap(body: dict, user=Depends(current_user)):
    """
    用户对答不上来/答不准的提问补充"缺什么"，生成知识缺口工单。

    body:
      question    必填，原始问题
      reasons     必填，至少一项，取值见 gap_store.FEEDBACK_REASONS
                  （inaccurate 答案不准确 / not_found 没有找到资料 /
                   irrelevant_cite 引用不相关 / outdated 内容已过期）
      note        选填，补充说明（期望看到什么资料 / 正确的答案应该是什么）
      department_id  选填，默认归属到当前用户所在部门

    工单会归并到"同一部门 + 同一问题"，多人反馈累积在同一张单上而非刷屏。
    已经被处理过（已补充/已关闭）的工单被再次反馈时会自动重开。
    """
    question = str(body.get("question") or "").strip()
    if not question:
        raise BadRequest("缺少要反馈的问题")

    raw_reasons = body.get("reasons") or []
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    reasons = [str(r) for r in raw_reasons][:8]
    invalid = [r for r in reasons if r not in gap_store.FEEDBACK_REASONS]
    if invalid:
        raise BadRequest(f"未知的反馈原因: {invalid[0]}")
    if not reasons:
        raise BadRequest("请至少选择一个反馈原因")

    note = str(body.get("note") or "").strip()[:1000]

    dept_id = str(body.get("department_id") or user.dept_id)
    accessible = get_accessible_depts(user)
    if dept_id not in accessible:
        # 不允许把工单甩到没权限的部门——那会把自己的问题变成别人的待办噪音
        dept_id = user.dept_id

    rec = gap_store.submit_feedback(
        question, dept_id, reasons, note,
        user_name=user.display_name, user_id=user.user_id,
        trace_id=str(body.get("trace_id") or ""))
    audit.record("gap_feedback", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{question[:60]} | {'/'.join(reasons)}", dept=dept_id,
                 changed=True)
    return {"ok": True, "gap_id": rec.get("id", ""), "status": rec.get("status"),
            "status_label": gap_store.STATUS_LABELS.get(rec.get("status"), ""),
            "feedback_count": len(rec.get("feedbacks") or [])}


@router.get("/gap/mine", summary="我提交的知识缺口反馈及处理进度")
async def my_gaps(limit: int = 20, user=Depends(current_user)):
    """用户侧视角：我报过哪些缺口，现在处理到哪一步了（含已补充文档的提示）。"""
    items = gap_store.my_feedbacks(user.display_name, limit=limit)
    for it in items:
        it["status_label"] = gap_store.STATUS_LABELS.get(it.get("status"), "")
    return {"items": items, "total": len(items)}


@router.get("/gap/notifications", summary="我的知识缺口处理通知")
async def my_gap_notifications(user=Depends(current_user)):
    """轮询式通知：我报的缺口被补充后，这里会出现"该问题已补充"的提示。"""
    items = gap_store.pull_notifications(user.display_name, unread_only=True)
    return {"items": items, "total": len(items)}


@router.post("/gap/notifications/{nid}/read", summary="标记通知已读")
async def read_gap_notification(nid: str, user=Depends(current_user)):
    ok = gap_store.mark_notification_read(nid, user.display_name)
    return {"ok": ok}


@router.post("/upload", summary="上传文档到指定部门知识库")
async def upload_doc(
    request: Request,
    file: UploadFile = File(...),
    department_id: str = Form(...),
    user=Depends(active_user),
):
    _assert_can_upload(user, department_id)
    _check_file(file)

    container = request.app.state.container
    os.makedirs(settings.DOC_UPLOAD_DIR, exist_ok=True)
    path = _resolve_upload_path(_stored_name(file.filename))

    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise BadRequest(f"文件超过 {settings.MAX_UPLOAD_MB}MB 限制")
    with open(path, "wb") as f:
        f.write(content)

    try:
        result = await ingest_document(path, file.filename, department_id, user.username, container)
    except Exception as e:  # noqa: BLE001
        os.remove(path)
        raise BadRequest(str(e)) from e
    return result


@router.post("/batch-upload", summary="批量上传（文件夹/ZIP）")
async def batch_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    department_id: str = Form(...),
    user=Depends(active_user),
):
    _assert_can_upload(user, department_id)
    container = request.app.state.container
    os.makedirs(settings.DOC_UPLOAD_DIR, exist_ok=True)

    results, failed = [], []
    for file in files:
        try:
            _check_file(file)
            path = _resolve_upload_path(_stored_name(file.filename))
            with open(path, "wb") as f:
                f.write(await file.read())
            results.append(
                await ingest_document(path, file.filename, department_id, user.username, container)
            )
        except Exception as e:  # noqa: BLE001
            failed.append({"file": file.filename, "error": str(e)})
    return {"success": results, "failed": failed, "total": len(files)}


@router.post("/search", summary="检索（自动带权限过滤）")
async def search(body: SearchRequest, request: Request, user=Depends(current_user)):
    from ..perm.policy import check_permission
    from ..retrieval.hybrid import hybrid_search

    container = request.app.state.container
    dec = check_permission(user, "dept_kb", None)
    if not dec.allowed:
        raise PermissionDenied()

    chunks, diag = await hybrid_search(body.query, dec.dept_ids, container.embedder,
                                       container.store, top_k=body.top_k)
    return {
        "query": body.query, "dept_ids": dec.dept_ids, "diag": diag,
        "results": [{
            "doc_name": c.doc_name, "page_num": c.page_num, "text": c.chunk_text[:300],
            "department": dept_name(c.department_id), "score": c.score,
            "confidence": confidence_of(c.score),
        } for c in chunks],
    }


@router.get("/docs", summary="文档列表（仅本人有权部门）")
async def list_docs(request: Request, user=Depends(current_user)):
    allowed = set(get_accessible_depts(user))
    return [{"doc_name": d, "department_id": dep, "department_name": dept_name(dep),
             "version": item["version"],
             # 追加字段（向后兼容，老前端忽略即可）
             "updated_at": item.get("updated_at"),
             "uploaded_by": item.get("uploaded_by", ""),
             "chunk_count": item.get("chunk_count", 0),
             "status": item.get("status", "published")}
            for item in list_doc_versions()
            if (d := item["doc_name"]) and (dep := item["department_id"]) in allowed]


@router.get("/health", summary="知识健康度（只读聚合，供前端环形图；不改动任何既有接口）")
async def knowledge_health(user=Depends(current_user)):
    """只读聚合：满意度 + 知识缺口闭环率 + 部门覆盖度 → 综合健康分。

    纯读取既有存储（反馈/缺口/版本），不触碰 LangGraph 工作流、DB 表结构、
    LLM、权限模型与部署方式；属于 §四 允许的「为新增展示数据而新增的接口」。
    """
    from ..core import feedback_store
    from ..db.models import DEPARTMENTS

    docs = list_doc_versions()
    docs_total = len(docs)
    depts_covered = len({d["department_id"] for d in docs})
    depts_total = len(DEPARTMENTS)
    coverage = round(depts_covered / depts_total, 3) if depts_total else 0.0

    fb = feedback_store.feedback_stats()
    satisfaction = fb.get("satisfaction", 0.0)  # 0~1
    feedback_total = fb.get("total", 0)
    negative = fb.get("dislike", 0)            # 不满意（点"没帮助"）次数

    gs = gap_store.gap_summary()
    gap_total = gs.get("total_unique", 0)
    gap_pending = gs.get("pending", 0)
    gap_closure = round(1 - gap_pending / gap_total, 3) if gap_total else 1.0

    # 未覆盖部门（用于健康度归因：哪些部门还没入库文档）
    covered_ids = {d["department_id"] for d in docs}
    missing_departments = [dept_name(dep) for dep in DEPARTMENTS if dep not in covered_ids]

    # 综合健康度：用户满意度 40% + 知识缺口闭环率 30% + 部门文档覆盖率 30%
    score = round((0.4 * satisfaction + 0.3 * gap_closure + 0.3 * coverage) * 100)
    level = "good" if score >= 80 else ("warn" if score >= 60 else "risk")
    components = {
        "satisfaction": {"label": "用户满意度", "value": satisfaction, "weight": 0.4},
        "gap_closure": {"label": "知识缺口闭环率", "value": gap_closure, "weight": 0.3},
        "coverage": {"label": "部门文档覆盖率", "value": coverage, "weight": 0.3},
    }
    return {
        "score": score,
        "level": level,
        "docs_total": docs_total,
        "depts_covered": depts_covered,
        "depts_total": depts_total,
        "satisfaction": satisfaction,
        "gap_closure": gap_closure,
        "coverage": coverage,
        "pending_gaps": gap_pending,
        "total_gaps": gap_total,
        # —— 以下为可解释性扩展字段（新增展示数据，不影响既有字段 ——
        "components": components,
        "breakdown": {
            "feedback_total": feedback_total,
            "negative": negative,
            "pending_gaps": gap_pending,
            "missing_departments": missing_departments,
        },
    }


async def _perform_delete(container, doc_name: str, department_id: str, user, **audit_extra):
    """真正执行删除：向量 + 版本历史 + 缓存失效 + 审计。供直接删除与审批通过后复用。"""
    removed = await container.store.delete_doc(doc_name, department_id)
    delete_versions(doc_name, department_id)
    await container.cache.invalidate_department(department_id)
    from ..core import audit
    audit.record("doc_delete", user_id=user.user_id, user_name=user.display_name,
                 detail=doc_name, dept=department_id, removed_chunks=removed, **audit_extra)
    return removed


@router.delete("/docs/{doc_name}", summary="删除文档（管理员直接删；主管/总监/经理转审批申请）")
async def delete_doc(doc_name: str, request: Request, department_id: str, user=Depends(active_user)):
    _assert_can_upload(user, department_id)
    container = request.app.state.container

    if user.role != "admin":
        # 非管理员：创建删除申请，等管理员审批后由系统真正删除
        from ..core import doc_deletion_store as del_store
        from ..core import audit
        req = del_store.create_request(doc_name, department_id, user.username, user.display_name)
        audit.record("doc_delete_request", user_id=user.user_id, user_name=user.display_name,
                     detail=doc_name, dept=department_id, request_id=req["id"])
        return {"pending_approval": True, "request_id": req["id"],
                "message": "删除文档需管理员审批，已提交申请"}

    removed = await _perform_delete(container, doc_name, department_id, user)
    return {"doc_name": doc_name, "removed_chunks": removed, "deleted": True}


@router.get("/versions/{doc_name}", summary="文档版本历史（FR-KB-05）")
async def doc_versions(doc_name: str, department_id: str, request: Request, user=Depends(current_user)):
    _assert_can_upload(user, department_id)
    return list_versions(doc_name, department_id)


@router.post("/versions/{doc_name}/rollback", summary="回滚到指定历史版本（FR-KB-05）")
async def rollback_doc(
    doc_name: str,
    body: dict,
    request: Request,
    user=Depends(active_user),
):
    """
    回滚 = 用历史版本对应的文件重新入库，生成新的递增版本号（保留全部历史）。
    仅部门主管及以上可操作。
    """
    department_id = str(body.get("department_id") or "")
    version = int(body.get("version") or 0)
    change_note = str(body.get("change_note") or f"回滚到 v{version}").strip()
    _assert_can_upload(user, department_id)

    rec = get_version(doc_name, department_id, version)
    if rec is None:
        raise BadRequest(f"未找到 {doc_name} 在 {department_id} 的 v{version} 版本")
    file_path = rec.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise BadRequest("该历史版本文件不可用（演示种子数据或未保留原文件），无法回滚")

    container = request.app.state.container
    result = await ingest_document(file_path, doc_name, department_id, user.username,
                                  container, change_note=change_note)
    from ..core import audit
    audit.record("doc_rollback", user_id=user.user_id, user_name=user.display_name,
                 detail=doc_name, dept=department_id, from_version=version,
                 to_version=result.get("version"))
    return {"doc_name": doc_name, "department_id": department_id,
            "rolled_back_from": version, "new_version": result.get("version"),
            "chunk_count": result.get("chunk_count"), "ok": True}

"""
钉钉「报销」审批适配器。

职责：把平台内采集到的报销字段，按钉钉 OA 审批（报销模板）的标准字段组装成
processinstance 创建请求，并提交到钉钉。审批动作在钉钉侧完成后，钉钉会通过
回调（本演示用平台内审批台模拟）把结果回写平台。

两种模式：
  · mock=True（默认，无凭证也能跑通全链路）
      直接返回一个形如 DINGMOCK-xxxx 的模拟实例 ID，不发任何外网请求。
  · mock=False 且 DINGTALK_ENABLED=True
      真实调用钉钉 OAPI：
        1) GET  /gettoken                      拿 access_token
        2) POST /topapi/processinstance/create 创建审批实例
      凭证缺失或调用失败时自动降级为 mock，并打 warning 日志，保证服务不中断。

字段映射（钉钉「报销」模板常见字段，详见钉钉开放平台「审批-报销」模板）：
    bxType      报销类型    String  （差旅费/办公用品/业务招待费/交通费/通讯费/培训费/其他）
    amount      报销金额    Double  单位：元
    expenseDate 费用发生日期 Date    毫秒时间戳
    reason      费用说明    String
    payee       收款人      String  （选填）
    approver    审批人      List    员工 userId 列表（按审批顺序）
"""
from __future__ import annotations

import json
import time
import uuid
import urllib.request
from typing import Any

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("dingtalk.reimburse")


def _to_ts(dt) -> int:
    """date/datetime -> 钉钉所需的毫秒时间戳。"""
    return int(dt.timestamp() * 1000)


def _build_form(payload: dict) -> list[dict[str, Any]]:
    """
    钉钉 processinstance/create 的 formComponentValues 结构：
    每一项形如 {"name": 字段名, "value": 字段值}。
    """
    form: list[dict[str, Any]] = [
        {"name": "报销类型", "value": payload.get("rb_type", "")},
        {"name": "报销金额", "value": round(float(payload.get("amount", 0)), 2)},
        {"name": "费用发生日期", "value": _to_ts(payload["expense_date"])},
        {"name": "费用说明", "value": payload.get("reason", "") or "无"},
    ]
    if payload.get("payee"):
        form.append({"name": "收款人", "value": payload["payee"]})
    return form


def create_expense_instance(payload: dict) -> str:
    """
    提交一条报销审批实例（同步；mock 模式零网络）。

    :param payload: 含 rb_type/amount/expense_date(date)/reason/payee/approver_ids(list) 的字典。
    :return: 钉钉审批实例 ID（mock 模式为 DINGMOCK-xxxx）。
    """
    if settings.DINGTALK_MOCK or not settings.DINGTALK_ENABLED:
        instance_id = "DINGMOCK-" + uuid.uuid4().hex[:12].upper()
        log.info("dingtalk.reimburse.mock_create", instance_id=instance_id,
                 rb_type=payload.get("rb_type"),
                 amount=payload.get("amount"),
                 approvers=payload.get("approver_ids"))
        return instance_id

    # ---- 真实模式：调用钉钉 OAPI（失败自动降级 mock）----
    try:
        token = _get_token()
        if not token:
            raise RuntimeError("获取钉钉 access_token 失败")
        approvers = payload.get("approver_ids") or []
        body = {
            "agent_id": int(settings.DINGTALK_AGENT_ID or 0),
            "process_code": settings.DINGTALK_REIMBURSE_PROCESS_CODE,
            "originator_user_id": payload.get("originator_ding_id", ""),
            "dept_id": payload.get("dept_id", ""),
            "form_component_values": _build_form(payload),
            "approvers": approvers,
        }
        raw = _post("https://oapi.dingtalk.com/topapi/processinstance/create",
                    token, body)
        result = (raw or {}).get("result") or {}
        instance_id = result.get("instance_id") or result.get("processInstanceId")
        if not instance_id:
            raise RuntimeError(f"钉钉未返回实例ID: {raw}")
        log.info("dingtalk.reimburse.create_ok", instance_id=instance_id)
        return instance_id
    except Exception as e:  # noqa: BLE001
        log.warning("dingtalk.reimburse.create_failed_fallback_mock", error=str(e))
        return "DINGMOCK-" + uuid.uuid4().hex[:12].upper()


def _get_token() -> str:
    url = (f"https://oapi.dingtalk.com/gettoken?appkey={settings.DINGTALK_APP_KEY}"
           f"&appsecret={settings.DINGTALK_APP_SECRET}")
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310
        data = json.loads(r.read().decode("utf-8"))
    if data.get("errcode") == 0:
        return data.get("access_token", "")
    return ""


def _post(api_url: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{api_url}?access_token={token}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))

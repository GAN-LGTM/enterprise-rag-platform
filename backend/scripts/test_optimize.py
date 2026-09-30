"""
优化项回归测试（对应需求书 FR-KB-05 / FR-CHAT-05 / FR-CHAT-06 / FR-AUTH-01 /
FR-PERM 权限配置 / FR-CHAT-02 L3 / NFR-SEC-07）。

直接驱动领域层 + FastAPI TestClient 做集成验证，无需手动起服务。
运行：python backend/scripts/test_optimize.py
"""
from __future__ import annotations

import os
import sys
import json
import tempfile

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")

# 测试环境与生产隔离：强制内存库 + 关闭 Redis，避免依赖外部组件
os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")

# 隔离持久化层：反馈 / 版本 / 授权全部写到临时目录，测试结果可重复（不受历史运行污染）
os.environ["APP_DATA_DIR"] = tempfile.mkdtemp()

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))

PASS, FAIL = 0, 0
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}  {extra}")


# ============================================================
#  纯逻辑层测试
# ============================================================
def test_feedback_store():
    print("\n[FR-CHAT-05/06] 反馈与热门问题持久化")
    from app.core import feedback_store

    feedback_store._FEEDBACKS.clear()
    feedback_store._HOT.clear()
    feedback_store.record_feedback({"question": "q1", "answer": "a1",
                                   "feedback_type": "like", "department_id": "mkt.sales",
                                   "user_id": "u1"})
    feedback_store.record_feedback({"question": "q2", "answer": "a2",
                                   "feedback_type": "dislike", "department_id": "mkt.sales",
                                   "user_id": "u2"})
    stats = feedback_store.feedback_stats()
    check("满意度统计总数=2", stats["total"] == 2)
    check("满意度=0.5", stats["satisfaction"] == 0.5)

    feedback_store.record_hot("如何报销", "mkt.sales")
    feedback_store.record_hot("如何报销", "mkt.sales")
    feedback_store.record_hot("请假流程", "mkt.sales")
    hot = feedback_store.hot_questions(top=10, department_id="mkt.sales")
    check("热门问题按部门去重计数", len(hot) == 2 and hot[0]["question"] == "如何报销" and hot[0]["ask_count"] == 2)
    total, unique = feedback_store.hot_totals()
    check("热门问题汇总", total == 3 and unique == 2)


def test_version_store():
    print("\n[FR-KB-05] 文档版本管理")
    from app.ingest import version_store

    version_store._STATE.clear()
    v1 = version_store.next_version("docA.txt", "fin.general")
    version_store.record_version("docA.txt", "fin.general", v1, 10, "fin_mgr", "/tmp/a.pdf", "h1")
    v2 = version_store.next_version("docA.txt", "fin.general")
    version_store.record_version("docA.txt", "fin.general", v2, 12, "fin_mgr", "/tmp/a2.pdf", "h2")
    lst = version_store.list_versions("docA.txt", "fin.general")
    check("版本历史含2版且最新在前", lst["current_version"] == 2 and len(lst["history"]) == 2
          and lst["history"][0]["version"] == 2)
    rec = version_store.get_version("docA.txt", "fin.general", 1)
    check("可查到 v1 记录", rec is not None and rec["version"] == 1)
    version_store.delete_versions("docA.txt", "fin.general")
    check("删除后无版本", version_store.current_version("docA.txt", "fin.general") == 0)


def test_grants():
    print("\n[FR-PERM] 跨部门手动授权")
    from app.db import models

    models._GRANTS.clear()
    models._GRANTS_LOADED = True
    ok = models.grant_department("sales_emp", "tech.rd")
    check("授予销售部员工研发部访问", ok is True)
    check("重复授予返回 False", models.grant_department("sales_emp", "tech.rd") is False)
    check("不存在部门授予返回 False", models.grant_department("sales_emp", "nope") is False)
    user = models.USERS["sales_emp"]
    accessible = models.get_accessible_depts(user)
    check("可访问部门合并了授权部门", "tech.rd" in accessible and "mkt.sales" in accessible)
    check("撤销后不再可访问", models.revoke_department("sales_emp", "tech.rd") is True
          and "tech.rd" not in models.get_accessible_depts(user))


def test_config():
    print("\n[NFR-SEC-07 / FR-CHAT-02 L3] 配置与敏感词")
    from app.config import settings

    check("敏感词为列表", isinstance(settings.SENSITIVE_WORDS, list) and len(settings.SENSITIVE_WORDS) > 0)
    check("BERT 默认关闭（离线回退）", settings.BERT_ENABLED is False)
    check("BERT 标签为6意图", len(settings.BERT_LABELS) == 6)

    # 逗号分隔写法必须可解析：否则在 .env 里写 BERT_LABELS=a,b,c 会被当 JSON 解析而启动崩溃
    from app.config import Settings
    s2 = Settings(BERT_LABELS="a,b,c", SENSITIVE_WORDS="x,y")
    check("BERT_LABELS 逗号分隔可解析", s2.BERT_LABELS == ["a", "b", "c"], str(s2.BERT_LABELS))
    check("SENSITIVE_WORDS 逗号分隔可解析", s2.SENSITIVE_WORDS == ["x", "y"], str(s2.SENSITIVE_WORDS))

    from app.intent import bert_classifier
    bert_classifier._initialized = False
    bert_classifier._session = None
    check("BERT 离线时 classify 返回 None", bert_classifier.classify("你好") is None)


def test_classifier_fallback():
    print("\n[FR-CHAT-02 L3] 小模型层离线回退关键词打分")
    from app.intent.classifier import IntentClassifier

    clf = IntentClassifier()
    r = clf._layer_small_model("我们部门的销售提成政策是什么")
    check("关键词打分命中 dept_kb", r is not None and r.intent == "dept_kb", str(r))
    r2 = clf._layer_small_model("公司的报销制度有哪些")
    check("关键词打分命中 public_kb", r2 is not None and r2.intent == "public_kb", str(r2))


# ============================================================
#  集成层测试（FastAPI TestClient）
# ============================================================
def test_integration():
    print("\n[集成] Auth 强制改密 + 反馈 + 授权 + 版本回滚")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        # --- 登录 admin ---
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": ADMIN_PWD})
        check("admin 登录 200", r.status_code == 200, r.text)
        token = r.json()["access_token"]
        check("登录返回 must_change 字段", "must_change_password" in r.json()["user"])
        h = {"Authorization": f"Bearer {token}"}

        # --- 反馈 + 统计 ---
        r = client.post("/api/chat/feedback", headers=h,
                        json={"session_id": "s1", "question": "q",
                              "answer": "a", "feedback_type": "like"})
        check("提交反馈 200", r.status_code == 200, r.text)
        r = client.get("/api/chat/feedback-stats", headers=h)
        check("反馈统计可查", r.status_code == 200 and r.json()["total"] >= 1, r.text)

        # --- 强制改密守卫 ---
        from app.db.models import USERS
        original_hash = USERS["admin"].password_hash      # 用例结束后原样还原，保证脚本可重复执行
        USERS["admin"].must_change_password = True
        r = client.post("/api/chat/feedback", headers=h,
                        json={"session_id": "s2", "question": "q2",
                              "answer": "a2", "feedback_type": "like"})
        check("强制改密期间受保护接口被拦截(401)", r.status_code == 401, r.text)
        # 错误旧密码
        r = client.post("/api/auth/change-password", headers=h,
                        json={"old_password": "wrong", "new_password": "newpass123"})
        check("旧密码错误被拒(401)", r.status_code == 401, r.text)
        # 正确改密（新口令满足复杂度要求：8 位以上 + 大小写 + 数字 + 特殊字符）
        r = client.post("/api/auth/change-password", headers=h,
                        json={"old_password": ADMIN_PWD, "new_password": "Newpass@123"})
        check("正确改密 200", r.status_code == 200 and r.json()["must_change_password"] is False, r.text)
        check("改密后守卫放行", USERS["admin"].must_change_password is False)
        # 还原口令：直接回写原哈希。改密接口强制校验复杂度，
        # 而演示口令（admin123）不满足"大小写+数字+特殊字符"，无法经接口改回。
        USERS["admin"].password_hash = original_hash
        check("口令已还原为初始值", USERS["admin"].password_hash == original_hash)

        # --- 跨部门授权 ---
        r = client.post("/api/admin/grants", headers=h,
                        json={"username": "sales_emp", "department_id": "tech.rd"})
        check("授予跨部门访问 200", r.status_code == 200, r.text)
        r = client.get("/api/admin/grants?username=sales_emp", headers=h)
        check("查询授权含 tech.rd", r.status_code == 200
              and "tech.rd" in r.json()["granted_departments"], r.text)
        r = client.delete("/api/admin/grants?username=sales_emp&department_id=tech.rd", headers=h)
        check("撤销授权 200", r.status_code == 200, r.text)
        r = client.get("/api/admin/grants?username=sales_emp", headers=h)
        check("撤销后授权为空", r.json()["granted_departments"] == [], r.text)

        # --- 文档上传 + 版本 + 回滚 ---
        # 用 fin_mgr（财务主管，可上传）
        rf = client.post("/api/auth/login",
                         json={"username": "fin_mgr", "password": "123456"})
        tk_fin = rf.json()["access_token"]
        hf = {"Authorization": f"Bearer {tk_fin}"}
        content_v1 = "# 差旅标准\n住宿 500 元/晚（v1）".encode("utf-8")
        content_v2 = "# 差旅标准\n住宿 600 元/晚（v2 调整）".encode("utf-8")
        files = {"file": ("travel.md", content_v1, "text/markdown")}
        r = client.post("/api/knowledge/upload", headers=hf, files=files,
                        data={"department_id": "fin.general"})
        check("上传文档 v1 200", r.status_code == 200, r.text)
        files2 = {"file": ("travel.md", content_v2, "text/markdown")}
        r = client.post("/api/knowledge/upload", headers=hf, files=files2,
                        data={"department_id": "fin.general"})
        check("再次上传生成 v2 200", r.status_code == 200 and r.json().get("version") == 2, r.text)
        r = client.get("/api/knowledge/versions/travel.md?department_id=fin.general", headers=hf)
        check("版本历史返回2版", r.status_code == 200 and r.json()["current_version"] == 2, r.text)
        # 回滚到 v1
        r = client.post("/api/knowledge/versions/travel.md/rollback", headers=hf,
                        json={"department_id": "fin.general", "version": 1})
        check("回滚到 v1 生成 v3 200", r.status_code == 200 and r.json().get("new_version") == 3, r.text)


if __name__ == "__main__":
    test_feedback_store()
    test_version_store()
    test_grants()
    test_config()
    test_classifier_fallback()
    try:
        test_integration()
    except Exception as e:  # noqa: BLE001
        import traceback
        print(f"\n[集成测试异常] {e}")
        traceback.print_exc()
        FAIL += 1

    print(f"\n==== 结果：通过 {PASS} / 失败 {FAIL} ====")
    try:
        with open(r"C:\Users\甘双灵\WorkBuddy\2026-09-22-15-40-48\opt_result.txt", "w", encoding="utf-8") as fh:
            fh.write(f"PASS={PASS}\nFAIL={FAIL}\n")
            for fn in FAILS:
                fh.write("FAIL_NAME: " + repr(fn) + "\n")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(1 if FAIL else 0)

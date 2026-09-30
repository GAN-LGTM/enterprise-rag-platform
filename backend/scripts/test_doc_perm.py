"""端到端验证：上传/更新权限 + 删除需管理员审批（对应前端文档管理与删除审批流）。"""
import os, sys, tempfile, json

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")

# 自包含：把 backend 加入导入路径，使脚本可独立运行
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ["DB_FORCE_MEMORY"] = "true"
os.environ["REDIS_ENABLED"] = "false"
os.environ["APP_DATA_DIR"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
c.__enter__()   # 触发 lifespan，注入 app.state.container
FAILS = []

def check(name, ok):
    print(("  ok " if ok else "  FAIL ") + name)
    if not ok:
        FAILS.append(name)

def login(username, password="123456"):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}

def upload(h, dept, content="端到端验证文档\n第二行内容"):
    files = {"file": ("e2e_doc.md", content.encode("utf-8"), "text/markdown")}
    return c.post("/api/knowledge/upload", data={"department_id": dept}, files=files, headers=h)

def listed(h, doc="e2e_doc.md"):
    return any(d["doc_name"] == doc for d in c.get("/api/knowledge/docs", headers=h).json())

def pending_reqs(h):
    return c.get("/api/admin/doc-deletions?status=pending", headers=h).json()["items"]

# 1) 主管可上传；删除转为审批申请，文档不被真正删除
h_mgr = login("sales_mgr")
check("主管上传 200", upload(h_mgr, "mkt.sales").status_code == 200)
r = c.delete("/api/knowledge/docs/e2e_doc.md?department_id=mkt.sales", headers=h_mgr)
check("主管删除转审批", r.status_code == 200 and r.json().get("pending_approval") is True)
check("申请期间文档仍存在", listed(h_mgr))

# 2) 重复提交同一文档删除申请 → 复用同一单
r2 = c.delete("/api/knowledge/docs/e2e_doc.md?department_id=mkt.sales", headers=h_mgr)
check("重复申请去重", r2.json().get("request_id") == r.json().get("request_id"))

# 3) 管理员查看待审列表并驳回 → 文档保持不变
h_admin = login("admin", ADMIN_PWD)
items = pending_reqs(h_admin)
rid = next((x["id"] for x in items if x["doc_name"] == "e2e_doc.md"), None)
check("管理员可见待审申请", rid is not None)
r = c.post(f"/api/admin/doc-deletions/{rid}/reject", headers=h_admin)
check("管理员驳回 200", r.status_code == 200)
check("驳回后文档仍存在", listed(h_mgr))

# 4) 主管再次申请 → 管理员通过 → 文档被真正删除
c.delete("/api/knowledge/docs/e2e_doc.md?department_id=mkt.sales", headers=h_mgr)
rid = next((x["id"] for x in pending_reqs(h_admin) if x["doc_name"] == "e2e_doc.md"), None)
r = c.post(f"/api/admin/doc-deletions/{rid}/approve", headers=h_admin)
check("管理员通过并删除 200", r.status_code == 200)
check("删除后文档不存在", not listed(h_mgr))
r = c.post(f"/api/admin/doc-deletions/{rid}/approve", headers=h_admin)
check("重复审批被拒 400", r.status_code == 400)

# 5) 非管理员不能调审批接口
r = c.get("/api/admin/doc-deletions", headers=h_mgr)
check("非管理员访问审批列表被拒 403", r.status_code == 403)

# 6) 管理员直接删除无需审批
upload(h_admin, "fin.general", "管理员直传文档")
r = c.delete("/api/knowledge/docs/e2e_doc.md?department_id=fin.general", headers=h_admin)
check("管理员直删 200", r.status_code == 200 and r.json().get("deleted") is True)

# 7) 经理(总监)可上传更新（重复内容跳过不报错）
h_dir = login("mkt_director")
check("总监上传 200", upload(h_dir, "mkt.sales", "总监上传的新内容").status_code == 200)

# 8) 专员上传被拒
check("专员上传被拒 403", upload(login("sales_emp"), "mkt.sales").status_code == 403)

# 清理：总监上传的文档由管理员直删
c.delete("/api/knowledge/docs/e2e_doc.md?department_id=mkt.sales", headers=h_admin)

print()
print("FAILS=" + json.dumps(FAILS, ensure_ascii=False))
print("RESULT:", "ALL_PASS" if not FAILS else "HAS_FAIL")

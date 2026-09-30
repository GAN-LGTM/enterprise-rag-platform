# 企业级知识检索中台（权限感知型 RAG · 私有化部署版）

> 一套可私有化部署的企业知识问答中台：**权限是硬约束而不是过滤器**，同时自带知识治理闭环与合规审计能力。

基于 **LangGraph 多节点编排** + **RAG 检索增强生成** 的企业级智能知识问答平台。
支持事业部 / 子部门两级知识隔离、跨部门权限硬拦截、混合检索、答案溯源，问答结果通过 **SSE 逐字流式输出**。

**核心能力一览**

| 能力域 | 说明 |
| --- | --- |
| 问答主链路 | LangGraph 6 节点 / 3 组条件边，SSE 逐字输出，带引用溯源与「依据不足」拒答 |
| 权限与合规 | 检索前硬拦截（R1~R6 六条递进规则）、跨部门申请工单、审计日志持久化与 6 条风险检测规则 |
| 知识治理 | 版本回滚、知识缺口工单闭环、满意度反馈、文档删除双阶段审批 |
| 办公审批 | 对话内完成请假 / 报销多步审批，按组织树自动路由审批人 |
| 实时数据工具 | 注册表式外部工具层：天气 + A股行情/指数/板块/汇率，结构化接口取数而非网页抓取 |
| 降级容错 | 8 类外部依赖全部有降级路径（向量库 / 缓存 / 数据库 / LLM / 图引擎 / Embedding / 意图分类 / 行情数据源），行情接口另设主备双源互切 |
| 运维可观测 | 生产启动门禁（preflight）、结构化日志 + trace_id、9 项 Prometheus 指标 + 12 条告警规则、模型热切换 |
| 交付工程 | 多阶段非 root 镜像、独立备份卷、备份/恢复 CLI、CI 七道门禁、离线依赖锁定、交付检查清单与回滚预案 |

**回归状态**：一键回归 `15/15` 套件通过（11 个功能套件 + 4 个端到端套件），退出码 0。

**这个项目想解决什么**

大多数 RAG demo 只回答一个问题：「怎么检索得更准」。但企业真正会死在这三件事上：

1. **权限**——员工问「研发部的薪酬规范」时，系统该拒答而不是"检索后过滤掉"。**检索后过滤在安全审计层面等同越权访问**，所以本项目把权限做成**检索前的硬拦截**，没权限时物理上不触达目标向量表。
2. **知识库烂尾**——上线三个月，文档还是那批，模型再好也答不准，而没人会主动告诉你"这里答错了"。所以本项目做了**知识缺口工单闭环**：答不上来或点 👎 → 自动生成工单 → 指派到人 → 补齐 → 通知提问人复检。
3. **外部依赖挂了**——客户环境千差万别，没有 GPU、没有 PostgreSQL、连不上 Redis 都是常态。本项目对 **8 类外部依赖都写了降级路径**，最差情况下退到内存库 + 哈希向量也能把全链路跑通。

---

## 架构总览

```
                        ┌──────────────────────────────────────┐
   浏览器 /ui   ───────▶│  FastAPI  (SSE 逐字流式)              │
                        └──────────────┬───────────────────────┘
                                       │ POST /api/chat/stream
                        ┌──────────────▼───────────────────────┐
                        │  LangGraph 工作流                     │
                        │                                      │
                        │  START → classify_intent             │
                        │            │                         │
                        │            ▼                         │
                        │       permission_gate ──┬─▶ deny     │
                        │            │            │   (拒答)    │
                        │            ▼            │            │
                        │        retrieve ────────┼─▶ handle_  │
                        │            │            │   error    │
                        │            ▼            │            │
                        │        generate ────────┴─▶ END      │
                        └──────────────┬───────────────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
┌───────────────┐          ┌────────────────────┐          ┌─────────────────┐
│ 意图四层分类   │          │ 检索：混合 + 融合    │          │ LLM 三级降级     │
│ L1 规则 0ms   │          │ vector + FTS       │          │ 主→备 3s 超时    │
│ L2 会话继承   │          │ RRF Σ1/(60+rank)   │          │ 3 次失败→熔断30s │
│ L3 小模型     │          │ rerank 可解释微调   │          │ 全挂→友好提示     │
│ L4 LLM 兜底   │          │ quality 四道防线    │          └─────────────────┘
└───────────────┘          └────────────────────┘
        │                              │
        ▼                              ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  PostgreSQL + pgvector  │  Redis 缓存  │  内存库 / LRU  │  vLLM  │  公网工具  │
│  （全部支持降级，最差情况下退化为纯内存运行，功能不中断）                      │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## 一、两种运行形态：交付形态 vs 联调形态

一套代码、两种形态，由 `ENVIRONMENT` 与 `DEMO_MODE` 控制：

| | 交付形态（私有化） | 联调形态（本机开发） |
| --- | --- | --- |
| `ENVIRONMENT` | `production` | `dev`（默认） |
| 演示模式 `DEMO_MODE` | 强制关闭 | 默认开启 |
| 内置演示账号 | 不加载，仅初始管理员 | 13 个演示账号 |
| 示例知识语料 | 不灌入 | 启动时自动灌入 |
| 启动自检 | 不合规**拒绝启动** | 只告警不阻断 |
| 向量后端 | 必须 `local`(BGE) / `http` | 允许 `hash` 伪向量 |
| 数据库 | 必须 PostgreSQL | 允许内存库 |
| 生成模型 | 本地 vLLM（离线） | 允许云端联调 |
| 前端登录 | 账号 + 密码输入 | 演示账号下拉（可选） |

**本地体验直接用联调形态**（按下方快速开始即可，无需任何配置）；正式部署见 [`docs/部署手册.md`](docs/部署手册.md)，
运维与应急见 [`docs/运维手册.md`](docs/运维手册.md)，
上线检查与回滚预案见 [`docs/交付检查清单.md`](docs/交付检查清单.md)，
版本差异见 [`CHANGELOG.md`](CHANGELOG.md)。

安全基线自检项（`backend/app/core/preflight.py`）：默认/过短 JWT 密钥、生产开启演示模式、
伪向量、内存库、mock LLM、私有化下外联云端 LLM、CORS 通配、目录不可写 —— 生产环境命中即拒绝启动。

---

## 二、快速开始（零 GPU、零外部依赖）

> **无需 GPU、无需 PostgreSQL / Redis**：系统会自动降级为内存向量库 + 进程内 LRU 缓存，
> 全链路（意图 → 权限 → 检索 → 流式生成）照常跑通；装了生产组件则自动切回。

### 第 0 步：安装依赖（首次运行必做）

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

> `DEMO_MODE=true`（默认）时首次启动会自动灌入演示语料（10 个知识库、64 篇文档、127 个片段），
> 启动即可提问，无需额外准备数据。

### 方式一：脚本启动（Windows 双击即可）

```
start.bat
```

### 方式二：手动启动（CMD，一行一行敲）

```bat
cd /d D:\enterprise-rag-platform
.venv\Scripts\activate
set PYTHONPATH=D:\enterprise-rag-platform\backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> ⚠️ **CMD 里不要用 Linux 写法**：路径用反斜杠 `\`，环境变量必须单独一行 `set PYTHONPATH=...`
> （不能写成 `PYTHONPATH=./backend python ...`）。venv 已存在时不要再执行 `python -m venv .venv`。

macOS / Linux 等价写法：

```bash
export PYTHONPATH=$PWD/backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

看到 `Uvicorn running on http://0.0.0.0:8000` 就是启动成功，**这个黑窗口不要关**（它就是服务本体）。

### 打开页面

| 入口 | 地址 |
| --- | --- |
| **演示页（聊天界面）** | **http://127.0.0.1:8000/ui** |
| 接口文档（OpenAPI） | http://127.0.0.1:8000/docs |
| 健康检查 | http://127.0.0.1:8000/api/health |
| 指标 | http://127.0.0.1:8000/metrics |

> 直接访问 http://127.0.0.1:8000 也会**自动跳转**到演示页。
> 注意：`0.0.0.0` 是监听参数不是网址，`http://0.0.0.0:8000` 浏览器打不开，请用 `127.0.0.1`。

登录方式按运行形态区分：

| 形态 | 登录方式 | 口令来源 |
| --- | --- | --- |
| 联调形态（`DEMO_MODE=true`，默认） | 登录页列出 13 个内置演示账号（`admin`、`sales_emp`、`ceo` …） | 演示口令：除 `admin` 外统一 `123456` |
| 交付形态（`DEMO_MODE=false`） | 登录页只有账号密码输入框，演示账号不加载 | 初始管理员口令由 `deploy/init_env.py` 随机生成，写入 `.env.production` 并落到 `data/initial_admin.txt`（该文件不入库），首次登录强制改密 |

自动化脚本（`eval_quality.py` / `smoke_test.py` / `test_doc_perm.py` / `scripts/verify_*.py` 等）
统一从环境变量 `ADMIN_PASSWORD` 取管理员口令，默认值 `admin123` 仅适用于联调形态；
在交付环境执行验收脚本时传 `ADMIN_PASSWORD=<初始口令>` 即可。

### 停止 / 排障

```bat
stop.bat                              :: 停止服务（推荐）
netstat -ano | findstr :8000          :: 端口被占用时，最后一列是 PID
taskkill /PID 12345 /F                :: 杀掉占用进程
.venv\Scripts\python.exe scripts\selfcheck.py   :: 15 项全链路体检
```

> 演示环境重新演示前：先 `stop.bat`，再执行 `python scripts/reset_demo_data.py` 清理调试残留
> （跨部门授权、权限申请单、令牌黑名单），最后 `start.bat`。

### 演示语料与组织架构

默认已灌入 **10 个知识库（公共库 + 9 个业务部门）、64 篇演示文档、127 个检索片段**，
含各部门制度、业务数据、行业公开数据与部门基础信息，**启动即可提问**。

组织架构为三级：公共知识库（`public`）→ 事业部/一级部门（`mkt` / `tech` / `fin` / `hr` / `legal`）→ 二级子部门（销售 / 市场 / 客服 / 研发 / 测试 / 运维 / 财务综合）。
组织树共 13 个节点（含事业部节点本身），其中带独立知识库的是上述 10 个。

> 本机没装 PostgreSQL / Redis 也没关系：系统会自动降级为内存向量库 + 进程内 LRU，
> 全链路（意图 → 权限 → 检索 → 流式生成）照常跑通。装了则自动切回生产组件。

### 演示账号（除 `admin` 外，密码统一 `123456`）

共 13 个内置账号，覆盖 7 种角色（`admin` / `executive` / `dept_director` / `sub_manager` / `sub_employee` / `knowledge_reviewer` / `compliance_reviewer`）：

| 账号 | 角色 | 可见范围 |
| --- | --- | --- |
| `admin` | 系统管理员 | 全部部门 |
| `sales_emp` | 销售专员 | 仅销售部 + 公共库 |
| `market_emp` | 市场专员 | 仅市场部 + 公共库 |
| `rd_emp` | 研发工程师 | 仅研发部 + 公共库 |
| `fin_mgr` | 财务主管 | 仅财务部（可上传文档） |
| `hr_emp` | 人力专员 | 仅人力部 + 公共库 |
| `legal_emp` | 法务专员 | 仅法务部 + 公共库 |
| `mkt_director` | 营销总监 | 销售 + 市场 + 客服 |
| `tech_director` | 技术总监 | 研发 + 测试 + 运维 |
| `ceo` | 总经理 | 营销 + 技术 + 财务 + 人力 + 法务 |
| `sales_mgr` | 销售主管 | 销售部（可上传文档） |
| `kb_reviewer` | 知识审核员 | 知识治理与文档审核 |
| `compliance_reviewer` | 合规审核员 | 审计日志与合规报告（只读） |

**对比体验权限隔离**：用 `sales_emp` 登录问「研发部的编码规范是什么？」→ 被权限门拦下；
换成 `admin` 问同一个问题 → 正常召回研发部文档。

---

## 三、目录结构

```
enterprise-rag-platform/
├── backend/app/
│   ├── main.py                # FastAPI 入口、异常处理器、lifespan
│   ├── config.py              # 全部配置项（环境变量驱动）
│   ├── deps.py                # 应用容器：组件装配 + 降级决策
│   ├── logging_setup.py       # structlog 结构化日志 + trace_id + 节点耗时
│   ├── metrics.py             # Prometheus 指标（9 项，端点 /metrics）
│   ├── errors.py              # 统一异常
│   ├── session_store.py       # 会话与消息持久化（按 user_id 分桶）
│   ├── api/                   # 接口层（auth / chat / knowledge / admin / compliance
│   │                          #   / permission / leave / reimburse / notifications / health / sse）
│   ├── graph/                 # ★ LangGraph 工作流（state / nodes / workflow）
│   ├── intent/                # 四层递进式意图分类
│   ├── perm/                  # R1~R6 递进规则的权限校验门
│   ├── retrieval/             # Embedding + 向量存储 + RRF 混合检索
│   │                          #   + rerank.py（融合后重排）/ quality.py（四道质量防线）
│   │                          #   + context.py（上下文窗口治理）
│   ├── core/                  # 审计 / 限流 / 反馈 / 知识缺口（gap_store）
│   │                          #   / 账号治理（user_admin_store）/ 备份恢复（backup）
│   │                          #   / 启动门禁（preflight）/ 令牌与登录锁定（security、token_store）
│   ├── leave/                 # 请假多步审批流（flow / routing / store / 钉钉适配）
│   ├── reimburse/             # 报销多步审批流（同构）
│   ├── tools/                 # 外部实时工具层，出网前强制脱敏
│   │   ├── base.py            #   工具统一契约 RealtimeResult（失败一律返回 None）
│   │   ├── realtime.py        #   工具注册表 TOOL_ROUTER + 两阶段调度（primary 抢答 / fallback 兜底）
│   │   ├── finance.py         #   财经行情：个股 / 指数 / 板块涨跌榜 / 汇率（主源失败自动切备源）
│   │   ├── reference.py       #   百科词条 + 中国节假日（含"公司制度类问题"的内部护栏）
│   │   └── websearch.py       #   通用联网搜索（结构化搜索 API 优先）+ 垃圾结果治理
│   ├── llm/                   # LLM 适配器 + 三级降级 + 熔断
│   ├── cache/                 # Redis 缓存（含内存降级）
│   ├── ingest/                # 文档解析 / 分块 / 入库流水线 / 版本管理
│   ├── db/models.py           # 组织、用户、权限模型
│   └── scripts/seed_demo.py   # 演示知识语料（10 个知识库 / 64 篇，仅演示模式灌入）
├── frontend/                  # 零构建前端，无需 npm / 打包器，由 index.html 直接加载
│   ├── index.html             # 页面骨架
│   ├── styles.css             # 样式
│   ├── app.js                 # 业务逻辑（真实消费 SSE 逐字渲染）
│   ├── antd-bridge.js         # React / Ant Design 桥接层
│   └── vendor/                # 本地化第三方资源（react / react-dom / dayjs / antd），无 CDN 依赖
├── sql/                       # 001 表结构 / 002 索引 / 003 RLS（RLS 默认未生效，启用方式见脚本注释）
├── deploy/                    # Dockerfile / docker-compose(.prod).yml / nginx.conf
│                              #   / nginx.dev.conf / api_proxy.inc / init_env.py
├── deploy/monitoring/         # Prometheus 抓取配置 + 12 条告警规则
├── .dockerignore              # 构建上下文排除清单（防止运行时凭据被打进交付镜像）
├── .github/workflows/         # CI 质量门禁（7 道，见 §十一）
├── docs/                      # 部署手册 / 运维手册 / 验收测试 / 交付检查清单
├── backend/scripts/           # 测试与运维脚本（run_all_tests / smoke_test / test_* / eval_quality
│                              #   / backup / check_dep_lock / intent_report）
├── scripts/                   # 环境自检与数据初始化（selfcheck / seed_demo_data / reset_demo_data）
├── requirements.txt           # 依赖声明（容器构建与 CI 安装用）
└── requirements.lock.txt      # 全量锁定版本（离线 / 内网安装用）
```

---

## 四、接口总览

全部接口挂在 `/api` 下，共 **95 个路由**，完整签名与请求体见 `http://127.0.0.1:8000/docs`（OpenAPI）。

| 模块 | 前缀 | 路由数 | 覆盖内容 |
| --- | --- | --- | --- |
| 认证 | `/api/auth` | 6 | 登录 / 刷新 / 登出 / 改密 / 当前身份 / 演示账号（仅演示模式） |
| 问答 | `/api/chat` | 11 | SSE 流式问答、会话 CRUD 与置顶排序、满意度反馈、热门问题 |
| 知识库 | `/api/knowledge` | 14 | 上传 / 批量上传 / 检索 / 文档列表 / 版本历史与回滚 / 知识缺口反馈 / 可上传部门 |
| 权限申请 | `/api/permission` | 7 | 发起跨部门申请、待我审批、批准 / 驳回 / 撤回、历史单 |
| 请假审批 | `/api/leave` | 8 | 我的申请、待我审批、审批结果通知、草稿退订、详情与审批动作 |
| 报销审批 | `/api/reimburse` | 8 | 同构于请假（多一级财务复核） |
| 管理后台 | `/api/admin` | 31 | 统计与数据看板、审计日志、用户与角色管理、`uploadable-departments` / `departments` 供部门选择、跨部门授权与授权矩阵、文档删除审批、知识缺口工单处置、缓存失效、模型热切换、手动备份 / 恢复 / 备份历史、组织树 |
| 合规审计 | `/api/compliance` | 6 | 审计日志（只读）、事件类型清单、CSV/JSON/PDF 导出、异常检测（R1~R6）、合规概览、月度报告 |
| 通知中心 | `/api/notifications` | 2 | 通知列表（通用 + 知识缺口）与标记已读 |
| 运维 | `/api` | 2 | `/api/health`（组件健康）、`/api/ping`（存活探针） |

> 另有 `/metrics`（Prometheus 文本格式，不在 OpenAPI 中）与 `/ui`（静态前端）。
> 权限模型：业务接口一律从 JWT 解析身份，**不信任前端传的部门字段**；管理员接口由 `_assert_admin` 前置校验；
> 知识缺口工单类接口按"管辖范围"授权（管理员看全部，总经理/总监/主管只看自己辖内部门）。

---

## 五、SSE 流式输出与会话管理

### 协议

`POST /api/chat/stream`，响应 `Content-Type: text/event-stream`。

| event | 时机 | 载荷要点 |
| --- | --- | --- |
| `meta` | 首帧 | trace_id、session_id、当前用户、存储/图引擎 |
| `stage` | 每个 LangGraph 节点完成 | 节点名、意图与置信度、权限命中规则、检索诊断 |
| `citation` | 检索完成（正文之前） | 文档名、页码、所属部门、融合得分、置信度、摘要 |
| `token` | 逐字 | `{"delta": "字"}` |
| `done` | 结束 | 完整答案、引用、各节点耗时、是否命中缓存 |
| `error` | 异常 | 错误码 + 用户可读文案（不回显内部堆栈） |

心跳帧：超过 `STREAM_HEARTBEAT_SECONDS`（默认 15s）无数据自动补 `: ping`，防止 Nginx / 网关掐断长连接。

### 会话管理（按账号隔离）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/chat/sessions` | 仅返回**当前登录账号**的会话（标题、轮次、更新时间） |
| POST | `/api/chat/sessions` | 新建会话 |
| GET | `/api/chat/sessions/{id}` | 会话详情含完整历史消息，**非本人会话一律 404** |
| PATCH | `/api/chat/sessions/{id}` | 重命名 |
| POST | `/api/chat/sessions/{id}/pin` | 置顶 / 取消置顶 |
| POST | `/api/chat/sessions/pin-order` | 置顶会话按拖拽顺序持久化 |
| DELETE | `/api/chat/sessions/{id}` | 删除，非本人会话返回 404 |

设计要点：

- **账号隔离**：会话以 `user_id` 分桶存储，接口层做归属校验，杜绝横向越权读取他人会话。
- **自动命名**：会话标题默认为「新会话」，首轮提问后自动改为问题摘要（截断 18 字），后续多轮不覆盖。
- **懒创建**：前端不发第一条消息就不建会话，避免侧边栏堆一排空的「新会话」。
- **持久化**：落盘到 `APP_DATA_DIR/sessions.json`（原子写 tmp+replace），服务重启、页面刷新历史不丢；生产替换为 PostgreSQL `conversations` / `messages` 两表即可，函数签名不变。
- 上限保护：每账号 50 个会话、每会话 100 条消息，超出自动裁剪。

### 为什么不用浏览器原生 `EventSource`

`EventSource` 只支持 GET，无法携带 `Authorization` 头和 JSON body。
所以后端输出标准 SSE 帧格式，前端用 `fetch + ReadableStream` 自行按 `\n\n` 分帧解析 ——
既保留了 SSE 的简单性，又能走统一网关鉴权。

### 流式怎么和图编排共存

`generate` 节点把 LLM 的 async generator 放进 `State["token_stream"]`；
图的 `astream` 输出**节点级事件**（stage），API 层再消费 `token_stream` 输出**字级事件**（token）。
两者叠加，既有多节点编排能力，又能逐字推送。

> 实现约束：LangGraph 在节点之间传递的是 state **副本**，`citations`、`intent`、`dept_ids`
> 这类"替换型"字段不会自动回到调用方持有的 state。`run_stream()` 在 `finally` 里把所有节点输出
> 回写到外部 state，引用才不会丢。

---

## 六、架构要点

### 1. LangGraph 工作流

```
START → classify_intent → permission_gate ─┬─→ retrieve → generate → END
                                           ├─→ deny ──────────────→ END
                                           └─→ handle_error ──────→ END
```

任何节点抛异常都会被 `_safe` 包装器捕获并转入 `handle_error`，不会让整个流程崩掉。
`langgraph` 不可用或 API 不兼容时，自动切换为内置的 `MiniGraph`（轻量状态机，接口等价）。

### 2. 四层递进式意图识别

| 层级 | 手段 | 耗时 | 覆盖 |
| --- | --- | --- | --- |
| L1 | 正则/关键词硬规则 | 0ms | ~80% |
| L2 | 会话状态继承 | 0ms | ~10% |
| L3 | 小模型分类器（BERT-tiny + ONNX 可插拔；未配置模型时回退关键词打分，保证离线可用） | <50ms | ~8% |
| L4 | 本地 LLM 兜底 | 1-3s | ~2% |

越贵的判断越靠后 —— 90% 的请求被挡在 GPU 之外。

**准确性的四道加固（对应"判错代价"而非"判错概率"设计）**

| 措施 | 做法 |
| --- | --- |
| 置信度分级 | L3 命中但置信度 0.5~0.8 属模糊地带，**不硬猜**：照常检索，但在答案最前面追加一句"我先按「XX」来理解，如果理解有误…"，用户一句话即可纠偏 |
| 会话上下文补偿 | 出现强指代（"刚才那个""还有吗"）或超短问句时，L2 **优先于 L1**，不再硬分类；但用户明确纠偏（"不是""换个问题"）或点名了新部门时禁止继承 |
| 兜底最安全 | L4 判成 `cross_dept` / `operation` 时必须有硬证据（点名部门 / 动作词），缺证一律降级为 `dept_kb`；仍不确定也落 `dept_kb`，绝不误触跨部门与误操作 |
| 规则持续反哺 | 每次判定写入 `APP_DATA_DIR/intent_log.jsonl`（带 trace_id），`scripts/intent_report.py` 汇总分层覆盖率、低置信样本、点踩样本，并给出补词建议与待标注微调集 |

```bash
python backend/scripts/intent_report.py                       # 复盘：覆盖率 / 低置信 / 点踩 / 补词建议
python backend/scripts/intent_report.py --export-finetune s.jsonl   # 导出待标注集 → 微调 BERT-tiny
python backend/scripts/test_intent.py                         # 意图准确性回归（含生活娱乐/联网搜索分组）
```

L1 规则内部还有优先级讲究：操作型 > 点名部门 > **强数据信号** > **生活娱乐/时效话题** > 通用制度 > 部门业务，
避免"劳动合同"被"合同"吃掉、"销售额同比"被"销售额"吃掉这类误判。

### 2·补、实时联网工具（时效性问题不进知识库）

大模型没有"今天"这个概念，演唱会、上映影片、金价、天气、今天的股价这类问题在知识库里必然检索不到。
工作流在**检索节点之前**插了一段工具调用（`backend/app/tools/`），按**注册表**组织（`tools/realtime.TOOL_ROUTER`）：

| 工具 | 触发条件 | 数据源 | 失败处理 |
| --- | --- | --- | --- |
| `weather` | 语义唯一，命中"天气/下雨/气温"即查（不看部门范围） | Open-Meteo（免密钥） | 静默降级，继续原流程 |
| `market` | 命中"股价/板块/大盘/汇率"等；**内部事项护栏**：问"我们公司的股价制度"不算行情问题 | 个股/指数/板块：东方财富 → 腾讯财经兜底；汇率：open.er-api | 主源失败切备源，双源都挂则静默降级 |
| `encyclopedia` | 明确的百科型问法（"什么是 X""X 是谁""介绍一下 X"）；**内部事项护栏**：问"我们公司的报销标准是什么"不算百科问题 | 百度百科 OpenAPI（免密钥） | 查无此条即弃权，交回内网 |
| `holiday` | 命中"放假/上班/调休/周末/国庆"等；同样有内部护栏（"我们公司什么时候放假"问的是内部安排） | timor.tech 节假日接口（免密钥，按国务院放假安排） | 静默降级 |
| `websearch` | ① 闲聊类意图 + 时效词（最近/最新/金价/演唱会…）；② **强时效兜底**：时间词 + 行情/娱乐名词，**无视意图分类结果直接联网** | **结构化搜索 API（Tavily / Serper，配了就用）→ 必应 → 搜狗兜底** | 静默降级，绝不阻塞主流程 |

**能走接口的一律走接口，网页抓取只当兜底。**

天气、行情、百科、节假日四类都是**结构化接口**：返回的就是带字段含义的 JSON，模型只需要"读"，不需要从网页正文里"猜"。只有这四类的语义都不命中、而又确实需要联网时，才回退到网页搜索——这也是主流联网问答产品的路子：**背后接的是数据源，不是爬虫**。

网页搜索本身还留了一道升级位：配了 `SEARCH_API_PROVIDER` + `SEARCH_API_KEY` 之后，它会从"抓取必应 HTML 再正则解析"切换成"直接向 Tavily/Serper 要结构化 JSON"，
拿到的是干净的 `title / snippet / url`，不再需要抓网页正文，也就不可能再混进导航站。没配密钥则自动回退，功能不受影响。

**为什么行情要单独做一层，而不是继续用网页搜索？**

搜索引擎本身读不懂"今天哪个板块涨"：它返回的是 hao123、360导航、词典词条这类**有标题有正文却没有答案**的页面。模型拿到这种语料，要么编数字，要么答不上来——两者都是幻觉。结构化行情接口给的是**带字段含义的数字**（板块涨跌幅、领涨股、板块内涨跌家数），模型只需要"读"，不需要"猜"。这也是本项目幻觉治理最有效的一招：**让模型在数据里作答，而不是在记忆里作答**。

`market` 内部再按子意图路由（`tools/finance.plan`，纯规则可单测）：
`汇率 → 六位股票代码 → 指数名 → 板块词 → 疑似个股名（丢给模糊搜索验证）→ 大盘概览兜底`，
所以「今天哪个板块涨」不会浪费地去查某只个股，「贵州茅台现在多少钱一股」也不会返回一整屏板块。

网页搜索那一侧同步做了结果治理（`websearch._is_junk_result`）：导航站、网址大全、词典词条、下载站、跳转中间页按域名黑名单剔除，
再对正文做第二道判定（出现"页面正在跳转""网址大全"等特征即丢弃），避免脏语料稀释上下文。

设计上的五条硬约束（内网 RAG 与公网工具严格分路）：

- **路由分清楚**：业务类问题走内网 RAG（`dept_ids` 非空、走混合检索）；闲聊/通用类（`chitchat`/`operation`，`dept_ids` 为空）才考虑外部工具，二者永不混用同一数据源。**唯一的例外**是"强时效"问题（时间词 + 行情/娱乐名词），这类问题内网必然没有答案，允许无视意图直接出网。
- **内部事项护栏**：`market` 命中"我们公司/本公司/我司/员工持股"等问法时不判定为行情问题，避免带着内部语义去查公网。
- **出网必脱敏**：公网搜索发出前会过 `tools/websearch.sanitize_query`，剔除**部门名 / 人名 / 用户身份 / 内部术语**（`INTERNAL_TERMS` 可配），只把"问题本身"交给搜索引擎，绝不携带内网上下文。
- **合规可开关**：`PUBLIC_SEARCH_ENABLED` 总开关同时控制天气、行情、百科、节假日、搜索等所有出公网工具；行情与参考类另有独立开关 `REALTIME_MARKET_ENABLED` / `REFERENCE_TOOLS_ENABLED`（总开关关闭时它们随之关闭，合规优先）。`OFFLINE_MODE=true`（私有化默认）时总开关默认关闭，等于"零外网流量"承诺生效；确需联网问答时显式设 `true` 开启。
- **联网结果不进知识库**：实时数据只作为本次 Prompt 的上下文，不写回向量库；且公网 / 天气 / 行情 / 百科 / 节假日答案**一律不写入内网 RAG 缓存**（见 `chat.EXTERNAL_SOURCES`），避免陈旧外网信息被当成内部知识反复下发。

**来源必标注**：外部工具回答前端显示橙色徽章，文案与来源一一对应（`frontend/app.js` 的来源映射）：

| 来源 | 徽章 |
| --- | --- |
| 公网搜索 | 🌐 公网搜索结果 · 仅供参考，不代表公司内部信息 |
| 实时天气 | 🌐 实时天气（公网）· 仅供参考 |
| 实时行情 | 📈 实时行情数据（公网）· 仅供参考，不构成投资建议 |
| 百科词条 | 📖 公开百科词条 · 仅供参考，不代表公司内部规定 |
| 法定节假日 | 📅 法定节假日安排（公网）· 以国务院公布为准 |
| 内网知识库 | 📚 来自公司知识库（绿色，并附引用卡片：文档名 / 页码 / 部门） |

模型被要求在外网答案末尾注明"以上信息来自公开互联网，仅供参考，不代表公司内部信息"。

私有化内网部署时若关闭公网搜索，工具分支被跳过，功能自动退化为"礼貌说明无法获取实时数据"，不影响其余能力。

### 3. 权限三层防护

| 层级 | 措施 |
| --- | --- |
| 应用层 | 所有检索方法强制传 `dept_ids`，不传直接抛异常 |
| API 层 | JWT 解析身份，绝不信任前端传的部门字段 |
| 数据库层 | PostgreSQL RLS（脚本 `sql/003_rls.sql` 已备，**默认未生效**：应用以库 owner 连接时 PostgreSQL 会绕过 RLS，启用需按脚本注释完成"受限角色 + `SET LOCAL app.current_depts`"两步接线） |

**检索前拦截，而非检索后过滤** —— 否则在安全审计层面等同"越权访问尝试"。
无权限时物理上不触达目标向量表，直接走 `deny` 节点返回友好提示，且拒绝文案不透露目标部门是否存在（防侧信道探测）。

### 4. 混合检索 + RRF

- 向量路：pgvector 余弦相似度 + HNSW 索引
- 关键词路：PostgreSQL FTS + GIN 索引（未装中文分词插件时降级 ILIKE）
- 融合：`score = Σ 1/(60 + rank_i)`，60 为 RRF 论文标准平滑常数
- **融合后重排**（`retrieval/rerank.py`，`RERANK_ENABLED`）：标题命中加权、长短语完整命中加分、
  「材料/制度/管理/申请」等高频官僚词降权 85%。RRF 仍是主排序，重排只做可解释的微调，
  解决"搜差旅报销却排在通用《行政管理制度》上"的问题
- 向量检索超时（`VECTOR_TIMEOUT_MS`，默认 1.5s）自动降级为纯关键词

按排名融合而非分数相加，天然免疫"某一路高分噪声压过另一路"的问题。

### 4·补、答案质量四道防线（`retrieval/quality.py`）

检索到文档 ≠ 拿到可用证据。四道防线逐层收口，避免模型拿着无关资料"努力作答"：

| 防线 | 位置 | 做法 |
| --- | --- | --- |
| ① 检索后相关性校验 | `hybrid_search` | 问题的区分性关键词在引用里命中不足 → 标记 `low_evidence` |
| ② 依据不足拒答 | `generate` 节点 | 不把无关资料丢给模型，改发确定性话术（说明未找到依据 + 列出最接近的几篇供核对） |
| ③ 引用强制标注 | `SYSTEM_PROMPT` | 要求每个关键结论后标 `[N]`，编号只能取自参考资料序号 |
| ④ 生成后引用归一化 | `api/chat.py` | 只保留答案里**实际引用**的 `[N]` 并按出现顺序重排编号，未被引用的一律不下发前端 |

防线④的收益常被低估：模型说"依据 [1][3]"却挂 8 张引用卡片，用户点开发现一半无关，
信任直接崩塌；而这些未被引用的检索结果还会白白吃掉下一轮上下文。

**前端同步呈现**（这部分决定用户能不能感知到防线存在）：

| 位置 | 呈现 |
| --- | --- |
| 答案下方 | `low_evidence=true` 时显示「依据不足」灰条，明确这是兜底提示、**不是**正式答复 |
| 引用卡内 | 相对相关度条（本组融合分归一化）。直接显示 RRF 原始分毫无意义（典型值 0.0x 且不可跨问题比较），归一化后用户才看得出"哪条最贴题" |
| 答案下方 | 来源徽章：内网知识库（绿）/ 公网搜索·实时天气·实时行情·百科·节假日（橙，标注仅供参考），文案见 §六·2·补 |

### 4·补之二、上下文窗口治理（`retrieval/context.py`）

LLM 的上下文窗口是 AI 产品的隐性天花板，靠扩大窗口解决不了——不控制输入量迟早还是会爆：

| 层 | 做法 | 收益 |
| --- | --- | --- |
| 追问检测 | 承接词（那/这/还有吗）+ 超短疑问句 + 相对长度 → 判定追问，检索范围收敛到**上一轮实际引用过的文档** | 候选数减半，上下文收敛 |
| 引用溯源过滤 | 只沿用"被引用的"而非"被召回的"文档名 | 后续轮次不再带入无关 chunk |
| 滑动窗口裁剪 | 历史按**字符预算**裁剪（`CTX_HISTORY_MAX_CHARS`，默认 3000），不再写死固定条数 | 长短消息一视同仁，输入量可控 |
| 会话边界识别 | 「好的」「不需要」「再见」→ 不重新检索 | 少一次向量查询，也不污染上下文 |

用户明确纠偏（"不是报销，是请假"）或点名换话题时，禁止沿用上一轮范围。

### 5. LLM 三级降级 + 熔断

1. 主模型首字超时（3s）→ 自动切备用模型
2. 连续失败 3 次 → 熔断 30s
3. 全部不可用 → 返回友好提示，不抛裸异常

### 6. 知识缺口台账（`core/gap_store.py`）

传统检索系统的浪费在于：用户问了 A 没结果，换成 B 找到了，这个行为数据就被丢弃了。

本系统把答不上来的问题按部门沉淀到 `APP_DATA_DIR/search_gaps.json`，分两类：

| 类型 | 含义 | 典型跟进动作 |
| --- | --- | --- |
| `no_hit` | 该部门知识库一条片段都没捞到 | 补文档，纯缺资料 |
| `low_evidence` | 捞到了但都不相关 | 资料写得太泛或缺用户实际用的关键词，改标题/补词 |

管理员接口：`GET /api/admin/knowledge-gaps?department_id=&reason=&top=`。
用途是回答"下一步该补哪份文档"，而不是凭感觉治理知识库。

**管理看板「🕳 知识缺口」板块**展示这份台账：按**重复被问次数**倒序，每条显示部门、
最近提问人与时间、缺口类型徽章（无命中 / 命中但不相关），支持按类型筛选。
重复次数越高越说明这是真实高频需求，而不是某个用户的一次随口提问。

> 契约约束：`answer_source` 必须在 `GraphState` 里声明。
> LangGraph 只回传 state 已声明的字段，未声明的 key 会被**静默丢弃**——
> 漏掉它会导致 `answer_source` 恒为 `chat`，台账永远为空、徽章永不出错也不生效。
> `scripts/test_quality_opt.py` 里有 AST 静态检查守着这条契约。

#### 知识缺口反馈闭环（工单状态机）

台账只解决"该补哪份文档"，**缺口仍然可能永远没人补**。工单把人和流程接进来形成闭环：

```
检索无命中 / 依据不足  ─┐
用户主动点"👎 没用"    ─┴→  回答下方反馈组件（原因标签 + 补充说明）
                              ↓  POST /api/knowledge/gap
                        知识缺口工单（状态：待处理）
                              ↓  管理员/部门主管在治理台处置
                        指派负责人 → 补充文档 → 标记"已补充"
                              ↓  GET /api/knowledge/gap/notifications
                        原提问人收到通知「你反馈的缺口已补充，再问一次试试」
                              ↓
                        仍然答不上来？再次反馈 → 工单自动重开（防止假结案）
```

| 工单状态 | 含义 | 触发方式 |
| --- | --- | --- |
| `pending` 待处理 | 已记录但还没人补 | 用户反馈 / 系统零命中；已结案工单被再次反馈时自动重开 |
| `supplemented` 已补充 | 资料已上架 | 管理员 `POST /knowledge-gaps/{id}/resolve`，需填文档名或说明 |
| `closed` 已关闭 | 不纳管 / 已线下解答 | 管理员 `POST /knowledge-gaps/{id}/close`；可用 `reopen` 拉回 |

用户反馈原因与系统自动判定分开存，语义不混淆：
`inaccurate` 答案不准确 / `not_found` 没有找到资料 / `irrelevant_cite` 引用不相关 / `outdated` 内容已过期
（系统侧仍是 `no_hit` / `low_evidence`）。

**权限隔离**：管理员看全部工单；总经理/总监/主管只看自己管辖范围内部门的工单；
普通员工无 `POST /admin/*` 处置权限，只能通过 `/api/knowledge/gap` 提交反馈并查看自己的进度。
提交、指派、补充、关闭、重开全部写审计（`gap_feedback`/`gap_assign`/`gap_resolve`/`gap_close`/`gap_reopen`）。

接口一览：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/knowledge/gap` | 提交缺口反馈，生成/归并工单 |
| GET | `/api/knowledge/gap/mine` | 我提交过的反馈与处理进度 |
| GET | `/api/knowledge/gap/notifications` | 我的补缺通知（轮询式，前端每 15s 拉取） |
| GET | `/api/admin/knowledge-gaps?reason=&status=` | 工单列表（按成因 + 状态筛选） |
| POST | `/api/admin/knowledge-gaps/{id}/assign` | 指派处理人 |
| POST | `/api/admin/knowledge-gaps/{id}/resolve` | 标记已补充（自动通知提问人） |
| POST | `/api/admin/knowledge-gaps/{id}/close` / `reopen` | 关闭 / 重新打开 |

> 主键稳定性：工单 id = `{department_id}:{question_hash}`，同一部门同一问题永远归并成一张单，
> 多人反馈累积在 `feedbacks` 列表而不是刷屏；历史主键在加载时自动迁移到该格式。

### 7. 引导式工作流的可退出性

请假/报销草稿一旦存在，**后续所有聊天消息都会被当成对该草稿的填空**。
为避免用户被卡在流程里，系统提供两个出口：`DELETE /api/leave/draft` 与 `DELETE /api/reimburse/draft`（REST），
对话内也支持「取消 / 退出 / 算了 / 不报了」直接放弃。

### 8. 缓存一致性

- Key = `MD5(问题 + 排序后的 dept_ids)`，不同部门不同 Key，权限不串味
- 文档更新时按部门批量失效问答缓存
- **缓存命中同样走流式**（伪流式逐字推送），保证前端体验一致

---

## 七、办公审批工作流

两条流程同构：对话内采集 → 纯规则状态机 → 按组织树路由审批人 → 面板展示进度 → 审批通过后通知申请人。
**审批期间不影响正常知识问答。**

### 7.1 请假（钉钉请假模板字段）

员工在聊天里输入「我想请假」，系统按 **钉钉 OA 审批「请假」模板字段** 逐项追问
（请假类型 → 开始时间 → 结束时间 → 请假事由 → 工作交接人），汇总核对后确认提交，
自动按申请人部门路由到审批负责人，并在右上角「请假 / 审批」面板展示审批进度；
全部审批通过后，申请人下次任意聊天或每 15s 轮询都会收到「审批通过，请假生效」提示。

- 触发：`api/chat.py` 检测到「请假」意图或存在进行中草稿时，改走 `_leave_stream`（SSE 输出，前端复用同一套渲染）。
- 采集与状态机：`leave/flow.py`（纯规则，不依赖 LLM，稳定可复现）。
- 钉钉适配：`leave/dingtalk_client.py`（`DINGTALK_MOCK=true` 默认模拟；填好 `DINGTALK_*` 配置并关闭 mock 即调用真实 OAPI `gettoken` + `processinstance/create`）。
- 审批路由：`leave/routing.py`（一级 = 部门负责人；年假/婚假/产假/≥3 天等追加总经理复核）。
- 存储：`leave/leave_store.py`（`APP_DATA_DIR/leave.json`，草稿按 user 隔离，申请含审批链）。
- REST：`api/leave.py` —— `GET /my`、`/pending/me`、`/notify`、`/draft`、`/{rid}`；`POST /{rid}/approve`、`/reject`（严格校验当前待审人，越权返回 404）。

生产接入真实钉钉时，把 `dingtalk_client.create_leave_instance` 的 mock 关掉、填好凭证即可，其余流程不变。

### 7.2 报销（钉钉报销模板字段）

员工在聊天里输入「我想报销」，系统按 **钉钉 OA 审批「报销」模板字段** 逐项追问
（报销类型 → 报销金额 → 费用发生日期 → 费用说明 → 收款人），汇总核对后确认提交，
自动按「部门负责人 + 必经财务复核 + 大额/财务本部追加总经理」路由审批，并在右上角「报销 / 审批」面板展示进度；
全部审批通过后，申请人收到「审批通过，报销生效」提示。

- 触发：`api/chat.py` 检测到「报销」意图或存在进行中草稿时，改走 `_reimburse_stream`；「报销进度怎么样」等问法走 `_reimburse_progress_stream`（优先于新流程触发，避免误开）。
- 采集与状态机：`reimburse/flow.py`（纯规则，不依赖 LLM）。
- 钉钉适配：`reimburse/dingtalk_client.py`（`DINGTALK_MOCK=true` 默认模拟；填好 `DINGTALK_REIMBURSE_PROCESS_CODE` 等并关闭 mock 即调用真实 OAPI）。
- 审批路由：`reimburse/routing.py`
  - 一级 = 部门负责人（营销/技术各总监、财务主管、人力·法务 → 总经理、公共库 → 管理员）；
  - 二级 = **财务复核**（固定财务主管，报销涉及资金必经）；
  - 三级（满足任一追加）= 报销金额 ≥ 5000 元，或申请人本身就是财务综合部门（避免自批）→ 总经理终审。
- 存储：`reimburse/reimburse_store.py`（`APP_DATA_DIR/reimburse.json`，草稿按 user 隔离，申请含审批链）。
- REST：`api/reimburse.py` —— `GET /my`、`/pending/me`、`/notify`、`/draft`、`/{rid}`；`POST /{rid}/approve`、`/reject`（严格校验当前待审人，越权返回 404）。

演示路径：用 `sales_emp` 登录 → 输入「我想报销」走完引导 → 用 `mkt_director` 登录在「报销 / 审批」通过一级 → 用 `fin_mgr`（财务主管）登录通过二级，即可看到报销生效提示。

---

## 八、企业级治理能力

### 8.1 审计日志持久化

`core/audit.py` —— JSON 落盘 `APP_DATA_DIR/audit.json`（原子写），覆盖关键事件：

| 事件类型 | 触发点 |
| --- | --- |
| `chat` / `chat_deny` | 每次问答（含 trace_id、意图、命中部门）、越权被拦截 |
| `login_ok` / `login_fail` | 登录成功与失败（失败含用户名，便于排查暴力破解） |
| `upload_ok` / `doc_delete` / `doc_delete_request` / `doc_rollback` | 文档上传、删除与审批、版本回滚 |
| `perm_request_*` / `grant_dept` / `revoke_dept` / `grant_expire` | 跨部门申请与审批、手动授权与回收、授权到期 |
| `leave_*` / `reimburse_*` | 请假与报销的提交、审批、驳回 |
| `change_pwd_ok` / `change_pwd_fail` | 改密成功与失败 |
| `user_create` / `user_reset_pwd` / `user_role_change` / `user_status` | 账号管理动作 |
| `gap_feedback` / `gap_assign` / `gap_resolve` / `gap_close` / `gap_reopen` | 知识缺口工单全流程 |
| `cache_invalidate` / `data_backup` | 缓存失效、数据备份 |

查询接口：`GET /api/admin/audit-logs?type=&user_id=&limit=`（管理员鉴权；`limit` 默认 100）。

> 生产替换：把 `audit.py` 的 `record()` 改为写 `audit_logs` 表或投递到 Kafka/CLS 即可，调用方无需改动。

### 8.2 合规审计与报送（`api/compliance.py`）

面向金融 / 医疗 / 政府客户验收必查项：审计员**只读**视角 + 主动异常发现 + 可归档导出。

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 日志检索 | `GET /api/compliance/audit-logs` | 可按时间 / 用户 / 类型 / 部门 / 风险等级筛选 |
| 事件字典 | `GET /api/compliance/event-types` | 可筛选的操作类型清单（供前端下拉） |
| 导出归档 | `GET /api/compliance/export` | CSV / JSON / PDF 三种格式，交付审计部门归档 |
| 异常检测 | `GET /api/compliance/risk-alerts` | 规则 R1~R6，只读；命中项带风险等级 |
| 合规概览 | `GET /api/compliance/summary` | 日志总数、异常数、活跃用户、按类型分布 |
| 合规报告 | `GET /api/compliance/report` | 按自然月 / 季度生成报告（月度、季度报送） |

访问控制：`admin` 与 `compliance_reviewer` 可读全部；其他角色访问返回 403。

### 8.3 问答限流

`core/ratelimit.py` —— 每用户**滑动窗口**限流，默认 **20 次 / 分钟**（`RATE_LIMIT_PER_MIN` / `RATE_LIMIT_WINDOW_SECONDS` 可调）。
超限抛 `RateLimited`（HTTP 429，错误码 `RATE_LIMITED`），前端给出友好提示。
按 `user_id` 而非 IP 计数，避免 NAT 出口 IP 误伤整个公司。

### 8.4 管理看板

管理员（role=admin）登录后右上角多出「📊 管理看板」按钮，弹窗包含：

- 统计卡片：用户数、会话数、问答数、知识文档/片段数、请假单量、报销单量、缓存命中率、满意度
- 数据看板：DAU/MAU、问答趋势、知识库增长、越权拦截统计（`GET /api/admin/analytics?days=14`）
- 热门问题 Top N
- 审计日志表格（支持按事件类型 / 用户筛选）
- 文档版本管理与回滚入口

后端接口：`GET /api/admin/stats`、`GET /api/admin/analytics`、`GET /api/admin/audit-logs`、`POST /api/admin/cache/invalidate`。

### 8.5 账号与角色治理

- `GET /api/admin/users`：用户列表（含来源 / 停用状态）
- `POST /api/admin/users`、`POST /api/admin/users/{username}/role`、`.../status`、`.../reset-password`：新增、调角色 / 部门、启用停用、重置密码（重置后首次登录强制改密）
- `GET /api/admin/grants-matrix`、`GET/POST/DELETE /api/admin/grants`、`POST /api/admin/grants/bulk`：跨部门授权的手动配置与批量勾选
- 账号治理变更全部写审计，改密与账号状态经 `dept_ids`/角色校验后才生效

### 8.6 检索质量评测

```bash
# 需服务已启动（默认 8000，可 BASE_URL 覆盖）
python backend/scripts/eval_quality.py
```

黄金问答集 11 条，覆盖两类不变量：

- **召回命中**（7 条）：本部门 / 公共库问题必须命中预期关键词
- **权限不变量**（4 条）：**引用列表中绝不允许出现申请人无权访问的 department_id**

评测输出「召回 x/y、越权拦截 x/y」，退出码可直接接 CI 质量门禁。

### 8.7 安全基线加固

- `JWT_SECRET_KEY` 生产必须 ≥32 字节（HMAC-SHA256 要求），仍为默认值或过短时启动自检直接阻断
- 令牌类型隔离：访问令牌与刷新令牌带 `typ` 声明，业务接口只接受 `typ=access`，刷新令牌不能当访问令牌使用
- 登出即时生效：访问令牌进黑名单、刷新令牌轮转作废，令牌状态与登录失败计数落盘，多 worker 下共享一致
- bcrypt 密码哈希、登录失败 5 次锁定 15 分钟、敏感词拦截（`SENSITIVE_WORDS` 可配）、越权统一 404 语义
- 上传文件名清洗 + 类型白名单 + 体积限制，杜绝路径穿越
- SSE 错误帧只下发用户可读文案，不回显异常堆栈
- 演示账号清单接口由 `DEMO_MODE` 门禁控制，交付形态下返回 404

---

## 九、演示语料与数据来源

`backend/app/scripts/seed_demo.py` 中 `fin.general` 库内置一套完整的"上市公司财务数据集"（演示公司：
深交所创业板 301562），覆盖 5 份文档：

- 《季度财务报告》：2025Q1—2026Q2 六个季度营收/净利/毛利率/费用率/现金流，含 Q2 vs Q1 对比与分业务收入
- 《年度财务报告摘要》：2025 年报主要会计数据、分红方案、审计意见、业绩指引
- 《资产负债表与现金流量表摘要》：总资产/负债率/货币资金/应收账款近三个时点 + 现金流三大活动
- 《公司治理与股本结构》：上市信息、前三大股东、董事会/监事会、高管薪酬
- 《投资者关系与信息披露》：定期报告披露日历、业绩说明会、分红派息记录、投资者联系方式

数字全部内部自洽（环比/同比可交叉验证），口径与《行业财务对标与经营分析》一致（亿元）。

**数据来源约定**：行业大盘数字取自工信部《2024年互联网和相关服务业运行情况》（公开数据）；
龙头公司对标数据取自腾讯/拼多多/美团/京东 2024 年公开年报。若要接入真实全市场财务数据，
推荐开源数据源：**AkShare / Tushare / Baostock**（A股行情与财报接口）、
**千言 FinGLM**（上市公司年报问答开源数据集）、巨潮资讯网（法定披露渠道）。
生产环境可直接通过 `/api/knowledge/upload` 上传接口灌入真实定期报告替换演示数据。

---

## 十、部署、备份与监控

### 10.1 切换到真实私有化环境

编辑 `.env`（或直接复制 `.env.example`）：

```bash
LLM_BACKEND=vllm                                    # 接本地 vLLM + Qwen
LLM_BASE_URL=http://vllm:8000/v1
LLM_MODEL=qwen-14b-chat

EMBEDDING_BACKEND=local                             # 接本地 BGE
EMBEDDING_MODEL_PATH=/data/models/bge-large-zh-v1.5

DATABASE_URL=postgresql+psycopg://rag:xxx@localhost:5432/ragdb
REDIS_ENABLED=true
OCR_ENABLED=true                                    # 扫描件识别（需 PaddleOCR）
```

Embedding 三档：`local`（本地 BGE）/ `http`（内网 embedding 微服务）/ `hash`（离线哈希向量，零模型依赖）。
LLM 三档：`cloud`（默认，WorkBuddy 云端免密钥模型，本机无 GPU 也能真实对话）/ `vllm`（生产，接本地 vLLM + Qwen）/ `mock`（完全离线话术）。云端通道的 endpoint 与 publishableKey 只存在服务端 `.env`，不下发前端；失败自动降级 mock。

### 10.2 一键起全栈（PostgreSQL + Redis + API + Nginx + 可选 vLLM）

联调编排（`deploy/docker-compose.yml`）：

```bash
cd deploy && docker compose up -d
docker compose --profile gpu up -d      # 带 GPU 的模型推理服务
```

生产编排（`deploy/docker-compose.prod.yml`，变量插值取自 `.env.production`，**`--env-file` 不可省略**）：

```bash
python deploy/init_env.py                                    # 生成 .env.production（随机密钥与管理员口令）
docker compose --env-file .env.production \
  -f deploy/docker-compose.prod.yml up -d
```

> 生产编排：PostgreSQL / Redis 不映射宿主机端口，只在内网；API 以非 root（uid 10001）运行；
> 数据目录、文档目录、日志目录、备份目录各挂独立卷，容器重建不丢数据。

离线交付流程：联网环境 `docker save` 打成 tar → 模型文件与依赖（`requirements.lock.txt`）打包 → 拷入内网 →
`docker load` → `docker compose --env-file .env.production -f deploy/docker-compose.prod.yml up -d`。启动后零外网流量。

### 10.3 备份与恢复

备份覆盖 **PostgreSQL 全量（`pg_dump -Fc`）+ `APP_DATA_DIR` 业务数据（会话 / 审计 / 反馈 / 工单 / 审批单等）**，
落到独立卷 `backupdata`（`BACKUP_DIR`），带 manifest（sha256 + 版本 + 大小）供完整性校验。

```bash
python backend/scripts/backup.py backup              # 生成一份完整备份
python backend/scripts/backup.py list                # 备份历史（含完整性与可恢复性标记）
python backend/scripts/backup.py verify              # 校验最近一份：产物齐全 + dump 可被 pg_restore 解析 + sha256 一致
python backend/scripts/backup.py restore <备份名>     # 恢复（破坏性操作，需输入 yes；恢复前自动做安全备份）
python backend/scripts/backup.py prune               # 按 BACKUP_KEEP（默认 14）清理旧备份
```

- **备份失败显式报错**：`pg_dump` 不可用或写入失败时抛 `BackupError` 并非 0 退出，不静默降级为"备份成功"
- **恢复可反悔**：恢复前自动打一份安全备份，命令输出 `revert_to`，恢复错了可用它再恢复回来
- **安全约束**：只对 `.dump` / `.zip` 白名单文件名操作，压缩包解压前逐条校验成员路径，阻断路径穿越（Zip-Slip）
- 定时备份：在宿主机 crontab 每日执行 `backup.py backup`，并确保备份失败有告警（见 10.4）

### 10.4 监控与告警

`deploy/monitoring/` 提供可直接使用的 Prometheus 配置：

| 文件 | 用途 |
| --- | --- |
| `prometheus.yml` | 抓取配置（应用 `/metrics` + PostgreSQL / Redis / 节点 exporter） |
| `alerts.yml` | 12 条告警规则，按可用性 / LLM / 性能 / 安全 / 缓存 / 业务六组划分 |
| `README.md` | 接入步骤与 exporter 部署说明 |

指标全部以 `rag_` 前缀定义在 `backend/app/metrics.py`（9 项）：请求数与延迟、LangGraph 节点耗时、
意图分布、缓存命中、权限拦截、LLM 调用与 token、活跃 SSE 流数量。

> 数据库 / 缓存 / 节点 exporter 属于可选增强，不部署不影响告警；对应指标不存在时规则不触发。

### 10.5 上线检查与回滚预案

- 上线前逐项核对 [`docs/交付检查清单.md`](docs/交付检查清单.md)（安全基线、备份恢复演练、监控加载、账号口令、合规开关）
- 回滚预案分两路：镜像回滚（切回上一版 tag）+ 数据库回滚（`backup.py restore <备份名>`），改过表结构时必须先回滚数据库再回滚应用

---

## 十一、验证与回归

**一键回归（推荐，退出码 0 即全部通过）：**

```bash
cd backend
python scripts/run_all_tests.py               # 11 个功能套件，无需外部服务
RUN_E2E=1 python scripts/run_all_tests.py     # 追加 4 个端到端套件（需服务已启动）
```

实测结果：

```
[PASS] 启动自检 preflight / 系统信息接口 / 权限与文档删除审批 / 需求符合性回归
[PASS] 检索质量与上下文治理 / 意图识别准确性 / 公网搜索脱敏与开关 / 实时工具注册表与行情
[PASS] 中断后继续问答 / 请假流程 / 报销流程
  ··（RUN_E2E=1）端到端冒烟 / 会话隔离 / 请假端到端 / 报销端到端
------------------------------------------------------------------
  合计 15/15 项套件通过
```

覆盖的断言数（各套件 `check()` 调用数）：

| 套件 | 断言数 | 套件 | 断言数 |
| --- | --- | --- | --- |
| 启动自检 preflight | 13 | 实时工具注册表与行情 | 57 |
| 系统信息接口 | 6 | 中断后继续问答 | 3 |
| 权限与文档删除审批 | 14 | 请假流程 | 63 |
| 需求符合性回归 | 37 | 报销流程 | 52 |
| 检索质量与上下文治理 | 85 | 端到端冒烟 / 会话隔离 / 请假 / 报销 | 12 / 16 / 16 / 18 |
| 意图识别准确性 | 30 | | |
| 公网搜索脱敏与开关 | 13 | | |

> 端到端套件共用同一个演示账号，而限流是「每用户 20 次/分钟」，连续跑会撞 429。
> 一键入口会在端到端套件之间自动插入冷却（`E2E_COOLDOWN_SECONDS`，默认 45s）；
> 设 `E2E_COOLDOWN_SECONDS=0` 可关闭（单跑某个脚本时用）。

> 全部端到端脚本统一读 `BASE_URL`（默认 `http://127.0.0.1:8000`），
> 管理员口令读 `ADMIN_PASSWORD`（默认 `admin123`，仅演示形态）：
> `BASE_URL=http://127.0.0.1:8000 RUN_E2E=1 python scripts/run_all_tests.py`

**其它可单独执行的门禁与脚本：**

```bash
# 依赖锁定一致性（离线安装可复现）——CI 门禁之一
python backend/scripts/check_dep_lock.py

# 检索质量与上下文治理回归（无需启动服务，85 项断言）
python backend/scripts/test_quality_opt.py

# 环境自检（15 项全链路体检，服务已启动）
python scripts/selfcheck.py

# 端到端冒烟测试（需服务已启动）
python backend/scripts/smoke_test.py

# 请假 / 报销工作流单元测试（无需启动服务，直接驱动领域层）
python backend/scripts/test_leave.py
python backend/scripts/test_reimburse.py

# 检索质量评测（需服务已启动，黄金问答集 + 权限不变量回归）
python backend/scripts/eval_quality.py

# 请假 / 报销工作流端到端验证（需服务已启动）
python backend/scripts/e2e_leave.py
python backend/scripts/e2e_reimburse.py
```

> 端到端脚本开头会先清掉演示账号的遗留草稿（存在草稿时每条消息都会被当成填空），
> 因此可以反复执行。

**CI 门禁（`.github/workflows/ci.yml`，push / PR 到 main、develop 时触发）：**

| # | 门禁 | 内容 |
| --- | --- | --- |
| 1 | 后端编译 | `compileall` 全量语法/导入检查 |
| 2 | 依赖锁定一致性 | `check_dep_lock.py` 校验 lock 覆盖全部直接依赖且精确 pin |
| 3 | pytest 单测 | `backend/tests/` 单测 + 覆盖率报告 |
| 4 | 功能回归 | `run_all_tests.py` 11 个套件 |
| 5 | 前端语法 | `node --check` app.js / antd-bridge.js + HTML 结构检查 |
| 6 | 交付卫生 | `.env`、运行时数据、日志、备份、初始口令文件不得入库；`.dockerignore` 必须覆盖运行时数据与密钥；`APP_VERSION` 三处默认值必须一致 |
| 7 | 镜像 | 生产编排可解析（`docker compose config`）+ 生产镜像可构建（不推送） |

**手动验证 SSE（命令行看逐字效果）：**

```bash
# 口令来源：联调形态为演示口令（DEMO_MODE=true 时由登录页/demo-accounts 接口提供）；
#           交付形态的初始管理员口令见 data/initial_admin.txt（不入库），首次登录强制改密。
ADMIN_PASSWORD='<管理员口令>'
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -N -X POST http://127.0.0.1:8000/api/chat/stream \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"差旅住宿的报销标准是多少？"}'
```

---

## 十二、需求对照

| 需求书编号 | 落地位置 |
| --- | --- |
| FR-CHAT-01 多轮对话 / 流式输出 | `api/chat.py` + `api/sse.py` |
| FR-CHAT-02 四层递进意图识别 | `intent/rules.py`、`intent/classifier.py`、`intent/bert_classifier.py`（L3 小模型层：BERT-tiny + ONNX 可插拔，离线时回退关键词打分） |
| FR-CHAT-04 Redis 高频问答缓存 | `cache/cache.py` |
| FR-CHAT-05 满意度反馈（👍/👎 持久化，不可删除） | `core/feedback_store.py`（`APP_DATA_DIR/feedback.json`，原子写）+ 前端答案下方反馈按钮 |
| FR-CHAT-06 热门问题排行榜（按部门去重计数） | `core/feedback_store.py`（`APP_DATA_DIR/hot.json`）+ `GET /api/chat/hot-questions` + 管理看板 Top N |
| FR-KB-01 部门向量隔离 | `sql/001_schema.sql`、`retrieval/store.py` |
| FR-KB-03 混合检索 | `retrieval/hybrid.py`（RRF） |
| FR-KB-05 文档版本管理（历史版本 / 回滚） | `ingest/version_store.py`（`APP_DATA_DIR/versions.json`）+ `GET /api/knowledge/versions/{doc_name}`、`POST /api/knowledge/versions/{doc_name}/rollback` + 管理看板「文档版本管理」 |
| FR-AUTH-01 首次登录强制改密 | `db/models.py`（`must_change_password`）+ `POST /api/auth/change-password` + `deps.active_user` 守卫 + 前端强制改密弹窗（`FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN` 开关） |
| FR-PERM-01 权限硬校验 | `perm/policy.py`（R1~R6，检索前拦截） |
| FR-PERM（跨事业部高管手动配置 permission_config） | `db/models.py`（`grant_department` / `revoke_department` / `get_grants`）+ `GET/POST/DELETE /api/admin/grants`，可访问部门白名单叠加手动授权 |
| FR-LLM-LOCAL-01/02/03 本地模型 | `llm/`（vLLM / BGE / OCR 适配器） |
| NFR-SEC-07 敏感词拦截（配置化，非硬编码） | `config.py`（`SENSITIVE_WORDS` 环境变量解析）+ `api/chat.py` `_check_sensitive` |
| NFR-OPS-01 结构化日志 | `logging_setup.py`（structlog + trace_id + 节点耗时） |
| NFR-OPS-05 一键部署 | `deploy/docker-compose.yml`、`deploy/docker-compose.prod.yml` |
| NFR-USA-01/02/03 降级策略 | `llm/manager.py`、`retrieval/hybrid.py`、`cache/cache.py` |

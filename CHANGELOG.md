# 变更记录

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)：`主版本.次版本.修订号`。

> **版本号单一来源**：`.env.production` 里的 `APP_VERSION`。
> 它同时决定 ① 健康检查接口返回的 version ② 备份清单里记录的 app_version
> ③ docker compose 的镜像 tag。发版时只改这一处即可。

## [1.2.1] - 2026-09-29

交付一致性收口：让文档、脚本、配置三者的描述与真实行为完全对齐。
回归 15/15 套件通过（11 功能 + 4 端到端），pytest 22 项通过。

### 变更（交付形态会影响行为，升级时注意）
- 生产配置模板（`.env.production.example`）与 `deploy/init_env.py` 生成的
  `RATE_LIMIT_PER_MIN` 由 120 调整为 **20**，与 `config.py` 默认值、`.env.example` 一致
  （每用户每分钟问答次数，按甲方容量要求可调；同时补上 `RATE_LIMIT_WINDOW_SECONDS`）。
- 验收类脚本统一改为从 `ADMIN_PASSWORD` 环境变量取管理员口令，默认值 `admin123` 仅适用于
  联调形态。交付环境执行 `eval_quality.py` / `smoke_test.py` / `test_doc_perm.py` /
  `scripts/verify_*.py` 时需传 `ADMIN_PASSWORD=<初始口令>` —— 这些脚本原先硬编码演示口令，
  在交付环境根本无法登录，等于验收脚本无法在客户现场执行。

### 修复
- 端到端脚本默认端口统一为 **8000**（`e2e_leave` / `e2e_reimburse` / `eval_quality` 原默认 8011，
  `test_sessions` 原默认 8014），均可被 `BASE_URL` 覆盖；单独执行不再需要记各脚本的端口。
- `test_optimize.py` 改密用例的"还原口令"步骤使用了不满足密码复杂度校验的口令，
  导致该套件 37 项断言中固定失败 1 项；现改为直接回写原哈希，用例幂等可重复执行。
- `backend/app/scripts/seed_demo.py` 模块说明与真实语料不符（写"3 个事业部、7 个子部门、
  30+ 篇文档"，实为 10 个知识库、64 篇文档），已按实际内容重写；
  `frontend/` 构成、`MiniGraph` 规模等注释与实际不符处一并修正。

### 新增
- `backend/scripts/check_dep_lock.py` + CI 门禁：校验 `requirements.lock.txt` 覆盖
  `requirements.txt` 的全部直接依赖且精确 pin，防止离线/内网交付装出与验收环境不一致的版本。
- CI 新增"版本号一致性"门禁：`config.py`、生产 compose、`.env.production.example`
  三处 `APP_VERSION` 默认值必须一致。

### 文档
- `README.md` 重写：章节编号连续化，补齐**接口总览（95 个路由）**、**备份与恢复**、
  **监控与告警**、**合规审计与报送**、**前端资源构成**、**CI 七道门禁**；
  文中的账号数、角色数、知识库数、文档数、片段数、断言数、指标数、告警数、命令与路径
  全部与代码逐条核对，并移除历史叙述，只描述当前状态。
- `docs/` 四份文档修正：备份脚本章节统一为 `backend/scripts/backup.py`；
  运维手册的 compose 命令补 `--env-file` 与 `-f`；验收清单的通过判据改为"退出码 0 +
  汇总行"（并非所有套件都打印 `RESULT:`）；来源徽章文案与前端一致；
  `preflight` 日志格式改写为实际格式；依赖锁定文件的实际用途写清。

## [1.2.0] - 2026-09-29

面向企业级交付的第二轮审视：修复安全与部署缺陷，并让文档与代码严格对齐。
本轮多数改动源于「宣称的能力与实际行为不一致」——这类问题在交付现场代价最大。

### 修复（安全）
- **刷新令牌可当访问令牌使用**：`decode_token` 此前只校验签名与有效期，不校验 `typ`。
  访问令牌 30 分钟、刷新令牌 7 天，同密钥签发，因此任何人都能拿 `refresh_token`
  直接调业务接口，且登出（只拉黑访问令牌）对它无效 —— 短期令牌的时效设计被绕过。
  现已强制令牌类型（`expect_typ`），业务接口只接受 `typ=access`。
- **匿名接口泄露管理员明文口令**：`GET /api/auth/demo-accounts` 无任何鉴权即返回
  全部演示账号与口令（含 `admin`）。现已加 `DEMO_MODE` 门禁，非演示模式返回 404。
- **上传路径穿越**：`file.filename` 未做清洗即拼进 `os.path.join`，
  构造 `../../x.txt` 可把文件写出上传目录、覆盖应用数据。现只取文件名末段并做落点校验。
- **多 worker 下会话状态不一致**：令牌黑名单/刷新白名单、登录失败锁定均为进程内内存，
  而生产以 `--workers 2` 运行（同容器两进程），导致"登出后另一个 worker 仍认这个令牌"、
  "5 次锁定实际变 10 次"。现按文件指纹（mtime+size）增量重载，两进程秒级收敛。
- **`/api/admin/departments` 缺管理员校验**：任意登录用户可拉全量组织树（含层级与父子关系）。
  合规台账改走只读的 `/api/knowledge/departments`，不影响非管理员角色的部门名展示。
- **SSE 错误帧回显异常细节**：原样返回 `类型名: 异常内容` 给前端，现改为统一可读提示，
  原始堆栈只进服务端日志。
- **Redis 生产未设口令**：仅靠内网隔离不够，compose 现强制 `--requirepass`
  （变量必填，取不到值会在解析阶段报错），healthcheck 同步带认证。

### 修复（数据与备份）
- **备份从不包含真正的业务数据目录**：`data_dir()` 读取的是 `settings.APP_DATA_DIR`，
  而 Settings 无此字段且 `extra="ignore"`，永远取到空值 → 静默回退到错误目录，
  备份"成功"但 `data.zip` 是空的。现与各业务 store 统一按 `APP_DATA_DIR` 取值。
- **6 处数据目录硬编码**（审计 / 会话 / 令牌 / 权限申请 / 请假 / 报销）不认 `APP_DATA_DIR`，
  生产环境会写进容器可写层，容器重建即丢。现已统一。
- **上传文档与日志落在可写层**：`DOC_UPLOAD_DIR` / `LOG_DIR` 未在生产配置中设置，
  挂载的 `ragdocs` / `raglogs` 卷永远是空的。已在 compose 与配置模板中显式对齐。
- 备份包解压改用逐条目校验（防 Zip-Slip）；`verify` 新增 SHA-256 与清单比对
  （此前只看"文件在不在"，磁盘损坏时照样判为通过）；`--out` 旧参数此前改了环境变量
  但配置已固化为单例，实际静默失效，现改为直接生效。

### 修复（部署可用性）
- **按手册启动必然失败**：生产 compose 的 `${PG_PASSWORD:?}` 等插值不读 service 的
  `env_file`，缺 `--env-file .env.production` 会在解析阶段报错。compose 注释与
  部署手册、运维手册、交付检查清单、验收清单均已补上正确命令。
- **开发 compose 的 Nginx 起不来**：它指向要求 443 + TLS 证书的生产配置，且未挂载
  被 `include` 的 `api_proxy.inc`。新增 `deploy/nginx.dev.conf`（纯 HTTP）供开发使用。
- `deploy/init_env.py` 生成的 `APP_VERSION=1.0.0`、`MAX_UPLOAD_MB=100` 与后端
  （1.1.0 / 50）及 Nginx（50m）长期漂移。现改为直接读取后端配置，单一来源。
- 生产镜像 `COPY backend` 会把 `backend/data/`（含 `initial_admin.txt` 明文管理员口令、
  审计与令牌文件）打进交付镜像。新增 `.dockerignore` 排除运行时数据、密钥与环境文件。
- 生产 compose 的 Redis 在未配置口令时不再静默以无认证启动。

### 变更（升级需注意）
- 生产启动命令必须带 `--env-file .env.production`。
- `.env.production` 需新增 `REDIS_PASSWORD`；未提供会启动失败（安全上的故意选择）。
- 版本号 `1.2.0`。

### 修复（可观测性）
- `rag_request_latency_seconds` 此前只定义、从未 `observe`，`rag_requests_total`
  只记 `status="ok"` —— 导致 P95 延迟告警与错误率告警在任何情况下都不会触发。
  现补齐耗时埋点，失败请求以 `status="error"` 计数。

### 文档
- 逐条核对文档中的可核实断言并修正失实项：不存在的 `GET /api/admin/overview`、
  `audit-logs` 错误参数名（`user`/`offset`）、"灌入 13 个部门"（实为 10 个）、
  脚本断言数（12→14、47→85、25→30、9→13）、"四级组织"（实为三级）、
  前端"单文件"（实为多文件）、角色表缺 2 种角色（实为 7 种）、
  验收用例数（32→37）、指标端点 `/api/metrics`（实为 `/metrics`）、
  上传上限 100m（实为 50m）、README 中的明文 `admin123`（与"口令不入库"自相矛盾）。
- RLS 一层如实标注为**默认未生效**，并写明启用所需的两步接线（此前描述会让人误以为
  数据库层已在兜底）。LDAP/SSO 标注为需二次开发（仓库内无实现）。

---

## [1.1.0] - 2026-09-29

面向企业级交付补齐工程化能力：备份恢复、CI 门禁、告警规则、版本追溯、交付清单。

### 新增
- **备份与恢复体系**（`backend/app/core/backup.py`）
  - 数据库全量备份（`pg_dump` custom 格式，可用 `pg_restore` 选择性恢复）
  - 恢复前自动对当前库做安全备份，任何一次恢复都可反悔
  - 备份清单 `manifest.json` 记录时间/版本/各部分大小与 SHA-256
  - 运维 CLI `backend/scripts/backup.py`（backup / list / restore / verify / prune），可挂 crontab
  - 管理后台新增 `POST /api/admin/restore`，需 `confirm=true` 二次确认
- **CI 质量门禁**（`.github/workflows/ci.yml`）：编译 → pytest → 回归套件 → 前端语法 → 交付卫生 → 镜像可构建
- **告警规则**（`deploy/monitoring/`）：12 条规则覆盖可用性、LLM、性能、安全、缓存、业务六组
- **交付检查清单与回滚预案**（`docs/交付检查清单.md`）

### 修复
- **备份此前只打包 `data/` 目录，不含 PostgreSQL** —— 而向量库、会话、审计、反馈全在主库，
  等于备份了最不重要的部分，且没有任何恢复手段。现已纳入数据库并补齐恢复能力。
- 生产镜像缺少 `pg_dump`，备份在容器内必然失败。已在 runtime 阶段从 PGDG 源安装
  `postgresql-client-16`（版本须与 pg16 主库一致，Debian 默认源的 client 15 会被拒绝执行）。
- 备份卷此前落在容器可写层，容器重建即丢失。已改为独立卷 `backupdata`。

### 变更
- 备份相关设计**故意不遵循**项目其他模块的"静默降级"约定：
  备份失败必须显式报错（抛 `BackupError`），因为"以为备份了其实没备份"比"明确告知失败"危险得多。

---

## [1.0.1] - 2026-09-29

### 新增
- **结构化实时数据接口层**（`backend/app/tools/finance.py`）
  - 个股 / 板块 / 大盘 / 汇率四种子工具，主源东方财富 push2，被限流时自动切腾讯行情备源
  - 内部事项护栏：问"我们公司的股价制度"不会被当成行情问题
  - 独立开关 `REALTIME_MARKET_ENABLED`
- **参考类工具**（`backend/app/tools/reference.py`）：百科（百度百科 OpenAPI）、节假日（timor.tech），均免密钥
- **搜索层升级**：配 `SEARCH_API_PROVIDER` + `SEARCH_API_KEY` 后直连 Tavily/Serper 取 JSON，
  不再抓取网页正文；未配置时自动回退
- **工具注册表两阶段调度**：primary（检索前抢答：天气/行情/节假日）+ fallback（内网答不上来后兜底：百科）

### 修复
- 网页搜索结果被导航站、词典页污染 → 新增垃圾结果过滤
- 板块排行误按"点位"排序而非涨跌幅 → 改为显式按涨跌幅排序
- 6 位股票代码抽取用 `\b`（中文边界不生效）→ 改为中文边界匹配
- `dept_ids` 为空时节点直接返回 chat，导致"李白是谁"类问题永远轮不到百科，只能靠模型记忆
- 百科 `card[].value` 是 list 不是 str（直接 strip 会崩）；属性值夹带 HTML 且需先删 `<sup>` 脚注
- `scripts/test_leave.py` 客套话断言假失败：`chitchat_reply` 是变体池随机，原断言只认 2/4 变体，
  40% 概率失败；改为断言"属于合法变体集合"

---

## [1.0.0] - 2026-09-29

首个可交付版本。

### 包含
- 权限感知型 RAG 检索（混合检索 + 权限前置拦截 + 引用溯源）
- LangGraph 6 节点工作流（不可用时降级为内置 MiniGraph）
- 上下文治理四层机制（追问检测 / 引用溯源 / 滑动窗口 / 会话边界）
- 8 类外部依赖降级容错（向量 / 缓存 / 数据库 / LLM / 图引擎 / Embedding）
- 请假与报销多轮采集流程、合规审计、知识库管理与版本、数据看板
- 私有化部署：多阶段镜像 + 生产编排 + Nginx(TLS/安全头)
- 生产启动门禁（preflight）、结构化日志 + trace_id、Prometheus 指标

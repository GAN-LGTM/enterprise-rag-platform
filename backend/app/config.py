"""
全局配置。所有开关都通过环境变量 / .env 控制，便于私有化交付时一份配置文件适配不同客户。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 以文件位置定位项目根，避免「从 backend/ 目录启动就读不到 .env」的问题。
# 列表中后者优先：cwd 下的 .env 若存在则覆盖项目根的默认值。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_PROJECT_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------- 基础 ----------------
    APP_NAME: str = "企业级知识检索中台"
    APP_VERSION: str = "1.2.1"
    PORT: int = 8000          # 仅用于启动横幅提示；实际端口以 uvicorn --port 为准
    DEBUG: bool = False
    OFFLINE_MODE: bool = True          # 私有化部署：启动后零外网流量
    PUBLIC_DEPT_ID: str = "public"     # 公共知识库部门ID

    # 敏感部门（合规 R5）：访问这些部门的知识库会被打上"敏感部门访问"风险标记。
    # 金融/医疗/政府客户通常把 财务、人事、法务 列为敏感部门；可按甲方制度用环境变量覆盖：
    #   SENSITIVE_DEPTS=fin,hr,legal
    # 同 SENSITIVE_WORDS：声明为标量 str 以避开 pydantic-settings 对 list 字段的 JSON 解码。
    SENSITIVE_DEPTS_RAW: str = Field(
        default="fin,hr,legal",
        validation_alias="SENSITIVE_DEPTS",
    )

    @property
    def SENSITIVE_DEPTS(self) -> list[str]:
        return [x.strip() for x in self.SENSITIVE_DEPTS_RAW.split(",") if x.strip()]

    # 部署环境：dev = 本机/联调（允许演示数据与降级后端）
    #          production = 甲方私有化交付（启动自检会强制校验安全基线，不合规拒绝启动）
    ENVIRONMENT: Literal["dev", "production"] = "dev"

    # 演示模式开关：显式设置时以此为准；未设置时按环境推断（production → 关闭）。
    # 开启后才会注册内置演示账号并灌入示例知识库；生产交付必须关闭。
    DEMO_MODE_RAW: str | None = Field(default=None, validation_alias="DEMO_MODE")

    @property
    def DEMO_MODE(self) -> bool:
        if self.DEMO_MODE_RAW is None:
            return self.ENVIRONMENT != "production"
        return str(self.DEMO_MODE_RAW).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def IS_PRODUCTION(self) -> bool:
        return self.ENVIRONMENT == "production"

    # ---------------- 安全 ----------------
    # 生产环境禁止使用默认密钥（preflight 会校验长度与默认值并拒绝启动）
    JWT_SECRET_KEY: str = "please-change-this-secret-in-production-32bytes-minimum!"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 30
    # 刷新令牌有效期（天）：短期访问令牌 30 分钟，刷新令牌 7 天并支持轮转，
    # 避免用户每 30 分钟被踢出重新登录（企业级体验基线）。
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    LOGIN_FAIL_LIMIT: int = 5          # 连续失败次数
    LOGIN_LOCK_MINUTES: int = 15       # 锁定分钟数

    # 密码复杂度（FR-AUTH-02）：至少 8 位，且须同时含大写、小写、数字、特殊字符
    PASSWORD_MIN_LEN: int = 8
    PASSWORD_REQUIRE_UPPER: bool = True
    PASSWORD_REQUIRE_LOWER: bool = True
    PASSWORD_REQUIRE_DIGIT: bool = True
    PASSWORD_REQUIRE_SPECIAL: bool = True

    # 入库前对文本做敏感数据脱敏（身份证 / 手机号 / 邮箱 / 银行卡），
    # 保证向量与检索存储中不留原始 PII（NFR-SEC-09）。
    MASK_SENSITIVE_DATA: bool = True

    # 文档有效期治理：最后更新时间超过该天数的文档标记为"已过期"（前端红色标识），
    # 提示责任部门复核更新。设为 0 表示不启用过期判定。
    DOC_EXPIRE_DAYS: int = 365

    # 首次登录强制改密：生产交付时为 True，演示环境置 False 避免阻塞体验
    FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN: bool = False

    # 初始管理员账号（生产交付由部署脚本/环境变量注入；留空则启动时随机生成并落盘一次）
    ADMIN_INITIAL_USERNAME: str = "admin"
    ADMIN_INITIAL_PASSWORD: str = ""
    ADMIN_FORCE_PASSWORD_CHANGE: bool = True   # 初始管理员首次登录强制改密

    # 浏览器来源白名单（逗号分隔）。生产环境禁止 "*"（preflight 校验）
    ALLOWED_ORIGINS_RAW: str = Field(default="*", validation_alias="ALLOWED_ORIGINS")

    @property
    def ALLOWED_ORIGINS(self) -> list[str]:
        return [x.strip() for x in self.ALLOWED_ORIGINS_RAW.split(",") if x.strip()]

    # ---------------- 敏感词 ----------------
    # 需求书 NFR-SEC-07：用户问题触发敏感词时返回友好提示，不进入检索。
    # 环境变量 SENSITIVE_WORDS 以逗号分隔覆盖默认值（如 SENSITIVE_WORDS=密码明文,root密码）。
    #
    # 注意：这里刻意声明为标量 str 而非 list[str]。pydantic-settings 对 list 字段会先做
    # json.loads 再交给校验器，而 "a,b,c" 不是合法 JSON，会直接抛 SettingsError 导致
    # **服务启动失败**（此时 field_validator / NoDecode 都救不了）。声明成标量后由下面的
    # property 统一按逗号切分，env、默认值、构造参数三种写法都能正确工作。
    SENSITIVE_WORDS_RAW: str = Field(
        default="密码明文,root密码,数据库口令,数据库密码,内网拓扑,VPN账号,跳板机密码",
        validation_alias="SENSITIVE_WORDS",
    )

    @property
    def SENSITIVE_WORDS(self) -> list[str]:
        return [x.strip() for x in self.SENSITIVE_WORDS_RAW.split(",") if x.strip()]

    # ---------------- 公网搜索 / 外部工具（合规总开关）----------------
    # 闲聊/通用类问题走公网搜索（必应/搜狗），业务问题走内网 RAG，二者不混。
    # 该开关**同时控制**「实时天气」等所有出公网的外部工具调用。
    #
    # 合规要点（金融/政务客户常要求零外网）：
    #   * OFFLINE_MODE=true（私有化默认）→ 本开关默认关闭，即"零外网流量"承诺生效；
    #   * 客户确实需要联网问答时，显式设 PUBLIC_SEARCH_ENABLED=true 开启（并理解将产生外网请求）；
    #   * 即便开启，出网前也会脱敏（见 tools/websearch.sanitize_query），绝不携带内网文档/
    #     部门名/人名/用户身份。
    PUBLIC_SEARCH_ENABLED_RAW: str | None = Field(default=None, validation_alias="PUBLIC_SEARCH_ENABLED")

    @property
    def PUBLIC_SEARCH_ENABLED(self) -> bool:
        if self.PUBLIC_SEARCH_ENABLED_RAW is None:
            return not self.OFFLINE_MODE
        return str(self.PUBLIC_SEARCH_ENABLED_RAW).strip().lower() in {"1", "true", "yes", "on"}

    # 行情类实时工具独立开关（A股行情/指数/板块/汇率，见 tools/finance.py）。
    # 双开关设计：公网总开关关 → 一律不出网（合规优先）；
    #            公网总开关开 → 仍可单独关掉行情工具（比如客户不想让员工在内部系统里问股票）。
    REALTIME_MARKET_ENABLED: bool = True

    # 参考类实时工具独立开关（百科词条 + 中国节假日，见 tools/reference.py）。
    # 同样是双开关：公网总开关关 → 不出网；总开关开 → 可单独关掉（比如客户认为
    # "百科/节假日"与业务无关，想让系统只答内网知识）。
    REFERENCE_TOOLS_ENABLED: bool = True

    # 结构化搜索 API（替代"自己爬搜索引擎网页"）。
    # 留空 = 不配，走内置的必应/搜狗抓取兜底（带垃圾页过滤，但质量天然不如 API）；
    # 配了 = 直接向搜索 API 要结构化结果（title/snippet/url），这才是豆包/DeepSeek
    # 联网问答的做法，能彻底告别导航站、词典页这类脏结果。
    #   tavily —— 面向 LLM 设计的搜索 API，返回可直接进 Prompt 的摘要（推荐）
    #   serper —— Google 结果封装
    SEARCH_API_PROVIDER: str = ""            # tavily | serper | 空
    SEARCH_API_KEY: str = ""

    # 出公网前脱敏用的"内部术语"清单（逗号分隔）：命中即剔除，避免把公司内部
    # 产品代号/项目名/黑话随问题捎带给公网搜索引擎。为空则仅脱敏部门名与用户身份。
    INTERNAL_TERMS_RAW: str = Field(default="", validation_alias="INTERNAL_TERMS")

    @property
    def INTERNAL_TERMS(self) -> list[str]:
        return [x.strip() for x in self.INTERNAL_TERMS_RAW.split(",") if x.strip()]

    # ---------------- 数据库 ----------------
    DATABASE_URL: str = "postgresql+psycopg://rag:rag123@localhost:5432/ragdb"
    DB_FORCE_MEMORY: bool = False     # true = 跳过 PG 探测，直接用内存向量库（纯演示/单测）
    DB_AUTO_FALLBACK: bool = True      # PG 不可用时自动降级到内存向量库（保证服务不中断）
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10

    # ---------------- 备份与恢复（企业级交付底线能力）----------------
    # BACKUP_DIR 为空时默认 <项目根>/backups；生产务必挂到独立卷，别放在容器可写层。
    BACKUP_DIR: str = ""
    # 保留最近 N 份，超出自动清理（0 或负 = 不清理）
    BACKUP_KEEP: int = 14

    # ---------------- 意图小模型（FR-CHAT-02 L3）----------------
    # 生产由 BERT-tiny + ONNX Runtime 本地推理；离线/未配置时自动回退到关键词打分器。
    BERT_ENABLED: bool = False
    BERT_MODEL_PATH: str = "/data/models/bert-tiny/intent.onnx"
    # 同 SENSITIVE_WORDS：声明为标量 str 以避开 list 字段的 JSON 解码，支持 BERT_LABELS=a,b,c
    BERT_LABELS_RAW: str = Field(
        default="chitchat,public_kb,dept_kb,cross_dept,data_analysis,operation",
        validation_alias="BERT_LABELS",
    )

    @property
    def BERT_LABELS(self) -> list[str]:
        return [x.strip() for x in self.BERT_LABELS_RAW.split(",") if x.strip()]

    # ---------------- 检索 ----------------
    # hash = 离线伪向量（仅 dev/演示可跑通链路，无真实语义）；生产必须显式配置 local 或 http，
    #        preflight 会在 production 环境拒绝 hash，避免交付后出现"检索结果不可信"的严重问题。
    EMBEDDING_BACKEND: Literal["local", "http", "hash"] = "hash"
    #   local = 本地 sentence-transformers(BGE)  http = 自建 embedding 服务  hash = 伪向量（仅演示）
    EMBEDDING_MODEL_PATH: str = "/data/models/bge-large-zh-v1.5"
    EMBEDDING_HTTP_URL: str = "http://localhost:9997/embed"
    EMBEDDING_DIM: int = 1024

    TOP_K_VECTOR: int = 10
    TOP_K_FTS: int = 10
    # 语料扩到 48 篇后，混合问题（如"公司编制 + 行业研发经费"）需要更多召回位；
    # 引用卡片前端已默认折叠，8 条不会挤占输出区
    TOP_K_FINAL: int = 8
    RRF_K: int = 60                    # RRF 平滑常数
    VECTOR_TIMEOUT_MS: int = 1500      # 向量检索超时 → 降级为关键词

    # ---- 检索质量优化（详见 retrieval/rerank.py / quality.py / context.py）----
    # RRF 融合后的可解释重排：标题命中加权 + 长短语命中加分 + 高频官僚词（材料/制度/管理…）降权。
    # 纯规则、零依赖，关掉即回到原始 RRF 顺序。
    RERANK_ENABLED: bool = True

    # 来源相关性校验（幻觉治理防线一）：检索到的文档跟问题不沾边时，
    # 不把无关资料丢给模型自由发挥，而是下发"依据不足"的确定性话术。
    RELEVANCE_GATE_ENABLED: bool = True

    # 生成后引用归一化（防线三）：只保留答案中实际引用的 [N]，按出现顺序重排编号。
    # 前端引用卡片数量随之收敛，下发给用户的信息不再"看起来有依据其实没用"。
    CITATION_NORMALIZE_ENABLED: bool = True

    # 上下文治理开关：追问检测 / 会话边界识别 / 历史按字符预算裁剪。
    CTX_GOVERNANCE_ENABLED: bool = True
    # 历史消息送入模型的字符预算（滑动窗口）。按字符而非条数，长短消息才公平。
    CTX_HISTORY_MAX_CHARS: int = 3000
    CTX_HISTORY_MAX_TURNS: int = 6
    # 追问时收敛检索范围：优先召回上一轮实际引用过的文档，并把候选数减半（省上下文）
    CTX_FOLLOWUP_SCOPE_ENABLED: bool = True

    # ---------------- 缓存 ----------------
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_ENABLED: bool = True
    CACHE_TTL_SECONDS: int = 3600
    CACHE_AUTO_FALLBACK: bool = True   # Redis 挂了自动降级为本地 LRU
    CACHE_MAX_ITEMS: int = 5000

    # ---------------- LLM ----------------
    # vllm   = 本地 vLLM / 任意 OpenAI 兼容服务（私有化主选）
    # mock   = 无 GPU 环境的模拟后端，逐字输出，用于打通链路与演示
    LLM_BACKEND: Literal["cloud", "vllm", "mock"] = "cloud"
    # WorkBuddy 云端 LLM（仅 dev 联调用；生产私有化环境默认禁止，需显式 ALLOW_CLOUD_LLM=true）
    ALLOW_CLOUD_LLM: bool = False
    # WorkBuddy 云端免密钥 LLM（服务端配置，不下发前端）
    CLOUD_LLM_ENDPOINT: str = "https://enterprise-rag.app.workbuddy.host"
    CLOUD_LLM_PUBLISHABLE_KEY: str = ""
    CLOUD_LLM_MODEL: str = "auto"
    LLM_BASE_URL: str = "http://localhost:8000/v1"
    LLM_MODEL: str = "qwen-14b-chat"
    LLM_API_KEY: str = "EMPTY"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 4096
    LLM_TIMEOUT_SECONDS: float = 3.0   # 首字超时 → 切备选（三级降级第一级）
    LLM_FALLBACK_MODEL: str = "qwen-7b-chat"
    LLM_CIRCUIT_FAIL_THRESHOLD: int = 3     # 连续失败 N 次熔断
    LLM_CIRCUIT_RECOVER_SECONDS: int = 30   # 熔断恢复时间
    LLM_SLOW_MS: float = 1000.0             # 健康检查中 LLM 响应超过该值记为"响应较慢"降级原因

    # ---------------- 钉钉请假集成 ----------------
    # mock=true 时无需任何凭证即可演示完整审批链路（本地模拟钉钉回调）；
    # 生产接入真实钉钉：DINGTALK_MOCK=false + DINGTALK_ENABLED=true 并填好下方凭证，
    # 系统会调用钉钉 OAPI（gettoken + topapi/processinstance/create）创建审批实例，
    # 审批动作在平台内完成后台回调到钉钉（此处用本地审批台模拟）。
    DINGTALK_MOCK: bool = True
    DINGTALK_ENABLED: bool = False
    DINGTALK_APP_KEY: str = ""
    DINGTALK_APP_SECRET: str = ""
    DINGTALK_AGENT_ID: str = ""
    DINGTALK_LEAVE_PROCESS_CODE: str = ""      # 钉钉 OA 审批「请假」模板 processCode
    DINGTALK_REIMBURSE_PROCESS_CODE: str = ""  # 钉钉 OA 审批「报销」模板 processCode

    # ---------------- SSE 流式 ----------------
    STREAM_HEARTBEAT_SECONDS: float = 15.0  # 心跳间隔，防止网关/Nginx 掐断长连接
    STREAM_CACHE_CHUNK_DELAY: float = 0.012 # 缓存命中时模拟逐字的间隔（前端体验一致）

    # ---------------- 限流 ----------------
    RATE_LIMIT_PER_MIN: int = 20        # 每用户每分钟问答次数（0 = 关闭限流）
    RATE_LIMIT_WINDOW_SECONDS: int = 60 # 滑动窗口宽度

    # ---------------- 文档 ----------------
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50
    MAX_UPLOAD_MB: int = 50
    OCR_ENABLED: bool = False          # PaddleOCR，私有化环境按需开启
    DOC_UPLOAD_DIR: str = "./data/docs"

    # ---------------- 可观测性 ----------------
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./logs"
    METRICS_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

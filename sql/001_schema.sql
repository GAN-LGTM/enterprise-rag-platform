-- ============================================================
-- 001 表结构：业务 / 向量 / 全文 三合一（PostgreSQL 15+ / pgvector）
-- ============================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- 组织与用户 ----------
CREATE TABLE IF NOT EXISTS departments (
    id            VARCHAR(50) PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    parent_id     VARCHAR(50) REFERENCES departments(id),
    level         SMALLINT NOT NULL DEFAULT 1,   -- 1=事业部 2=子部门
    created_at    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id                VARCHAR(50) PRIMARY KEY,
    username          VARCHAR(64) UNIQUE NOT NULL,
    password_hash     VARCHAR(255) NOT NULL,
    display_name      VARCHAR(100),
    role              VARCHAR(20) NOT NULL,      -- admin/executive/dept_director/sub_manager/sub_employee
    dept_id           VARCHAR(50) REFERENCES departments(id),
    managed_dept_ids  JSONB DEFAULT '[]',        -- executive 管辖范围
    can_upload        BOOLEAN DEFAULT FALSE,
    failed_logins     INT DEFAULT 0,
    locked_until      TIMESTAMP,
    created_at        TIMESTAMP DEFAULT NOW()
);

-- ---------- 知识库（统一向量表 + department_id 逻辑隔离）----------
CREATE TABLE IF NOT EXISTS doc_chunks (
    id             BIGSERIAL PRIMARY KEY,
    department_id  VARCHAR(50) NOT NULL REFERENCES departments(id),
    doc_name       VARCHAR(255) NOT NULL,
    doc_version    INT DEFAULT 1,
    chunk_text     TEXT NOT NULL,
    chunk_index    INT NOT NULL,
    page_num       INT,
    embedding      vector(1024) NOT NULL,        -- BGE-large-zh 1024 维
    metadata       JSONB DEFAULT '{}',
    uploaded_by    VARCHAR(64),
    uploaded_at    TIMESTAMP DEFAULT NOW(),
    fts_document   TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', chunk_text)) STORED
);

-- ---------- 文档版本 ----------
CREATE TABLE IF NOT EXISTS doc_versions (
    id             BIGSERIAL PRIMARY KEY,
    doc_name       VARCHAR(255) NOT NULL,
    version        INT NOT NULL,
    department_id  VARCHAR(50) NOT NULL,
    file_path      VARCHAR(500),
    chunk_count    INT,
    content_hash   VARCHAR(64),                  -- 内容哈希，用于去重
    uploaded_by    VARCHAR(64),
    uploaded_at    TIMESTAMP DEFAULT NOW(),
    change_note    TEXT,
    UNIQUE(doc_name, version)
);

-- ---------- 会话与消息 ----------
CREATE TABLE IF NOT EXISTS chat_sessions (
    id            VARCHAR(50) PRIMARY KEY,
    user_id       VARCHAR(50) NOT NULL,
    department_id VARCHAR(50),
    title         VARCHAR(200),
    last_intent   VARCHAR(30),
    created_at    TIMESTAMP DEFAULT NOW(),
    updated_at    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   VARCHAR(50) NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role         VARCHAR(10) NOT NULL,
    content      TEXT NOT NULL,
    citations    JSONB,
    intent       VARCHAR(30),
    trace_id     VARCHAR(32),
    created_at   TIMESTAMP DEFAULT NOW()
);

-- ---------- 反馈 / 审计 / 热门 ----------
CREATE TABLE IF NOT EXISTS qa_feedback (
    id             BIGSERIAL PRIMARY KEY,
    session_id     VARCHAR(50),
    question       TEXT NOT NULL,
    answer         TEXT,
    feedback_type  VARCHAR(10) CHECK (feedback_type IN ('like','dislike')),
    feedback_reason TEXT,
    department_id  VARCHAR(50),
    user_id        VARCHAR(50),
    created_at     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    user_id     VARCHAR(50),
    action      VARCHAR(50) NOT NULL,
    target_type VARCHAR(50),
    target_id   VARCHAR(50),
    details     JSONB,
    ip_address  VARCHAR(45),
    user_agent  TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hot_questions (
    question     TEXT PRIMARY KEY,
    hit_count    INT DEFAULT 1,
    department_id VARCHAR(50),
    updated_at   TIMESTAMP DEFAULT NOW()
);

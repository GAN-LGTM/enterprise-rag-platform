-- ============================================================
-- 002 索引：向量 HNSW / 全文 GIN / 部门 B-tree
-- ============================================================

-- 向量语义检索（余弦距离）
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON doc_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 全文检索
CREATE INDEX IF NOT EXISTS idx_chunks_fts
    ON doc_chunks USING GIN (fts_document);

-- 部门过滤（权限隔离的命脉：所有查询都会带 department_id）
CREATE INDEX IF NOT EXISTS idx_chunks_dept
    ON doc_chunks (department_id);

-- 文档维度（删除/版本回滚时按文档定位）
CREATE INDEX IF NOT EXISTS idx_chunks_doc
    ON doc_chunks (doc_name, department_id, doc_version);

-- 审计与反馈的常用查询条件
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_logs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action    ON audit_logs (action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_dept   ON qa_feedback (department_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages (session_id, created_at);

-- 中文全文检索增强（可选）：
-- 若客户环境安装了 zhparser 或 pg_jieba，把 001 里的 'simple' 换成对应分词配置，
-- 中文召回率会明显提升；未安装时系统走 ILIKE 降级，功能不受影响。
-- CREATE INDEX IF NOT EXISTS idx_chunks_fts_zh ON doc_chunks USING GIN (to_tsvector('zhparser', chunk_text));

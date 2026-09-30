-- ============================================================
-- 003 行级安全（RLS）：三层防护的最后一层（默认未启用，启用需按下文接线）
-- ------------------------------------------------------------
-- 应用层已经强制传 dept_ids，这一层是"兜底"——
-- 万一某处漏写 WHERE，数据库依然会强制过滤，无权限数据查不出来。
--
-- ⚠ 必须知道的前提：执行完本脚本的 CREATE POLICY，RLS **并不会自动生效**。
--    PostgreSQL 对「表 owner」和「superuser」默认绕过 RLS，而 compose 里应用用的
--    正是 POSTGRES_USER（即库 owner / superuser）；并且应用从不执行
--    SET app.current_depts。两项叠加的结果是：当前默认部署下这层兜底的实际
--    拦截能力为 0。真正的权限隔离由应用层 backend/app/perm/policy.py
--    的「检索前硬拦截」保证（那是本项目 R1~R6 规则的主防线）。
--
-- 要让本层真正生效，必须完成两步（缺一不可）：
--   1) 应用改以「非 superuser、非表 owner」的角色连接（见下方第 3 步）；
--   2) 每次查询前执行 SET LOCAL app.current_depts = 'public,mkt.sales'
--      （必须与查询在同一事务内：连接池会复用连接，不带 LOCAL 的 SET 会串到
--        其他请求，导致越权读到别人的数据）。第 2 步需要改检索层代码，
--      属二次开发项，本仓库默认不实现。
-- ============================================================

-- 1) 开启 RLS
ALTER TABLE doc_chunks ENABLE ROW LEVEL SECURITY;

-- 2) 创建策略：只允许访问 app.current_depts 里列出的部门
--    注意：current_setting(..., true) 的第二个参数 true 表示变量不存在时返回 NULL 而不报错
DROP POLICY IF EXISTS policy_dept_isolation ON doc_chunks;
CREATE POLICY policy_dept_isolation ON doc_chunks
    USING (
        department_id = ANY (
            string_to_array(
                COALESCE(current_setting('app.current_depts', true), ''),
                ','
            )
        )
    );

-- 3) 授权：应用账号不要给 BYPASSRLS
-- CREATE ROLE rag_app LOGIN PASSWORD 'xxx';
-- GRANT SELECT, INSERT, DELETE ON doc_chunks TO rag_app;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;

-- 4) 管理员例外（如需给某个账号开全量）：
-- CREATE POLICY policy_admin_all ON doc_chunks TO rag_admin USING (true);

-- 5) 验证脚本（用受限账号执行，应返回 0 行）：
--    SET app.current_depts = 'public';
--    SELECT count(*) FROM doc_chunks WHERE department_id = 'tech.rd';  -- 期望 0

-- ⚠ 生产提示：
--    本项目应用层与 API 层已做严格校验，RLS 默认"预留未强制开启"。
--    客户安全审计有要求时，按上面步骤打开即可，业务代码零改动。

-- wall_reader:訊息牆專用唯讀角色。只授權需要的欄位,看不到 email、密碼雜湊、token、頁面內文。
-- 任何欄位不存在(Plane 改版)都會讓整個交易失敗並回滾,同時也是資料表結構檢查。
-- 密碼由 setup_db_role.sh 以 psql 變數 :'pw' 傳入。

BEGIN;

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wall_reader') AS need_create \gset
\if :need_create
  CREATE ROLE wall_reader LOGIN CONNECTION LIMIT 5 PASSWORD :'pw';
\else
  ALTER ROLE wall_reader LOGIN CONNECTION LIMIT 5 PASSWORD :'pw';
\endif

ALTER ROLE wall_reader SET default_transaction_read_only = on;
ALTER ROLE wall_reader SET statement_timeout = '5s';
ALTER ROLE wall_reader SET idle_in_transaction_session_timeout = '10s';

SELECT format('GRANT CONNECT ON DATABASE %I TO wall_reader', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO wall_reader;

GRANT SELECT (id, project_id, issue_id, actor_id, verb, field, old_value, new_value, created_at, deleted_at)
  ON issue_activities TO wall_reader;
GRANT SELECT (id, name, sequence_id, project_id) ON issues TO wall_reader;
GRANT SELECT (id, name, identifier, workspace_id, deleted_at, archived_at, guest_view_all_features)
  ON projects TO wall_reader;
GRANT SELECT (project_id, member_id, role, is_active, deleted_at) ON project_members TO wall_reader;
GRANT SELECT (id, slug, name) ON workspaces TO wall_reader;
GRANT SELECT (id, project_id, name, created_at, created_by_id, updated_at, updated_by_id, deleted_at)
  ON cycles TO wall_reader;
GRANT SELECT (id, project_id, name, created_at, created_by_id, updated_at, updated_by_id, deleted_at)
  ON modules TO wall_reader;
GRANT SELECT (id, name, access, owned_by_id, created_at, created_by_id, deleted_at) ON pages TO wall_reader;
GRANT SELECT (project_id, page_id, deleted_at) ON project_pages TO wall_reader;
GRANT SELECT (page_id, owned_by_id, last_saved_at) ON page_versions TO wall_reader;
GRANT SELECT (id, display_name, first_name, last_name, is_bot) ON users TO wall_reader;

COMMIT;

\echo 'wall_reader 設定完成'

#!/usr/bin/env bash
# 建立或更新 Plane 資料庫的唯讀角色 wall_reader,並把連線資訊寫到 secrets/(權限 600)。
# 會用 docker exec 進 Plane 資料庫容器。不會印出密碼。
# 重複執行是安全的:會換一組新密碼,之後要重啟 plane-wall(docker compose up -d --force-recreate)。
# 資料庫容器名稱不同時:PLANE_DB_CONTAINER=xxx ./setup_db_role.sh
set -euo pipefail

cd "$(dirname "$0")"
DB_CONTAINER=${PLANE_DB_CONTAINER:-plane-app-plane-db-1}

umask 077
mkdir -p secrets
chmod 700 secrets

python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > secrets/db_password.new
docker exec "$DB_CONTAINER" sh -c 'printf %s "$POSTGRES_DB"' > secrets/db_name

# 密碼經由 stdin 傳進容器,不出現在命令列(ps 看不到)
{ printf '\\set pw %s\n' "$(cat secrets/db_password.new)"; cat grants.sql; } |
  docker exec -i "$DB_CONTAINER" sh -c \
    'psql -X -q -v ON_ERROR_STOP=1 -h /var/run/postgresql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

mv secrets/db_password.new secrets/db_password
chmod 600 secrets/db_password secrets/db_name
echo "已寫入 $(pwd)/secrets/(db_password、db_name,權限 600)"

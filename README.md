# plane-wall:Plane 專案訊息牆

> 非官方工具,與 Plane / makeplane 無隸屬關係。
> Unofficial add-on — not affiliated with or endorsed by Plane (makeplane).

給自架 [Plane](https://github.com/makeplane/plane) 用的唯讀「專案動態牆」:一頁看完專案最近 7 天發生的事——
工作項目的狀態、優先度、指派、標籤變動、留言、頁面、Cycle、Module。

Plane 本身的動態散在每張工作項目裡;訊息牆把它們依時間串成一條,適合每天早上掃一眼、或給不常進系統的成員快速掌握進度。
**Plane 不需要任何修改**,訊息牆是旁邊多跑一個小容器。

**English summary:** A read-only activity feed for self-hosted Plane. It shows the last 7 days of
work-item changes, comments, pages, cycles and modules for each project the signed-in user belongs to.
It reuses the user's Plane session (no separate login), reads the database through a column-restricted
read-only role, and requires no changes to Plane. UI text is Traditional Chinese.

## 特點

- **沿用 Plane 登入**:把瀏覽器帶來的 Plane session cookie 轉給 Plane API `/api/users/me/` 確認身分,不另外做帳號。
- **照 Plane 權限顯示**:只列出使用者是有效成員的專案;Guest 要專案開啟「guest_view_all_features」才看得到;私人頁面只有擁有者看得到。
- **最小權限的資料庫存取**:`wall_reader` 角色只對必要欄位有 SELECT 權限(看不到 email、密碼雜湊、token、頁面內文),連線層級唯讀,單次查詢最多 5 秒。
- **輕量**:Python 標準函式庫加 psycopg,容器唯讀、移除所有 capabilities、不保存狀態。
- 頁面自動存檔、描述連續修改等會在 10 分鐘內合併成一筆;支援深色模式與手機版面;時間以台灣時間(UTC+8)顯示。

## 需求

- 用官方 `setup.sh` 以 Docker Compose 自架的 Plane(在 **v1.4.2** 上測試)。
- 一個能把 `https://<你的 Plane 網域>/wall/` 轉到本機連接埠的反向代理(Cloudflare Tunnel、Caddy、Nginx 等皆可)。
  訊息牆必須和 Plane **同網域**,瀏覽器才會把 Plane 的 cookie 帶過來。

## 安裝

```bash
git clone https://github.com/vachelhung/plane-wall.git
cd plane-wall

# 1. 在 Plane 資料庫建立唯讀角色,密碼寫到 secrets/(權限 600)
./setup_db_role.sh

# 2. 啟動
docker compose up -d --build
curl -s http://127.0.0.1:8090/wall/healthz   # 應回 ok
```

3. 在反向代理加一條規則:路徑 `/wall/*` → `http://127.0.0.1:8090`,其他路徑照舊給 Plane。
4. 登入 Plane 後開 `https://<你的 Plane 網域>/wall/`。

### 可調整的環境變數

在 `compose.yaml` 同目錄放 `.env`,或在指令前加上變數:

| 變數 | 預設值 | 說明 |
|---|---|---|
| `PLANE_NETWORK` | `plane-app_default` | Plane 的 Docker 網路名稱 |
| `PLANE_DB_HOST` | `plane-db` | Plane 資料庫的服務名稱 |
| `PLANE_API_URL` | `http://api:8000` | Plane API 位址(容器網路內) |
| `PLANE_DB_CONTAINER` | `plane-app-plane-db-1` | 給 `setup_db_role.sh` 用的資料庫容器名稱 |
| `WALL_PORT` | `8090` | 綁在 127.0.0.1 的連接埠 |
| `WALL_DAYS` / `WALL_LIMIT` | `7` / `100` | 顯示幾天內、最多幾筆 |
| `WALL_UID` / `WALL_GID` | `1000` | 容器執行身分,要與 `secrets/` 擁有者相同 |

## Plane 升級後

1. `docker compose up -d`。Plane 整組重建時 Docker 網路也會重建,訊息牆要重新接上。
2. 檢查 `curl -s http://127.0.0.1:8090/wall/healthz` 是否回 `ok`。
3. 若頁面顯示「暫時無法使用」,看 `docker compose logs`。常見原因是新版改了資料表欄位:
   重新執行 `./setup_db_role.sh`(欄位不存在時會直接報錯並指出是哪個),再依新版調整 `app.py` 的 SQL。
4. 若登入後一直被導回首頁,可能是 Plane 改了 cookie 名稱或 `/api/users/me/` 路徑,調整 `PLANE_SESSION_COOKIE` 或 `app.py`。

## 移除

`docker compose down`,刪掉反向代理的 `/wall` 規則,再到 Plane 資料庫執行:

```sql
DROP OWNED BY wall_reader; DROP ROLE wall_reader;
```

## 致謝

訊息牆的構想來自一位同事分享的其他專案管理軟體使用經驗,特別是訊息牆的用法與好處。

## 授權

[MIT](LICENSE)。Plane 是 makeplane 的產品,以 AGPL-3.0 授權;本專案不包含 Plane 的程式碼,只透過其 API 與資料庫讀取資料。

"""Plane 專案訊息牆:唯讀查 Plane 資料庫,列出專案最近的動態。

- 身分:把使用者瀏覽器帶來的 Plane session cookie 轉送給 Plane API /api/users/me/,取得 user id。
- 權限:只顯示該使用者是有效成員的專案(Guest 需專案開啟 guest_view_all_features)。
- 資料庫:wall_reader 角色,只有必要欄位的 SELECT 權限,連線層級唯讀並限制查詢時間。
"""

import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import psycopg

TW = timezone(timedelta(hours=8))  # 台灣沒有日光節約時間
DAYS = int(os.environ.get("WALL_DAYS", "7"))
LIMIT = int(os.environ.get("WALL_LIMIT", "100"))
API_URL = os.environ.get("PLANE_API_URL", "http://api:8000").rstrip("/")
COOKIE_NAME = os.environ.get("PLANE_SESSION_COOKIE", "session-id")
PREFIX = "/wall"


def read_secret(name):
    with open(f"/run/secrets/{name}") as f:
        return f.read().strip()


DB_KW = dict(
    host=os.environ.get("DB_HOST", "plane-db"),
    port=int(os.environ.get("DB_PORT", "5432")),
    user=os.environ.get("DB_USER", "wall_reader"),
    password=read_secret("db_password"),
    dbname=read_secret("db_name"),
    connect_timeout=3,
    options="-c statement_timeout=5000 -c default_transaction_read_only=on",
    application_name="plane-wall",
)


def query(sql, params=()):
    with psycopg.connect(**DB_KW) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


# ---------- 身分 ----------

_identity_cache = {}
_identity_lock = threading.Lock()


def current_user_id(cookie_header):
    """回傳 Plane user id;未登入回傳 None。結果快取 60 秒。"""
    m = re.search(r"(?:^|;\s*)" + re.escape(COOKIE_NAME) + r"=([^;]+)", cookie_header or "")
    if not m:
        return None
    session = m.group(1)
    key = hashlib.sha256(session.encode()).hexdigest()
    now = time.time()
    with _identity_lock:
        hit = _identity_cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
    req = urllib.request.Request(
        f"{API_URL}/api/users/me/",
        headers={"Cookie": f"{COOKIE_NAME}={session}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            uid = json.load(resp).get("id")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            uid = None
        else:
            raise
    with _identity_lock:
        if len(_identity_cache) > 1000:
            _identity_cache.clear()
        _identity_cache[key] = (uid, now + 60)
    return uid


# ---------- 資料 ----------

PROJECTS_SQL = """
SELECT p.id, p.name, p.identifier, w.slug, w.name AS workspace_name
FROM project_members pm
JOIN projects p ON p.id = pm.project_id
JOIN workspaces w ON w.id = p.workspace_id
WHERE pm.member_id = %(uid)s AND pm.is_active AND pm.deleted_at IS NULL
  AND p.deleted_at IS NULL AND p.archived_at IS NULL
  AND (pm.role >= 15 OR p.guest_view_all_features)
ORDER BY w.name, p.name
"""

EVENTS_SQL = """
WITH ev AS (
  SELECT a.created_at AS ts, a.actor_id, a.verb, a.field, a.old_value, a.new_value,
         a.issue_id AS obj_id, i.name AS obj_name, i.sequence_id AS seq, 'activity' AS src
  FROM issue_activities a
  LEFT JOIN issues i ON i.id = a.issue_id
  WHERE a.project_id = %(pid)s AND a.created_at > %(since)s AND a.deleted_at IS NULL
    AND (a.field IS NULL OR a.field NOT IN ('reaction', 'vote', 'draft'))
  UNION ALL
  SELECT created_at, created_by_id, 'created', NULL, NULL, NULL, id, name, NULL, 'cycle'
  FROM cycles WHERE project_id = %(pid)s AND created_at > %(since)s AND deleted_at IS NULL
  UNION ALL
  SELECT updated_at, updated_by_id, 'updated', NULL, NULL, NULL, id, name, NULL, 'cycle'
  FROM cycles WHERE project_id = %(pid)s AND updated_at > %(since)s AND deleted_at IS NULL
    AND updated_by_id IS NOT NULL AND updated_at - created_at > interval '1 minute'
  UNION ALL
  SELECT created_at, created_by_id, 'created', NULL, NULL, NULL, id, name, NULL, 'module'
  FROM modules WHERE project_id = %(pid)s AND created_at > %(since)s AND deleted_at IS NULL
  UNION ALL
  SELECT updated_at, updated_by_id, 'updated', NULL, NULL, NULL, id, name, NULL, 'module'
  FROM modules WHERE project_id = %(pid)s AND updated_at > %(since)s AND deleted_at IS NULL
    AND updated_by_id IS NOT NULL AND updated_at - created_at > interval '1 minute'
  UNION ALL
  SELECT pg.created_at, pg.created_by_id, 'created', NULL, NULL, NULL, pg.id, pg.name, NULL, 'page'
  FROM project_pages pp JOIN pages pg ON pg.id = pp.page_id
  WHERE pp.project_id = %(pid)s AND pp.deleted_at IS NULL AND pg.deleted_at IS NULL
    AND pg.created_at > %(since)s AND (pg.access = 0 OR pg.owned_by_id = %(uid)s)
  UNION ALL
  SELECT v.last_saved_at, v.owned_by_id, 'updated', NULL, NULL, NULL, pg.id, pg.name, NULL, 'page'
  FROM project_pages pp JOIN pages pg ON pg.id = pp.page_id
  JOIN page_versions v ON v.page_id = pg.id
  WHERE pp.project_id = %(pid)s AND pp.deleted_at IS NULL AND pg.deleted_at IS NULL
    AND v.last_saved_at > %(since)s AND (pg.access = 0 OR pg.owned_by_id = %(uid)s)
)
SELECT ev.*, COALESCE(NULLIF(u.display_name, ''), TRIM(u.first_name || ' ' || u.last_name)) AS actor_name,
       u.is_bot
FROM ev LEFT JOIN users u ON u.id = ev.actor_id
ORDER BY ev.ts DESC
LIMIT %(raw_limit)s
"""

PRIORITY = {"urgent": "緊急", "high": "高", "medium": "中", "low": "低", "none": "無"}
KIND_LABEL = {"issue": "工作項目", "comment": "留言", "cycle": "Cycle", "module": "Module", "page": "頁面"}


def strip_html(s, n=80):
    text = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


def q(s):
    return f"「{s}」" if s else "(空白)"


def classify(e):
    if e["src"] != "activity":
        return e["src"]
    f = e["field"] or ""
    if f == "comment":
        return "comment"
    if f in ("cycles", "modules"):
        return f[:-1]
    return "issue"


def describe(e):
    """把一筆事件轉成中文描述(純文字,之後再跳脫)。"""
    src, verb, f = e["src"], e["verb"], e["field"] or ""
    old, new = e["old_value"], e["new_value"]
    if src in ("cycle", "module"):
        return f"{'建立' if verb == 'created' else '修改'}了 {KIND_LABEL[src]}{q(e['obj_name'])}"
    if src == "page":
        return f"{'建立' if verb == 'created' else '編輯'}了頁面{q(e['obj_name'] or '未命名頁面')}"
    if not f:
        return "建立了工作項目" if verb == "created" else f"{verb} 了工作項目"
    if f == "issue" and verb == "deleted":
        return "刪除了工作項目"
    if f == "state":
        return f"把狀態從{q(old)}改為{q(new)}" if old else f"把狀態改為{q(new)}"
    if f == "priority":
        return f"把優先度從{q(PRIORITY.get(old, old))}改為{q(PRIORITY.get(new, new))}"
    if f == "name":
        return f"把標題改為{q(new)}"
    if f == "description":
        return "更新了描述"
    if f == "assignees":
        return f"指派給 {new}" if new else f"取消指派 {old}"
    if f == "labels":
        return f"加上標籤{q(new)}" if new else f"移除標籤{q(old)}"
    if f in ("target_date", "start_date"):
        label = "到期日" if f == "target_date" else "開始日"
        return f"把{label}改為 {new}" if new else f"清除了{label}"
    if f == "parent":
        return "更新了上層項目"
    if f.startswith("estimate_"):
        return f"把估點改為 {new}" if new else "移除了估點"
    if f == "archived_at":
        return "還原了工作項目" if new == "restore" else "封存了工作項目"
    if f == "comment":
        return {"created": f"留言:{strip_html(new)}", "updated": "編輯了留言", "deleted": "刪除了留言"}.get(verb, "更新了留言")
    if f in ("cycles", "modules"):
        k = "Cycle" if f == "cycles" else "Module"
        if verb == "created":
            return f"加入 {k}{q(new)}"
        if verb == "deleted":
            return f"從 {k}{q(old)}移出"
        return f"把 {k} 從{q(old)}改為{q(new)}"
    if f == "link":
        return {"created": "新增了連結", "updated": "修改了連結", "deleted": "刪除了連結"}.get(verb, "更新了連結")
    if f == "attachment":
        return "上傳了附件" if verb == "created" else "刪除了附件"
    if f == "intake":
        return "透過 Intake 建立"
    return f"更新了關聯({f})"


def collapse(events):
    """同一人對同一物件同一欄位、10 分鐘內的連續修改只留最新一筆(描述、頁面自動存檔會很頻繁)。"""
    out, seen = [], {}
    for e in events:  # 已依時間新到舊
        if e["verb"] == "updated" and (e["field"] in ("description", "comment") or e["src"] == "page"):
            key = (e["src"], e["obj_id"], e["actor_id"], e["field"])
            last = seen.get(key)
            if last and last - e["ts"] < timedelta(minutes=10):
                seen[key] = e["ts"]
                continue
            seen[key] = e["ts"]
        out.append(e)
    return out


def load_events(project, uid):
    since = datetime.now(timezone.utc) - timedelta(days=DAYS)
    rows = query(EVENTS_SQL, {"pid": project["id"], "uid": uid, "since": since, "raw_limit": LIMIT * 5})
    return collapse(rows)[:LIMIT]


# ---------- HTML ----------

CSS = """
:root{--bg:#fff;--fg:#1f2937;--muted:#6b7280;--line:#e5e7eb;--card:#f9fafb;--accent:#3f76ff;
--issue:#3f76ff;--comment:#16a34a;--cycle:#d97706;--module:#9333ea;--page:#0891b2}
@media (prefers-color-scheme:dark){:root{--bg:#191b1f;--fg:#e5e7eb;--muted:#9ca3af;--line:#2d3139;--card:#21242a;--accent:#6b9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,"Segoe UI","Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif}
main{max-width:880px;margin:0 auto;padding:20px 16px 48px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;margin-bottom:12px}
h1{font-size:20px;margin:0}.muted{color:var(--muted);font-size:13px}
.btn{display:inline-block;padding:4px 12px;border:1px solid var(--line);border-radius:6px;color:var(--fg);font-size:13px}
.btn.on{background:var(--accent);border-color:var(--accent);color:#fff}
nav.filters{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 16px}
ul.feed{list-style:none;margin:0;padding:0}
ul.feed li{display:grid;grid-template-columns:92px 1fr;gap:4px 12px;padding:10px 0;border-bottom:1px solid var(--line)}
.time{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:4px;color:#fff;margin-right:6px;vertical-align:1px}
.who{font-weight:600}.obj{word-break:break-word}
.day{font-weight:600;margin:20px 0 4px;color:var(--muted);font-size:13px}
.projects a{display:block;padding:12px 14px;margin-bottom:8px;background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--fg)}
.projects .ident{color:var(--muted);font-size:13px;margin-left:8px}
@media (max-width:520px){ul.feed li{grid-template-columns:1fr}}
"""


def page(title, body):
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body><main>{body}</main></body></html>"""


def obj_link(e, project):
    slug, pid = project["slug"], project["id"]
    src, name = e["src"], e["obj_name"]
    if src == "cycle":
        url = f"/{slug}/projects/{pid}/cycles/{e['obj_id']}"
    elif src == "module":
        url = f"/{slug}/projects/{pid}/modules/{e['obj_id']}"
    elif src == "page":
        url = f"/{slug}/projects/{pid}/pages/{e['obj_id']}"
    elif e["seq"] is not None:
        key = f"{project['identifier']}-{e['seq']}"
        url = f"/{slug}/browse/{key}/"
        name = f"{key} {name or ''}"
    else:
        return ""
    return f'<a href="{html.escape(url)}">{html.escape(name or "(未命名)")}</a>'


def render_project(project, events, kind):
    base = f"{PREFIX}/{quote(project['slug'])}/{quote(project['identifier'])}"
    filters = [("", "全部")] + list(KIND_LABEL.items())
    nav = "".join(
        f'<a class="btn{" on" if k == kind else ""}" href="{base}{"?type=" + k if k else ""}">{v}</a>' for k, v in filters
    )
    items, last_day = [], None
    for e in events:
        k = classify(e)
        if kind and k != kind:
            continue
        ts = e["ts"].astimezone(TW)
        day = ts.strftime("%m/%d")
        if day != last_day:
            items.append(f'</ul><div class="day">{ts.strftime("%Y/%m/%d")}</div><ul class="feed">')
            last_day = day
        who = "Plane 自動" if e["is_bot"] or not e["actor_name"] else e["actor_name"]
        items.append(
            f'<li><span class="time">{ts.strftime("%H:%M")}</span><span>'
            f'<span class="tag" style="background:var(--{k})">{KIND_LABEL[k]}</span>'
            f'<span class="who">{html.escape(who)}</span> {html.escape(describe(e))}'
            f'<br><span class="obj">{obj_link(e, project)}</span></span></li>'
        )
    feed = "".join(items) if items else '<p class="muted">這段期間沒有動態。</p>'
    body = f"""<header><div><h1>📌 {html.escape(project['name'])} 專案動態</h1>
<div class="muted">最近 {DAYS} 天、最新 {LIMIT} 筆・台灣時間・{datetime.now(TW).strftime('%m/%d %H:%M')} 更新</div></div>
<div><a class="btn" href="/{html.escape(project['slug'])}/projects/{project['id']}/issues">← 回到專案</a>
<a class="btn" href="{PREFIX}/">所有專案</a></div></header>
<nav class="filters">{nav}</nav><ul class="feed">{feed}</ul>"""
    return page(f"{project['name']} 專案動態", body)


def render_index(projects):
    links = "".join(
        f'<a href="{PREFIX}/{quote(p["slug"])}/{quote(p["identifier"])}">{html.escape(p["name"])}'
        f'<span class="ident">{html.escape(p["workspace_name"])} / {html.escape(p["identifier"])}</span></a>'
        for p in projects
    ) or '<p class="muted">你目前不是任何專案的成員。</p>'
    return page("專案動態", f'<header><h1>📌 專案動態</h1><a class="btn" href="/">← 回到 Plane</a></header><div class="projects">{links}</div>')


# ---------- HTTP ----------


class Handler(BaseHTTPRequestHandler):
    server_version = "plane-wall"

    def log_message(self, fmt, *args):  # 不記錄網址參數以外的東西,也不記 cookie
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def send(self, code, body="", ctype="text/html; charset=utf-8", headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/")
        try:
            if path == f"{PREFIX}/healthz":
                query("SELECT 1")
                return self.send(200, "ok", "text/plain; charset=utf-8")
            if path != PREFIX and not path.startswith(PREFIX + "/"):
                return self.send(404, page("找不到", "<p>找不到頁面。</p>"))
            uid = current_user_id(self.headers.get("Cookie"))
            if not uid:
                return self.send(302, headers={"Location": "/"})
            projects = query(PROJECTS_SQL, {"uid": uid})
            parts = [p for p in path[len(PREFIX):].split("/") if p]
            if not parts:
                return self.send(200, render_index(projects))
            if len(parts) != 2:
                return self.send(404, page("找不到", "<p>找不到頁面。</p>"))
            slug, ident = parts
            project = next((p for p in projects if p["slug"] == slug and p["identifier"].lower() == ident.lower()), None)
            if not project:  # 不存在或沒有權限,一律同樣回應
                return self.send(404, page("找不到", f'<p>找不到這個專案,或你沒有權限。<a href="{PREFIX}/">看所有專案</a></p>'))
            kind = parse_qs(url.query).get("type", [""])[0]
            kind = kind if kind in KIND_LABEL else ""
            return self.send(200, render_project(project, load_events(project, uid), kind))
        except Exception as exc:  # 不把內部錯誤細節送到瀏覽器
            print(f"ERROR {type(exc).__name__}: {exc}", flush=True)
            return self.send(503, page("暫時無法使用", "<p>訊息牆暫時無法讀取資料,請稍後再試。</p>"))


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

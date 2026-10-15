#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地轻量挡板(Mock)小工具 —— 零依赖, 仅用 Python 标准库

用法:
    python mock_server.py [端口] [https端口]   # 默认端口 8899; 传入 https端口 后同时开启 HTTPS
    python3 mock_server.py [端口] [https端口]  # macOS / Linux
    mock_server.exe [端口] [https端口]        # PyInstaller 打包的免安装 exe, 用法相同
    注意: Windows 上若 python3 指向 Microsoft Store 别名, 会静默退出, 请使用 python 或 py
    HTTPS 需同目录存在 mock_cert.pem / mock_key.pem (自签名证书)

启动后:
    管理界面: http://127.0.0.1:8899/
    在网页上配置 URL / 请求头 / 请求体匹配条件与响应内容, 命中的请求即返回配置的响应
    每条规则可点"测试"模拟一次匹配, 验证是否命中/是否被其他规则抢先(未保存草稿也可测)
    规则自动持久化到同目录 rules.json
"""
import fnmatch
import json
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

if getattr(sys, "frozen", False):            # PyInstaller 打包成 exe 后, 以 exe 所在目录为基准
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_FILE = os.path.join(BASE_DIR, "rules.json")
CERT_FILE = os.path.join(BASE_DIR, "mock_cert.pem")
KEY_FILE = os.path.join(BASE_DIR, "mock_key.pem")

METHODS = {"ANY", "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

SERVER_PORT = 8899

_lock = threading.RLock()
_rules = []                   # 规则列表(自上而下匹配, 命中第一条即返回)
_logs = deque(maxlen=200)     # 最近 200 条请求日志


# ---------------- 规则存取 ----------------
def new_id():
    return "r" + uuid.uuid4().hex[:8]


def normalize_rule(data, rid=None):
    """清洗/补全一条规则, 保证字段合法"""
    method = str(data.get("method") or "ANY").upper()
    if method not in METHODS:
        method = "ANY"
    path = str(data.get("path") or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    try:
        status = int(data.get("status") or 200)
    except (TypeError, ValueError):
        status = 200
    if not (100 <= status <= 599):
        status = 200
    try:
        delay = int(data.get("delay_ms") or 0)
    except (TypeError, ValueError):
        delay = 0
    delay = max(0, min(delay, 60000))
    req_headers = data.get("req_headers") if isinstance(data.get("req_headers"), dict) else {}
    req_query = data.get("req_query") if isinstance(data.get("req_query"), dict) else {}
    resp_headers = data.get("resp_headers") if isinstance(data.get("resp_headers"), dict) else {}
    return {
        "id": rid or data.get("id") or new_id(),
        "enabled": bool(data.get("enabled", True)),
        "name": str(data.get("name") or "").strip(),
        "method": method,
        "path": path,
        "req_headers": {str(k): str(v) for k, v in req_headers.items()},
        "req_query": {str(k): str(v) for k, v in req_query.items()},
        "req_body_contains": str(data.get("req_body_contains") or ""),
        "status": status,
        "resp_headers": {str(k): str(v) for k, v in resp_headers.items()},
        "resp_body": str(data.get("resp_body") or ""),
        "resp_file": str(data.get("resp_file") or "").strip(),
        "delay_ms": delay,
    }


def load_rules():
    global _rules
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _rules = [normalize_rule(r) for r in data if isinstance(r, dict)]
            return
        except Exception as e:
            print("[warn] 规则文件读取失败, 使用空规则:", e, flush=True)
    # 首次启动给一条示例规则
    _rules = [normalize_rule({
        "name": "示例:GET /api/demo",
        "method": "GET",
        "path": "/api/demo",
        "status": 200,
        "resp_headers": {"Content-Type": "application/json; charset=utf-8"},
        "resp_body": '{"code":0,"msg":"挡板已就绪","data":{"hello":"mock"}}',
    })]


def save_rules():
    with _lock:
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(_rules, f, ensure_ascii=False, indent=2)


def add_log(entry):
    with _lock:
        _logs.append(entry)


# ---------------- 匹配逻辑 ----------------
def path_match(pattern, path):
    if pattern == path:
        return True
    if "*" in pattern:
        return fnmatch.fnmatchcase(path, pattern)
    return False


class _CIDict(dict):
    """大小写不敏感的请求头容器, 仅用于规则测试的模拟匹配"""
    def get(self, key, default=None):
        lk = str(key).lower()
        for k, v in self.items():
            if str(k).lower() == lk:
                return v
        return default


def find_rule(method, path, headers, body_text, query=None, rules=None):
    """query: parse_qs 解析后的查询参数 {k: [v, ...]}, 为 None 表示不带查询参数匹配条件"""
    if rules is None:
        with _lock:
            rules = list(_rules)
    for r in rules:
        if not r["enabled"]:
            continue
        if r["method"] != "ANY" and r["method"] != method:
            continue
        if not path_match(r["path"], path):
            continue
        ok = True
        for k, v in (r.get("req_query") or {}).items():
            vals = (query or {}).get(k)
            if not vals or (v != "" and v not in vals):
                ok = False
                break
        for k, v in r["req_headers"].items():
            actual = headers.get(k)  # email.Message 的 get 大小写不敏感
            if actual is None or (v != "" and actual != v):
                ok = False
                break
        if not ok:
            continue
        kw = r["req_body_contains"]
        if kw and kw not in body_text:
            continue
        return r
    return None


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认访问日志, 用界面里的日志代替
        pass

    # ---- 基础工具 ----
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _send(self, status, headers, body, default_ct=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        sent = set()
        for k, v in (headers or {}).items():
            self.send_header(str(k), str(v))
            sent.add(k.lower())
        if "content-type" not in sent:
            if default_ct:
                ct = default_ct
            else:
                s = body.lstrip()[:1]
                ct = "application/json; charset=utf-8" if s in (b"{", b"[") else "text/plain; charset=utf-8"
            self.send_header("Content-Type", ct)
        if "access-control-allow-origin" not in sent:
            self.send_header("Access-Control-Allow-Origin", "*")
        if status not in (204, 304):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body and status not in (204, 304):
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, {"Content-Type": "application/json; charset=utf-8"},
                   json.dumps(obj, ensure_ascii=False))

    # ---- 请求入口 ----
    def do_GET(self):     self._handle()
    def do_POST(self):    self._handle()
    def do_PUT(self):     self._handle()
    def do_DELETE(self):  self._handle()
    def do_PATCH(self):   self._handle()
    def do_HEAD(self):    self._handle()
    def do_OPTIONS(self): self._handle()

    def _handle(self):
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parsed.query
            body = self._read_body()

            if path in ("/", "/_admin"):
                return self._serve_ui()
            if path == "/favicon.ico":
                return self._send(204, {}, b"")
            if path.startswith("/_admin/"):
                return self._admin(path, parse_qs(query), body)

            self._mock(path, query, body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass

    # ---- 管理界面 ----
    def _serve_ui(self):
        self._send(200, {"Content-Type": "text/html; charset=utf-8"},
                   PAGE_HTML.replace("__PORT__", str(SERVER_PORT)))

    def _admin(self, path, query, body):
        # 规则 CRUD
        if path == "/_admin/api/rules":
            if self.command == "GET":
                with _lock:
                    return self._json(200, {"rules": list(_rules)})
            if self.command == "POST":
                data = json.loads(body.decode("utf-8") or "{}")
                rule = normalize_rule(data)
                with _lock:
                    _rules.insert(0, rule)  # 新规则放最前, 优先匹配
                save_rules()
                return self._json(200, {"ok": True, "rule": rule})
            if self.command == "PUT":
                data = json.loads(body.decode("utf-8") or "{}")
                rid = data.get("id")
                with _lock:
                    for i, r in enumerate(_rules):
                        if r["id"] == rid:
                            _rules[i] = normalize_rule(data, rid=rid)
                            break
                    else:
                        return self._json(404, {"error": "规则不存在"})
                save_rules()
                return self._json(200, {"ok": True})
            if self.command == "DELETE":
                rid = (query.get("id") or [""])[0]
                with _lock:
                    _rules[:] = [r for r in _rules if r["id"] != rid]
                save_rules()
                return self._json(200, {"ok": True})
            return self._json(405, {"error": "方法不支持"})

        # 规则上移/下移(调整优先级)
        if path == "/_admin/api/rules/move" and self.command == "POST":
            rid = (query.get("id") or [""])[0]
            direction = (query.get("dir") or ["up"])[0]
            with _lock:
                for i, r in enumerate(_rules):
                    if r["id"] == rid:
                        j = i - 1 if direction == "up" else i + 1
                        if 0 <= j < len(_rules):
                            _rules[i], _rules[j] = _rules[j], _rules[i]
                        break
            save_rules()
            return self._json(200, {"ok": True})

        # 规则测试: 按规则自身的匹配条件构造一条"理想请求"做模拟匹配(不发真实请求)
        if path == "/_admin/api/test" and self.command == "POST":
            data = json.loads(body.decode("utf-8") or "{}")
            rule = normalize_rule(data)
            method = rule["method"] if rule["method"] != "ANY" else "GET"
            sim_path = rule["path"].replace("*", "test")  # 通配符代入一个样例值
            headers = _CIDict(rule["req_headers"])
            body_text = rule["req_body_contains"]
            # 查询参数: 值为空的条件代入一个样例值
            sim_query = {k: (v if v != "" else "test") for k, v in (rule.get("req_query") or {}).items()}
            with _lock:
                snapshot = list(_rules)
            if any(r["id"] == rule["id"] for r in snapshot):
                # 已保存的规则: 在原位置参与匹配, 优先级与实际一致
                snapshot = [rule if r["id"] == rule["id"] else r for r in snapshot]
            else:
                # 未保存的草稿: 按保存时的行为插到最前
                snapshot.insert(0, rule)
            winner = find_rule(method, sim_path, headers, body_text, query=sim_query, rules=snapshot)
            result = ("no_match" if winner is None
                      else "matched" if winner["id"] == rule["id"]
                      else "matched_other")
            sim_qs = "&".join(f"{k}={v}" for k, v in sim_query.items())
            return self._json(200, {
                "result": result,
                "request": {"method": method, "path": sim_path + (("?" + sim_qs) if sim_qs else ""),
                            "headers": dict(headers), "body": body_text},
                "winner": winner,
            })

        # 请求日志
        if path == "/_admin/api/logs":
            if self.command == "GET":
                with _lock:
                    return self._json(200, {"logs": list(reversed(_logs))})
            if self.command == "DELETE":
                with _lock:
                    _logs.clear()
                return self._json(200, {"ok": True})
            return self._json(405, {"error": "方法不支持"})

        return self._json(404, {"error": "未知接口"})

    # ---- Mock 匹配与响应 ----
    def _mock(self, path, query, body):
        body_text = body.decode("utf-8", errors="replace")
        try:
            q = parse_qs(query, keep_blank_values=True) if query else {}
        except Exception:
            q = {}
        rule = find_rule(self.command, path, self.headers, body_text, query=q)
        if rule:
            if rule["delay_ms"]:
                time.sleep(rule["delay_ms"] / 1000.0)
            # 优先返回文件内容(二进制), 适用于图片/文件下载场景
            fp = rule.get("resp_file") or ""
            if fp:
                fp_full = fp if os.path.isabs(fp) else os.path.join(BASE_DIR, fp)
                if os.path.isfile(fp_full):
                    with open(fp_full, "rb") as fh:
                        self._send(rule["status"], rule["resp_headers"], fh.read())
                else:
                    self._json(500, {"error": "响应文件不存在", "resp_file": fp})
            else:
                self._send(rule["status"], rule["resp_headers"], rule["resp_body"].encode("utf-8"))
            status, matched = rule["status"], (rule["name"] or rule["path"])
        else:
            self._json(404, {"error": "未匹配到任何挡板规则", "method": self.command, "path": path})
            status, matched = 404, None
        add_log({
            "time": time.strftime("%m-%d %H:%M:%S"),
            "method": self.command,
            "path": path + ("?" + query if query else ""),
            "req_headers": dict(self.headers.items()),
            "req_body": body_text[:4000],
            "status": status,
            "matched": matched,
        })


class QuietHTTPServer(ThreadingHTTPServer):
    """客户端异常断开时静默处理, 其余错误仍打印堆栈"""
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                            TimeoutError, ssl.SSLError)):
            return  # 浏览器预连接/端口探测等造成的正常网络噪声
        super().handle_error(request, client_address)


# ---------------- 内嵌管理界面 ----------------
PAGE_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>挡板 Mock 工具</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--line:#e5e7eb;--text:#1f2328;--muted:#6b7280;
--brand:#3b82f6;--brand-d:#2563eb;--ok:#10b981;--err:#ef4444}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--text);background:var(--bg)}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 24px;background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{font-size:16px;font-weight:700}
.brand small{font-weight:400;color:var(--muted);margin-left:8px}
main{max-width:1080px;margin:20px auto;padding:0 16px}
.tabs{display:flex;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.tab{border:1px solid var(--line);background:#fff;color:var(--text);padding:6px 16px;border-radius:8px;cursor:pointer;font-size:14px}
.tab:hover{background:#f6f7f9}
.tab.active{background:var(--brand);color:#fff;border-color:var(--brand)}
.tabs .sp{flex:1}
.tabs label{color:var(--muted);font-size:13px;display:flex;align-items:center;gap:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse;table-layout:fixed}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow:hidden;text-overflow:ellipsis}
th{background:#fafbfc;font-size:12px;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:none}
.cell-path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block}
tr.row{cursor:pointer}
tr.row:hover{background:#f8fafc}
code{background:#f3f4f6;padding:1px 6px;border-radius:4px;font-size:12.5px;word-break:break-all}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.badge.ANY{background:#eef2ff;color:#6366f1}.badge.GET{background:#e8f7ef;color:#059669}
.badge.POST{background:#e8f1fd;color:#2563eb}.badge.PUT{background:#fef3c7;color:#b45309}
.badge.DELETE{background:#fee2e2;color:#dc2626}.badge.PATCH{background:#f3e8ff;color:#7c3aed}
.badge.HEAD,.badge.OPTIONS{background:#f3f4f6;color:#4b5563}
.empty{padding:40px;text-align:center;color:var(--muted)}
button{border:none;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13.5px;background:var(--brand);color:#fff}
button:hover{background:var(--brand-d)}
button.ghost{background:#fff;color:var(--text);border:1px solid var(--line)}
button.ghost:hover{background:#f6f7f9}
button.mini{padding:3px 9px;font-size:12.5px}
button.danger{background:#fff;color:var(--err);border:1px solid #fecaca}
button.danger:hover{background:#fef2f2}
.overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:flex-start;justify-content:center;z-index:20;padding:40px 16px;overflow:auto}
.overlay.show{display:flex}
.modal{background:#fff;border-radius:12px;width:680px;max-width:100%;padding:20px 24px;box-shadow:0 20px 50px rgba(0,0,0,.2)}
.modal h3{margin:0 0 4px}
.modal .hint{color:var(--muted);font-size:12.5px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.f{display:flex;flex-direction:column;gap:4px}
.f.wide{grid-column:1/-1}
.f label{font-size:12.5px;color:var(--muted);font-weight:600}
.f input,.f select,.f textarea{border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:13.5px;font-family:inherit;width:100%}
.f textarea{font-family:ui-monospace,Consolas,monospace;resize:vertical}
.foot{display:flex;justify-content:flex-end;gap:10px;margin-top:18px}
.err{color:var(--err);font-size:12.5px;min-height:18px;margin-top:8px}
.detail td{background:#fafbfc}
pre{margin:4px 0 10px;padding:8px 10px;background:#f6f8fa;border-radius:6px;font-size:12px;overflow:auto;max-height:220px;white-space:pre-wrap;word-break:break-all}
.muted{color:var(--muted)}
.tag-ok{color:var(--ok);font-weight:600}
.tag-err{color:var(--err);font-weight:600}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#111827;color:#fff;padding:8px 18px;border-radius:8px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;z-index:30}
#toast.show{opacity:1}
#testOverlay{z-index:25}
</style>
</head>
<body>
<header>
  <div class="brand">🛡️ 本地挡板 Mock 工具<small>轻量 · 零依赖 · 端口 __PORT__</small></div>
  <button id="btnAdd">＋ 新增规则</button>
</header>
<main>
  <div class="tabs">
    <button class="tab active" data-tab="rules">接口规则</button>
    <button class="tab" data-tab="logs">请求日志</button>
    <div class="sp"></div>
    <label id="autoWrap" hidden><input type="checkbox" id="autoRefresh" checked> 自动刷新</label>
    <button class="ghost" id="btnClearLogs" hidden>清空日志</button>
  </div>

  <section id="tab-rules">
    <div class="card">
      <table>
        <thead><tr><th style="width:52px">启用</th><th style="width:140px">名称</th><th style="width:72px">方法</th><th>路径</th><th style="width:150px">匹配条件</th><th style="width:100px">响应</th><th style="width:200px">操作</th></tr></thead>
        <tbody id="rulesBody"></tbody>
      </table>
      <div class="empty" id="rulesEmpty" hidden>暂无规则,点击右上角"新增规则"开始配置</div>
    </div>
    <p class="muted" style="font-size:12.5px">规则自上而下匹配,命中第一条即返回;路径支持 * 通配(如 /api/*),匹配时忽略查询参数;请求头/请求体条件留空表示不校验。点「测试」可模拟一次请求,验证规则是否会命中。</p>
  </section>

  <section id="tab-logs" hidden>
    <div class="card">
      <table>
        <thead><tr><th style="width:96px">时间</th><th>方法</th><th>路径</th><th>匹配规则</th><th>状态码</th></tr></thead>
        <tbody id="logsBody"></tbody>
      </table>
      <div class="empty" id="logsEmpty" hidden>暂无请求日志,向本服务发送请求试试</div>
    </div>
    <p class="muted" style="font-size:12.5px">点击行可展开查看请求头与请求体;最多保留最近 200 条。</p>
  </section>
</main>

<div class="overlay" id="overlay">
  <div class="modal">
    <h3 id="modalTitle">新增规则</h3>
    <div class="hint">配置匹配条件与响应内容,条件留空表示不校验;保存前可点「测试」模拟一次请求验证是否命中。</div>
    <div class="grid">
      <div class="f"><label>名称(可选)</label><input id="f_name" placeholder="例如:查询用户列表"></div>
      <div class="f"><label>请求方法</label><select id="f_method"></select></div>
      <div class="f wide"><label>URL 路径 *</label><input id="f_path" placeholder="/api/users 或 /api/users/* (支持 * 通配,不含查询参数)"></div>
      <div class="f wide"><label>请求头匹配(JSON,可选)</label><textarea id="f_reqHeaders" rows="2" placeholder='例如 {"X-Token":"abc123"} ,请求头需完全一致才命中'></textarea></div>
      <div class="f wide"><label>查询参数匹配(JSON,可选)</label><textarea id="f_reqQuery" rows="2" placeholder='例如 {"fileId":"abc123"} ;值为空串表示只要求该参数存在'></textarea></div>
      <div class="f wide"><label>请求体包含(文本,可选)</label><textarea id="f_reqBody" rows="2" placeholder="请求体中包含该文本才命中,留空不校验"></textarea></div>
      <div class="f"><label>响应状态码</label><input id="f_status" type="number" value="200"></div>
      <div class="f"><label>延迟(毫秒)</label><input id="f_delay" type="number" value="0"></div>
      <div class="f wide"><label>响应头(JSON,可选)</label><textarea id="f_respHeaders" rows="2" placeholder='例如 {"Content-Type":"application/json"}'></textarea></div>
      <div class="f wide"><label>响应文件路径(可选, 优先于响应体)</label><input id="f_respFile" placeholder="填本地文件路径则返回文件原始二进制内容, 适用于图片/文件下载; 留空则用响应体"></div>
      <div class="f wide"><label>响应体</label><textarea id="f_respBody" rows="7" placeholder='{"code":0,"msg":"ok","data":{}}'></textarea></div>
    </div>
    <div class="err" id="formErr"></div>
    <div class="foot">
      <button class="ghost" id="btnCancel">取消</button>
      <button class="ghost" id="btnTest">测试</button>
      <button id="btnSave">保存</button>
    </div>
  </div>
</div>

<div class="overlay" id="testOverlay">
  <div class="modal" style="width:620px">
    <h3>规则测试</h3>
    <div class="hint">按该规则自身的匹配条件构造一条请求做模拟匹配:不发送真实请求、不产生日志、不等待延迟。</div>
    <div id="testResult"></div>
    <div class="foot"><button class="ghost" id="btnCloseTest">关闭</button></div>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let rules = [], logs = [], editingId = null, editingRule = null;

const METHODS = ['ANY','GET','POST','PUT','DELETE','PATCH','HEAD','OPTIONS'];
$('f_method').innerHTML = METHODS.map(m => `<option>${m}</option>`).join('');

function esc(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function toast(msg){
  const t = $('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1500);
}

// ---------- 规则 ----------
async function loadRules(){
  const r = await fetch('/_admin/api/rules').then(x => x.json());
  rules = r.rules || [];
  renderRules();
}
function condText(r){
  const parts = [];
  const h = Object.keys(r.req_headers || {}).length;
  if (h) parts.push('请求头×' + h);
  const q = Object.entries(r.req_query || {});
  if (q.length) parts.push('参数 ' + q.map(([k, v]) => k + '=' + (v || '*')).join('&'));
  if (r.req_body_contains) parts.push('体含 "' + r.req_body_contains + '"');
  return parts.length ? parts.join(' · ') : '<span class="muted">无</span>';
}
function renderRules(){
  const tb = $('rulesBody');
  tb.innerHTML = '';
  $('rulesEmpty').hidden = rules.length > 0;
  rules.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="checkbox" ${r.enabled ? 'checked' : ''} onchange="toggleRule('${r.id}', this.checked)"></td>
      <td>${esc(r.name) || '<span class="muted">-</span>'}</td>
      <td><span class="badge ${r.method}">${r.method}</span></td>
      <td><code class="cell-path" title="${esc(r.path)}">${esc(r.path)}</code></td>
      <td style="font-size:12.5px">${condText(r)}</td>
      <td class="muted" style="font-size:12.5px">HTTP ${r.status}${r.delay_ms ? ' · 延迟' + r.delay_ms + 'ms' : ''}</td>
      <td>
        <button class="ghost mini" onclick="testRule('${r.id}')">测试</button>
        <button class="ghost mini" onclick="editRule('${r.id}')">编辑</button>
        <button class="danger mini" onclick="delRule('${r.id}')">删除</button>
        <button class="ghost mini" onclick="moveRule('${r.id}',-1)" title="上移(提高优先级)">↑</button>
        <button class="ghost mini" onclick="moveRule('${r.id}',1)" title="下移(降低优先级)">↓</button>
      </td>`;
    tb.appendChild(tr);
  });
}
function openModal(rule){
  editingId = rule ? rule.id : null;
  editingRule = rule || null;
  $('modalTitle').textContent = rule ? '编辑规则' : '新增规则';
  $('f_name').value = rule ? rule.name : '';
  $('f_method').value = rule ? rule.method : 'ANY';
  $('f_path').value = rule ? rule.path : '/';
  $('f_reqHeaders').value = rule && Object.keys(rule.req_headers || {}).length ? JSON.stringify(rule.req_headers, null, 2) : '';
  $('f_reqQuery').value = rule && Object.keys(rule.req_query || {}).length ? JSON.stringify(rule.req_query, null, 2) : '';
  $('f_reqBody').value = rule ? rule.req_body_contains : '';
  $('f_status').value = rule ? rule.status : 200;
  $('f_delay').value = rule ? rule.delay_ms : 0;
  $('f_respHeaders').value = rule && Object.keys(rule.resp_headers || {}).length ? JSON.stringify(rule.resp_headers, null, 2) : '';
  $('f_respFile').value = rule ? (rule.resp_file || '') : '';
  $('f_respBody').value = rule ? rule.resp_body : '';
  $('formErr').textContent = '';
  $('overlay').classList.add('show');
}
function editRule(id){ openModal(rules.find(r => r.id === id)); }
function parseJsonField(v, label){
  v = v.trim();
  if (!v) return {};
  try {
    const o = JSON.parse(v);
    if (typeof o !== 'object' || Array.isArray(o) || o === null) throw 0;
    return o;
  } catch (e) { throw new Error(label + ' 不是合法的 JSON 对象'); }
}
function collectForm(){
  const path = $('f_path').value.trim();
  if (!path) throw new Error('URL 路径不能为空');
  const status = parseInt($('f_status').value, 10);
  if (!(status >= 100 && status <= 599)) throw new Error('状态码需在 100-599 之间');
  return {
    id: editingId || undefined,
    enabled: editingRule ? editingRule.enabled : true,
    name: $('f_name').value.trim(),
    method: $('f_method').value,
    path: path,
    req_headers: parseJsonField($('f_reqHeaders').value, '请求头匹配'),
    req_query: parseJsonField($('f_reqQuery').value, '查询参数匹配'),
    req_body_contains: $('f_reqBody').value,
    status: status,
    delay_ms: parseInt($('f_delay').value, 10) || 0,
    resp_headers: parseJsonField($('f_respHeaders').value, '响应头'),
    resp_file: $('f_respFile').value.trim(),
    resp_body: $('f_respBody').value
  };
}
async function saveRule(){
  try {
    const rule = collectForm();
    const res = await fetch('/_admin/api/rules', {
      method: editingId ? 'PUT' : 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(rule)
    });
    if (!res.ok) throw new Error('保存失败: ' + (await res.text()));
    $('overlay').classList.remove('show');
    toast(editingId ? '已更新' : '已新增');
    loadRules();
  } catch (e) { $('formErr').textContent = e.message; }
}
async function toggleRule(id, enabled){
  const r = rules.find(x => x.id === id);
  if (!r) return;
  r.enabled = enabled;
  await fetch('/_admin/api/rules', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(r)
  });
  toast(enabled ? '已启用' : '已停用');
}
async function delRule(id){
  if (!confirm('确定删除该规则?')) return;
  await fetch('/_admin/api/rules?id=' + encodeURIComponent(id), {method: 'DELETE'});
  toast('已删除');
  loadRules();
}
async function moveRule(id, dir){
  await fetch('/_admin/api/rules/move?id=' + encodeURIComponent(id) + '&dir=' + (dir < 0 ? 'up' : 'down'), {method: 'POST'});
  loadRules();
}

// ---------- 规则测试 ----------
async function runTest(rule){
  try {
    const res = await fetch('/_admin/api/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(rule)
    }).then(x => x.json());
    if (!res.result) { toast('测试失败: ' + (res.error || '未知错误')); return; }
    renderTest(res, rule);
  } catch (e) { toast('测试请求失败: ' + e.message); }
}
function testRule(id){ const r = rules.find(x => x.id === id); if (r) runTest(r); }
async function testDraft(){
  let rule;
  try { rule = collectForm(); }
  catch (e) { $('formErr').textContent = e.message; return; }
  $('formErr').textContent = '';
  runTest(rule);
}
function fmtReq(req){
  let s = req.method + ' ' + req.path + '\n';
  for (const [k, v] of Object.entries(req.headers || {})) s += k + ': ' + v + '\n';
  if (req.body) s += '\n' + req.body;
  return s;
}
function fmtResp(r){
  let s = 'HTTP ' + r.status + (r.delay_ms ? '  (延迟 ' + r.delay_ms + 'ms 返回)' : '') + '\n';
  for (const [k, v] of Object.entries(r.resp_headers || {})) s += k + ': ' + v + '\n';
  if (r.resp_file) {
    s += '\n[返回文件内容] ' + r.resp_file + '\n(二进制数据不在此预览)';
  } else {
    s += '\n' + (r.resp_body || '(空)');
  }
  return s;
}
function renderTest(res, self){
  const w = res.winner;
  let html = '<div><b>模拟请求</b>(按规则自身的匹配条件构造)</div><pre>' + esc(fmtReq(res.request)) + '</pre>';
  if (res.result === 'matched'){
    html += '<div class="tag-ok">✅ 规则生效:该请求将命中本规则</div>'
         + '<div style="margin-top:10px"><b>将返回的响应</b></div><pre>' + esc(fmtResp(w)) + '</pre>';
  } else if (res.result === 'matched_other'){
    const who = '「' + esc(w.name || w.path) + '」(' + esc(w.method + ' ' + w.path) + ')';
    const why = (self && !self.enabled)
      ? '本规则处于停用状态,该请求命中了上方的规则 ' + who
      : '被更靠上的规则 ' + who + ' 抢先命中;可将本规则上移,或收窄上方规则的条件';
    html += '<div class="tag-err">⚠️ 未命中本规则:' + why + '</div>'
         + '<div style="margin-top:10px"><b>该请求实际将返回</b></div><pre>' + esc(fmtResp(w)) + '</pre>';
  } else {
    const why = (self && !self.enabled)
      ? '本规则当前处于停用状态,勾选"启用"后重试'
      : '没有任何已启用的规则能匹配该请求,请检查路径/方法/条件';
    html += '<div class="tag-err">❌ 未命中:' + why + '</div>';
  }
  $('testResult').innerHTML = html;
  $('testOverlay').classList.add('show');
}

// ---------- 日志 ----------
async function loadLogs(){
  const r = await fetch('/_admin/api/logs').then(x => x.json());
  logs = r.logs || [];
  renderLogs();
}
function renderLogs(){
  const tb = $('logsBody');
  tb.innerHTML = '';
  $('logsEmpty').hidden = logs.length > 0;
  logs.forEach((l, i) => {
    const tr = document.createElement('tr');
    tr.className = 'row';
    tr.innerHTML = `
      <td class="muted" style="font-size:12.5px">${esc(l.time)}</td>
      <td><span class="badge ${esc(l.method)}">${esc(l.method)}</span></td>
      <td><code>${esc(l.path)}</code></td>
      <td>${l.matched ? esc(l.matched) : '<span class="tag-err">未匹配</span>'}</td>
      <td class="${l.status < 400 ? 'tag-ok' : 'tag-err'}">${l.status}</td>`;
    tr.onclick = () => toggleDetail(i, tr);
    tb.appendChild(tr);
  });
}
function toggleDetail(i, tr){
  if (tr.nextSibling && tr.nextSibling.className === 'detail'){
    tr.nextSibling.remove();
    return;
  }
  document.querySelectorAll('tr.detail').forEach(x => x.remove());
  const l = logs[i];
  const d = document.createElement('tr');
  d.className = 'detail';
  d.innerHTML = `<td colspan="5">
    <div><b>请求头</b></div>
    <pre>${esc(JSON.stringify(l.req_headers, null, 2))}</pre>
    <div><b>请求体</b></div>
    <pre>${esc(l.req_body) || '<span class="muted">(空)</span>'}</pre>
  </td>`;
  tr.after(d);
}

// ---------- 页面行为 ----------
document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === b));
  const tab = b.dataset.tab;
  $('tab-rules').hidden = tab !== 'rules';
  $('tab-logs').hidden = tab !== 'logs';
  $('autoWrap').hidden = tab !== 'logs';
  $('btnClearLogs').hidden = tab !== 'logs';
  if (tab === 'logs') loadLogs();
});
$('btnClearLogs').onclick = async () => { await fetch('/_admin/api/logs', {method: 'DELETE'}); loadLogs(); };
setInterval(() => { if ($('tab-logs').hidden === false && $('autoRefresh').checked) loadLogs(); }, 2000);
$('btnAdd').onclick = () => openModal(null);
$('btnCancel').onclick = () => $('overlay').classList.remove('show');
$('overlay').onclick = e => { if (e.target === e.currentTarget) $('overlay').classList.remove('show'); };
$('btnSave').onclick = saveRule;
$('btnTest').onclick = testDraft;
$('btnCloseTest').onclick = () => $('testOverlay').classList.remove('show');

loadRules();
loadLogs();
</script>
</body>
</html>
'''


# ---------------- 启动 ----------------
def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _pause_on_windows():
    """Windows 下双击运行出错时窗口会一闪而过, 停住让用户看到错误信息"""
    if os.name == "nt":
        try:
            input("\n按回车键退出...")
        except Exception:
            pass


def main():
    global SERVER_PORT
    # 兼容 Windows 等环境下控制台编码, 避免中文输出报错
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    except ValueError:
        port = 8899
    try:
        https_port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    except ValueError:
        https_port = 0
    SERVER_PORT = port

    load_rules()
    try:
        httpd = QuietHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        print(f"[错误] 端口 {port} 启动失败: {e}", flush=True)
        hint = "mock_server.exe" if getattr(sys, "frozen", False) else "python mock_server.py"
        print(f"端口可能已被占用, 可换一个端口重试: {hint} 9090", flush=True)
        _pause_on_windows()
        return
    except Exception:
        import traceback
        print("[错误] 服务启动异常:", flush=True)
        traceback.print_exc()
        _pause_on_windows()
        return
    httpd.daemon_threads = True

    # 可选 HTTPS: 传入第二个端口参数且证书文件存在时开启, 与 HTTP 共用同一套规则
    https_ok = False
    if https_port:
        if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
            try:
                httpd_tls = QuietHTTPServer(("0.0.0.0", https_port), Handler)
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(CERT_FILE, KEY_FILE)
                httpd_tls.socket = ctx.wrap_socket(httpd_tls.socket, server_side=True)
                httpd_tls.daemon_threads = True
                threading.Thread(target=httpd_tls.serve_forever, daemon=True).start()
                https_ok = True
            except OSError as e:
                print(f"[错误] HTTPS 端口 {https_port} 启动失败: {e}", flush=True)
        else:
            print(f"[提示] 缺少证书文件 {CERT_FILE} / {KEY_FILE}, 本次未开启 HTTPS", flush=True)

    print("=" * 52, flush=True)
    print(f"  挡板 Mock 服务已启动 (端口 {port})", flush=True)
    print(f"  管理界面:  http://127.0.0.1:{port}/", flush=True)
    print(f"  局域网访问: http://{lan_ip()}:{port}/", flush=True)
    if https_ok:
        print(f"  HTTPS 访问: https://127.0.0.1:{https_port}/ (自签名证书, 浏览器首次访问需在警告页选择继续前往)", flush=True)
    print(f"  规则文件:  {RULES_FILE}", flush=True)
    print("  按 Ctrl+C 停止", flush=True)
    print("=" * 52, flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止", flush=True)


if __name__ == "__main__":
    main()

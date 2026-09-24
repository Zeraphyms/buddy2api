"""Small, persistent management layer for workbuddy2api. Single-process deployment."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from core import converter
from .browser_login import BrowserLogin
from .pool import AccountPool, PoolMiddleware
from .metrics import RequestMetrics, MetricsMiddleware
from .rates import ModelRates
from .modelmeta import ModelMeta
from .usage import UsageStats

COOKIE = "workbuddy_admin"
MAX_BODY = 1024 * 1024
ASSETS = Path(__file__).parent / "static"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def clean_name(value, fallback):
    if value is None:
        return fallback
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
        raise HTTPException(400, "名称需为 1–60 个字符")
    return value.strip()


class Store:
    def __init__(self, root, auth_dir, initial_key, admin_key):
        if len(admin_key) < 20:
            raise RuntimeError("ADMIN_KEY must contain at least 20 characters")
        self.root, self.auth_dir = Path(root), Path(auth_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.admin_digest = digest(admin_key)
        # 初始客户端 Key（来自 .env）保留在内存中，便于管理员复制到客户端。
        # 其它密钥只存摘要，无法还原。
        self.initial_key = initial_key or ""
        self.sessions, self.attempts = {}, {}
        self.test_lock = asyncio.Lock()
        self.test_keys = set()
        self.managers = {}
        self.started = time.time()
        self.events = deque(maxlen=40)
        self.path = self.root / "state.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"accounts": {}, "active": None, "keys": {}}
            for p in sorted(self.auth_dir.glob("*.info")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    self.validate_credential(doc)
                except (ValueError, OSError, HTTPException):
                    continue
                aid = secrets.token_hex(8)
                name = str(doc.get("account", {}).get("nickname") or "已迁移账号")[:60]
                self.data["accounts"][aid] = {"name": name, "file": p.name, "enabled": not bool(doc.get("disabled")), "created": int(time.time())}
                if self.data["active"] is None and not doc.get("disabled"):
                    self.data["active"] = aid
            if initial_key:
                self.add_key("原有 API Key", initial_key)
            self.save()
        self.sync_active()

    @staticmethod
    def validate_credential(doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict) or not isinstance(doc.get("account"), dict):
            raise HTTPException(400, "需要桌面端 .info／JSON 登录文件，包含 auth 和 account 对象")
        for key in ("accessToken", "refreshToken"):
            if not isinstance(doc["auth"].get(key), str) or not doc["auth"][key].strip():
                raise HTTPException(400, "凭据缺少 accessToken 或 refreshToken，请重新导出桌面端登录文件")
            if len(doc["auth"][key]) > 65536:
                raise HTTPException(400, "凭据字段过长")
        if not isinstance(doc["account"].get("uid"), str) or not doc["account"]["uid"]:
            raise HTTPException(400, "凭据缺少 account.uid")
        if not isinstance(doc["auth"].get("expiresAt"), (int, float)) or isinstance(doc["auth"].get("expiresAt"), bool):
            raise HTTPException(400, "凭据需要有效的 expiresAt 时间戳")
        for key in ("domain",):
            value = doc["auth"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "凭据包含无效字段")
        for key in ("uid", "enterpriseId"):
            value = doc["account"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "账号字段格式不正确")

    def save(self):
        write_json(self.path, self.data)

    def save_browser_account(self, doc, name):
        self.validate_credential(doc)
        with self.lock:
            identity = (doc["account"]["uid"], doc["account"].get("enterpriseId", ""))
            for aid, item in self.data["accounts"].items():
                try:
                    old = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if (old.get("account", {}).get("uid"), old.get("account", {}).get("enterpriseId", "")) == identity:
                    current = self.manager_for(aid, item)
                    if current:
                        with current._lock:
                            write_json(self.file_for(item), doc)
                            current._cached = None
                    else:
                        write_json(self.file_for(item), doc)
                    if name:
                        item["name"] = name
                    self.data.get("account_status", {}).pop(aid, None)
                    self.save()
                    self.sync_active()
                    return {"account_id": aid, "updated": True}
            if len(self.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            write_json(self.auth_dir / (aid + ".info"), doc)
            self.data["accounts"][aid] = {"file": aid + ".info", "name": name or str(doc["account"].get("nickname") or "浏览器授权账号")[:60], "created": int(time.time()), "enabled": True}
            if self.data["active"] is None:
                self.data["active"] = aid
            self.save()
            self.sync_active()
            return {"account_id": aid, "updated": False}

    def file_for(self, item):
        path = self.auth_dir / item["file"]
        if path.parent.resolve() != self.auth_dir.resolve() or path.is_symlink():
            raise HTTPException(400, "凭据路径不合法")
        return path

    def sync_active(self):
        aid = self.data.get("active")
        item = self.data["accounts"].get(aid)
        converter.CONFIG["cred"] = self.manager_for(aid, item) if item and item["enabled"] and self.file_for(item).exists() else None

    def manager_for(self, aid, item):
        with self.lock:
            if aid not in self.managers:
                self.managers[aid] = converter.CredentialManager(self.file_for(item))
            return self.managers[aid]

    def account_rows(self):
        result = []
        for aid, item in self.data["accounts"].items():
            row = {"id": aid, "name": item["name"], "enabled": item["enabled"], "active": aid == self.data["active"], "created": item["created"]}
            try:
                doc = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                expiry = doc["auth"].get("expiresAt", 0)
                row.update({"nickname": str(doc["account"].get("nickname") or ""), "uid": str(doc["account"].get("uid") or ""), "expires_at": expiry, "expired": expiry < time.time() * 1000, "refresh_available": bool(doc["auth"].get("refreshToken")), "status": "ready"})
            except (ValueError, OSError, KeyError):
                row.update({"status": "invalid", "expired": True, "expires_at": 0, "refresh_available": False})
            result.append(row)
        return result

    def add_key(self, name, value=None):
        value = value or "sk-wb-" + secrets.token_urlsafe(32)
        kid = secrets.token_hex(8)
        self.data["keys"][kid] = {"name": name, "hash": digest(value), "hint": value[:5] + "…" + value[-4:], "created": int(time.time())}
        return kid, value

    def key_rows(self):
        """密钥列表。

        上游设计：除创建时的一次性响应外，完整密钥不可再查看（服务端只存 SHA-256）。
        本部署保留该默认行为；若确实需要从后台复制启动时的初始 Key，可显式设置
        环境变量 ADMIN_REVEAL_INITIAL_KEY=1 打开（仅对 .env 里那一个 Key 生效）。
        """
        reveal = os.environ.get("ADMIN_REVEAL_INITIAL_KEY", "") == "1"
        initial_digest = digest(self.initial_key) if (reveal and self.initial_key) else None
        rows = []
        for kid, item in self.data["keys"].items():
            row = {"id": kid, **{k: v for k, v in item.items() if k != "hash"}}
            row["recoverable"] = bool(initial_digest) and hmac.compare_digest(item.get("hash", ""), initial_digest)
            if row["recoverable"]:
                row["key"] = self.initial_key
            rows.append(row)
        return rows

    def check_api(self, authorization, x_api_key):
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else x_api_key or ""
        with self.lock:
            hashed = digest(token)
            allowed = hashed in self.test_keys or any(hmac.compare_digest(hashed, item["hash"]) for item in self.data["keys"].values())
        if not allowed:
            raise HTTPException(401, "invalid api key")

    def require_admin(self, req):
        sid = req.cookies.get(COOKIE, "")
        with self.lock:
            record = self.sessions.get(digest(sid))
            if not record or record["expires"] < time.time():
                raise HTTPException(401, "请先登录管理后台")
        if req.method not in ("GET", "HEAD"):
            if not hmac.compare_digest(req.headers.get("X-CSRF-Token", ""), record["csrf"]):
                raise HTTPException(403, "页面已失效，请重新登录")
        return record


class AdminMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                response = JSONResponse({"detail": "文件或请求超过 1 MB"}, 413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)
        await self.app(scope, replay, secure_send)


def create_app(root=None, auth_dir=None, initial_key=None, admin_key=None, secure_cookie=True):
    store = Store(root or os.environ.get("MANAGEMENT_DATA_DIR", "/data/management"), auth_dir or os.environ.get("CODEBUDDY_AUTH_DIR", "/data/auth"), initial_key if initial_key is not None else os.environ.get("CODEBUDDY2OPENAI_KEY", ""), admin_key or os.environ.get("ADMIN_KEY", ""))
    converter._check_auth = store.check_api
    converter.CONFIG.update({"desensitize": True, "no_compact": False, "log_path": None})
    browser_login = BrowserLogin(store.save_browser_account)
    pool = AccountPool(store)

    def _current_account():
        """本次请求实际命中的账号；后台测试等绕过账号池时返回 None。"""
        try:
            from .pool import REQUEST_ACCOUNT
            return REQUEST_ACCOUNT.get()
        except Exception:
            return None

    def _usage_sink(model, usage):
        # 归属到本次请求实际使用的账号区域（cn / intl）。
        # 后台测试走 MetricsMiddleware(converter.app)，绕过账号池中间件，
        # 此时 REQUEST_REGION 未设置，回退到当前生效凭据的区域。
        try:
            from .pool import REQUEST_REGION
            region = REQUEST_REGION.get()
        except Exception:
            region = None
        if not region:
            region = converter.current_region()
        rates.record(region, model, usage)
        # 有真实扣费说明该账号确实能用这个模型（可能在目录外）
        aid = _current_account()
        if aid:
            rates.learn(aid, model, True)

    def _success_sink(model):
        """上游成功返回 200 即证明该账号能用该模型，与是否带 usage 无关。

        此前只有 `_usage_sink` 会 learn，而上游某些模型（如国际版
        deepseek-v4.1-flash 免费档、/v1/responses 与 /v1/messages 适配层）
        的响应可能不带可用的 usage 块，导致「能调用但永远不进账号的可用列表」。
        这里补一条与 usage 解耦的判定路径。
        """
        aid = _current_account()
        if aid and model:
            rates.learn(aid, model, True)

    def _model_error_sink(model, code):
        """上游 11102 = 该账号没有此模型；据此学习账号的模型可用性。"""
        if not model or code != 11102:
            return
        try:
            from .pool import REQUEST_ACCOUNT
            aid = REQUEST_ACCOUNT.get()
        except Exception:
            aid = None
        if aid:
            rates.learn(aid, model, False)

    rates = ModelRates(store)
    model_meta = ModelMeta(store.path.parent / "model_meta.json")
    metrics = RequestMetrics(usage_sink=_usage_sink, model_error_sink=_model_error_sink,
                             success_sink=_success_sink)

    usage_stats = UsageStats(store)

    def _request_sink(info):
        """把每次请求的用量归集到账号 / 模型 / 域三个维度。"""
        aid = _current_account()
        try:
            from .pool import REQUEST_REGION
            region = REQUEST_REGION.get()
        except Exception:
            region = None
        if not region:
            region = converter.current_region()
        usage_stats.record(
            aid, region, info.get("model"), info.get("usage"),
            failed=not info.get("ok", True), duration_ms=info.get("duration_ms"),
        )

    metrics.request_sink = _request_sink

    def cred_for(region):
        """按区域找一个可用账号的 CredentialManager，供拉取上游模型目录。"""
        with store.lock:
            for aid, item in store.data["accounts"].items():
                if not item.get("enabled"):
                    continue
                try:
                    manager = store.manager_for(aid, item)
                except HTTPException:
                    continue
                if manager is None:
                    continue
                try:
                    domain = (manager.get_headers() or {}).get("X-Domain", "")
                except Exception:
                    continue
                if (region == "intl") == converter.is_intl_domain(domain):
                    return manager
        return None

    def _model_provider(region):
        """返回当前账号实际可用的模型，供 /v1/models 与测试下拉使用。

        原则：**返回哪个号、就返回那个号有的模型**。
        - 手动模式：只返回当前 active 账号的模型
        - 轮转模式：返回所有参与轮转账号的并集（因为无法预知某请求走哪个号）

        每个账号的模型 = 其所在区域的上游目录 + 实测可用（该账号的
        deepseek 可能不在目录里）− 实测返回 11102 的模型。
        """
        with store.lock:
            routing = (store.data.get("pool") or {}).get("routing")
            active = store.data.get("active")
            pairs = []
            for aid, item in store.data["accounts"].items():
                if not item.get("enabled"):
                    continue
                try:
                    manager = store.manager_for(aid, item)
                    domain = (manager.get_headers() or {}).get("X-Domain", "") if manager else ""
                except Exception:
                    continue
                pairs.append((aid, "intl" if converter.is_intl_domain(domain) else "cn"))
        if routing == "manual":
            pairs = [p for p in pairs if p[0] == active]
        if not pairs:
            return []
        return rates.models_for_accounts(pairs)

    converter.MODEL_PROVIDER = _model_provider

    def _model_meta():
        """model_id -> 元数据，供 /v1/models 透出上下文长度与思考档位。

        与 _model_provider 用同一份快照口径：按 region:model 取值，
        同 id 出现在多个区域时取第一个可用的。
        """
        try:
            rows = rates.snapshot(fallback=model_meta.lookup).get("models") or []
        except Exception:
            return {}
        table = {}
        for row in rows:
            mid = row.get("id")
            if not mid or mid in table:
                continue
            table[mid] = {
                "context_length": row.get("context_length"),
                "context_default_length": row.get("context_default_length"),
                "context_lengths": row.get("context_lengths") or [],
                "max_output_tokens": row.get("max_output_tokens"),
                "supports_reasoning": row.get("supports_reasoning"),
                "only_reasoning": row.get("only_reasoning"),
                "can_disable_thinking": row.get("can_disable_thinking"),
                "default_reasoning_effort": row.get("default_effort"),
                "supports_images": row.get("supports_images"),
                "supports_tools": row.get("supports_tools"),
                "credit_multiplier": row.get("effective"),
                "description": row.get("description"),
            }
        return table

    converter.MODEL_META = _model_meta

    @asynccontextmanager
    async def lifespan(app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                await browser_login.cleanup()
        cleanup_task = asyncio.create_task(reap())

        async def catalog_worker():
            # 启动即拉一次上游目录，之后每小时刷新，保证 /v1/models 与测试下拉
            # 反映当前账号区域的真实模型。
            while True:
                for region in ("cn", "intl"):
                    try:
                        await asyncio.to_thread(rates.ensure, region, cred_for)
                    except Exception:
                        pass
                await asyncio.sleep(3600)

        catalog_task = asyncio.create_task(catalog_worker())
        async def pool_worker():
            while True:
                try:
                    await pool.tick()
                except Exception:
                    pass  # Per-account errors are persisted without credential data.
                await asyncio.sleep(60)
        pool_task = asyncio.create_task(pool_worker())
        try:
            yield
        finally:
            cleanup_task.cancel()
            pool_task.cancel()
            catalog_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await browser_login.close()
            try:
                await pool_task
            except asyncio.CancelledError:
                pass
            try:
                await catalog_task
            except asyncio.CancelledError:
                pass
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.browser_login = browser_login
    app.state.pool = pool
    app.state.metrics = metrics
    app.state.rates = rates
    app.add_middleware(AdminMiddleware)
    app.add_middleware(PoolMiddleware, pool=pool)
    app.add_middleware(MetricsMiddleware, metrics=metrics)

    async def payload(req):
        try:
            body = await req.json()
        except ValueError:
            raise HTTPException(400, "请求不是有效 JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "请求格式不正确")
        return body

    @app.get("/")
    async def home():
        return RedirectResponse("/admin/", 302)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/admin")
    async def redirect_admin():
        return RedirectResponse("/admin/", 302)

    @app.get("/admin/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/admin/assets/{filename}")
    async def asset(filename: str):
        if filename not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / filename)

    @app.post("/admin/api/login")
    async def login(req: Request):
        body = await payload(req)
        # Nginx overwrites X-Real-IP; the application port is loopback-only.
        ip = req.headers.get("x-real-ip") or (req.client.host if req.client else "unknown")
        now = time.time()
        with store.lock:
            store.attempts = {k: v for k, v in store.attempts.items() if v[-1] > now - 600}
            failures = [t for t in store.attempts.get(ip, []) if t > now - 600]
            if len(failures) >= 8:
                raise HTTPException(429, "尝试次数过多，请 10 分钟后重试")
            value = body.get("key")
            if not isinstance(value, str) or not hmac.compare_digest(digest(value), store.admin_digest):
                store.attempts[ip] = failures + [now]
                raise HTTPException(401, "管理密钥不正确")
            store.attempts.pop(ip, None)
            store.sessions = {k: v for k, v in store.sessions.items() if v["expires"] > now}
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            if len(store.sessions) >= 100:
                store.sessions.pop(next(iter(store.sessions)))
            store.sessions[digest(sid)] = {"csrf": csrf, "expires": now + 12 * 3600}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(COOKIE, sid, secure=secure_cookie, httponly=True, samesite="strict", path="/admin", max_age=12 * 3600)
        return response

    @app.get("/admin/api/session")
    async def session(req: Request):
        return {"csrf": store.require_admin(req)["csrf"]}

    @app.post("/admin/api/logout")
    async def logout(req: Request):
        store.require_admin(req)
        await browser_login.cancel_owner(digest(req.cookies.get(COOKIE, "")))
        with store.lock:
            store.sessions.pop(digest(req.cookies.get(COOKIE, "")), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/admin", secure=secure_cookie, httponly=True, samesite="strict")
        return response

    @app.get("/admin/api/accounts/models")
    async def accounts_models(req: Request):
        """每个账号实际可用的模型（含未探测过的候选）。"""
        store.require_admin(req)
        with store.lock:
            rows = []
            for aid, item in store.data["accounts"].items():
                try:
                    manager = store.manager_for(aid, item)
                    domain = (manager.get_headers() or {}).get("X-Domain", "") if manager else ""
                except Exception:
                    domain = ""
                region = "intl" if converter.is_intl_domain(domain) else "cn"
                ok, no = rates.learned(aid)
                rows.append({
                    "id": aid,
                    "name": item.get("name"),
                    "region": region,
                    "enabled": bool(item.get("enabled")),
                    "active": aid == store.data.get("active"),
                    "available": rates.models_for_account(aid, region),
                    "confirmed_ok": ok,
                    "confirmed_no": no,
                    "unknown_count": len(rates.probe_candidates(aid, region)),
                })
        return {"accounts": rows}

    @app.post("/admin/api/accounts/{aid}/probe")
    async def account_probe(aid: str, req: Request):
        """探测某账号的模型可用性。

        每次探测是一次真实上游调用（max_tokens=1），消耗极少额度。
        limit 默认 8，避免一次打太多请求触发限流。
        """
        store.require_admin(req)
        body = {}
        try:
            body = await payload(req)
        except HTTPException:
            pass
        limit = body.get("limit", 8)
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            limit = 8
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            manager = store.manager_for(aid, item)
            domain = (manager.get_headers() or {}).get("X-Domain", "") if manager else ""
            region = "intl" if converter.is_intl_domain(domain) else "cn"
        candidates = rates.probe_candidates(aid, region, limit=limit)
        if not candidates:
            return {"id": aid, "probed": {}, "message": "候选模型都已确认，无需探测"}
        results = await asyncio.to_thread(rates.probe, aid, manager, candidates)
        return {
            "id": aid,
            "probed": results,
            "available": rates.models_for_account(aid, region),
        }

    @app.post("/admin/api/oauth/start")
    async def oauth_start(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), None)
        # 区域：cn（国内，默认）/ intl（国际）
        region = body.get("region")
        region = region if region in ("cn", "intl") else "cn"
        try:
            return await browser_login.start(digest(req.cookies.get(COOKIE, "")), name, region)
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权服务连接失败，请稍后重试")

    @app.post("/admin/api/oauth/{fid}/poll")
    async def oauth_poll(fid: str, req: Request):
        store.require_admin(req)
        try:
            return await browser_login.poll(fid, digest(req.cookies.get(COOKIE, "")))
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权状态暂时无法获取，请稍后重试")

    @app.delete("/admin/api/oauth/{fid}")
    async def oauth_cancel(fid: str, req: Request):
        store.require_admin(req)
        return await browser_login.cancel(fid, digest(req.cookies.get(COOKIE, "")))

    @app.get("/admin/api/models")
    async def models_view(req: Request):
        """模型与倍率：上游官方倍率 + 限时促销 + 实测扣费。"""
        store.require_admin(req)

        for region in ("cn", "intl"):
            await asyncio.to_thread(rates.ensure, region, cred_for)
        await asyncio.to_thread(model_meta.refresh)
        snapshot = rates.snapshot(fallback=model_meta.lookup)
        snapshot["meta"] = model_meta.stats()
        snapshot["metrics"] = metrics.snapshot()
        return snapshot

    @app.post("/admin/api/models/refresh")
    async def models_refresh(req: Request):
        store.require_admin(req)
        with store.lock:
            accounts = [(aid, dict(item)) for aid, item in store.data["accounts"].items()]
        result = {}
        for aid, item in accounts:
            if not item.get("enabled"):
                continue
            try:
                manager = store.manager_for(aid, item)
                domain = (manager.get_headers() or {}).get("X-Domain", "")
            except Exception:
                continue
            region = "intl" if converter.is_intl_domain(domain) else "cn"
            entry = await asyncio.to_thread(rates.refresh, manager, domain)
            result[region] = {"count": len(entry.get("models") or []), "error": entry.get("error")}
        snapshot = rates.snapshot()
        snapshot["metrics"] = metrics.snapshot()
        snapshot["refreshed"] = result
        return snapshot

    @app.post("/admin/api/models/usage/reset")
    async def models_usage_reset(req: Request):
        store.require_admin(req)
        rates.reset_usage()
        return {"ok": True}

    @app.get("/admin/api/usage")
    async def usage_view(req: Request):
        """用量统计：顶部总量 + 按账号 / 模型 / 域明细 + 分钟级时序。"""
        store.require_admin(req)
        with store.lock:
            # 显示名与账号池一致：备注名优先，其次凭据里的昵称，最后回落短 id。
            # 区域由凭据域名判定，供用量表展示（历史併入的记录没有实时区域）。
            labels, regions = {}, {}
            for row in store.account_rows():
                aid = row.get("id")
                if not aid:
                    continue
                labels[aid] = (row.get("name") or row.get("nickname")
                               or row.get("uid") or aid)
                try:
                    manager = store.manager_for(aid, store.data["accounts"][aid])
                    domain = (manager.get_headers() or {}).get("X-Domain", "")
                    regions[aid] = "intl" if converter.is_intl_domain(domain) else "cn"
                except Exception:
                    pass
        return usage_stats.snapshot(labels, regions)

    @app.post("/admin/api/usage/reset")
    async def usage_reset(req: Request):
        store.require_admin(req)
        usage_stats.reset()
        return {"ok": True}

    @app.get("/admin/api/overview")
    async def overview(req: Request):
        store.require_admin(req)
        with store.lock:
            keys = store.key_rows()
            return {"accounts": pool.rows(store.account_rows()), "pool": dict(store.data["pool"]), "metrics": metrics.snapshot(), "keys": keys, "models": converter.get_available_models(), "uptime": int(time.time() - store.started), "events": list(store.events)}

    @app.post("/admin/api/accounts/{aid}/actions/{action}")
    async def account_action(aid: str, action: str, req: Request):
        store.require_admin(req)
        if action not in ("refresh", "status", "checkin"):
            raise HTTPException(404)
        return await asyncio.to_thread(pool.operate, aid, action)

    @app.post("/admin/api/pool/actions/{action}")
    async def pool_action(action: str, req: Request):
        store.require_admin(req)
        if action not in ("status", "checkin"):
            raise HTTPException(404)
        return await pool.batch(action)

    @app.patch("/admin/api/pool/settings")
    async def pool_settings(req: Request):
        store.require_admin(req)
        body = await payload(req)
        import re
        with store.lock:
            settings = dict(store.data["pool"])
            if "routing" in body:
                if body["routing"] not in ("manual", "round_robin"):
                    raise HTTPException(400, "调度方式无效")
                settings["routing"] = body["routing"]
            if "auto_checkin" in body:
                if type(body["auto_checkin"]) is not bool:
                    raise HTTPException(400, "签到开关无效")
                settings["auto_checkin"] = body["auto_checkin"]
            if "checkin_time" in body:
                if not isinstance(body["checkin_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["checkin_time"]):
                    raise HTTPException(400, "请选择有效的签到时间")
                settings["checkin_time"] = body["checkin_time"]
            store.data["pool"] = settings
            store.save()
        return {"ok": True, "pool": settings}

    @app.post("/admin/api/accounts")
    async def add_account(req: Request):
        store.require_admin(req)
        body = await payload(req)
        doc = body.get("credential")
        store.validate_credential(doc)
        name = clean_name(body.get("name"), str(doc["account"].get("nickname") or "新账号")[:60])
        with store.lock:
            if len(store.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            filename = aid + ".info"
            write_json(store.auth_dir / filename, doc)
            store.data["accounts"][aid] = {"file": filename, "name": name, "created": int(time.time()), "enabled": True}
            if store.data["active"] is None:
                store.data["active"] = aid
            store.save()
            store.sync_active()
        return {"id": aid}

    @app.patch("/admin/api/accounts/{aid}")
    async def edit_account(aid: str, req: Request):
        store.require_admin(req)
        body = await payload(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            new = dict(item)
            if "name" in body:
                new["name"] = clean_name(body["name"], item["name"])
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise HTTPException(400, "启用状态不正确")
                new["enabled"] = body["enabled"]
            if body.get("active") is True:
                if not new["enabled"]:
                    raise HTTPException(400, "请先启用账号")
                try:
                    store.validate_credential(json.loads(store.file_for(new).read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    raise HTTPException(400, "凭据文件无法读取，请重新导入")
                store.data["active"] = aid
            store.data["accounts"][aid] = new
            if not new["enabled"] and store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
        return {"ok": True}

    @app.delete("/admin/api/accounts/{aid}")
    async def delete_account(aid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            trash = store.root / "trash" / (aid + "-" + secrets.token_hex(4))
            trash.mkdir(parents=True, mode=0o700)
            source = store.file_for(item)
            if source.exists():
                write_json(trash / "credential.json", json.loads(source.read_text(encoding="utf-8")))
            write_json(trash / "metadata.json", item)
            del store.data["accounts"][aid]
            if store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
            # Keep the file so an in-flight token refresh may still complete safely.
            # Account storage is authoritative; deleted records cannot be selected.
        return {"ok": True}

    @app.post("/admin/api/keys")
    async def add_key(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), "新客户端")
        with store.lock:
            if len(store.data["keys"]) >= 100:
                raise HTTPException(400, "最多保存 100 枚 API Key")
            kid, key = store.add_key(name)
            store.save()
        return {"id": kid, "key": key}

    @app.delete("/admin/api/keys/{kid}")
    async def revoke_key(kid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            if kid not in store.data["keys"]:
                raise HTTPException(404, "密钥不存在")
            if len(store.data["keys"]) <= 1:
                raise HTTPException(400, "请先创建一枚新密钥，再撤销最后一枚密钥")
            del store.data["keys"][kid]
            store.save()
        return {"ok": True}

    @app.post("/admin/api/test")
    async def test_model(req: Request):
        store.require_admin(req)
        body = await payload(req)
        model = body.get("model", "deepseek-v4-flash")
        prompt = body.get("prompt", "请只回复：连接成功")
        if not isinstance(model, str) or model not in converter.get_available_models():
            raise HTTPException(400, "请选择列表中的模型")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise HTTPException(400, "测试消息需为 1–2000 个字符")
        if store.test_lock.locked():
            raise HTTPException(409, "已有测试正在进行")
        if converter.CONFIG.get("cred") is None:
            raise HTTPException(400, "请先导入并启用一个账号")
        async with store.test_lock:
            started = time.monotonic()
            # Use a short-lived API key with the same authentication path as clients.
            with store.lock:
                temp_key = secrets.token_urlsafe(48)
                store.test_keys.add(digest(temp_key))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=MetricsMiddleware(converter.app, metrics, source="test")), base_url="http://internal") as client:
                    result = await asyncio.wait_for(client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + temp_key}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024, "stream": False}), timeout=90)
                data = result.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "") if result.status_code == 200 else ""
                output = {"ok": result.status_code == 200 and bool(answer), "status": result.status_code, "answer": answer, "seconds": round(time.monotonic() - started, 2), "usage": data.get("usage") if result.status_code == 200 else None}
                if not output["ok"]:
                    output["error"] = "上游未返回有效回复，请检查登录凭据、账号额度或模型权限。"
                    if result.status_code == 200 and data.get("choices", [{}])[0].get("finish_reason") == "length":
                        output["error"] = "已连接上游，但生成预算耗尽，未获得正文。请在客户端提高 max_tokens 后重试。"
            except (Exception, asyncio.TimeoutError):
                output = {"ok": False, "error": "调用失败或超时，请检查凭据是否有效，稍后重试。", "seconds": round(time.monotonic() - started, 2)}
            finally:
                with store.lock:
                    store.test_keys.discard(digest(temp_key))
            store.events.appendleft({"time": int(time.time()), "model": model, "ok": output["ok"], "seconds": output["seconds"]})
            return output

    app.mount("/", converter.app)
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8787, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")

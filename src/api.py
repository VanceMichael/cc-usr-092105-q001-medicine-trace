"""稽核后台 HTTP 接口（仅依赖标准库）。

身份与角色假定由前置政务网关注入：
  X-User-Id、X-Role ∈ supervisor/investigator/viewer
公众接口 /api/public/* 不需要任何身份，且输出经过白名单脱敏。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .access import PublicVerifier
from .services import AccessDenied, AuditService, ConflictError
from .store import SOURCE_KINDS, Store

INTERNAL_ROLES = {"supervisor", "investigator", "viewer"}
WRITER_ROLES = {"supervisor", "investigator"}


def create_app(store: Store, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    svc = AuditService(store)
    verifier = PublicVerifier(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AuditBackend/1.0"

        def log_message(self, fmt, *args):  # 测试环境保持安静
            pass

        # ---- 基础工具 ----
        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        @property
        def user(self) -> tuple[str, str]:
            return self.headers.get("X-User-Id", "anonymous"), \
                self.headers.get("X-Role", "public")

        def _require(self, roles=INTERNAL_ROLES) -> tuple[str, str]:
            uid, role = self.user
            if role not in roles:
                raise AccessDenied(f"需要 {sorted(roles)} 身份")
            return uid, role

        def _handle(self, fn):
            try:
                fn()
            except AccessDenied as e:
                self._send(403, {"error": "access_denied", "detail": str(e)})
            except ConflictError as e:
                self._send(409, {"error": "version_conflict", "detail": str(e)})
            except KeyError as e:
                self._send(404, {"error": "not_found", "detail": str(e)})
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, {"error": "bad_request", "detail": str(e)})

        # ---- 路由 ----
        def do_GET(self):
            self._handle(self._route_get)

        def do_POST(self):
            self._handle(self._route_post)

        def _route_get(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                self._send(200, {"status": "ok"})
                return
            m = re.fullmatch(r"/api/public/verify/([A-Za-z0-9_-]+)", path)
            if m:
                self._send(200, verifier.verify(m.group(1)))
                return
            m = re.fullmatch(r"/api/leads/([A-Za-z0-9-]+)", path)
            if m:
                self._require()
                self._send(200, svc.get_lead(m.group(1)))
                return
            m = re.fullmatch(r"/api/leads/([A-Za-z0-9-]+)/lineage", path)
            if m:
                self._require()
                self._send(200, svc.lineage(m.group(1)))
                return
            m = re.fullmatch(r"/api/cases/([A-Za-z0-9_-]+)/seal", path)
            if m:
                self._require()
                self._send(200, svc.verify_seal(m.group(1)))
                return
            if path == "/api/leads":
                self._require()
                qs = self._query()
                self._send(200, svc.list_leads(qs.get("status"),
                                               qs.get("rule_code")))
                return
            self._send(404, {"error": "not_found"})

        def _route_post(self):
            path = self.path.split("?", 1)[0]
            body = self._read_json()

            m = re.fullmatch(r"/api/ingest/([a-z_]+)", path)
            if m:
                uid, role = self._require(WRITER_ROLES)
                kind = m.group(1)
                if kind not in SOURCE_KINDS:
                    raise ValueError(f"未知来源类型: {kind}")
                result = store.ingest_batch(
                    kind, body.get("source_org", "未知单位"),
                    body.get("records", []),
                    batch_id=body.get("batch_id"),
                    received_at=body.get("received_at"))
                self._send(200, result)
                return

            if path == "/api/batch-jobs":
                uid, role = self._require(WRITER_ROLES)
                self._send(200, svc.run_batch(
                    rule_codes=body.get("rule_codes"),
                    params=body.get("params"), scope=body.get("scope"),
                    trigger=body.get("trigger", "manual"),
                    note=body.get("note", "批量交叉比对")))
                return

            m = re.fullmatch(r"/api/leads/([A-Za-z0-9-]+)/decisions", path)
            if m:
                uid, role = self._require(WRITER_ROLES)
                result = svc.decide(
                    m.group(1), actor=uid, action=body["action"],
                    expected_version=body.get("expected_version"),
                    comment=body.get("comment", ""),
                    payload=body.get("payload"))
                self._send(200, result)
                return

            if path == "/api/cases":
                uid, role = self._require({"supervisor"})
                result = svc.create_case(body["case_no"], body["title"],
                                         body["region"], uid)
                self._send(200, result)
                return

            m = re.fullmatch(r"/api/cases/([A-Za-z0-9_-]+)/members", path)
            if m:
                uid, role = self._require({"supervisor"})
                svc.require_case_member(m.group(1), uid,
                                        need_roles=("supervisor",))
                svc.add_member(m.group(1), body["user_id"], body["role"])
                self._send(200, {"ok": True})
                return

            m = re.fullmatch(r"/api/cases/([A-Za-z0-9_-]+)/links", path)
            if m:
                uid, role = self._require(WRITER_ROLES)
                svc.link_lead(m.group(1), body["lead_no"], uid)
                self._send(200, {"ok": True})
                return

            m = re.fullmatch(r"/api/cases/([A-Za-z0-9_-]+)/seal", path)
            if m:
                uid, role = self._require(WRITER_ROLES)
                self._send(200, svc.seal_case(m.group(1), uid))
                return

            m = re.fullmatch(r"/api/cases/([A-Za-z0-9_-]+)/transfer", path)
            if m:
                uid, role = self._require({"supervisor"})
                self._send(200, svc.transfer_case(
                    m.group(1), uid, body["to_org"], body["to_region"],
                    body.get("note", "")))
                return

            m = re.fullmatch(r"/api/transfers/([A-Za-z0-9-]+)/receive", path)
            if m:
                # 接收方为外地办案单位，核验动作不做本地案件成员限制
                uid, role = self._require(WRITER_ROLES)
                self._send(200, svc.receive_transfer(
                    m.group(1), uid, body["receiver_org"],
                    body.get("note", "")))
                return

            self._send(404, {"error": "not_found"})

        def _query(self) -> dict:
            if "?" not in self.path:
                return {}
            out = {}
            for pair in self.path.split("?", 1)[1].split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    out[k] = v
            return out

    return ThreadingHTTPServer((host, port), Handler)

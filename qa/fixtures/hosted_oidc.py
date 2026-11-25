# fixture: disposable OIDC issuer
# produces: discovery, authorization, token exchange and JWKS over loopback HTTPS
# fidelity: rung 3 hand-rolled-seed
# fidelity-checked: PENDING — do not trust for assertions until real Logto comparison
# teardown: context manager shuts down the owned server and joins its thread
"""Test-only OIDC issuer; it never reads live configuration or credentials."""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import secrets
import ssl
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class Issuer:
    def __init__(self, temp_parent, *, mcp=False, app_origin="https://helheim.example"):
        self.temp_parent = temp_parent
        self.mcp = mcp
        self.app_origin = app_origin

    def __enter__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}
        self.fault = ""
        self.refresh_mode = "absent"
        self.observations = []
        self.material = []
        self.codes_issued = []
        self.identity = "owner"
        self.deny = False
        tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "owned test issuer")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(
                ipaddress.ip_address("127.0.0.1")
            )]), critical=False)
            .sign(tls_key, hashes.SHA256())
        )
        self.temp = tempfile.TemporaryDirectory(prefix="oidc-tls-", dir=self.temp_parent)
        try:
            cert_path = Path(self.temp.name) / "issuer.pem"
            key_path = Path(self.temp.name) / "issuer-key.pem"
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(tls_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            self.client_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.client_tls.load_verify_locations(cafile=str(cert_path))
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.load_cert_chain(str(cert_path), str(key_path))
        except BaseException:
            self.temp.cleanup()
            raise
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def json_response(self, value, status=200):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/.well-known/openid-configuration":
                    fixture.observations.append({"stage": "discovery"})
                    self.json_response({
                        "issuer": ("https://wrong.example" if fixture.fault == "discovery-issuer"
                                   else fixture.url),
                        "authorization_endpoint": fixture.url + "/authorize",
                        "token_endpoint": fixture.url + "/token", "jwks_uri": fixture.url + "/jwks",
                        "id_token_signing_alg_values_supported": ["RS256"],
                    })
                elif parsed.path == "/jwks":
                    fixture.observations.append({"stage": "jwks"})
                    public = json.loads(
                        jwt.algorithms.RSAAlgorithm.to_jwk(fixture.key.public_key())
                    )
                    public.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
                    self.json_response({"keys": [public]})
                elif parsed.path == "/authorize":
                    query.setdefault("identity", [fixture.identity])
                    callback = ("/auth/mcp/callback" if fixture.mcp else "/login/callback")
                    expected_scope = {"openid", "offline_access"} if fixture.mcp else {"openid"}
                    scopes = set(query.get("scope", [""])[0].split())
                    resource = query.get("resource", [""])[0]
                    if (query.get("client_id") != ["test-client"]
                             or query.get("code_challenge_method") != ["S256"]
                             or query.get("response_mode") != ["form_post"]
                            or scopes != expected_scope
                             or query.get("redirect_uri") != [fixture.app_origin + callback]
                            or (fixture.mcp and resource not in (
                                "https://one.example/mcp", "https://two.example/mcp"))
                            or query.get("identity", [""])[0] not in ("owner", "other")):
                        self.json_response({"error": "invalid_request"}, 400)
                        return
                    code = secrets.token_urlsafe(24)
                    response_value = "synthetic_access_denied" if fixture.deny else code
                    fixture.material.extend([response_value, query["state"][0]])
                    if not fixture.deny:
                        fixture.codes_issued.append(code)
                    fixture.codes[code] = query
                    fixture.observations.append({
                        "stage": "authorize", "resource": resource, "callback": callback,
                        "pkce": query.get("code_challenge_method") == ["S256"],
                        "nonce": bool(query.get("nonce")),
                    })
                    field = "error" if fixture.deny else "code"
                    action = html.escape(query["redirect_uri"][0], quote=True)
                    value = html.escape(response_value, quote=True)
                    state = html.escape(query["state"][0], quote=True)
                    body = (f'<form method="post" action="{action}">'
                            f'<input name="{field}" value="{value}">'
                            f'<input name="state" value="{state}">'
                            '</form><script>document.forms[0].submit()</script>').encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.json_response({"error": "not_found"}, 404)

            def do_POST(self):
                if self.path != "/token":
                    self.json_response({"error": "not_found"}, 404)
                    return
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                basic = base64.b64encode(b"test-client:test-only").decode()
                if self.headers.get("Authorization") != "Basic " + basic:
                    self.json_response({"error": "invalid_client"}, 401)
                    return
                if fixture.mcp and form.get("grant_type") == ["refresh_token"]:
                    fixture.observations.append({
                        "stage": "refresh", "resource": form.get("resource", [""])[0],
                    })
                    self.json_response({"error": "invalid_grant"}, 400)
                    return
                transaction = fixture.codes.pop(form.get("code", [""])[0], None)
                verifier = form.get("code_verifier", [""])[0]
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()
                ).rstrip(b"=").decode()
                if (not transaction or challenge != transaction["code_challenge"][0]
                        or form.get("redirect_uri") != transaction["redirect_uri"]
                        or (fixture.mcp and form.get("resource") != transaction["resource"])):
                    self.json_response({"error": "invalid_grant"}, 400)
                    return
                fixture.observations.append({
                    "stage": "exchange", "resource": form.get("resource", [""])[0],
                    "pkce_verified": True,
                })
                now = int(time.time())
                claims = {
                    "iss": fixture.url, "sub": transaction["identity"][0], "aud": "test-client",
                    "iat": now, "exp": now + 120, "nonce": transaction["nonce"][0],
                }
                if fixture.fault == "issuer":
                    claims["iss"] = "https://wrong.example"
                elif fixture.fault == "audience":
                    claims["aud"] = "wrong-client"
                elif fixture.fault == "expired":
                    claims["exp"] = now - 30
                elif fixture.fault == "nonce":
                    claims["nonce"] = "wrong-nonce"
                signing_key = fixture.key
                if fixture.fault == "signature":
                    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                identity_token = jwt.encode(claims, signing_key, algorithm="RS256",
                                            headers={"kid": "test-key"})
                payload = {"access_token": "fixture-only", "token_type": "Bearer",
                           "id_token": identity_token}
                if fixture.mcp:
                    payload["expires_in"] = 600
                    if fixture.refresh_mode == "refused":
                        payload["refresh_token"] = "fixture-refresh"
                self.json_response(payload)

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.server.socket = server_tls.wrap_socket(self.server.socket, server_side=True)
            self.url = f"https://127.0.0.1:{self.server.server_port}"
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        except BaseException:
            if hasattr(self, "server"):
                self.server.server_close()
            self.temp.cleanup()
            raise
        return self

    def __exit__(self, *args):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("OIDC fixture did not stop")
        finally:
            self.temp.cleanup()

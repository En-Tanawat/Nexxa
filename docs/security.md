# NEXXA Security & Threat Model: OWASP Top 10:2025 Compliance

This document provides a comprehensive security assessment, threat model, and verification record for the NEXXA automated hydroponic monitoring system, evaluated against the **OWASP Top 10:2025** standard.

---

## 1. Executive Summary

NEXXA is an edge-connected greenhouse inspection system combining an autonomous mobile robot (inspection carrier), a FastAPI backend, an SQLite/PostgreSQL database, and multi-provider AI vision analysis (Claude, OpenAI, Gemini, Qwen, DeepSeek).

Security architecture follows the principle of **Defense-in-Depth**, **Fail-Closed Default**, and **Least Privilege**:
- **Zero Public Mutation**: All public-facing web routes (`/`, `/trends`, `/api/public/*`) are strictly read-only (`GET` / `HEAD`). State mutations require either admin web session credentials with valid CSRF tokens or a cryptographic device key.
- **Fail-Closed Secrets Management**: Secrets (AI API keys) are Fernet-encrypted (AES-128-CBC + HMAC-SHA256) at rest. When encryption keys are absent, secret storage operations are refused (`409`).
- **Timing-Safe Cryptography**: Password verification (`scrypt`), session tokens, CSRF tokens, and robot device key validations exclusively use constant-time comparisons (`hmac.compare_digest`).
- **Comprehensive Automated Verification**: 35 automated security tests in `server/tests/test_web_owasp.py` continuously enforce each OWASP Top 10:2025 control.

---

## 2. OWASP Top 10:2025 Compliance Matrix

| Identifier | OWASP Category | NEXXA Status | Automated Test Coverage |
| :--- | :--- | :---: | :--- |
| **A01:2025** | Broken Access Control & SSRF | **COMPLIANT** | `test_a01_*` (6 tests) |
| **A02:2025** | Security Misconfiguration | **COMPLIANT** | `test_a02_*` (5 tests) |
| **A03:2025** | Software Supply Chain Failures | **COMPLIANT** | `test_a03_*` (2 tests) |
| **A04:2025** | Cryptographic Failures | **COMPLIANT** | `test_a04_*` (3 tests) |
| **A05:2025** | Injection | **COMPLIANT** | `test_a05_*` (4 tests) |
| **A06:2025** | Insecure Design | **COMPLIANT** | `test_a06_*` (4 tests) |
| **A07:2025** | Identification & Authentication Failures | **COMPLIANT** | `test_a07_*` (4 tests) |
| **A08:2025** | Software and Data Integrity Failures | **COMPLIANT** | `test_a08_*` (3 tests) |
| **A09:2025** | Security Logging and Alerting Failures | **COMPLIANT** | `test_a09_*` (2 tests) |
| **A10:2025** | Mishandling of Exceptional Conditions | **COMPLIANT** | `test_a10_*` (4 tests) |

---

## 3. Detailed Threat Model & Controls by Category

### A01: Broken Access Control & SSRF

#### Description
Attackers bypass authorization checks to access administrative endpoints, manipulate other users' data, or induce the server into making unintended requests to internal/loopback network targets (Server-Side Request Forgery).

#### NEXXA Mitigation
1. **Dynamic Route Authorization**:
   - Every admin route under `/admin` enforces `Depends(deps.require_admin)` (`server/app/web/deps.py#L67-L74`). Unauthenticated requests are redirected (`HTTP 303`) to `/login?next=...` (or return `401` on API subpaths).
   - All state-changing methods (`POST`, `PUT`, `DELETE`) require `Depends(deps.require_csrf)` (`server/app/web/deps.py#L77-L84`).
   - Dynamic route introspection test iterates through all registered application endpoints to prevent developer omission on new routes.
2. **Read-Only Public Surface**:
   - `/api/public/*` only implements `GET` and `HEAD` methods (`server/app/web/routes_public_api.py`). State-changing methods return `405 Method Not Allowed`.
   - Inspection capture images are never served on public endpoints. The static files directory is restricted to `server/app/web/static` (`server/app/main.py#L91`), never exposing raw data or image directories.
3. **SSRF Filtering & Allowlist**:
   - Custom provider base URLs are validated at input time and use time (`server/app/runtime_config.py#L137-L187`).
   - Rejects non-HTTPS schemes, private/loopback IPv4/IPv6 ranges (`127.0.0.0/8`, `10.0.0.0/8`, `192.168.0.0/16`, `172.16.0.0/12`, `169.254.0.0/16`, `::1`, `::ffff:127.0.0.1`), embedded IPv4 addresses (6to4, Teredo, NAT64), internal domains (`localhost`, `.local`, `.internal`), port 0, credentials in URL (`user:pass@`), and query/hash fragments.
   - Provider hosts are pinned to official API endpoints (`server/app/providers.py#L18-L35`). Qwen (Alibaba DashScope) is strictly constrained to `dashscope*.aliyuncs.com` and `*.maas.aliyuncs.com`, preventing SSRF to arbitrary cloud resources.

#### Automated Tests
- `test_a01_all_admin_routes_reject_unauthenticated`
- `test_a01_all_admin_state_changing_routes_require_csrf`
- `test_a01_public_api_read_only`
- `test_a01_no_private_capture_images_served_unauthenticated`
- `test_a01_ssrf_rejects_private_ips_and_internal_hostnames`
- `test_a01_qwen_allowlist_tightened`

#### Residual Risk & Out-of-Scope
- **DNS Rebinding**: Hostname resolution occurs at request dispatch by HTTP client libraries. Production deployments should enforce DNS pinning or egress firewall policies at the container/host level.

---

### A02: Security Misconfiguration

#### Description
Unsafe defaults, verbose debug endpoints enabled in production, missing HTTP security headers, or overly permissive caching allowing sensitive data exposure.

#### NEXXA Mitigation
1. **HTTP Security Headers Middleware**:
   - Applied to all web page responses via `page_security_headers` middleware (`server/app/main.py#L80-L103`):
     - `Content-Security-Policy`: `default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: http: https:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`
     - `X-Content-Type-Options`: `nosniff`
     - `Referrer-Policy`: `same-origin`
     - `X-Frame-Options`: `DENY`
2. **Authenticated Cache Prevention**:
   - All responses under `/admin` enforce `Cache-Control: no-store, private`, preventing browser disk caching and intermediary proxy retention of sensitive farm data.
3. **Debug Docs Disabled Outside Development**:
   - Swagger UI (`/docs`), ReDoc (`/redoc`), and OpenAPI schema (`/openapi.json`) are blocked (`404 Not Found`) via `block_debug_docs` middleware (`server/app/main.py#L66-L73`) unless `NEXXA_ENV == "development"`.
4. **Leak-Free Error Responses**:
   - Production error responses (404, 422, 500) never leak Python tracebacks, internal file system paths, or database errors.
5. **Session Cookie Hardening**:
   - Session cookies use `HttpOnly=True`, `SameSite=Lax`, and `Secure=True` on HTTPS (`server/app/web/deps.py#L33-L38`).

#### Automated Tests
- `test_a02_security_headers_on_html_pages`
- `test_a02_cache_control_no_store_on_admin_pages`
- `test_a02_debug_docs_disabled_when_not_development`
- `test_a02_error_responses_do_not_leak_internals`
- `test_a02_session_cookie_security_flags`

#### Residual Risk & Out-of-Scope
- **TLS Termination**: NEXXA assumes an upstream reverse proxy (Nginx, Caddy, Cloudflare, Traefik) handles TLS certificates and HSTS headers.

---

### A03: Software Supply Chain Failures

#### Description
Using vulnerable, compromised, unpinned, or tampered third-party packages and client-side dependencies.

#### NEXXA Mitigation
1. **Strictly Pinned Dependencies**:
   - All backend packages in `server/requirements.txt` are pinned to exact versions using `==` (e.g. `fastapi==0.163.1`, `cryptography==50.0.2`, `SQLAlchemy==2.1.8`).
2. **Vendored Client-Side Libraries**:
   - Front-end dependencies (`Chart.js 4.5.1`, `three.js 0.185.1`) are vendored locally in `server/app/web/static/vendor/` and never fetched from runtime CDNs (enforcing CSP `script-src 'self'`).
   - Every vendored file's upstream source, license, and SHA-256 hash are recorded in `server/app/web/static/vendor/VENDOR.md`.
   - `.gitattributes` enforces `*.min.js -text` to prevent CRLF line ending alteration from corrupting cryptographic hashes across platforms.

#### Automated Tests
- `test_a03_requirements_pinned`
- `test_a03_vendor_assets_documented_and_hashed`

#### Residual Risk & Out-of-Scope
- **Automated Dependency Vulnerability Scanning**: Continuous scanning via `pip-audit` or Dependabot should be integrated into CI/CD pipelines to monitor newly published CVEs against pinned packages.

---

### A04: Cryptographic Failures

#### Description
Storing sensitive secrets in plaintext, using weak cryptographic algorithms, or mishandling passwords.

#### NEXXA Mitigation
1. **Fernet Encryption at Rest**:
   - Third-party AI provider keys are encrypted using Fernet (AES-128-CBC with PKCS7 padding and HMAC-SHA256 authenticated encryption) via `secretbox` (`server/app/web/secretbox.py#L1-L70`).
   - Database column `ProviderKey.secret_enc` stores binary Fernet tokens; cleartext keys never persist to disk.
   - Fail-closed: If `NEXXA_SECRET_KEY` is missing or invalid, key write operations fail with `409 Conflict`.
2. **Modern Password Hashing**:
   - Passwords are hashed using `scrypt` with a cryptographically secure 16-byte salt and recommended parameters (`N=16384, r=8, p=1`) (`server/app/web/security.py#L10-L33`).
   - Each hash operation generates an independent random salt.
3. **No Weak Algorithms**:
   - Codebase static audit guarantees zero usage of `md5` or `sha1` for security tokens, passwords, or authentication. Device keys are hashed with `sha256` (`server/app/web/security.py#L104-L115`).

#### Automated Tests
- `test_a04_secret_keys_in_db_encrypted_with_fernet`
- `test_a04_passwords_hashed_with_scrypt`
- `test_a04_no_weak_algorithms_for_security_tokens`

#### Residual Risk & Out-of-Scope
- **Master Secret Rotation**: Rotation of `NEXXA_SECRET_KEY` requires operational key re-encryption tooling.

---

### A05: Injection (SQL, XSS, Jinja SSTI)

#### Description
Untrusted input evaluated as commands or interpreted as markup/queries by the interpreter or browser.

#### NEXXA Mitigation
1. **SQL Injection Resistance**:
   - Database operations exclusively use SQLAlchemy 2.0 ORM and parameterized queries (`server/app/web/routes_admin.py`, `server/app/web/routes_keys.py`). Raw string concatenation into SQL statements is prohibited.
2. **Cross-Site Scripting (XSS) Prevention**:
   - Jinja2 autoescaping is enforced across all templates (`server/app/web/templating.py`). HTML tags and characters (`<`, `>`, `"`, `'`, `&`) are escaped to their entity representations (`&lt;`, `&gt;`, `&#39;`).
   - Content Security Policy forbids `'unsafe-inline'` and `'unsafe-eval'` scripts.
3. **Template Injection (SSTI) Immunity**:
   - User inputs are passed as template context variables, never rendered via Jinja `from_string` or evaluated dynamically.
4. **Input Length Restrictions**:
   - All form text fields strictly enforce length bounds (e.g. name ≤ 120 chars, password ≤ 128 chars, RFID ≤ 64 chars, base URL ≤ 300 chars). Oversized inputs trigger `422 Validation Error`.

#### Automated Tests
- `test_a05_sql_injection_resisted`
- `test_a05_xss_html_injection_escaped_in_jinja`
- `test_a05_template_injection_not_evaluated`
- `test_a05_oversized_inputs_rejected`

#### Residual Risk & Out-of-Scope
- None identified for application layer.

---

### A06: Insecure Design

#### Description
Design flaws and missing business logic invariants that allow unauthorized states or circumvention of domain rules.

#### NEXXA Mitigation
1. **Admin User Invariant**:
   - The system prevents deleting the last remaining administrative account (`server/app/web/routes_admin.py#L608-L618`), preventing administrator lockout.
2. **Station Data Integrity**:
   - Stations with active batches, capture records, or daily statistics cannot be deleted (`server/app/web/routes_admin.py#L275-L285`), preventing orphan foreign keys and data loss.
3. **Temporal Logical Validation**:
   - Crop batches enforce that `harvested_on` cannot be chronologically earlier than `planted_on` (`server/app/web/routes_admin.py#L451-L452`).
4. **Docking Station Protection**:
   - Stations designated as `home` (robot dock) cannot be targeted for inspection captures (`server/app/main.py#L173-L174`), preventing erroneous analysis of docking hardware.

#### Automated Tests
- `test_a06_cannot_delete_last_admin_user`
- `test_a06_cannot_delete_station_with_active_data`
- `test_a06_batch_harvest_date_before_planting_date_rejected`

#### Residual Risk & Out-of-Scope
- **Physical Robot Safety**: Hardware-level e-stop, collision avoidance, and motor overcurrent protections are implemented on the robot microcontroller, outside web backend scope.

---

### A07: Identification & Authentication Failures

#### Description
Credential stuffing, brute force attacks, session hijacking, or acceptance of weak credentials.

#### NEXXA Mitigation
1. **Login Throttling & Lockout**:
   - In-memory `LoginThrottle` (`server/app/web/security.py#L118-L142`) limits failed login attempts to 5 per 15-minute window per IP. Exceeding this threshold returns `429 Too Many Requests`.
2. **User Enumeration Prevention**:
   - Non-existent usernames and incorrect passwords return identical error messages: `"ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"` (`server/app/web/routes_auth.py#L68`).
   - Constant-time dummy password verification is performed even when the user does not exist (`server/app/web/routes_auth.py#L65`).
3. **Absolute Session Expiration**:
   - Signed HMAC session tokens embed absolute creation timestamps and enforce `MAX_AGE_S = 8 * 3600` (8 hours). Expired sessions cannot be renewed without re-authenticating (`server/app/web/deps.py#L12, L27`).
4. **Password Complexity**:
   - Password creation and updates reject passwords under 10 characters (`server/app/create_admin.py#L18`, `server/app/web/routes_admin.py#L588`).

#### Automated Tests
- `test_a07_login_throttling_lockout`
- `test_a07_consistent_error_message_on_bad_credentials`
- `test_a07_absolute_session_timeout`
- `test_a07_weak_password_rejected`

#### Residual Risk & Out-of-Scope
- **Distributed Multi-IP Brute Force**: Network-level rate limiting (Cloudflare WAF / fail2ban) is recommended for internet-exposed instances.

---

### A08: Software and Data Integrity Failures

#### Description
Lack of integrity validation on sensitive data exchanges, insecure deserialization, or unverified script inclusions.

#### NEXXA Mitigation
1. **HMAC-Protected CSRF Tokens**:
   - CSRF tokens are generated per session and verified using constant-time comparison (`hmac.compare_digest`) (`server/app/web/deps.py#L83`, `server/app/web/security.py#L97-L101`).
   - Fresh CSRF tokens are re-issued upon authentication (`server/app/web/routes_auth.py#L76`).
2. **Zero Insecure Deserialization**:
   - Neither `pickle`, unsafe `yaml.load`, nor `marshal` are used anywhere in the codebase. All JSON interactions utilize Starlette / Pydantic safe JSON decoders.
3. **Static Asset Integrity**:
   - SHA-256 hashes of all client scripts are verified against `VENDOR.md`.

#### Automated Tests
- `test_a08_csrf_tokens_verified_with_timing_safe_hmac`
- `test_a08_no_unsafe_deserialization_in_codebase`
- `test_a03_vendor_assets_documented_and_hashed`

#### Residual Risk & Out-of-Scope
- None identified.

---

### A09: Security Logging and Alerting Failures

#### Description
Failure to log security-relevant events, or logging plaintext secrets and sensitive data into audit trails.

#### NEXXA Mitigation
1. **Structured Audit Trail**:
   - Security-critical events (`station.*`, `crop.*`, `batch.*`, `user.*`, `key.*`, `device.*`, `ai.update`) are committed to the `audit_logs` database table (`server/app/web/audit.py#L1-L30`).
2. **Secret Sanitization**:
   - Plaintext passwords and API keys are strictly excluded from audit logs and application loggers. Provider keys record only the last 4 characters (`last4`).
3. **Device Authentication Auditing**:
   - Unauthorized device requests (`HTTP 401`) are logged with sanitized error descriptors without reflecting secret token values.

#### Automated Tests
- `test_a09_security_events_logged_without_plaintext_secrets`
- `test_a09_device_key_unauthorized_logged`

#### Residual Risk & Out-of-Scope
- **SIEM / Remote Log Forwarding**: Centralized log shipping (e.g. Syslog, OpenTelemetry, Datadog) should be configured in production infrastructure.

---

### A10: Mishandling of Exceptional Conditions

#### Description
Uncaught exceptions, unhandled malformed payloads, or database connection failures resulting in unhandled crashes or stack trace disclosure.

#### NEXXA Mitigation
1. **Graceful Fail-Closed Architecture**:
   - Malformed JSON sent to robot telemetry or API endpoints returns clean `422 Unprocessable Entity` JSON responses (`server/app/main.py`).
2. **Oversized Upload Early Rejection**:
   - Requests exceeding `MAX_UPLOAD_BYTES` (5 MB) are rejected early in middleware via `reject_oversized` (`server/app/main.py#L52-L59`) before spooling to memory or disk (`HTTP 413 Payload Too Large`).
3. **Image Format & Corrupt Data Rejection**:
   - Non-image files or malformed JPEG/PNG payloads uploaded to `/captures` are caught by `check_quality` (`server/app/quality.py`) and return `422` ("not an image").
4. **Safe Stats Rollup**:
   - Daily statistics computation errors never crash round capture requests (`server/app/main.py#L26-L36`).
5. **URL Parameter Overflow Guard**:
   - Integer path parameters constrain IDs to valid SQLite ranges (`1 <= id <= 2,147,483,647`), returning `422/404` rather than triggering database driver overflow exceptions.

#### Automated Tests
- `test_a10_malformed_json_fails_closed_gracefully`
- `test_a10_oversized_upload_fails_closed_with_413`
- `test_a10_non_image_payload_fails_closed_with_422`
- `test_a10_invalid_path_parameters_handled_cleanly`

#### Residual Risk & Out-of-Scope
- **Out of Disk Space**: OS-level disk space monitoring and database backup policies must be managed by the system administrator.

---

## 4. Verification Instructions

To execute the entire OWASP Top 10:2025 automated verification test suite:

```powershell
cd D:\Work\Nexxa
server\.venv\Scripts\python.exe -m pytest server\tests\test_web_owasp.py -v
```

All 35 verification tests run in isolated in-memory SQLite environments and complete with zero external network dependencies.

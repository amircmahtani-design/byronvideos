# Owner setup without provider authorization

Use the explicit `--administration-only` service bootstrap flag for first-owner
setup. This is a separate signed authority, not production or media acceptance.
The default production path and its policy/billing requirements are unchanged.

The bootstrap ENV/file delivery, independently pinned reviewer key, SDK lock,
attached service identity, numeric CSRF secret version, named database, exact
Google subject/email mapping, HTTPS callback and restricted proxy remain required.
Use the existing private config/plan schema; its inactive generation settings do
not grant generation permission. Never invent verified runtime fingerprints.

The acceptance envelope has exactly `payload` and base64 Ed25519 `signature`.
Sign canonical JSON for this exact payload schema:

```
schema_version: 1
purpose: byron-administration-only-v1
config_sha256: <SHA256 of canonical private config>
plan_sha256: <SHA256 of canonical private processing plan>
issued_at: <integer Unix timestamp>
expires_at: <integer Unix timestamp, at most seven days after issuance>
owner_only: true
generation_allowed: false
provider_calls_allowed: false
```

Only one configured owner may be both creator and administrator. The signature
authorizes setup, not claims of completed cloud/login/media acceptance. It is
rechecked for login, admin requests and attached credential refresh. It cannot
be reused as a production authorization. Policy/billing flags and legacy private
adapters are rejected when setup-only mode is selected.

Existing static assets, Google login and CSRF-protected write-only key setup are
mounted. No generation dispatcher, job/upload/resume/exact-film routes, provider
tests or voice catalog calls are installed. Provider tests/default-voice validation
remain unavailable; saving a key cannot activate generation. Creator capabilities
report disabled. Production activation requires a separately reviewed normal
startup with all existing signed gates and permissions.

The administrative service needs only its scoped login database and setup secret
permissions; do not grant job-execution or provider prediction access for setup.
These source tests are offline evidence, not live deployment acceptance.

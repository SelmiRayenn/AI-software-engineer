# Security and Secret Redaction

The platform treats repository content, issue text, model output, tool output, test logs, and
failure messages as potentially sensitive. Redaction is a defense-in-depth control for logs and
shared output; it is not a substitute for keeping production credentials out of benchmark
workspaces.

## Redaction policy

`backend/app/core/redaction.py` is the canonical redaction implementation. It uses the stable
marker `[REDACTED]` and applies bounded recursive sanitization to structured payloads. Long values
use a `[TRUNCATED]` marker with the omitted character count.

The utility recognizes:

- bearer credentials and authorization headers;
- OpenAI-, Anthropic-, GitHub-, Google-, Slack-, JWT-, and AWS-style token shapes;
- API key, access token, password, private key, client secret, and common cloud secret field names;
- credential-bearing PostgreSQL, MySQL, MariaDB, MongoDB, Redis, and AMQP URLs;
- sensitive URL query parameters;
- `.env`-style uppercase assignments; and
- PEM private-key blocks.

Non-sensitive surrounding text is retained where practical so failures remain diagnosable.
Token counts and other numeric usage fields are not treated as credentials.

## Covered surfaces

Redaction runs before persistence for `AgentEvent` payloads, test command results, failure
summaries, human review text, flakiness logs, and benchmark-pack run snapshots. Response schemas
also sanitize these values to protect older rows that predate the persistence hooks.

The same policy is applied to:

- agent run traces and model-response summaries;
- replay snapshots in JSON and Markdown;
- agent-run and benchmark-pack reports;
- public demo snapshots;
- test-failure analyses and repair feedback;
- sandbox command responses; and
- analytics labels derived from events, models, repositories, tools, or file paths.

Gold patches and hidden-test payloads have separate access controls and omission rules. Redaction
does not make those payloads safe to expose.

## Verifiable audit exports

Run and patch audit exports provide a deterministic SHA-256 hash chain over sanitized event fields:

```text
GET /agent-runs/{run_id}/audit-log
GET /patches/{patch_id}/audit-log
```

Every event records a sequence number, canonical UTC timestamp, event type, actor type, sanitized
summary, related entity ID, previous hash, and its own hash. The first event has a null previous
hash. The response exposes `final_audit_hash`, also returned in the `ETag` and `X-Audit-Hash`
headers. Export time is metadata and is excluded from the chain, so an unchanged run produces the
same event hashes on repeated export.

The timeline includes explicit run/task/model/configuration records, persisted agent events,
patch versions and application status, tests, repairs, metrics, failures, and human reviews.
Configuration content is not exported; only a hash of its sanitized canonical form is included.
Hidden evaluation appears only as aggregate phase outcomes. Gold and hidden payloads are never read
into the export.

The chain detects changes when compared with a previously retained final hash. It does not prevent
database modification and is not a digital signature, trusted timestamp, transparency log, or
external notarization. Stronger assurance requires storing or signing the final hash outside the
application's database and host.

## Testing

The backend test suite injects fake credentials representing all supported categories into traces,
test failures, reports, replay snapshots, public snapshots, and audit logs. Tests assert that raw
fixture values never appear in exported JSON or Markdown.

## Limitations

Pattern-based redaction cannot reliably identify every arbitrary secret, especially high-entropy
values without a recognizable prefix or sensitive field name. Secrets split across fields or
encoded in an unknown format may also evade detection. Conversely, `.env`-style assignments and
credential-shaped strings may be redacted even when they are harmless examples.

Operational deployments should additionally:

- use short-lived, least-privilege credentials;
- avoid injecting host credentials into sandbox containers;
- restrict access to the PostgreSQL database and application logs;
- rotate any credential suspected of reaching an untrusted workspace; and
- periodically extend the centralized patterns as providers introduce new key formats.

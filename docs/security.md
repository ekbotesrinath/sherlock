# Security Model

Sherlock is **read-only by design** and treats both New Relic data and
credentials as hostile-environment assets: mutations are blocked at the
transport layer, tool output is scrubbed for prompt injection, and API
keys never touch disk in plaintext.

## Read-Only Enforcement

The `NerdGraphClient` parses every GraphQL document with `graphql-core`
**before sending it** and asserts that every operation is a `query` — any
`mutation` (or unparseable document) raises `ReadOnlyViolation`, which is
logged as a **SECURITY WARNING** in the audit log.

Because enforcement is based on the parsed operation type (not keyword
matching), legitimate queries containing words like "update" or "delete"
in embedded NRQL strings — e.g. `search_logs(keyword="failed to update
order")` — are not falsely blocked.

All NRQL travels as a typed GraphQL **variable** through the single
executor in `sherlock/core/nrql_executor.py`, never string-interpolated
into the GraphQL document, which removes the GraphQL-injection and
quote-escaping bug class.

## Credential Security

* API keys are stored in the **OS keychain** via the `keyring` library
  (service name `sherlock`), never in `profiles.json` or logs
* The `redacted_key` property masks all but the first/last 4 characters;
  `model_dump()` excludes the raw API key entirely
* `connect_account`'s `api_key` argument is **deprecated** — passing a key
  through the LLM conversation leaks it into chat logs. Create profiles
  once via `make connect` (`sherlock-validate`) and connect by profile name
* A plaintext key in `.env` triggers a startup warning — rotate any key
  that has sat in a plaintext file

### Keyring Backend Hardening

On headless Linux, the `keyring` library can silently fall back to
backends that either discard secrets (`fail`/`null`) or write them to a
world-readable file (`keyrings.alt` plaintext). Sherlock **refuses** to
store or load API keys through these weak backends and raises
`CredentialError` with remediation guidance instead.

* Detection covers chained backends (`ChainerBackend`) — one weak member
  marks the chain weak
* Fix by configuring a real backend: Windows Credential Locker, macOS
  Keychain, or GNOME Keyring / KWallet via Secret Service
* Environments that knowingly accept the risk can set
  `SHERLOCK_ALLOW_INSECURE_KEYRING=1` — Sherlock then proceeds with a
  loud log warning

## Prompt Injection Defense

All tool responses are scanned by `scrub_tool_response()` before
returning to the LLM. Detected patterns include:

* "ignore all previous instructions"
* "you are now" / "act as"
* "system prompt" / "override"
* Markdown/HTML injection attempts

Only the **matched span** is replaced with `[REDACTED:injection]` — the
surrounding log evidence is preserved — and responses carry a
`_redactions` count so agents know content was removed.

## Webhook Output Safety

The `post_report_to_webhook` tool and the `sherlock-digest --post` flag
deliver reports to Slack/Teams. The webhook URL comes **only** from
operator-set environment variables (`SHERLOCK_SLACK_WEBHOOK_URL` /
`SHERLOCK_TEAMS_WEBHOOK_URL`); neither surface accepts a URL argument, so
a prompt-injected model cannot redirect report content to an attacker
endpoint. Non-https values are ignored.

## Audit Logging

Every tool invocation is logged to `.sherlock/logs/audit.log`
(JSON lines, rotating 10MB × 10) with:

* Timestamp
* Tool name
* Arguments (API keys redacted)
* Success/failure status
* Execution duration

## Data at Rest

Everything under `.sherlock/` (intelligence cache, dependency graphs,
account memory, incident history, logs) is **unencrypted JSON on disk**,
scoped to entity names, metrics, and investigation summaries — no API
keys. Treat the directory with the same sensitivity as your New Relic
read access: keep the checkout on an encrypted volume and out of shared
folders. Set `SHERLOCK_HOME` to relocate it.

## How to Revoke Access

If you suspect an API key has been compromised, or want to remove the MCP
server's access to a New Relic account:

1. **Rotate the API key in New Relic.** Go to
   [one.newrelic.com](https://one.newrelic.com) **→ User menu → API keys**
   and delete or regenerate the key. This immediately invalidates all
   sessions using the old key.
2. **Delete the local profile:**
   ```shell
   python -m sherlock.scripts.cli --tool list_profiles   # find the profile name
   python -c "import keyring; keyring.delete_password('sherlock', '<profile_name>')"
   ```
3. **Clear the on-disk state** so no stale data remains:
   ```shell
   rm -rf .sherlock/cache/ .sherlock/deps/
   ```
4. **Review the audit log** at `.sherlock/logs/audit.log` to verify which
   tools were called and when.

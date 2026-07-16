# Sherlock

Intelligent New Relic investigation MCP for GitHub Copilot and any
MCP-compatible AI client.

Like the detective, Sherlock investigates incidents by gathering clues
from every available source — APM, distributed traces, logs, Kubernetes,
synthetic monitors, alerts, and deployment history — then synthesizes
them into a clear diagnosis with prioritized recommendations. All from a
single natural-language prompt.

A **production-grade, multi-tenant MCP server** exposing **30 read-only
tools** over the NerdGraph GraphQL API, plus a 7-agent GitHub Copilot
investigation team.

## Highlights

* **One-call investigations** — `investigate_service` sweeps golden
  signals (with last-week baselines), recent changes (correlated against
  the error onset), logs (with drain-style pattern clustering), K8s,
  incidents, dependencies, and synthetics concurrently server-side
* **"What changed?"** — deploy markers + K8s rollouts on one timeline:
  *"deploy X landed 4m before the error spike — prime suspect"*
* **Trace forensics** — find the slowest distributed traces, then the
  **hot span**: *"72% of this trace is one SELECT in payment-svc"*
* **Incident memory** — persistent history recalls *"this looks like the
  incident from 3 days ago; root cause was X"*
* **Team output** — post reports to Slack/Teams; a proactive
  `sherlock-digest` cron sweep flags unhealthy services before anyone
  opens an incident
* **Read-only & hardened** — mutations blocked by GraphQL parse, NRQL as
  typed variables, prompt-injection scrubbing, keychain-only credentials,
  audit logging

## Quickstart

```shell
git clone https://github.com/ekbotesrinath/sherlock.git sherlock
cd sherlock
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
make install                     # pip install -e ".[dev]"
make connect                     # save credentials to the OS keychain
make run                         # start the MCP server (python -m sherlock.main)
```

Point your MCP client at `python -m sherlock.main` (full setup for
VS Code/Copilot, Claude Desktop, and Cursor in
[docs/installation.md](docs/installation.md)), then:

```
@sherlock investigate the checkout service — it seems slow
@sherlock what changed in the last 4 hours?
@sherlock why are some checkout requests taking 5 seconds?
@sherlock have we seen this before?
@sherlock post that report to the team channel
```

## Documentation

| Doc | Contents |
| --- | -------- |
| [docs/installation.md](docs/installation.md) | Prerequisites, macOS/Windows setup, MCP client configuration, team sharing, environment variables |
| [docs/tools.md](docs/tools.md) | All 30 tools with descriptions and response conventions |
| [docs/architecture.md](docs/architecture.md) | Layers, agent team (sweep-first flow), project structure, design patterns, adding a tool |
| [docs/features.md](docs/features.md) | Workflows and feature deep-dives: baselines, change correlation, traces, log patterns, incident memory, digest, synthetics, dependencies, ASB, OTel, fallback protocol |
| [docs/security.md](docs/security.md) | Read-only enforcement, credential/keyring hardening, injection defense, webhook safety, audit logging, revoking access |
| [docs/development.md](docs/development.md) | Tests, lint/type checks, CI, make targets, troubleshooting |

## Requirements

Python 3.11+, a New Relic User API key (`NRAK-...`), and an OS keychain
(macOS Keychain / Windows Credential Locker / Linux Secret Service).

## License

MIT

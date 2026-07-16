# Installation & Configuration

## Prerequisites

| Requirement            | Minimum Version                                                    |
| ---------------------- | ------------------------------------------------------------------ |
| Python                 | 3.11+                                                              |
| pip                    | 23.0+                                                              |
| New Relic User API Key | `NRAK-...` format                                                  |
| OS Keychain            | macOS Keychain / Windows Credential Locker / Linux Secret Service  |
| VS Code (optional)     | 1.113+ recommended (nested subagents and CLI MCP support)          |

> **Keychain required:** Sherlock refuses to store or load API keys through weak
> keyring backends (headless-Linux plaintext fallbacks). See
> [security.md — Keyring Backend Hardening](security.md#keyring-backend-hardening).

## macOS

```shell
# 1. Install Python 3.11+ via pyenv
brew install pyenv
pyenv install 3.11.9
pyenv local 3.11.9

# 2. Clone the repository
cd ~/Documents
git clone https://github.com/ekbotesrinath/sherlock.git sherlock
cd sherlock

# 3. Create virtual environment
python -m venv .venv
source .venv/bin/activate

# 4. Install in development mode
make install
# or: pip install -e ".[dev]"

# 5. Verify the connection
make connect
# Prompts for Account ID, API Key, Region
# Validates against NerdGraph and saves to OS keychain

# 6. Run the MCP server
make run
# or: python -m sherlock.main
```

## Windows

### Quick Setup (automated)

1. Clone the repository:

```powershell
cd $env:USERPROFILE\Documents
git clone <repo-url> sherlock
cd sherlock
```

2. Right-click **`setup.bat`** and select **Run as administrator**.
   The script installs Chocolatey, Make, Python (latest), creates the
   virtual environment, and installs all dependencies. It skips anything
   already installed.

3. Open a **new terminal** (regular, not admin), then:

```powershell
cd $env:USERPROFILE\Documents\sherlock
.venv\Scripts\activate.bat
make connect
```

`make connect` prompts for:

* **Account ID** — your New Relic account ID (numeric)
* **API Key** — a User API key in `NRAK-...` format ([create one here](https://one.newrelic.com/api-keys))
* **Region** — `US` or `EU`

Credentials are saved securely in Windows Credential Locker.

### Manual Setup (step-by-step)

<details>
<summary>Click to expand manual steps</summary>

**Install Chocolatey** — open PowerShell as Administrator:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

**Install Make and Python** — close and reopen PowerShell, then:

```powershell
choco install make -y
choco install python -y
```

**Clone, create venv, install** — close and reopen PowerShell:

```powershell
cd $env:USERPROFILE\Documents
git clone https://github.com/ekbotesrinath/sherlock.git sherlock
cd sherlock
python -m venv .venv
.venv\Scripts\Activate.ps1
make install
```

> **Note:** If you get an execution-policy error on `Activate.ps1`, run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` first.

**Connect and save profile:**

```powershell
make connect
```

</details>

## MCP Configuration

After setup, add the Sherlock MCP server to your AI client's configuration.
The server runs as a Python module: `python -m sherlock.main`.

**VS Code / GitHub Copilot** — add to your `settings.json` (or use the
pre-configured `.vscode/settings.json` in this repo):

```json
{
  "github.copilot.chat.mcpServers": {
    "sherlock": {
      "command": "C:\\Users\\<your-username>\\Documents\\sherlock\\.venv\\Scripts\\python.exe",
      "args": ["-m", "sherlock.main"],
      "cwd": "C:\\Users\\<your-username>\\Documents\\sherlock"
    }
  }
}
```

> **VS Code 1.113+:** Sherlock supports nested subagent chains, configurable
> thinking effort per investigation, and CLI agent invocation.

**Claude Desktop** — add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sherlock": {
      "command": "C:\\Users\\<your-username>\\Documents\\sherlock\\.venv\\Scripts\\python.exe",
      "args": ["-m", "sherlock.main"],
      "cwd": "C:\\Users\\<your-username>\\Documents\\sherlock"
    }
  }
}
```

**Cursor** — add to `.cursor/mcp.json` in your project root (or global config):

```json
{
  "mcpServers": {
    "sherlock": {
      "command": "C:\\Users\\<your-username>\\Documents\\sherlock\\.venv\\Scripts\\python.exe",
      "args": ["-m", "sherlock.main"],
      "cwd": "C:\\Users\\<your-username>\\Documents\\sherlock"
    }
  }
}
```

> **Important:** Replace `<your-username>` with your actual username. Use the
> full path to the `.venv` Python executable so the MCP client uses the
> correct virtual environment. On macOS/Linux the executable is
> `.venv/bin/python`.

### Verify everything works

```shell
make run
# or: python -m sherlock.main
```

Then open your AI client and try:

```
@sherlock list all profiles
@sherlock how is my-service performing?
```

### VS Code Integration

The server is pre-configured in `.vscode/settings.json`. After installation:

1. Open the `sherlock` folder in VS Code
2. Ensure the Python extension is installed
3. The MCP server will appear under **GitHub Copilot → MCP Servers**
4. Use `@sherlock` in Copilot Chat to interact with your telemetry

## Sharing with Teammates (Private Repository)

You do **not** need to make this repository public for your team to use it.

### Option 1 — GitHub Collaborators (personal repositories)

1. Go to **Settings → Collaborators** on GitHub
2. Click **Add people** and enter each teammate's GitHub username or email
3. Each teammate accepts the invitation and clones the private repository:

```shell
# HTTPS (no SSH setup required)
git clone https://github.com/<your-username>/sherlock.git sherlock

# SSH (requires SSH key configured on the teammate's GitHub account)
git clone git@github.com:<your-username>/sherlock.git sherlock
```

### Option 2 — GitHub Teams (organization repositories)

1. Go to **Settings → Collaborators and teams**
2. Add an existing team (or create one) with at least **Read** access
3. All members of that team can clone and install Sherlock

### Option 3 — Install from a private repository using a PAT

Teammates can install without cloning by using a
[GitHub Personal Access Token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
with `repo` scope:

```shell
# Set the token as an environment variable to avoid storing it in shell history
export GITHUB_PAT=<PAT>
pip install "git+https://${GITHUB_PAT}@github.com/<your-username>/sherlock.git"
```

### Option 4 — Distribute a pre-built wheel

```shell
# Build the wheel (run once by the maintainer)
pip install build
python -m build --wheel

# Share the generated file, e.g.:
#   dist/sherlock-1.0.0-py3-none-any.whl

# Teammates install it with (from the directory containing the .whl):
pip install sherlock-1.0.0-py3-none-any.whl
```

The wheel packages the whole `sherlock/` package and installs the console
scripts `sherlock` (MCP server), `sherlock-cli`, `sherlock-relearn`,
`sherlock-validate`, and `sherlock-digest`.

> **Tip:** use Option 1 or 2 if you want teammates to receive updates via
> `git pull`. Use Option 3 or 4 for zero-friction installation without
> GitHub access.

## Configuration

### Profiles (recommended)

Credentials belong in OS-keychain-backed profiles, created once via:

```shell
make connect          # interactive: account id, API key, region
# or: python -m sherlock.scripts.validate_connection
```

Profile metadata (no secrets) lives in `.sherlock/profiles.json`; the API
key goes to the OS keychain under service name `sherlock`. Connect from
chat with `@sherlock connect to <profile-name>` — never paste an API key
into the conversation.

### Environment Variables (fallback)

Copy `.env.example` to `.env` and fill in:

```shell
# Required (only when not using profiles)
NEW_RELIC_ACCOUNT_ID=123456
NEW_RELIC_API_KEY=NRAK-xxxxxxxxxxxxxxxxxxxx
NEW_RELIC_REGION=US          # US or EU

# Optional
LOG_LEVEL=INFO               # DEBUG, INFO, WARNING, ERROR
CACHE_TTL_SECONDS=1800       # Intelligence cache TTL (default: 30 min)

# Team-scale features (optional)
SHERLOCK_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SHERLOCK_TEAMS_WEBHOOK_URL=https://<tenant>.webhook.office.com/...
SHERLOCK_HOME=/path/for/.sherlock/state   # defaults to the repo root
```

> **Warning:** a plaintext API key in `.env` is a standing exposure — the
> server logs a startup warning when it finds one. Prefer profiles.

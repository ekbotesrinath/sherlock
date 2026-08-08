"""
Multi-tenant credential manager for Sherlock.

Stores profile metadata in ~/.sherlock/profiles.json and API keys
securely in the system keychain via the keyring library. API keys are
never written to disk in plain text.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Literal

import httpx
import keyring
from pydantic import BaseModel, Field, field_serializer, field_validator

from sherlock import paths
from sherlock.core.exceptions import CredentialError

logger = logging.getLogger("sherlock.credentials")

# Keyring service name used for all stored API keys.
KEYRING_SERVICE = "sherlock"

# ── Keyring backend hardening (S5) ───────────────────────────────────────
# On headless Linux, keyring silently falls back to backends that either
# discard secrets (fail/null) or write them to a world-readable file
# (keyrings.alt plaintext). Storing/loading API keys through those defeats
# the whole point of the keychain — fail loudly instead.

WEAK_KEYRING_PREFIXES = (
    "keyring.backends.fail",
    "keyring.backends.null",
    "keyrings.alt.",
)

# Operator escape hatch for environments that knowingly accept the risk.
ALLOW_INSECURE_KEYRING_ENV = "SHERLOCK_ALLOW_INSECURE_KEYRING"

_insecure_keyring_warned = False


def _backend_names(backend: object) -> list[str]:
    """Fully-qualified class names of a backend (expanding chainers)."""
    names: list[str] = []
    inner = getattr(backend, "backends", None)
    try:
        if inner:
            names = [f"{type(b).__module__}.{type(b).__name__}" for b in inner]
    except TypeError:
        names = []
    if not names:
        names = [f"{type(backend).__module__}.{type(backend).__name__}"]
    return names


def describe_keyring_backend() -> dict:
    """Inspect the resolved keyring backend.

    Returns:
        {"names": [...], "weak": [...], "secure": bool} — weak lists the
        backend classes that store secrets without OS-level protection.
    """
    names = _backend_names(keyring.get_keyring())
    weak = [name for name in names if name.startswith(WEAK_KEYRING_PREFIXES)]
    return {"names": names, "weak": weak, "secure": not weak}


def ensure_secure_keyring(operation: str) -> None:
    """Fail loudly when the keyring backend is weak (S5).

    Args:
        operation: What the caller is about to do ("store"/"load"), used
            in the error message.

    Raises:
        CredentialError: When the resolved backend is a known-weak
            fallback and SHERLOCK_ALLOW_INSECURE_KEYRING is not set.
    """
    global _insecure_keyring_warned
    info = describe_keyring_backend()
    if info["secure"]:
        return

    weak = ", ".join(info["weak"])
    if os.environ.get(ALLOW_INSECURE_KEYRING_ENV, "").strip().lower() in ("1", "true", "yes"):
        if not _insecure_keyring_warned:
            logger.warning(
                "INSECURE keyring backend in use (%s) — API keys are NOT "
                "protected by an OS keychain. Allowed by %s.",
                weak,
                ALLOW_INSECURE_KEYRING_ENV,
            )
            _insecure_keyring_warned = True
        return

    raise CredentialError(
        f"Refusing to {operation} API keys: the keyring backend ({weak}) "
        "stores secrets without OS-level protection (or not at all). This "
        "usually means no OS keychain is available — common on headless "
        "Linux. Configure a real backend (Windows Credential Locker, macOS "
        "Keychain, GNOME Keyring/KWallet via SecretService), or set "
        f"{ALLOW_INSECURE_KEYRING_ENV}=1 to knowingly accept the risk.",
        account_id="",
        http_status=None,
    )


# Base directory for all NewRelic MCP configuration.
CONFIG_DIR = paths.CONFIG_DIR

# Path to the profiles metadata file.
PROFILES_FILE = CONFIG_DIR / "profiles.json"

# NerdGraph endpoints by region.
NERDGRAPH_ENDPOINTS: dict[str, str] = {
    "US": "https://api.newrelic.com/graphql",
    "EU": "https://api.eu.newrelic.com/graphql",
}

# Validation query — minimal query to verify credentials.
VALIDATION_QUERY = """
{
  actor {
    user {
      name
      email
    }
    account(id: %s) {
      name
    }
  }
}
"""


class Credentials(BaseModel):
    """New Relic API credentials for a single account."""

    account_id: str
    api_key: str = Field(exclude=True)
    region: Literal["US", "EU"] = "US"

    @field_validator("account_id")
    @classmethod
    def _account_id_must_be_numeric(cls, v: str) -> str:
        """Reject non-numeric account IDs — they end up in GraphQL documents."""
        v = v.strip()
        if not v.isdigit():
            raise ValueError(f"account_id must be numeric, got {v!r}")
        return v

    @field_serializer("api_key")
    def _redact_key(self, v: str) -> str:
        """Never serialize the real key."""
        return self.redacted_key

    @property
    def endpoint(self) -> str:
        """Return the NerdGraph endpoint URL for this credential's region."""
        return NERDGRAPH_ENDPOINTS[self.region]

    @property
    def redacted_key(self) -> str:
        """Return a redacted version of the API key for display/logging."""
        if len(self.api_key) <= 8:
            return "****"
        return f"{self.api_key[:4]}-***-{self.api_key[-4:]}"


def resolve_profile_credentials(
    profile_name: str | None = None,
    account_id: str | None = None,
) -> Credentials:
    """Resolve Credentials for a one-off cross-account call.

    Lets a single tool call (e.g. ``run_nrql_query``) target a different
    account than the active ``AccountContext`` singleton, without calling
    ``connect_account`` first and disturbing what's currently active —
    the query engine already reconnects per-account reliably; this just
    avoids paying a full connect_account round-trip for one query.

    Resolution order:
      1. ``profile_name`` given — load directly from the keychain.
      2. ``account_id`` only — look up the saved profile whose
         ``account_id`` matches, then load it.

    Args:
        profile_name: Saved profile name to load credentials from.
        account_id: Account ID to resolve via a matching saved profile.

    Returns:
        Credentials for the resolved profile.

    Raises:
        CredentialError: Neither argument was given, or no matching
            profile/keychain entry could be found.
    """
    manager = CredentialManager()
    if profile_name:
        return manager.load_profile(profile_name)

    if account_id:
        target = str(account_id).strip()
        for meta in manager.list_profiles():
            if str(meta.get("account_id", "")) == target:
                return manager.load_profile(str(meta.get("name", "")))
        raise CredentialError(
            f"No saved profile found for account_id={account_id!r}. "
            "Pass profile_name instead, or save a profile for that account "
            "once via the sherlock-validate CLI.",
            account_id=target,
            http_status=None,
        )

    raise CredentialError(
        "resolve_profile_credentials requires profile_name or account_id.",
        account_id="",
        http_status=None,
    )


class CredentialManager:
    """Manages New Relic credential profiles with secure keychain storage."""

    def __init__(self) -> None:
        """Initialize the credential manager and ensure config directories exist."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not PROFILES_FILE.exists():
            PROFILES_FILE.write_text("[]", encoding="utf-8")

    def _load_profiles_data(self) -> list[dict]:
        """Load raw profile metadata from disk.

        Returns:
            List of profile metadata dicts.
        """
        try:
            data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save_profiles_data(self, profiles: list[dict]) -> None:
        """Write profile metadata to disk.

        Args:
            profiles: List of profile metadata dicts to persist.
        """
        PROFILES_FILE.write_text(json.dumps(profiles, indent=2, default=str), encoding="utf-8")

    def save_profile(
        self,
        profile_name: str,
        account_id: str,
        api_key: str,
        region: str = "US",
    ) -> dict:
        """Save a new credential profile.

        Stores metadata in profiles.json and the API key in the system keychain.

        Args:
            profile_name: Unique human-readable name for this profile.
            account_id: New Relic account ID.
            api_key: New Relic User API key.
            region: 'US' or 'EU'.

        Returns:
            Dict with profile metadata.
        """
        region = region.upper()
        if region not in ("US", "EU"):
            region = "US"

        # Store API key in keychain — refuse weak backends (S5).
        ensure_secure_keyring("store")
        keyring.set_password(KEYRING_SERVICE, profile_name, api_key)

        # Update profiles metadata.
        profiles = self._load_profiles_data()
        # Remove existing profile with same name.
        profiles = [p for p in profiles if p.get("name") != profile_name]
        profile_meta = {
            "name": profile_name,
            "account_id": account_id,
            "region": region,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        profiles.append(profile_meta)
        self._save_profiles_data(profiles)

        logger.info("Profile saved: %s (account %s, region %s)", profile_name, account_id, region)
        return profile_meta

    def load_profile(self, profile_name: str) -> Credentials:
        """Load credentials for a named profile.

        Args:
            profile_name: The profile name to load.

        Returns:
            Credentials instance with account_id, api_key, and region.

        Raises:
            CredentialError: If the profile or its keychain entry is not found.
        """
        profiles = self._load_profiles_data()
        meta = next((p for p in profiles if p.get("name") == profile_name), None)
        if not meta:
            raise CredentialError(
                f"Profile '{profile_name}' not found.",
                account_id="",
                http_status=None,
            )

        ensure_secure_keyring("load")
        api_key = keyring.get_password(KEYRING_SERVICE, profile_name)
        if not api_key:
            raise CredentialError(
                f"API key for profile '{profile_name}' not found in keychain.",
                account_id=meta.get("account_id", ""),
                http_status=None,
            )

        return Credentials(
            account_id=meta["account_id"],
            api_key=api_key,
            region=meta.get("region", "US"),
        )

    def delete_profile(self, profile_name: str) -> bool:
        """Delete a credential profile and its keychain entry.

        Args:
            profile_name: The profile name to delete.

        Returns:
            True if the profile was found and deleted.
        """
        profiles = self._load_profiles_data()
        new_profiles = [p for p in profiles if p.get("name") != profile_name]
        deleted = len(new_profiles) < len(profiles)

        if deleted:
            self._save_profiles_data(new_profiles)
            try:
                keyring.delete_password(KEYRING_SERVICE, profile_name)
            except keyring.errors.PasswordDeleteError:
                pass
            logger.info("Profile deleted: %s", profile_name)

        return deleted

    def list_profiles(self) -> list[dict]:
        """List all saved credential profiles (without API keys).

        Returns:
            List of profile metadata dicts with name, account_id, region, created_at.
        """
        return self._load_profiles_data()

    async def validate_credentials(self, account_id: str, api_key: str, region: str = "US") -> dict:
        """Validate credentials by making a test NerdGraph query.

        Args:
            account_id: New Relic account ID to validate.
            api_key: New Relic User API key.
            region: 'US' or 'EU'.

        Returns:
            Dict with valid, user_name, account_name, and error fields.
        """
        if not str(account_id).strip().isdigit():
            return {
                "valid": False,
                "user_name": "",
                "account_name": "",
                "error": f"account_id must be numeric, got {account_id!r}.",
            }

        endpoint = NERDGRAPH_ENDPOINTS.get(region.upper(), NERDGRAPH_ENDPOINTS["US"])
        query = VALIDATION_QUERY % int(str(account_id).strip())

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    endpoint,
                    json={"query": query},
                    headers={"API-Key": api_key, "Content-Type": "application/json"},
                )

            if resp.status_code == 401:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": "Invalid API key (HTTP 401).",
                }

            if resp.status_code == 403:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": "Forbidden — key lacks required permissions (HTTP 403).",
                }

            if resp.status_code != 200:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": f"Unexpected HTTP {resp.status_code}.",
                }

            body = resp.json()
            errors = body.get("errors")
            if errors:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": errors[0].get("message", "Unknown NerdGraph error."),
                }

            actor = body.get("data", {}).get("actor", {})
            user = actor.get("user", {})
            account = actor.get("account", {})

            return {
                "valid": True,
                "user_name": user.get("name", ""),
                "account_name": account.get("name", ""),
                "error": "",
            }

        except httpx.TimeoutException:
            return {
                "valid": False,
                "user_name": "",
                "account_name": "",
                "error": "Connection timed out.",
            }
        except httpx.ConnectError:
            return {
                "valid": False,
                "user_name": "",
                "account_name": "",
                "error": "Could not connect to New Relic API.",
            }
        except Exception as exc:
            return {
                "valid": False,
                "user_name": "",
                "account_name": "",
                "error": f"Validation failed: {type(exc).__name__}",
            }

"""
Per-call account targeting for Sherlock tools.

The active account lives in the AccountContext singleton and is switched
with connect_account. That model is right for single-account
investigations but forces a full ~5s reconnect round-trip for every hop
of a cross-account correlation (e.g. request enters account A, lands in
account B). resolve_call_target() lets one tool call address another
account directly — credentials come from the saved keyring profile, and
any cached intelligence for that account is used opportunistically —
WITHOUT disturbing the active context. The singleton stays the default
for every call that doesn't ask otherwise.
"""

import logging

from sherlock.client.newrelic import NerdGraphClient, get_client
from sherlock.core.cache import IntelligenceCache
from sherlock.core.context import AccountContext
from sherlock.core.credentials import CredentialManager, Credentials, resolve_profile_credentials
from sherlock.core.intelligence_models import AccountIntelligence

logger = logging.getLogger("sherlock.target")

# Module-level cache handle — disk-backed, so it sees intelligence written
# by connect_account regardless of which IntelligenceCache instance wrote it.
_cache = IntelligenceCache()


def resolve_call_target(
    account_id: str | None = None,
    profile: str | None = None,
) -> tuple[Credentials, AccountIntelligence | None, NerdGraphClient, bool]:
    """Resolve which account one tool call should run against.

    With neither argument, returns the active AccountContext account —
    the unchanged default path. With ``account_id`` or ``profile``,
    resolves credentials for that account from saved keyring profiles
    for this call only; the active context is not touched.

    Args:
        account_id: Optional account ID to target (resolved via a saved
            profile with a matching account_id).
        profile: Optional saved profile name to target. Takes precedence
            over account_id when both are given.

    Returns:
        Tuple of (credentials, intelligence-or-None, client, overridden):
        intelligence is None when the target account has never been
        learned/cached; overridden is True when an override argument
        actually redirected the call away from needing the active context.

    Raises:
        NotConnectedError: No override given and no account is active.
        CredentialError: Override given but no matching saved profile.
    """
    ctx = AccountContext()

    if not account_id and not profile:
        active_credentials, active_intelligence = ctx.get_active()
        return active_credentials, active_intelligence, get_client(), False

    # Map a profile name to its account_id from metadata only (no keychain
    # access) so the active-account shortcut below can work for both forms.
    target_account_id = str(account_id).strip() if account_id else ""
    if profile:
        for meta in CredentialManager().list_profiles():
            if meta.get("name") == profile:
                target_account_id = str(meta.get("account_id", "")).strip()
                break

    # Shortcut: the override names the account that is already active.
    if target_account_id and ctx.is_connected():
        active_credentials, active_intelligence = ctx.get_active()
        if active_credentials.account_id == target_account_id:
            return active_credentials, active_intelligence, get_client(), True

    credentials = resolve_profile_credentials(profile_name=profile, account_id=account_id)

    intelligence: AccountIntelligence | None = None
    cached = _cache.get(credentials.account_id) or _cache.get_stale(credentials.account_id)
    if cached:
        try:
            intelligence = AccountIntelligence(**cached)
        except Exception as exc:
            logger.warning(
                "Cached intelligence for account %s failed to load: %s",
                credentials.account_id,
                exc,
            )

    logger.info(
        "Per-call account override: targeting account %s (intelligence %s)",
        credentials.account_id,
        "cached" if intelligence else "unavailable",
    )
    return credentials, intelligence, NerdGraphClient(credentials), True

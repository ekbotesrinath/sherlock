"""
Per-domain account-intelligence learners.

Each learner exposes ``async def learn(runner, intel)`` which fires its
domain's discovery queries concurrently (its own asyncio.gather) and fills
its slice of AccountIntelligence in place. Learners never raise — every
processing block is individually guarded, matching the old single-gather
behavior where any query could fail without aborting the learn.
"""

from sherlock.core.learners import (  # noqa: F401
    alerts,
    apm,
    asb,
    entities,
    k8s,
    logs,
    synthetics,
)
from sherlock.core.learners.base import QueryRunner  # noqa: F401

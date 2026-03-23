"""
Multi-datasource connection context for Sherlock.

Holds optional live connections to Prometheus, Datadog, Azure Monitor, Loki,
and Application Insights. Separate from AccountContext which is New Relic-specific.
"""

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from client.app_insights import AppInsightsClient
    from client.azure_monitor import AzureMonitorClient
    from client.datadog import DatadogClient
    from client.loki import LokiClient
    from client.prometheus import PrometheusClient

logger = logging.getLogger("sherlock.datasource_context")


class DataSourceContext:
    """Thread-safe singleton holding active connections to external data sources."""

    _instance: "DataSourceContext | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "DataSourceContext":
        """Ensure only one DataSourceContext instance exists."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._prometheus = None
                cls._instance._datadog = None
                cls._instance._azure_monitor = None
                cls._instance._loki = None
                cls._instance._app_insights = None
            return cls._instance

    # ── Prometheus ────────────────────────────────────────────────────

    def set_prometheus(self, client: "PrometheusClient") -> None:
        """Store an active Prometheus client.

        Args:
            client: Connected PrometheusClient instance.
        """
        with self._lock:
            self._prometheus = client
        logger.info("Prometheus connection stored: %s", client.base_url)

    def get_prometheus(self) -> "PrometheusClient | None":
        """Return the active Prometheus client, or None if not connected."""
        return self._prometheus

    def clear_prometheus(self) -> None:
        """Remove the active Prometheus connection."""
        with self._lock:
            self._prometheus = None
        logger.info("Prometheus connection cleared")

    # ── Datadog ───────────────────────────────────────────────────────

    def set_datadog(self, client: "DatadogClient") -> None:
        """Store an active Datadog client.

        Args:
            client: Connected DatadogClient instance.
        """
        with self._lock:
            self._datadog = client
        logger.info("Datadog connection stored: site=%s", client.site)

    def get_datadog(self) -> "DatadogClient | None":
        """Return the active Datadog client, or None if not connected."""
        return self._datadog

    def clear_datadog(self) -> None:
        """Remove the active Datadog connection."""
        with self._lock:
            self._datadog = None
        logger.info("Datadog connection cleared")

    # ── Azure Monitor ─────────────────────────────────────────────────

    def set_azure_monitor(self, client: "AzureMonitorClient") -> None:
        """Store an active Azure Monitor client.

        Args:
            client: Connected AzureMonitorClient instance.
        """
        with self._lock:
            self._azure_monitor = client
        logger.info(
            "Azure Monitor connection stored: subscription=%s",
            client.subscription_id,
        )

    def get_azure_monitor(self) -> "AzureMonitorClient | None":
        """Return the active Azure Monitor client, or None if not connected."""
        return self._azure_monitor

    def clear_azure_monitor(self) -> None:
        """Remove the active Azure Monitor connection."""
        with self._lock:
            self._azure_monitor = None
        logger.info("Azure Monitor connection cleared")

    # ── Loki ────────────────────────────────────────────────

    def set_loki(self, client: "LokiClient") -> None:
        """Store an active Loki client.

        Args:
            client: Connected LokiClient instance.
        """
        with self._lock:
            self._loki = client
        logger.info("Loki connection stored: %s", client.base_url)

    def get_loki(self) -> "LokiClient | None":
        """Return the active Loki client, or None if not connected."""
        return self._loki

    def clear_loki(self) -> None:
        """Remove the active Loki connection."""
        with self._lock:
            self._loki = None
        logger.info("Loki connection cleared")

    # ── Application Insights ──────────────────────────────────────────

    def set_app_insights(self, client: "AppInsightsClient") -> None:
        """Store an active Application Insights client.

        Args:
            client: Connected AppInsightsClient instance.
        """
        with self._lock:
            self._app_insights = client
        logger.info("Application Insights connection stored: app_id=%s", client.app_id)

    def get_app_insights(self) -> "AppInsightsClient | None":
        """Return the active Application Insights client, or None if not connected."""
        return self._app_insights

    def clear_app_insights(self) -> None:
        """Remove the active Application Insights connection."""
        with self._lock:
            self._app_insights = None
        logger.info("Application Insights connection cleared")

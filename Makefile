.PHONY: install run test test-fast test-synthetics test-investigate test-dependencies test-discovery test-deeplinks test-domain-tools test-session-memory test-structured-output test-frustration test-cov lint format logs audit connect relearn cli clean
.PHONY: install run test test-fast test-synthetics test-investigate test-dependencies test-discovery test-deeplinks test-domain-tools test-monitoring-tools test-cov lint format logs audit newrelic-connect relearn cli clean azure-connect azure-connect-cli azure-connect-interactive azure-alerts azure-alerts-cli azure-alerts-interactive azure-app-insights azure-app-insights-interactive azure-logout prometheus-connect prometheus-alerts prometheus-query datadog-connect datadog-monitors datadog-logs loki-connect loki-logs grafana-connect grafana-alerts

install:
	pip install -e ".[dev]"

run:
	python main.py

test:
	pytest tests/ -v -n auto

test-fast:
	pytest tests/ -x -q -n auto

test-synthetics:
	pytest tests/test_synthetics.py -v

test-investigate:
	@echo "NOTE: investigate_service is LEGACY. Prefer agent-team architecture."
	pytest tests/test_investigate.py tests/test_discovery.py tests/test_query_builder.py -v

test-dependencies:
	pytest tests/test_dependencies_tool.py tests/test_dependency_graph.py tests/test_graph_builder.py -v

test-discovery:
	@echo "NOTE: discovery engine is DEPRECATED. Kept for backward compat."
	pytest tests/test_discovery.py -v

test-deeplinks:
	pytest tests/test_deeplinks.py -v

test-domain-tools:
	pytest tests/test_golden_signals.py tests/test_k8s.py tests/test_apm.py tests/test_logs.py tests/test_alerts.py tests/test_synthetics.py -v

test-session-memory:
	pytest tests/test_session_memory.py -v

test-structured-output:
	pytest tests/test_structured_output.py -v

test-frustration:
	pytest tests/test_frustration_detection.py -v

test-monitoring-tools:
	pytest tests/test_app_insights.py tests/test_azure_monitor.py tests/test_datadog.py tests/test_prometheus.py -v

test-cov:
	pytest tests/ -v --cov=. --cov-report=html --cov-report=term

lint:
	ruff check . && mypy .

format:
	ruff format .

logs:
	tail -f .sherlock/logs/sherlock.log | python -m json.tool

audit:
	tail -f .sherlock/logs/audit.log | python -m json.tool

newrelic-connect:
	python scripts/newrelic/validate_connection.py

relearn:
	python scripts/newrelic/cli.py --profile DFIN_AD --tool learn_account

cli:
	python scripts/newrelic/cli.py $(ARGS)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +

azure-connect:
	python scripts/azure/connect.py --tool none

azure-connect-cli:
	python scripts/azure/connect.py --auth cli --tool none

azure-connect-interactive:
	python scripts/azure/connect.py --auth interactive --tool none

azure-alerts:
	python scripts/azure/connect.py --auth interactive --tool alerts

azure-alerts-cli:
	python scripts/azure/connect.py --auth cli --tool alerts

azure-alerts-interactive:
	python scripts/azure/connect.py --auth interactive --tool alerts

azure-app-insights:
	python scripts/azure/connect.py --tool app-insights

azure-app-insights-interactive:
	python scripts/azure/connect.py --auth interactive --tool app-insights

azure-logout:
	python -c "from pathlib import Path; p = Path.home() / '.sherlock' / 'azure_token_cache.json'; p.unlink(missing_ok=True); print('Azure token cache cleared. Next connect will require a new sign-in.')"

# ── Prometheus ──────────────────────────────────────────────────────────────

prometheus-connect:
	python scripts/prometheus/connect.py --tool none

prometheus-alerts:
	python scripts/prometheus/connect.py --tool alerts

prometheus-query:
	python scripts/prometheus/connect.py --tool query --promql "$(or $(PROMQL),up)"

# ── Datadog ─────────────────────────────────────────────────────────────────

datadog-connect:
	python scripts/datadog/connect.py --tool none

datadog-monitors:
	python scripts/datadog/connect.py --tool monitors

datadog-logs:
	python scripts/datadog/connect.py --tool logs --log-query "$(or $(QUERY),status:error)" --since $(or $(SINCE),60)

# ── Loki ─────────────────────────────────────────────────────────────────────

loki-connect:
	python scripts/loki/connect.py --tool labels

loki-logs:
	python scripts/loki/connect.py --tool logs --logql "$(or $(LOGQL),{job=~\".+\"})" --since $(or $(SINCE),60)

# -- Grafana ------------------------------------------------------------------

grafana-connect:
	python scripts/grafana/connect.py --tool datasources

grafana-alerts:
	python scripts/grafana/connect.py --tool alerts $(if $(SEVERITY),--severity $(SEVERITY),) $(if $(SERVICE),--service $(SERVICE),)

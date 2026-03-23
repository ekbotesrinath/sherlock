.PHONY: install run test test-fast test-synthetics test-investigate test-dependencies test-discovery test-deeplinks test-domain-tools test-monitoring-tools test-cov lint format logs audit connect relearn cli clean azure-connect azure-connect-cli azure-connect-interactive azure-alerts azure-alerts-cli azure-alerts-interactive azure-app-insights azure-app-insights-interactive prometheus-connect prometheus-alerts prometheus-query datadog-connect datadog-monitors datadog-logs loki-connect loki-logs

install:
	pip install -e ".[dev]"

run:
	python main.py

test:
	pytest tests/ -v

test-fast:
	pytest tests/ -x -q

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

connect:
	python scripts/test_connection.py

relearn:
	python scripts/cli.py --profile DFIN_AD --tool learn_account

cli:
	python scripts/cli.py $(ARGS)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +

azure-connect:
	python scripts/connect_azure.py --tool none

azure-connect-cli:
	python scripts/connect_azure.py --auth cli --tool none

azure-connect-interactive:
	python scripts/connect_azure.py --auth interactive --tool none

azure-alerts:
	python scripts/connect_azure.py --auth interactive --tool alerts

azure-alerts-cli:
	python scripts/connect_azure.py --auth cli --tool alerts

azure-alerts-interactive:
	python scripts/connect_azure.py --auth interactive --tool alerts

azure-app-insights:
	python scripts/connect_azure.py --tool app-insights

azure-app-insights-interactive:
	python scripts/connect_azure.py --auth interactive --tool app-insights

# ── Prometheus ──────────────────────────────────────────────────────────────

prometheus-connect:
	python scripts/connect_prometheus.py --tool none

prometheus-alerts:
	python scripts/connect_prometheus.py --tool alerts

prometheus-query:
	python scripts/connect_prometheus.py --tool query --promql "$(or $(PROMQL),up)"

# ── Datadog ─────────────────────────────────────────────────────────────────

datadog-connect:
	python scripts/connect_datadog.py --tool none

datadog-monitors:
	python scripts/connect_datadog.py --tool monitors

datadog-logs:
	python scripts/connect_datadog.py --tool logs --log-query "$(or $(QUERY),status:error)" --since $(or $(SINCE),60)

# ── Loki ─────────────────────────────────────────────────────────────────────

loki-connect:
	python scripts/connect_loki.py --tool labels

loki-logs:
	python scripts/connect_loki.py --tool logs --logql "$(or $(LOGQL),{job=~\".+\"})" --since $(or $(SINCE),60)

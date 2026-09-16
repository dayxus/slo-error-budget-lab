# sloctl: setup, tests, lint and the demo pipeline.
PY ?= .venv/bin/python
CONFIG ?= examples/slo
SAMPLES ?= examples/samples
RULES_OUT ?= build/rules
REPORT_OUT ?= reports

.PHONY: help setup fixtures fixtures-check lint format test coverage validate budget burn rules report demo promtool promtool-check clean

PROMTOOL ?= build/tools/bin/promtool

help: ## List the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-16s %s\n", $$1, $$2}'

setup: ## Create .venv and install the package with its dev dependencies
	python3 -m venv .venv
	.venv/bin/python -m pip install --quiet --upgrade pip
	.venv/bin/python -m pip install --quiet --editable ".[dev]"
	@echo "ready: $(PY) -m sloctl --help"

fixtures: ## Regenerate the synthetic sample fixtures
	$(PY) scripts/make_fixtures.py --out $(SAMPLES)

fixtures-check: ## Fail if the committed fixtures differ from a fresh generation
	$(PY) scripts/make_fixtures.py --out $(SAMPLES) --check

lint: ## ruff check + ruff format --check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format: ## Apply ruff formatting
	$(PY) -m ruff format .

test: ## Run the test suite
	$(PY) -m pytest -q

coverage: ## Run the test suite with a coverage report
	$(PY) -m pytest -q --cov=sloctl --cov-report=term-missing

validate: ## Validate every SLO definition
	$(PY) -m sloctl validate --config $(CONFIG)

budget: ## Error budget table for every service
	$(PY) -m sloctl budget --config $(CONFIG) --samples $(SAMPLES)

burn: ## Multi-window burn rate table
	$(PY) -m sloctl burn --config $(CONFIG) --samples $(SAMPLES)

rules: ## Generate the Prometheus rule files
	$(PY) -m sloctl rules generate --config $(CONFIG) --out $(RULES_OUT)

report: ## Write $(REPORT_OUT)/slo-report.md
	$(PY) -m sloctl report --config $(CONFIG) --samples $(SAMPLES) --out $(REPORT_OUT)

demo: report rules ## Report plus Prometheus rules, the two artifacts the CI uploads
	@echo "artifacts: $(REPORT_OUT)/slo-report.md and $(RULES_OUT)/*.rules.yml"

promtool: ## Download the newest promtool from the official Prometheus release
	$(PY) scripts/fetch_promtool.py --out build/tools/bin

promtool-check: rules promtool ## Validate the generated rules with promtool
	$(PROMTOOL) check rules $(RULES_OUT)/*.rules.yml

clean: ## Remove build artefacts
	rm -rf build .pytest_cache .ruff_cache .coverage

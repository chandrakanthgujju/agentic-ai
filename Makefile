# Common tasks. Run `make help` for the list.
PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin
export CDK_DEFAULT_REGION ?= us-east-1

.DEFAULT_GOAL := help
.PHONY: help venv install lint format test synth diff deploy destroy diagrams clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv:
	test -d $(VENV) || $(PYTHON) -m venv $(VENV)

install: venv ## Create .venv and install CDK + dev dependencies
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt -r requirements-dev.txt

lint: ## Ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Auto-format and fix lint
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

test: ## Unit tests + CDK assertion tests
	$(BIN)/pytest

synth: ## cdk synth (bundles Lambdas, runs cdk-nag)
	PATH=$(abspath $(BIN)):$$PATH npx --yes aws-cdk@2 synth --quiet

diff: ## Show what a deploy would change
	PATH=$(abspath $(BIN)):$$PATH npx --yes aws-cdk@2 diff --all

deploy: ## Deploy all stacks to the current AWS credentials/region
	PATH=$(abspath $(BIN)):$$PATH npx --yes aws-cdk@2 deploy --all --require-approval broadening

destroy: ## Delete all stacks
	PATH=$(abspath $(BIN)):$$PATH npx --yes aws-cdk@2 destroy --all

diagrams: ## Re-render docs/architecture/*.png (needs graphviz)
	$(BIN)/python docs/architecture/render_diagrams.py

clean: ## Remove build output
	rm -rf cdk.out .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

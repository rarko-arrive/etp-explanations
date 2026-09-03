SHELL := /bin/bash

env_raw = $(shell awk '/^[[:space:]]*$(1)=/ { line=$$0; sub(/^[[:space:]]*$(1)=/, "", line); val=line } END { print val }' .env 2>/dev/null)

UV_ENV := $(shell bash scripts/expand_user_path.sh "$(call env_raw,UV_PROJECT_ENVIRONMENT)")
ifeq ($(UV_ENV),)
UV_ENV := .venv
endif
export UV_PROJECT_ENVIRONMENT := $(UV_ENV)
export VIRTUAL_ENV :=

DQT_DATA := $(shell bash scripts/expand_user_path.sh "$(call env_raw,DQT_DATA_DIR)")
ifeq ($(DQT_DATA),)
DQT_DATA := data
endif
export DQT_DATA_DIR := $(DQT_DATA)

.DEFAULT_GOAL := help
.PHONY: help install lint test explain

help: ## list targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## uv venv + Jupyter kernel
	bash scripts/install.sh

lint: ## ruff
	uv run ruff check src/ scripts/ --fix

test: ## pytest
	uv run pytest tests/ -q

explain: ## explain one load  [LOAD=9199475 | RANK=3]
	uv run python scripts/explain_etp_load.py \
		$(if $(LOAD),--load $(LOAD),--rank $(or $(RANK),1)) \
		--data-dir $(DQT_DATA_DIR)

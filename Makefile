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
.PHONY: help install lint test pre-commit pre-commit-install explain explain-ui explain-serve docker-build docker-run

# Support: make explain LOAD=9199475  OR  make explain 9199475
ifneq ($(filter explain explain-ui,$(MAKECMDGOALS)),)
  _explain_extra := $(filter-out explain explain-ui,$(MAKECMDGOALS))
  ifneq ($(_explain_extra),)
    LOAD := $(firstword $(_explain_extra))
  endif
endif

help: ## list targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## uv venv + Jupyter kernel + pre-commit hooks
	bash scripts/install.sh

pre-commit-install: ## install git pre-commit hook
	uv run pre-commit install

pre-commit: ## run all pre-commit hooks on entire repo
	uv run pre-commit run --all-files

lint: ## ruff (check + fix); mirrors pre-commit ruff hooks
	uv run ruff check src/ scripts/ app/ tests/ --fix --unsafe-fixes
	uv run ruff format src/ scripts/ app/ tests/

sql-lint: ## sqlfluff on SQL/
	uv run sqlfluff lint SQL/

test: ## pytest
	uv run pytest tests/ -q

explain: ## explain one load  [LOAD=9199475 | RANK=3 | make explain 9199475]
	uv run python scripts/explain_etp_load.py \
		$(if $(LOAD),--load $(LOAD),--rank $(or $(RANK),1)) \
		--data-dir $(DQT_DATA_DIR)

explain-ui: ## static HTML explainer  [LOAD=6963033 | RANK=1 | make explain-ui 6963033]
	uv run python scripts/explain_ui.py \
		$(if $(LOAD),--load $(LOAD),--rank $(or $(RANK),1)) \
		--data-dir $(DQT_DATA_DIR) --open

explain-serve: ## interactive explainer dashboard  [PORT=8765]
	uv run python scripts/explain_serve.py \
		--port $(or $(PORT),8765) \
		--data-dir $(DQT_DATA_DIR)

docker-build: ## production image  [IMAGE=etp-explainer:local]
	bash scripts/docker_build.sh $(or $(IMAGE),etp-explainer:local)

docker-run: ## run image locally  [DQT_DATA=../etp-dqt/data]
	DQT_DATA=$(or $(DQT_DATA),data) docker compose up --build

# Swallow bare loadnumbers passed as goals (see _explain_extra above).
ifneq ($(_explain_extra),)
.PHONY: $(_explain_extra)
$(_explain_extra):
	@:
endif

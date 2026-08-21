PYTHON ?= python3.11
COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help run test check logs

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'  run    Run sfbot locally using .env' \
		'  test   Run the unit test suite' \
		'  check  Run tests and compile Python sources' \
		'  logs   Follow the production container logs' \
		'' \
		'Overrides: PYTHON=python3.12 COMPOSE="docker compose"'

run:
	@test -f .env || { echo 'Missing .env; create it with TELEGRAM_BOT_TOKEN first.' >&2; exit 1; }
	@set -a; . ./.env; set +a; exec $(PYTHON) -m sfbot

test:
	$(PYTHON) -m unittest discover -s tests -v

check: test
	$(PYTHON) -m compileall -q sfbot tests

logs:
	$(COMPOSE) logs --tail=100 -f sfbot

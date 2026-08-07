# Bitcoin Bot developer and release verification helpers.
SHELL := /bin/bash
PYTHON ?= python3

.PHONY: verify test audit audit-ledgers manifest health secretscan compose backtest download-data

verify:
	PYTHON="$(PYTHON)" bash deploy/verify_release.sh

test:
	$(PYTHON) -m pytest -q tests

audit:
	$(PYTHON) -m pip_audit -r requirements.services.lock --strict
	$(PYTHON) -m pip_audit -r monitoring/requirements-monitoring.lock --strict

audit-ledgers:
	$(PYTHON) scripts/build_audit_ledgers.py --check

manifest:
	$(PYTHON) scripts/build_manifest.py && $(PYTHON) scripts/verify_manifest.py

health:
	bash scripts/healthcheck.sh

secretscan:
	$(PYTHON) tests/secret_scan.py

compose:
	FREQTRADE_API_PASSWORD='GENERATE_CI_ONLY_PASSWORD_000000' \
	FREQTRADE_API_JWT_SECRET='GENERATE_CI_ONLY_JWT_SECRET_00000000000' \
	FREQTRADE_API_WS_TOKEN='GENERATE_CI_ONLY_WS_TOKEN_0000000000000' \
	SIGNAL_HMAC_KEY='GENERATE_CI_ONLY_SIGNAL_HMAC_00000000000' \
	COMMAND_HMAC_KEY='GENERATE_CI_ONLY_COMMAND_HMAC_000000000' \
	TELEGRAM_BOT_TOKEN='123456789:GENERATE_CI_ONLY_BOT_TOKEN_000000000' \
	TELEGRAM_OWNER_CHAT_ID='1' \
	SIDECAR_RELEASE_HASH='0000000000000000000000000000000000000000000000000000000000000000' \
	DEPLOYED_RELEASE_HASH='0000000000000000000000000000000000000000000000000000000000000000' \
	DEPLOYED_CONFIG_SHA256='1111111111111111111111111111111111111111111111111111111111111111' \
	docker compose --env-file .env.example config -q

backtest:
	bash freqtrade/scripts/backtest.sh

download-data:
	bash freqtrade/scripts/download_data.sh

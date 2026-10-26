PY := python3
.PHONY: help run test check clean

help:
	@echo "make run    — веб-клиент на http://127.0.0.1:8000"
	@echo "make test   — 49 тестов (сеть не нужна)"
	@echo "make check  — быстрая проверка адреса из аргумента WALLET=..."

run:
	$(PY) -m neon_miner serve --host 127.0.0.1 --port 8000

test:
	$(PY) -m unittest discover -s tests -v

check:
	$(PY) -m neon_miner check "$(WALLET)"

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf neon-miner-workdir

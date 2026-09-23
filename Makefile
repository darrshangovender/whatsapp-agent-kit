.PHONY: install test lint example serve clean

install:
	pip install -e ".[dev]"

test:
	pytest tests/ -q

lint:
	ruff check .

example:
	python examples/booking_bot.py

serve:
	python examples/run_server.py

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info .ruff_cache

# Makefile for Ambient Expense Agent development

.PHONY: install playground run generate-traces grade

install:
	uv pip install -e .
	uv tool install google-agents-cli

playground:
	uvx google-agents-cli playground

run:
	uv run uvicorn expense_agent.fast_api_app:fastapi_app --host 127.0.0.1 --port 8080

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	uvx google-agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml


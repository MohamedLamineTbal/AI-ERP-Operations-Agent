"""Configuration tests for optional environment-driven LangSmith tracing."""

from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_example_environment_keeps_langsmith_tracing_opt_in() -> None:
    values = dotenv_values(PROJECT_ROOT / ".env.example")

    assert values["LANGSMITH_TRACING"] == "false"
    assert values["LANGSMITH_API_KEY"] == ""
    assert values["LANGSMITH_PROJECT"] == "erp-ai-agent"


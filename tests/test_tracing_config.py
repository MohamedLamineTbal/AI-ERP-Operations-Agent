"""Configuration tests for optional environment-driven LangSmith tracing."""

from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_example_environment_keeps_langsmith_tracing_opt_in() -> None:
    values = dotenv_values(PROJECT_ROOT / ".env.example")

    assert values["LANGSMITH_TRACING"] == "false"
    assert values["LANGSMITH_API_KEY"] == ""
    assert values["LANGSMITH_PROJECT"] == "erp-ai-agent"


def test_langsmith_sdk_is_declared_for_observability() -> None:
    requirements = (PROJECT_ROOT / "requirements.txt").read_text()

    assert "langsmith>=0.4,<1.0" in requirements.splitlines()

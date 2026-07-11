import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from app.services.collectors import (  # noqa: E402
    LLMJudgeCollector,
    TaskAccuracyCollector,
    get_collector,
)


def test_task_accuracy_exact_match():
    c = TaskAccuracyCollector()
    result = c.collect(model_output="billing", reference_answer="billing")
    assert result.score == 1.0


def test_task_accuracy_mismatch():
    c = TaskAccuracyCollector()
    result = c.collect(model_output="technical", reference_answer="billing")
    assert result.score == 0.0


def test_task_accuracy_normalizes_whitespace_and_case():
    c = TaskAccuracyCollector()
    result = c.collect(model_output="  Billing \n", reference_answer="billing")
    assert result.score == 1.0


def test_task_accuracy_requires_reference():
    c = TaskAccuracyCollector()
    with pytest.raises(ValueError):
        c.collect(model_output="billing", reference_answer=None)


def test_get_collector_registry():
    assert isinstance(get_collector("task_accuracy"), TaskAccuracyCollector)
    assert isinstance(get_collector("llm_judge"), LLMJudgeCollector)
    with pytest.raises(ValueError):
        get_collector("nonexistent_collector")


def test_llm_judge_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = LLMJudgeCollector()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        c.collect(model_output="some output", rendered_prompt="some prompt")

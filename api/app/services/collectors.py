"""Phase 3: pluggable metric collectors.

Built-in metrics (latency, tokens, cost, errors) are captured directly on
ExperimentEvent by the /events endpoint — they don't need a "collector"
abstraction, the caller already has those numbers.

Custom metrics are different: quality/correctness usually can't be measured
by the caller inline, it needs a second opinion. This module implements two
real collectors:

  - LLMJudgeCollector: calls Claude to score a model's output against a
    rubric, 0.0-1.0. This is a genuine API call, not a stub — it requires
    ANTHROPIC_API_KEY to be set to actually run.
  - TaskAccuracyCollector: exact/normalized-match scoring against a known
    reference answer, for tasks (like the classifier demo) where you do
    have ground truth.

New collectors register themselves in COLLECTOR_REGISTRY by name, so the
/experiments/{id}/judge-event endpoint can dispatch to any of them by string
without the route layer knowing collector internals.
"""

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CollectorResult:
    score: float  # normalized 0.0-1.0, used as primary_metric_value for binary/continuous metrics
    reasoning: str | None = None


class MetricCollector(ABC):
    name: str

    @abstractmethod
    def collect(self, **kwargs) -> CollectorResult: ...


class TaskAccuracyCollector(MetricCollector):
    """Exact-match (after light normalization) against a known reference
    answer. Use when you have ground truth, e.g. eval sets with labeled
    correct answers."""

    name = "task_accuracy"

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    def collect(self, model_output: str, reference_answer: str, **kwargs) -> CollectorResult:
        if reference_answer is None:
            raise ValueError("task_accuracy collector requires reference_answer")
        match = self._normalize(model_output) == self._normalize(reference_answer)
        return CollectorResult(
            score=1.0 if match else 0.0,
            reasoning=f"exact match: {match}",
        )


class LLMJudgeCollector(MetricCollector):
    """Uses Claude as a judge to score a model output 0.0-1.0 against an
    optional rubric. This is the real, wired-up implementation — it makes
    an actual Anthropic API call. Requires ANTHROPIC_API_KEY.

    Kept deliberately simple (single score + one-line reasoning) rather than
    a multi-criteria rubric parser, since the point of this collector is to
    demonstrate the integration pattern — swapping in a more elaborate rubric
    schema is a prompt change, not an architecture change.
    """

    name = "llm_judge"

    DEFAULT_RUBRIC = (
        "Score how well the output correctly and completely accomplishes "
        "what the prompt asked for. Score 1.0 for a fully correct response, "
        "0.5 for partially correct, 0.0 for wrong or unusable."
    )

    def __init__(self, model: str = "claude-sonnet-5"):
        self.model = model

    def collect(
        self,
        model_output: str,
        rendered_prompt: str | None = None,
        rubric: str | None = None,
        **kwargs,
    ) -> CollectorResult:
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("anthropic package not installed — add it to requirements.txt") from e

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set — llm_judge collector needs it to call the judge model"
            )

        client = anthropic.Anthropic(api_key=api_key)
        rubric_text = rubric or self.DEFAULT_RUBRIC

        judge_prompt = (
            f"You are grading an AI system's output.\n\n"
            f"Original prompt given to the system:\n{rendered_prompt or '(not provided)'}\n\n"
            f"System's output:\n{model_output}\n\n"
            f"Grading rubric:\n{rubric_text}\n\n"
            f"Respond with ONLY a JSON object: "
            f'{{"score": <float 0.0-1.0>, "reasoning": "<one sentence>"}}'
        )

        response = client.messages.create(
            model=self.model,
            max_tokens=200,
            messages=[{"role": "user", "content": judge_prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

        import json

        text_clean = re.sub(r"^```json\s*|\s*```$", "", text.strip())
        try:
            parsed = json.loads(text_clean)
            score = max(0.0, min(1.0, float(parsed["score"])))
            reasoning = parsed.get("reasoning")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise RuntimeError(f"judge model returned unparseable response: {text!r}") from e

        return CollectorResult(score=score, reasoning=reasoning)


COLLECTOR_REGISTRY: dict[str, MetricCollector] = {
    "task_accuracy": TaskAccuracyCollector(),
    "llm_judge": LLMJudgeCollector(),
}


def get_collector(name: str) -> MetricCollector:
    if name not in COLLECTOR_REGISTRY:
        raise ValueError(f"unknown collector '{name}'; available: {list(COLLECTOR_REGISTRY)}")
    return COLLECTOR_REGISTRY[name]

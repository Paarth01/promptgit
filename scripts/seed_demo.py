"""Phase 5 demo: a support-email classifier with 3 meaningfully different
prompt approaches, run against 500+ synthetic requests until the experiment
converges on a statistically significant winner.

The three variants represent real, distinct prompt-engineering strategies:
  A. baseline    — bare zero-shot instruction
  B. few-shot    — same instruction + 4 labeled examples
  C. cot         — chain-of-thought: asks the model to reason before answering

Since there's no live LLM in this demo, synthetic "ground truth" success
rates are assigned per variant (few-shot > cot > baseline, a realistic
ordering) and each simulated request's outcome is sampled from a Bernoulli
distribution at that rate. This is exactly the kind of harness you'd swap
a real LLM-judge call into later — the experiment engine doesn't care where
the labels come from.

Usage:
    python scripts/seed_demo.py --api http://localhost:8000
"""
import argparse
import random
import time

import requests

CLASSIFIER_PROMPT_TEXT_BASELINE = (
    "Classify the following support email into one of these categories: "
    "billing, technical, account, general.\n\nEmail: {email_body}\n\nCategory:"
)

CLASSIFIER_PROMPT_TEXT_FEWSHOT = (
    "Classify the following support email into one of these categories: "
    "billing, technical, account, general.\n\n"
    "Examples:\n"
    "Email: \"I was charged twice this month\" -> billing\n"
    "Email: \"The app crashes when I upload a file\" -> technical\n"
    "Email: \"I can't log into my account\" -> account\n"
    "Email: \"What are your business hours?\" -> general\n\n"
    "Email: {email_body}\n\nCategory:"
)

CLASSIFIER_PROMPT_TEXT_COT = (
    "Classify the following support email into one of these categories: "
    "billing, technical, account, general.\n\n"
    "First, briefly reason about what the customer is asking for. Then give "
    "your final answer on a new line as 'Category: <label>'.\n\n"
    "Email: {email_body}\n\nReasoning:"
)

# Realistic ground-truth accuracy per approach (few-shot beats CoT beats baseline
# for a straightforward classification task like this — CoT overhead doesn't
# always pay off for simple tasks, which is a realistic and interesting result).
TRUE_SUCCESS_RATES = {
    "baseline": 0.74,
    "few_shot": 0.89,
    "cot": 0.83,
}

N_REQUESTS_PER_VARIANT = 180  # 3 variants x 180 = 540 total, comfortably past 500


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--target-sample-size", type=int, default=150)
    args = parser.parse_args()

    api = args.api
    session = requests.Session()

    print("Creating prompt...")
    r = session.post(f"{api}/prompts", json={
        "slug": "support-email-classifier",
        "description": "Classifies inbound support emails into billing/technical/account/general.",
    })
    if r.status_code == 409:
        print("  prompt already exists, continuing")
    else:
        r.raise_for_status()

    print("Creating 3 versions (baseline, few-shot, chain-of-thought)...")
    versions = {}
    for label, text, msg in [
        ("baseline", CLASSIFIER_PROMPT_TEXT_BASELINE, "v1: zero-shot baseline"),
        ("few_shot", CLASSIFIER_PROMPT_TEXT_FEWSHOT, "v2: add 4 few-shot examples"),
        ("cot", CLASSIFIER_PROMPT_TEXT_COT, "v3: chain-of-thought reasoning"),
    ]:
        r = session.post(
            "{}/prompts/support-email-classifier/versions".format(api),
            json={
                "prompt_text": text,
                "few_shot_examples": [],
                "params": {"model": "gpt-4o-mini", "temperature": 0.0},
                "template_variables": ["email_body"],
                "commit_message": msg,
                "created_by": "seed-script",
                "activate": label == "baseline",
            },
        )
        r.raise_for_status()
        versions[label] = r.json()
        print(f"  created {label} -> version {versions[label]['version_number']}")

    print("Creating experiment...")
    r = session.post(f"{api}/experiments", json={
        "prompt_slug": "support-email-classifier",
        "name": "Support classifier: baseline vs few-shot vs CoT",
        "primary_metric": "task_success",
        "metric_type": "binary",
        "target_sample_size": args.target_sample_size,
        "min_detectable_effect": 0.05,
        "created_by": "seed-script",
        "variants": [
            {"label": "baseline", "prompt_version_id": versions["baseline"]["id"],
             "traffic_weight": 0.34, "is_baseline": True},
            {"label": "few_shot", "prompt_version_id": versions["few_shot"]["id"],
             "traffic_weight": 0.33, "is_baseline": False},
            {"label": "cot", "prompt_version_id": versions["cot"]["id"],
             "traffic_weight": 0.33, "is_baseline": False},
        ],
    })
    r.raise_for_status()
    experiment = r.json()
    print(f"  experiment id: {experiment['id']}")

    print("Starting experiment...")
    r = session.post(f"{api}/experiments/{experiment['id']}/start")
    r.raise_for_status()

    variant_by_label = {v["label"]: v for v in experiment["variants"]}

    print(f"Simulating {N_REQUESTS_PER_VARIANT * 3} synthetic requests...")
    unit_counter = 0
    for label in ["baseline", "few_shot", "cot"]:
        variant = variant_by_label[label]
        true_rate = TRUE_SUCCESS_RATES[label]
        for _ in range(N_REQUESTS_PER_VARIANT):
            unit_counter += 1
            unit_id = f"synthetic-user-{unit_counter}"

            # Serve through the real /serve endpoint so consistent hashing +
            # assignment persistence gets exercised too, not just direct event writes.
            serve_resp = session.post(f"{api}/serve/support-email-classifier", json={
                "unit_id": unit_id,
                "context": {"email_body": "sample support email content"},
            })
            serve_resp.raise_for_status()
            served = serve_resp.json()
            actual_variant_id = served.get("variant_id") or variant["id"]
            actual_label = served.get("variant_label") or label
            actual_rate = TRUE_SUCCESS_RATES[actual_label]

            success = 1 if random.random() < actual_rate else 0
            latency = random.gauss(420 if actual_label == "cot" else 260, 40)
            is_error = random.random() < 0.01  # 1% baseline error rate, well under guardrail

            session.post(f"{api}/events", json={
                "unit_id": unit_id,
                "variant_id": actual_variant_id,
                "latency_ms": max(latency, 50),
                "input_tokens": random.randint(80, 200),
                "output_tokens": random.randint(5, 60),
                "cost_usd": round(random.uniform(0.0004, 0.002), 6),
                "is_error": is_error,
                "primary_metric_value": success,
            }).raise_for_status()

        print(f"  {label}: {N_REQUESTS_PER_VARIANT} events sent")

    print("\nFetching results...")
    r = session.get(f"{api}/experiments/{experiment['id']}/results")
    r.raise_for_status()
    results = r.json()

    print(f"\nExperiment status: {results['status']}")
    print(f"Progress: {results['progress_pct']}%")
    for v in results["variants"]:
        print(
            f"  {v['label']:10s} n={v['sample_size']:4d} "
            f"success_rate={v['mean_value']:.3f} "
            f"p={v['p_value_vs_baseline']} "
            f"significant={v['is_significant']} "
            f"lift={v['relative_lift_vs_baseline']}"
        )
    print(f"\nWinner ready: {results['winner_ready']}")
    print(f"Reason: {results['winner_reason']}")
    print(f"\nExperiment id for dashboard: {experiment['id']}")


if __name__ == "__main__":
    main()

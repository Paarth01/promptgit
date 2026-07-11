"""Phase 4: management interface.

Streamlit was chosen over a full React app for the dashboard because the
demo's job is to show the platform's *reasoning* (experiment monitoring,
significance results, promote-with-one-click) clearly and quickly, not to
showcase frontend engineering. Swapping in a React dashboard later is a
drop-in replacement since it just consumes the same REST API.
"""
import os

import pandas as pd
import requests
import streamlit as st

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="Prompt A/B Platform", layout="wide")
st.title("🧪 Prompt Versioning & A/B Testing Platform")

page = st.sidebar.radio(
    "Navigate",
    ["Registry", "Experiments", "Compare Versions", "Create Experiment"],
)


def api_get(path, **params):
    r = requests.get(f"{API_BASE_URL}{path}", params=params)
    r.raise_for_status()
    return r.json()


def api_post(path, json=None, **params):
    r = requests.post(f"{API_BASE_URL}{path}", json=json, params=params)
    r.raise_for_status()
    return r.json()


# ── Registry ──────────────────────────────────────────────────────────────
if page == "Registry":
    st.header("Prompt Registry")
    try:
        prompts = api_get("/prompts")
    except Exception as e:
        st.error(f"Could not reach API at {API_BASE_URL}: {e}")
        st.stop()

    if not prompts:
        st.info("No prompts yet. Run the seed script to load the demo dataset.")
        st.stop()

    slugs = [p["slug"] for p in prompts]
    slug = st.selectbox("Prompt", slugs)
    prompt = next(p for p in prompts if p["slug"] == slug)

    versions = api_get(f"/prompts/{slug}/versions")
    st.subheader(f"Versions ({len(versions)})")

    for v in versions:
        is_active = v["id"] == prompt["active_version_id"]
        with st.expander(
            f"v{v['version_number']} — {v['commit_message']}"
            + (" 🟢 ACTIVE" if is_active else ""),
            expanded=is_active,
        ):
            st.code(v["prompt_text"], language="text")
            col1, col2 = st.columns(2)
            col1.write(f"**Params:** {v['params']}")
            col2.write(f"**Template vars:** {v['template_variables']}")
            st.caption(f"by {v['created_by']} · {v['created_at']}")
            if not is_active:
                if st.button(f"Rollback to v{v['version_number']}", key=f"rb-{v['id']}"):
                    api_post(
                        f"/prompts/{slug}/activate",
                        json={"version_id": v["id"], "actor": "dashboard-user", "reason": "manual rollback via dashboard"},
                    )
                    st.rerun()

    st.subheader("Audit Log")
    audit = api_get(f"/prompts/{slug}/audit-log")
    if audit:
        st.dataframe(pd.DataFrame(audit)[["action", "actor", "reason", "created_at"]])
    else:
        st.caption("No audit events yet.")


# ── Compare Versions ─────────────────────────────────────────────────────
elif page == "Compare Versions":
    st.header("Side-by-Side Comparison")
    prompts = api_get("/prompts")
    if not prompts:
        st.info("No prompts yet.")
        st.stop()
    slug = st.selectbox("Prompt", [p["slug"] for p in prompts])
    versions = api_get(f"/prompts/{slug}/versions")
    version_numbers = [v["version_number"] for v in versions]

    col1, col2 = st.columns(2)
    from_v = col1.selectbox("From version", version_numbers, index=min(1, len(version_numbers) - 1))
    to_v = col2.selectbox("To version", version_numbers, index=0)

    if st.button("Diff"):
        diff = api_get(f"/prompts/{slug}/diff", **{"from_": from_v, "to": to_v})
        st.code(diff["prompt_text_diff"] or "(no text changes)", language="diff")
        st.write("**Params diff:**", diff["params_diff"] or "none")
        st.write("**Template variables diff:**", diff["template_variables_diff"])
        st.write("**Few-shot examples changed:**", diff["few_shot_examples_changed"])


# ── Experiments ───────────────────────────────────────────────────────────
elif page == "Experiments":
    st.header("Experiments")
    experiments = api_get("/experiments")
    if not experiments:
        st.info("No experiments yet. Create one from the sidebar or run the seed script.")
        st.stop()

    exp_labels = {f"{e['name']} ({e['status']})": e for e in experiments}
    chosen = st.selectbox("Experiment", list(exp_labels.keys()))
    exp = exp_labels[chosen]

    col1, col2, col3 = st.columns(3)
    if col1.button("▶ Start", disabled=exp["status"] not in ("draft", "paused")):
        api_post(f"/experiments/{exp['id']}/start")
        st.rerun()
    if col2.button("⏸ Pause", disabled=exp["status"] != "running"):
        api_post(f"/experiments/{exp['id']}/pause")
        st.rerun()

    results = api_get(f"/experiments/{exp['id']}/results")

    st.metric("Status", results["status"])
    st.progress(min(results["progress_pct"] / 100, 1.0), text=f"{results['progress_pct']}% of target sample size")

    df = pd.DataFrame(results["variants"])
    if not df.empty:
        display_df = df[[
            "label", "is_baseline", "sample_size", "mean_value", "error_rate",
            "p_value_vs_baseline", "is_significant", "relative_lift_vs_baseline",
        ]].copy()
        display_df["relative_lift_vs_baseline"] = display_df["relative_lift_vs_baseline"].apply(
            lambda x: f"{x:+.1%}" if x is not None else "—"
        )
        display_df["error_rate"] = display_df["error_rate"].apply(lambda x: f"{x:.1%}")
        st.dataframe(display_df, use_container_width=True)

        st.bar_chart(df.set_index("label")["mean_value"])

    st.subheader("Winner status")
    if results["winner_variant_id"]:
        winner_label = df[df["variant_id"] == results["winner_variant_id"]]["label"].iloc[0] if not df.empty else "?"
        st.success(f"Winner determined: **{winner_label}** — {results['winner_reason']}")
        if col3.button("🏆 Promote Winner", disabled=exp["status"] == "completed"):
            api_post(f"/experiments/{exp['id']}/promote", actor="dashboard-user")
            st.rerun()
    else:
        st.info(results["winner_reason"] or "No winner yet.")


# ── Create Experiment ────────────────────────────────────────────────────
elif page == "Create Experiment":
    st.header("Create Experiment")
    prompts = api_get("/prompts")
    if not prompts:
        st.info("Create a prompt in the Registry first.")
        st.stop()

    slug = st.selectbox("Prompt", [p["slug"] for p in prompts])
    versions = api_get(f"/prompts/{slug}/versions")

    name = st.text_input("Experiment name", value=f"{slug} experiment")
    metric_type = st.selectbox("Metric type", ["binary", "continuous"])
    primary_metric = st.text_input("Primary metric name", value="task_success")
    target_n = st.number_input("Target sample size per variant", min_value=10, value=200)

    st.write("**Variants** (select versions and set traffic split)")
    n_variants = st.number_input("Number of variants", min_value=2, max_value=5, value=2)

    variant_specs = []
    remaining = 1.0
    for i in range(int(n_variants)):
        c1, c2, c3 = st.columns([2, 1, 1])
        v_num = c1.selectbox(
            f"Version for variant {i+1}", [v["version_number"] for v in versions], key=f"v-{i}"
        )
        weight = c2.number_input(
            f"Traffic weight {i+1}", min_value=0.0, max_value=1.0,
            value=round(1.0 / n_variants, 2), key=f"w-{i}",
        )
        is_baseline = c3.checkbox("Baseline", value=(i == 0), key=f"b-{i}")
        version_obj = next(v for v in versions if v["version_number"] == v_num)
        variant_specs.append({
            "label": f"variant_{i+1}_v{v_num}",
            "prompt_version_id": version_obj["id"],
            "traffic_weight": weight,
            "is_baseline": is_baseline,
        })

    if st.button("Create Experiment"):
        try:
            api_post("/experiments", json={
                "prompt_slug": slug,
                "name": name,
                "primary_metric": primary_metric,
                "metric_type": metric_type,
                "target_sample_size": int(target_n),
                "variants": variant_specs,
                "created_by": "dashboard-user",
            })
            st.success("Experiment created as draft. Go to Experiments tab to start it.")
        except requests.HTTPError as e:
            st.error(e.response.json())

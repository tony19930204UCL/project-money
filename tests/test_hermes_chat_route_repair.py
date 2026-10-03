import pytest

from cio_market_lab.integrations.hermes_chat import (
    HermesCIODecisionExecutor, build_production_chat_command, resolve_cio_route,
)


def test_daily_route_is_canonical_openai_codex():
    assert resolve_cio_route() == {
        "provider_id": "openai-codex", "model_id": "gpt-6.1-sol",
        "route": "default", "escalation_reason": "",
    }
    executor = HermesCIODecisionExecutor()
    assert (executor.provider_id, executor.model_id) == ("openai-codex", "gpt-6.1-sol")


def test_only_semantic_major_escalation_selects_astra():
    for reason in ("high_consequence", "thesis_conflict", "multiasset_complexity"):
        route = resolve_cio_route(escalation_reason=reason)
        assert (route["provider_id"], route["model_id"]) == ("openai-codex", "gpt-6-astra")
        assert HermesCIODecisionExecutor(escalation_reason=reason).model_id == "gpt-6-astra"


@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-6-astra"), ("openai-codex", "gpt-6-astra"), ("other", "custom-model"),
])
def test_production_executor_rejects_model_or_provider_override(provider, model):
    with pytest.raises(ValueError):
        HermesCIODecisionExecutor(provider_id=provider, model_id=model, is_production=True)


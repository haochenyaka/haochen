"""配置表单链路与运行期资源归一化验证。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.agents.buildin.front_desk.agent import FrontDeskAgent, FrontDeskContext
from app.agents.context import (
    AgentConfigError,
    AgentResourceRequest,
    DeclaredBuiltinPolicy,
    DenyAllResourcePolicy,
    ResourceRef,
    StaticResourcePolicy,
    prepare_agent_runtime_context,
)


def test_configurable_items_follow_field_order_and_metadata() -> None:
    items = FrontDeskContext.get_configurable_items()
    assert [item.key for item in items] == ["system_prompt", "model", "temperature"]

    by_key = {item.key: item for item in items}
    assert by_key["system_prompt"].kind == "textarea"
    assert by_key["system_prompt"].group == "prompt"
    assert by_key["system_prompt"].default.startswith("你是无人酒店的前台助理")
    assert by_key["temperature"].kind == "number"
    assert by_key["temperature"].default == 0.2


def test_agent_exposes_context_schema_config_items() -> None:
    assert FrontDeskAgent.get_configurable_items() == FrontDeskContext.get_configurable_items()


def test_from_config_applies_values_and_keeps_defaults() -> None:
    context = FrontDeskContext.from_config({"model": "qwen-plus", "temperature": 0.5})
    assert context.model == "qwen-plus"
    assert context.temperature == 0.5
    assert context.system_prompt == FrontDeskContext().system_prompt


def test_from_config_rejects_unknown_keys() -> None:
    with pytest.raises(AgentConfigError, match="unknown_key"):
        FrontDeskContext.from_config({"unknown_key": 1})


def test_resource_normalization_is_deny_by_default() -> None:
    requested = AgentResourceRequest(
        refs=(ResourceRef("tool", "room_type_query"), ResourceRef("skill", "refund"))
    )
    snapshot = asyncio.run(
        prepare_agent_runtime_context(agent_slug="front-desk", user_id=uuid4(), requested=requested)
    )
    assert snapshot.tools == ()
    assert snapshot.skills == ()
    assert snapshot.ids("knowledge_base") == ()


def test_resource_normalization_keeps_only_authorized_refs() -> None:
    requested = AgentResourceRequest(
        refs=(
            ResourceRef("tool", "room_type_query"),
            ResourceRef("tool", "lock_open"),
            ResourceRef("knowledge_base", "hotel-faq"),
        )
    )
    policy = StaticResourcePolicy(
        allowed=frozenset({("tool", "room_type_query"), ("knowledge_base", "hotel-faq")})
    )
    snapshot = asyncio.run(
        prepare_agent_runtime_context(
            agent_slug="front-desk", user_id=uuid4(), requested=requested, policy=policy
        )
    )
    assert snapshot.ids("tool") == ("room_type_query",)
    assert snapshot.ids("knowledge_base") == ("hotel-faq",)
    assert snapshot.ids("skill") == ()


def test_deny_all_policy_is_the_default_fallback() -> None:
    policy = DenyAllResourcePolicy()

    async def _check() -> bool:
        return await policy.authorizes(ResourceRef("tool", "room_type_query"))

    assert asyncio.run(_check()) is False


def test_declared_builtin_policy_allows_declared_tools_and_knowledge_bases_only() -> None:
    policy = DeclaredBuiltinPolicy(
        declared_tools=frozenset({"get_current_time", "create_order"}),
        knowledge_bases=frozenset({"hotel-guest"}),
    )

    async def _check(ref: ResourceRef) -> bool:
        return await policy.authorizes(ref)

    assert asyncio.run(_check(ResourceRef("tool", "get_current_time"))) is True
    assert asyncio.run(_check(ResourceRef("tool", "create_order"))) is True
    assert asyncio.run(_check(ResourceRef("tool", "lock_open"))) is False
    assert asyncio.run(_check(ResourceRef("knowledge_base", "hotel-guest"))) is True
    assert asyncio.run(_check(ResourceRef("knowledge_base", "internal-faq"))) is False
    assert asyncio.run(_check(ResourceRef("skill", "refund"))) is False

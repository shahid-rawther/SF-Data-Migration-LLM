"""Environment-driven configuration for the agent layer.

Model, region, autonomy level and spend caps are all read from the environment
so they can be changed per deployment without a rebuild.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Transport used to reach Claude on Bedrock.
PROVIDER_BOTO3 = "boto3"          # bedrock-runtime Converse API, SigV4 auth
PROVIDER_ANTHROPIC = "anthropic"  # Anthropic SDK against a Bedrock endpoint, API-key auth

# How much the agent is allowed to do on its own.
AUTONOMY_PREVIEW = "preview"  # analyse and report; never write a mapping config
AUTONOMY_APPLY = "apply"      # write the resolution into the mapping configs

PURPOSE_RELATIONSHIP = "relationship"
PURPOSE_REPAIR = "repair"
PURPOSE_FIELD_MATCH = "field_match"

# The Bedrock runtime addresses these models through US geo inference profiles;
# the API-key endpoints take the bare model family name instead.
_MODEL_DEFAULTS: dict[str, dict[str, str]] = {
    PROVIDER_BOTO3: {
        PURPOSE_RELATIONSHIP: "us.anthropic.claude-sonnet-5",
        PURPOSE_REPAIR: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        PURPOSE_FIELD_MATCH: "us.anthropic.claude-sonnet-5",
    },
    PROVIDER_ANTHROPIC: {
        PURPOSE_RELATIONSHIP: "anthropic.claude-sonnet-5",
        PURPOSE_REPAIR: "anthropic.claude-haiku-4-5",
        PURPOSE_FIELD_MATCH: "anthropic.claude-sonnet-5",
    },
}

DEFAULT_REGION = "us-east-1"


def _text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _integer(name: str, default: int) -> int:
    try:
        return int(_text(name) or default)
    except ValueError:
        return default


def _decimal(name: str, default: float) -> float:
    try:
        return float(_text(name) or default)
    except ValueError:
        return default


def _flag(name: str, default: bool) -> bool:
    raw = _text(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AgentSettings:
    provider: str
    region: str
    api_key: str
    base_url: str
    relationship_model_id: str
    repair_model_id: str
    field_match_model_id: str
    max_tokens_per_call: int
    max_tokens_per_session: int
    max_tool_iterations: int
    temperature: float
    autonomy: str
    repair_max_attempts: int
    repair_enabled: bool
    preflight_enforce: bool
    autowire_enabled: bool
    # Ask the model to review even when the deterministic checks found nothing
    # they recognise, so an edge case nobody wrote a rule for is still reported.
    review_always: bool
    field_match_enabled: bool
    # Caps how many unmatched fields from each side are shown to the model, so a
    # wide table costs a predictable number of tokens.
    field_match_max_fields: int
    # Proposals below this land in the report as rejected rather than offered.
    field_match_min_confidence: float

    @property
    def has_credentials(self) -> bool:
        """Whether the transport has something to authenticate with.

        Only checked for the API-key transport: boto3 resolves credentials from
        the instance role, shared config or environment at call time, and a
        false negative here would disable the agent on a correctly configured
        instance.
        """
        if self.provider == PROVIDER_ANTHROPIC:
            return bool(self.api_key)
        return True

    @property
    def may_apply(self) -> bool:
        return self.autonomy == AUTONOMY_APPLY

    def model_for(self, purpose: str) -> str:
        if purpose == PURPOSE_REPAIR:
            return self.repair_model_id
        if purpose == PURPOSE_FIELD_MATCH:
            return self.field_match_model_id
        return self.relationship_model_id

    def redacted(self) -> dict:
        """Configuration summary safe to return over the API."""
        return {
            "provider": self.provider,
            "region": self.region,
            "base_url": self.base_url,
            "relationship_model_id": self.relationship_model_id,
            "repair_model_id": self.repair_model_id,
            "field_match_model_id": self.field_match_model_id,
            "autonomy": self.autonomy,
            "max_tokens_per_call": self.max_tokens_per_call,
            "max_tokens_per_session": self.max_tokens_per_session,
            "max_tool_iterations": self.max_tool_iterations,
            "repair_enabled": self.repair_enabled,
            "repair_max_attempts": self.repair_max_attempts,
            "preflight_enforce": self.preflight_enforce,
            "autowire_enabled": self.autowire_enabled,
            "review_always": self.review_always,
            "field_match_enabled": self.field_match_enabled,
            "has_credentials": self.has_credentials,
        }


def resolve_provider() -> str:
    """Pick the transport, preferring an explicit setting.

    An API key in the environment means the deployment was handed an endpoint
    rather than an IAM role, so that transport wins when nothing is declared.
    """
    declared = _text("BEDROCK_PROVIDER").lower()
    if declared in (PROVIDER_BOTO3, PROVIDER_ANTHROPIC):
        return declared
    if _text("ANTHROPIC_API_KEY"):
        return PROVIDER_ANTHROPIC
    return PROVIDER_BOTO3


def load_settings() -> AgentSettings:
    provider = resolve_provider()
    defaults = _MODEL_DEFAULTS[provider]
    autonomy = _text("AGENT_AUTONOMY", AUTONOMY_APPLY).lower()
    if autonomy not in (AUTONOMY_PREVIEW, AUTONOMY_APPLY):
        autonomy = AUTONOMY_APPLY

    return AgentSettings(
        provider=provider,
        region=_text("BEDROCK_REGION") or _text("AWS_REGION", DEFAULT_REGION),
        api_key=_text("ANTHROPIC_API_KEY"),
        base_url=_text("ANTHROPIC_BASE_URL"),
        relationship_model_id=_text(
            "BEDROCK_RELATIONSHIP_MODEL_ID",
            _text("BEDROCK_MODEL_ID", defaults[PURPOSE_RELATIONSHIP]),
        ),
        repair_model_id=_text("BEDROCK_REPAIR_MODEL_ID", defaults[PURPOSE_REPAIR]),
        field_match_model_id=_text(
            "BEDROCK_FIELD_MATCH_MODEL_ID", defaults[PURPOSE_FIELD_MATCH]
        ),
        max_tokens_per_call=_integer("AGENT_MAX_TOKENS_PER_CALL", 8000),
        max_tokens_per_session=_integer("AGENT_MAX_TOKENS_PER_SESSION", 200_000),
        max_tool_iterations=_integer("AGENT_MAX_TOOL_ITERATIONS", 12),
        temperature=_decimal("AGENT_TEMPERATURE", 0.0),
        autonomy=autonomy,
        repair_max_attempts=_integer("AGENT_REPAIR_MAX_ATTEMPTS", 2),
        repair_enabled=_flag("AGENT_REPAIR_ENABLED", True),
        preflight_enforce=_flag("AGENT_PREFLIGHT_ENFORCE", True),
        autowire_enabled=_flag("AGENT_AUTOWIRE", True),
        review_always=_flag("AGENT_REVIEW_ALWAYS", True),
        field_match_enabled=_flag("AGENT_FIELD_MATCH", True),
        field_match_max_fields=_integer("AGENT_FIELD_MATCH_MAX_FIELDS", 250),
        field_match_min_confidence=_decimal("AGENT_FIELD_MATCH_MIN_CONFIDENCE", 0.5),
    )

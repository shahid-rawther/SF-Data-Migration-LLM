"""Bedrock client for tool-using agents.

Two transports reach the same Claude models and are selected by configuration:
the ``bedrock-runtime`` Converse API over boto3 (IAM auth) and the Anthropic SDK
pointed at a Bedrock endpoint (API-key auth). Both are normalised behind
:class:`AgentClient` so the agent loops in this package never branch on
transport.

Every call is counted and logged, and a session-wide token budget aborts a run
that would otherwise loop, so spend per migration is bounded and attributable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from common.logger import logger

from agents.config import (
    PROVIDER_ANTHROPIC,
    PROVIDER_BOTO3,
    PURPOSE_RELATIONSHIP,
    AgentSettings,
    load_settings,
)


class AgentError(RuntimeError):
    """A call to the model failed or the agent layer is not usable."""


class AgentUnavailable(AgentError):
    """The transport is not installed or not configured."""


class TokenBudgetExceeded(AgentError):
    """The session token budget was consumed before the agent finished."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ToolOutcome:
    tool_use_id: str
    content: Any
    is_error: bool = False


@dataclass
class ConverseResult:
    text: str
    tool_uses: list[ToolUse]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    assistant_message: dict

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_uses)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def _dumps(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _rejects_temperature(exc: Exception) -> bool:
    """Whether the model refused the request because it was sent a temperature.

    The newer Claude models fix their own sampling and treat the parameter as an
    error rather than ignoring it, so a run configured for determinism has to
    detect that and stop sending it.
    """
    message = str(exc).lower()
    if "temperature" not in message:
        return False
    return any(
        phrase in message
        for phrase in ("deprecated", "not supported", "unsupported", "not allowed")
    )


class AgentClient:
    """Transport-agnostic single-turn interface to a Claude model on Bedrock."""

    def __init__(self, model_id: str, settings: AgentSettings) -> None:
        self.model_id = model_id
        self.settings = settings
        self.usage = Usage()
        # Cleared for the rest of the session the first time the model rejects
        # the parameter, so one model's refusal costs one retry, not one per call.
        self.send_temperature = True

    # ── conversation building blocks (provider-native shapes) ──────────────

    def user_message(self, text: str) -> dict:
        raise NotImplementedError

    def assistant_message(self, text: str) -> dict:
        raise NotImplementedError

    def tool_result_message(self, outcomes: list[ToolOutcome]) -> dict:
        raise NotImplementedError

    def converse(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
    ) -> ConverseResult:
        raise NotImplementedError

    # ── shared accounting ──────────────────────────────────────────────────

    def _check_budget(self) -> None:
        if self.usage.total_tokens >= self.settings.max_tokens_per_session:
            raise TokenBudgetExceeded(
                f"Session token budget of {self.settings.max_tokens_per_session:,} "
                f"exhausted after {self.usage.calls} call(s)"
            )

    def _record(self, input_tokens: int, output_tokens: int, stop_reason: str) -> None:
        self.usage.calls += 1
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        logger.info(
            "[agent] %s call %d — in=%d out=%d cumulative=%d stop=%s",
            self.model_id,
            self.usage.calls,
            input_tokens,
            output_tokens,
            self.usage.total_tokens,
            stop_reason,
        )

    def probe(self) -> dict:
        """Round-trip a trivial prompt to prove the model is reachable."""
        result = self.converse(
            system="Reply with the single word OK.",
            messages=[self.user_message("Reply with the single word OK.")],
        )
        return {
            "ok": True,
            "model_id": self.model_id,
            "provider": self.settings.provider,
            "region": self.settings.region,
            "reply": result.text.strip()[:80],
            "usage": self.usage.to_dict(),
        }


class Boto3AgentClient(AgentClient):
    """bedrock-runtime Converse API. Credentials resolve through boto3."""

    def __init__(self, model_id: str, settings: AgentSettings) -> None:
        super().__init__(model_id, settings)
        try:
            import boto3
        except ImportError as exc:
            raise AgentUnavailable(
                "boto3 is not installed; run `pip install boto3` or set "
                "BEDROCK_PROVIDER=anthropic"
            ) from exc
        self._runtime = boto3.client("bedrock-runtime", region_name=settings.region)

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": [{"text": text}]}

    def assistant_message(self, text: str) -> dict:
        return {"role": "assistant", "content": [{"text": text}]}

    def tool_result_message(self, outcomes: list[ToolOutcome]) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": outcome.tool_use_id,
                        "content": [{"text": _dumps(outcome.content)}],
                        "status": "error" if outcome.is_error else "success",
                    }
                }
                for outcome in outcomes
            ],
        }

    def converse(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
    ) -> ConverseResult:
        self._check_budget()
        inference: dict[str, Any] = {"maxTokens": self.settings.max_tokens_per_call}
        if self.send_temperature:
            inference["temperature"] = self.settings.temperature
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": inference,
        }
        if system:
            request["system"] = [{"text": system}]
        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": tool.input_schema},
                        }
                    }
                    for tool in tools
                ]
            }

        try:
            response = self._runtime.converse(**request)
        except Exception as exc:  # noqa: BLE001 — botocore raises many shapes
            if not (self.send_temperature and _rejects_temperature(exc)):
                raise AgentError(f"Bedrock Converse call failed: {exc}") from exc
            logger.info("[agent] %s sets its own sampling; dropping temperature", self.model_id)
            self.send_temperature = False
            inference.pop("temperature", None)
            try:
                response = self._runtime.converse(**request)
            except Exception as retry_exc:  # noqa: BLE001
                raise AgentError(f"Bedrock Converse call failed: {retry_exc}") from retry_exc

        message = (response.get("output") or {}).get("message") or {}
        texts: list[str] = []
        tool_uses: list[ToolUse] = []
        for block in message.get("content") or []:
            if "text" in block:
                texts.append(block["text"])
            elif "toolUse" in block:
                use = block["toolUse"]
                tool_uses.append(
                    ToolUse(
                        id=use.get("toolUseId", ""),
                        name=use.get("name", ""),
                        input=use.get("input") or {},
                    )
                )

        usage = response.get("usage") or {}
        stop_reason = response.get("stopReason", "")
        self._record(
            int(usage.get("inputTokens") or 0),
            int(usage.get("outputTokens") or 0),
            stop_reason,
        )
        return ConverseResult(
            text="\n".join(texts).strip(),
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
            assistant_message={"role": "assistant", "content": message.get("content") or []},
        )


class AnthropicAgentClient(AgentClient):
    """Anthropic SDK against a Bedrock endpoint using an API key."""

    def __init__(self, model_id: str, settings: AgentSettings) -> None:
        super().__init__(model_id, settings)
        if not settings.api_key:
            raise AgentUnavailable(
                "ANTHROPIC_API_KEY is not set. Note that python-dotenv cannot "
                "read `set KEY=value` lines in .env — the assignment must not "
                "be prefixed."
            )
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise AgentUnavailable(
                'the anthropic SDK is not installed; run `pip install "anthropic[bedrock]"`'
            ) from exc
        kwargs: dict[str, Any] = {"api_key": settings.api_key}
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        self._client = Anthropic(**kwargs)

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def assistant_message(self, text: str) -> dict:
        return {"role": "assistant", "content": [{"type": "text", "text": text}]}

    def tool_result_message(self, outcomes: list[ToolOutcome]) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": outcome.tool_use_id,
                    "content": _dumps(outcome.content),
                    "is_error": outcome.is_error,
                }
                for outcome in outcomes
            ],
        }

    def converse(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
    ) -> ConverseResult:
        self._check_budget()
        request: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": self.settings.max_tokens_per_call,
            "messages": messages,
        }
        if self.send_temperature:
            request["temperature"] = self.settings.temperature
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]

        try:
            response = self._client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 — SDK raises many shapes
            if not (self.send_temperature and _rejects_temperature(exc)):
                raise AgentError(f"Anthropic messages call failed: {exc}") from exc
            logger.info("[agent] %s sets its own sampling; dropping temperature", self.model_id)
            self.send_temperature = False
            request.pop("temperature", None)
            try:
                response = self._client.messages.create(**request)
            except Exception as retry_exc:  # noqa: BLE001
                raise AgentError(f"Anthropic messages call failed: {retry_exc}") from retry_exc

        texts: list[str] = []
        tool_uses: list[ToolUse] = []
        content: list[dict] = []
        for block in response.content or []:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                texts.append(getattr(block, "text", ""))
                content.append({"type": "text", "text": getattr(block, "text", "")})
            elif block_type == "tool_use":
                tool_uses.append(
                    ToolUse(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=getattr(block, "input", None) or {},
                    )
                )
                content.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", None) or {},
                    }
                )

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        stop_reason = getattr(response, "stop_reason", "") or ""
        self._record(input_tokens, output_tokens, stop_reason)
        return ConverseResult(
            text="\n".join(texts).strip(),
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            assistant_message={"role": "assistant", "content": content},
        )


def build_client(
    purpose: str = PURPOSE_RELATIONSHIP,
    settings: AgentSettings | None = None,
) -> AgentClient:
    """Construct the client for one agent role."""
    settings = settings or load_settings()
    model_id = settings.model_for(purpose)
    if settings.provider == PROVIDER_ANTHROPIC:
        return AnthropicAgentClient(model_id, settings)
    if settings.provider == PROVIDER_BOTO3:
        return Boto3AgentClient(model_id, settings)
    raise AgentUnavailable(f"Unknown agent provider '{settings.provider}'")


# ── tool loop ──────────────────────────────────────────────────────────────────


def _alternating(turns: list[dict]) -> list[dict]:
    """Merge consecutive same-role turns and drop a leading assistant turn."""
    merged: list[dict] = []
    for turn in turns:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += f"\n\n{content}"
        else:
            merged.append({"role": role, "content": content})
    while merged and merged[0]["role"] == "assistant":
        merged.pop(0)
    return merged


@dataclass
class LoopResult:
    """Outcome of a bounded tool-use conversation."""

    final_text: str
    submission: dict | None
    stop_reason: str
    iterations: int
    tool_calls: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def run_tool_loop(
    client: AgentClient,
    *,
    system: str,
    prompt: str,
    tools: list[ToolSpec],
    dispatch,
    submit_tool: str | None = None,
    history: list[dict] | None = None,
) -> LoopResult:
    """Drive a tool-use conversation until the agent submits its conclusion.

    ``dispatch(name, arguments)`` executes one read-only tool and returns a
    JSON-serialisable result. The loop ends when the agent calls ``submit_tool``
    (whose arguments are returned untouched for the caller to validate), when the
    model stops asking for tools, or when the iteration cap is reached — so a
    confused agent costs a bounded number of calls rather than an open-ended run.

    Without a ``submit_tool`` the turn ends when the model answers in text,
    which is what a conversational turn needs. ``history`` is earlier turns as
    ``{"role": "user"|"assistant", "content": str}``; roles must alternate and
    start with ``user``, so consecutive same-role turns are merged.
    """
    turns = _alternating([*(history or []), {"role": "user", "content": prompt}])
    messages: list[dict] = [
        (client.assistant_message if turn["role"] == "assistant" else client.user_message)(
            turn["content"]
        )
        for turn in turns
    ]
    called: list[str] = []
    iterations = 0
    final_text = ""
    stop_reason = ""

    while iterations < client.settings.max_tool_iterations:
        iterations += 1
        result = client.converse(system=system, messages=messages, tools=tools)
        final_text = result.text or final_text
        stop_reason = result.stop_reason

        if not result.wants_tools:
            break

        messages.append(result.assistant_message)

        submission: dict | None = None
        outcomes: list[ToolOutcome] = []
        for use in result.tool_uses:
            called.append(use.name)
            if submit_tool and use.name == submit_tool:
                submission = use.input
                outcomes.append(
                    ToolOutcome(tool_use_id=use.id, content={"accepted": True})
                )
                continue
            try:
                outcomes.append(
                    ToolOutcome(tool_use_id=use.id, content=dispatch(use.name, use.input))
                )
            except Exception as exc:  # noqa: BLE001 — surfaced back to the model
                logger.warning("[agent] tool %s failed: %s", use.name, exc)
                outcomes.append(
                    ToolOutcome(
                        tool_use_id=use.id,
                        content={"error": str(exc)},
                        is_error=True,
                    )
                )

        if submission is not None:
            return LoopResult(
                final_text=final_text,
                submission=submission,
                stop_reason=stop_reason,
                iterations=iterations,
                tool_calls=called,
                usage=client.usage.to_dict(),
            )

        reply = client.tool_result_message(outcomes)
        if submit_tool and iterations == client.settings.max_tool_iterations - 1:
            # Without this an agent still investigating on its last turn ends the
            # loop with nothing submitted, and everything it found is lost.
            reply["content"].extend(
                client.user_message(
                    f"This is your last turn. Call {submit_tool} now with what you "
                    "have found; do not call any other tool."
                )["content"]
            )
        messages.append(reply)

    return LoopResult(
        final_text=final_text,
        submission=None,
        stop_reason=stop_reason or "max_iterations",
        iterations=iterations,
        tool_calls=called,
        usage=client.usage.to_dict(),
    )

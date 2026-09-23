"""Declarative conversation flows and the runner that advances them."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wa_kit.conversation import ConversationState
from wa_kit.models import Message, ModelClient

log = logging.getLogger("wa_kit.flow")

Validator = Callable[[str], bool]
PromptSource = str | Callable[[ConversationState], str] | None
NextSource = str | Callable[[ConversationState], str | None] | None


# --- validators ---------------------------------------------------------------------


def one_of(*choices: str) -> Validator:
    lowered = {c.lower() for c in choices}
    return lambda v: v.strip().lower() in lowered


def min_length(n: int) -> Validator:
    return lambda v: len(v.strip()) >= n


def yes_no(v: str) -> bool:
    return v.strip().lower() in {"yes", "y", "no", "n", "ja", "nee", "yebo", "cha"}


def is_yes(v: str) -> bool:
    return v.strip().lower() in {"yes", "y", "ja", "yebo"}


# --- stages ------------------------------------------------------------------------


@dataclass
class Stage:
    """One step in a flow.

    - ``prompt`` is sent when the stage is entered (str, ``str.format``-ed with slots,
      or a callable of the state).
    - ``slot`` is the slot the user's next message fills; ``None`` means the stage
      is a pass-through (prompt then move on to ``next``).
    - ``validator`` rejects input; a rejection re-prompts (``error_prompt`` or the
      prompt) up to ``FlowRunner.max_clarify`` times, then escalates.
    - ``next`` is a stage name or a function of the state returning one.
    """

    name: str
    prompt: PromptSource = None
    slot: str | None = None
    validator: Validator | None = None
    next: NextSource = None
    error_prompt: str | None = None
    terminal: bool = False


@dataclass
class LLMStage(Stage):
    """A stage whose input is interpreted by a ``ModelClient`` into an ``LLMDecision``."""

    model: ModelClient | None = None
    system_prompt: str = "You are a helpful WhatsApp assistant for a small business."
    fallback_reply: str = "Sorry, I didn't quite catch that. Could you say it another way?"
    history_limit: int = 12

    def __post_init__(self) -> None:
        if self.model is None:
            raise TypeError("LLMStage requires model=")


class LLMDecision(BaseModel):
    """Strict output contract for an ``LLMStage``."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    reply: str
    slots_extracted: dict[str, str] = Field(default_factory=dict)
    needs_human: bool = False


class DecisionParseError(ValueError):
    pass


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_decision(raw: str) -> LLMDecision:
    text = _FENCE.sub("", raw.strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise DecisionParseError("no JSON object found in model output")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"invalid JSON: {exc.msg}") from exc
    try:
        return LLMDecision.model_validate(data)
    except ValidationError as exc:
        raise DecisionParseError(f"schema mismatch: {exc.errors()[0]['msg']}") from exc


SCHEMA_INSTRUCTIONS = (
    "Reply with ONLY a JSON object matching this schema, no prose:\n"
    + json.dumps(LLMDecision.model_json_schema(), separators=(",", ":"))
)


# --- flow -------------------------------------------------------------------------


class Flow:
    def __init__(self, name: str, stages: list[Stage], *, start: str | None = None) -> None:
        if not stages:
            raise ValueError("a Flow needs at least one stage")
        self.name = name
        self.stages: dict[str, Stage] = {}
        for s in stages:
            if s.name in self.stages:
                raise ValueError(f"duplicate stage name: {s.name}")
            self.stages[s.name] = s
        self.start = start or stages[0].name
        if self.start not in self.stages:
            raise ValueError(f"unknown start stage: {self.start}")
        for s in stages:
            if isinstance(s.next, str) and s.next not in self.stages:
                raise ValueError(f"stage {s.name!r} points to unknown stage {s.next!r}")

    def stage(self, name: str) -> Stage:
        return self.stages[name]


@dataclass
class StepResult:
    replies: list[str] = field(default_factory=list)
    escalate: bool = False
    reason: str | None = None
    decision: LLMDecision | None = None
    used_fallback: bool = False


class _SlotDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class FlowRunner:
    """Advances a ``ConversationState`` through a ``Flow`` one inbound message at a time.

    The runner never talks to a transport; it mutates the state and returns the
    replies to send. ``redact`` is applied to everything handed to a model.
    """

    def __init__(
        self,
        flow: Flow,
        *,
        max_clarify: int = 2,
        redact: Callable[[str], str] | None = None,
        clarify_prefix: str = "Sorry, I didn't understand that. ",
    ) -> None:
        self.flow = flow
        self.max_clarify = max_clarify
        self.redact = redact or (lambda s: s)
        self.clarify_prefix = clarify_prefix

    # -- public ---------------------------------------------------------------

    async def step(self, state: ConversationState, text: str) -> StepResult:
        if state.stage is None or state.stage not in self.flow.stages:
            state.reset_flow()
            return StepResult(replies=self._enter(state, self.flow.start))

        stage = self.flow.stage(state.stage)
        if stage.terminal:
            state.reset_flow()
            return StepResult(replies=self._enter(state, self.flow.start))

        if isinstance(stage, LLMStage):
            return await self._llm_step(state, stage, text)

        value = text.strip()
        if stage.slot is None:
            # A slot-less stage is only ever waited on if it has no next; restart.
            state.reset_flow()
            return StepResult(replies=self._enter(state, self.flow.start))

        ok = stage.validator(value) if stage.validator else bool(value)
        if not ok:
            return self._clarify(state, stage)

        state.slots[stage.slot] = value
        state.clarify_count = 0
        return StepResult(replies=self._advance(state, stage))

    # -- internals -------------------------------------------------------------

    def render(self, stage: Stage, state: ConversationState) -> str | None:
        p = stage.prompt
        if p is None:
            return None
        if callable(p):
            return p(state)
        return p.format_map(_SlotDict(state.slots))

    def _resolve_next(self, stage: Stage, state: ConversationState) -> str | None:
        nxt = stage.next(state) if callable(stage.next) else stage.next
        if nxt is not None and nxt not in self.flow.stages:
            raise KeyError(f"stage {stage.name!r} routed to unknown stage {nxt!r}")
        return nxt

    def _enter(self, state: ConversationState, name: str) -> list[str]:
        """Enter ``name``; pass through slot-less stages, emitting each prompt."""
        replies: list[str] = []
        for _ in range(len(self.flow.stages) + 1):
            stage = self.flow.stage(name)
            state.stage = name
            prompt = self.render(stage, state)
            if prompt:
                replies.append(prompt)
            if stage.terminal or stage.slot or isinstance(stage, LLMStage):
                return replies
            nxt = self._resolve_next(stage, state)
            if nxt is None:
                state.stage = None
                return replies
            name = nxt
        raise RuntimeError("flow pass-through loop exceeded stage count")

    def _advance(self, state: ConversationState, stage: Stage) -> list[str]:
        nxt = self._resolve_next(stage, state)
        if nxt is None:
            state.stage = None
            return []
        return self._enter(state, nxt)

    def _clarify(self, state: ConversationState, stage: Stage) -> StepResult:
        state.clarify_count += 1
        if state.clarify_count > self.max_clarify:
            state.clarify_count = 0
            return StepResult(escalate=True, reason="clarify_limit")
        prompt = stage.error_prompt or (self.clarify_prefix + (self.render(stage, state) or ""))
        return StepResult(replies=[prompt.strip()])

    async def _llm_step(self, state: ConversationState, stage: LLMStage, text: str) -> StepResult:
        assert stage.model is not None
        messages = self._messages(state, stage, text)
        system = f"{stage.system_prompt}\n\n{SCHEMA_INSTRUCTIONS}"

        try:
            decision = await self._decide(stage, messages, system)
        except DecisionParseError as exc:
            log.warning("LLMStage %s: fallback after retry (%s)", stage.name, exc)
            return self._llm_fallback(state, stage)
        except Exception:
            # Provider down / timeout / auth error: the user must not get silence. The
            # deterministic fallback reply goes out and, after max_clarify, a human.
            log.exception("LLMStage %s: model call failed; using fallback reply", stage.name)
            return self._llm_fallback(state, stage)

        state.clarify_count = 0
        state.slots.update(decision.slots_extracted)
        if stage.slot and stage.slot not in decision.slots_extracted:
            state.slots[stage.slot] = text.strip()
        state.slots["intent"] = decision.intent

        replies = [decision.reply] if decision.reply else []
        if decision.needs_human:
            return StepResult(replies=replies, escalate=True, reason="llm_needs_human", decision=decision)
        replies += self._advance(state, stage)
        return StepResult(replies=replies, decision=decision)

    async def _decide(self, stage: LLMStage, messages: list[Message], system: str) -> LLMDecision:
        """One model call, plus one self-correction retry if the output fails the schema."""
        assert stage.model is not None
        raw = await stage.model.complete(messages, system=system)
        try:
            return parse_decision(raw)
        except DecisionParseError as first_error:
            log.info("LLMStage %s: bad output (%s); retrying once", stage.name, first_error)
            correction: list[Message] = [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Your previous reply was rejected: {first_error}. "
                        "Reply again with ONLY the JSON object, nothing else."
                    ),
                },
            ]
            raw2 = await stage.model.complete(messages + correction, system=system)
            return parse_decision(raw2)

    def _llm_fallback(self, state: ConversationState, stage: LLMStage) -> StepResult:
        state.clarify_count += 1
        if state.clarify_count > self.max_clarify:
            state.clarify_count = 0
            return StepResult(escalate=True, reason="llm_unparseable", used_fallback=True)
        return StepResult(replies=[stage.fallback_reply], used_fallback=True)

    def _messages(self, state: ConversationState, stage: LLMStage, text: str) -> list[Message]:
        history = [
            {"role": h.role, "content": self.redact(h.content)}
            for h in state.history[-stage.history_limit :]
            if h.role in ("user", "assistant")
        ]
        current = self.redact(text)
        if not history or history[-1] != {"role": "user", "content": current}:
            history.append({"role": "user", "content": current})
        if state.slots:
            # Slots hold the *raw* user input (the business needs it); the model does not.
            known: dict[str, Any] = {
                k: self.redact(v) if isinstance(v, str) else v
                for k, v in state.slots.items()
                if not k.startswith("_")
            }
            history.insert(0, {"role": "user", "content": f"Known so far: {json.dumps(known)}"})
        return history

"""Template registry + a sender that enforces the 24h window, approval, consent, quiet hours."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from wa_kit.compliance import Compliance, OptedOutError, QuietHoursError
from wa_kit.conversation import ConversationState, ConversationStore
from wa_kit.messages import utcnow
from wa_kit.transport.base import SendResult, Transport

Category = Literal["utility", "marketing", "authentication"]


class TemplateError(ValueError):
    pass


class TemplateNotFound(TemplateError):
    pass


class TemplateNotApproved(TemplateError):
    pass


class TemplateParamError(TemplateError):
    pass


class OutsideWindowError(PermissionError):
    """Free-form send attempted more than 24h after the user's last message."""


class Template(BaseModel):
    """A Meta message template as registered in your WABA.

    ``body`` mirrors the approved body with ``{param}`` placeholders; ``params`` is
    the ordered list of parameter names (Meta positional ``{{1}}``, ``{{2}}`` ...).
    """

    name: str
    body: str
    params: list[str] = Field(default_factory=list)
    language: str = "en"
    category: Category = "utility"
    approved: bool = True

    def render(self, **values: str) -> tuple[str, list[str]]:
        """Validate the parameter set and return ``(preview_text, ordered_values)``."""
        missing = [p for p in self.params if p not in values]
        extra = [k for k in values if k not in self.params]
        if missing or extra:
            raise TemplateParamError(
                f"template {self.name!r} expects {self.params}; missing={missing} extra={extra}"
            )
        ordered = [str(values[p]) for p in self.params]
        preview = self.body
        for p, v in zip(self.params, ordered, strict=True):
            preview = preview.replace("{" + p + "}", v)
        return preview, ordered


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class TemplateRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, Template] = {}

    def register(self, template: Template) -> Template:
        found = _PLACEHOLDER.findall(template.body)
        if set(found) != set(template.params):
            raise TemplateParamError(
                f"template {template.name!r}: body placeholders {sorted(set(found))} "
                f"do not match params {template.params}"
            )
        self._templates[template.name] = template
        return template

    def get(self, name: str) -> Template:
        try:
            return self._templates[name]
        except KeyError:
            raise TemplateNotFound(f"no template registered as {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._templates

    def names(self) -> list[str]:
        return sorted(self._templates)


class TemplateSender:
    """The only sanctioned way to message a user *proactively*.

    - ``send_text``: free-form; refused outside the 24h window.
    - ``send_template``: refused if the template is unapproved, if it is marketing
      without recorded consent, or (marketing) inside quiet hours.
    - ``send``: text if the window is open, else the named template, else refuse.

    Every path refuses a user who has opted out (``OptedOutError``).
    """

    def __init__(
        self,
        registry: TemplateRegistry,
        transport: Transport,
        store: ConversationStore,
        *,
        compliance: Compliance | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.registry = registry
        self.transport = transport
        self.store = store
        self.compliance = compliance
        self._clock = clock

    def window_open(self, wa_id: str) -> bool:
        return self.store.can_send_freeform(wa_id, now=self._clock())

    def _state_for_send(self, wa_id: str) -> ConversationState:
        state = self.store.get(wa_id)
        if state.opted_out:
            raise OptedOutError(f"{wa_id} has opted out; nothing proactive may be sent")
        return state

    async def send_text(self, wa_id: str, body: str) -> SendResult:
        state = self._state_for_send(wa_id)
        if not state.can_send_freeform(self._clock()):
            raise OutsideWindowError(
                f"{wa_id}: outside the 24h service window; send an approved template instead"
            )
        return await self.transport.send_text(wa_id, body)

    async def send_template(
        self, wa_id: str, template: str, *, force_quiet_hours: bool = False, **params: str
    ) -> SendResult:
        """Send ``template`` with named ``params`` (e.g. ``name="Thandi"``)."""
        tpl = self.registry.get(template)
        if not tpl.approved:
            raise TemplateNotApproved(f"template {template!r} is not approved by Meta yet")
        state = self._state_for_send(wa_id)
        if tpl.category == "marketing":
            if self.compliance is not None:
                self.compliance.require_consent(state)
                if not force_quiet_hours and self.compliance.is_quiet_hours(self._clock()):
                    raise QuietHoursError(
                        f"marketing template {template!r} blocked until "
                        f"{self.compliance.next_send_time(self._clock()).isoformat()}"
                    )
            elif not state.consent_recorded:
                raise PermissionError(f"no marketing consent recorded for {wa_id}")
        _, ordered = tpl.render(**params)
        return await self.transport.send_template(wa_id, tpl.name, tpl.language, ordered)

    async def send(
        self, wa_id: str, text: str, *, template: str | None = None, **params: str
    ) -> SendResult:
        state = self._state_for_send(wa_id)
        if state.can_send_freeform(self._clock()):
            return await self.transport.send_text(wa_id, text)
        if template is None or template not in self.registry or not self.registry.get(template).approved:
            raise OutsideWindowError(
                f"{wa_id}: outside the 24h window and no approved template was given"
            )
        return await self.send_template(wa_id, template, **params)

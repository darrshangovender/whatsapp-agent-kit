"""Model client protocol and a scriptable mock. No provider SDK is imported here."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

Message = dict[str, str]  # {"role": "user" | "assistant", "content": "..."}


@runtime_checkable
class ModelClient(Protocol):
    async def complete(self, messages: list[Message], *, system: str | None = None) -> str: ...


class MockModel:
    """Returns scripted responses in order (or from a callable) and records every call.

    ``script`` may be a list of strings, or a callable ``(messages, system) -> str``.
    With ``loop=True`` the list is cycled instead of exhausted.
    """

    def __init__(
        self,
        script: Iterable[str] | Callable[[list[Message], str | None], str],
        *,
        loop: bool = False,
    ) -> None:
        self._fn = script if callable(script) else None
        self._script: list[str] = [] if callable(script) else list(script)
        self._i = 0
        self.loop = loop
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[Message], *, system: str | None = None) -> str:
        self.calls.append({"messages": [dict(m) for m in messages], "system": system})
        if self._fn is not None:
            return self._fn(messages, system)
        if not self._script:
            raise RuntimeError("MockModel has no scripted responses")
        if self._i >= len(self._script):
            if not self.loop:
                raise RuntimeError("MockModel script exhausted")
            self._i = 0
        out = self._script[self._i]
        self._i += 1
        return out

    @property
    def call_count(self) -> int:
        return len(self.calls)

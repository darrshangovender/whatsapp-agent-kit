"""Opt-out keywords, quiet hours, and POPIA-aware PII handling.

POPIA (Protection of Personal Information Act, South Africa) is why history is
redacted *before* it is stored, why nothing reaches the LLM unredacted, why there
is a retention purge, and why marketing templates need a recorded consent flag.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from wa_kit.conversation import ConversationState, ConversationStore
from wa_kit.messages import utcnow

# --- opt-out ----------------------------------------------------------------------

#: English, Afrikaans ("hou op" = stop), isiZulu ("yeka" = stop/leave it).
DEFAULT_OPT_OUT_KEYWORDS: frozenset[str] = frozenset(
    {"STOP", "UNSUBSCRIBE", "HOU OP", "YEKA", "OPT OUT"}
)
DEFAULT_OPT_IN_KEYWORDS: frozenset[str] = frozenset({"START", "BEGIN", "QALA"})

#: Unicode categories treated as "not a word character" for keyword matching: punctuation
#: (P*), symbols incl. emoji (S*), combining marks / variation selectors (M*) and format
#: characters such as the zero-width joiner (Cf). iOS turns ``...`` into ``…`` (Po) and
#: users send ``Stop 🛑`` — ``string.punctuation`` catches neither.
_STRIP_CATEGORIES = ("P", "S", "M")


def normalise_text(text: str) -> str:
    """Replace punctuation/symbols with spaces and collapse whitespace; case is untouched."""
    chars = [
        " " if unicodedata.category(ch)[0] in _STRIP_CATEGORIES or unicodedata.category(ch) == "Cf"
        else ch
        for ch in unicodedata.normalize("NFC", text)
    ]
    return " ".join("".join(chars).split())


def normalise_keyword(text: str) -> str:
    """Upper-case, strip punctuation, collapse whitespace — so ``"Stop!"`` matches ``STOP``."""
    return normalise_text(text).upper()


# --- quiet hours ------------------------------------------------------------------


@dataclass(frozen=True)
class QuietHours:
    """A daily window in which proactive messages must not go out.

    ``start > end`` means the window crosses midnight (the default 20:00 → 08:00).
    Datetimes with a tzinfo are converted to ``tz``; naive datetimes are assumed
    to already be local to ``tz``.
    """

    start: time = time(20, 0)
    end: time = time(8, 0)
    tz: str = "Africa/Johannesburg"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def local(self, now: datetime | None = None) -> datetime:
        if now is None:
            return datetime.now(self.zone)
        if now.tzinfo is None:
            return now.replace(tzinfo=self.zone)
        return now.astimezone(self.zone)

    def is_quiet(self, now: datetime | None = None) -> bool:
        if self.start == self.end:
            return False
        t = self.local(now).time()
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_allowed(self, now: datetime | None = None) -> datetime:
        """Earliest local datetime at/after ``now`` that is outside the quiet window."""
        local = self.local(now)
        if not self.is_quiet(local):
            return local
        candidate = local.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0
        )
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate


# --- PII ----------------------------------------------------------------------


@dataclass(frozen=True)
class PIIMatch:
    kind: str
    start: int
    end: int
    value: str


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_sa_id(candidate: str) -> bool:
    """13 digits: YYMMDD SSSS C A Z — valid date, citizenship 0/1, Luhn check digit."""
    if len(candidate) != 13 or not candidate.isdigit():
        return False
    yy, mm, dd = int(candidate[0:2]), int(candidate[2:4]), int(candidate[4:6])
    if not _plausible_birth_date(yy, mm, dd):
        return False
    if candidate[10] not in "01":
        return False
    return luhn_ok(candidate)


def _plausible_birth_date(yy: int, mm: int, dd: int) -> bool:
    """The ID carries no century, so accept a date that is real in either 19yy or 20yy."""
    for century in (1900, 2000):
        try:
            date(century + yy, mm, dd)
        except ValueError:
            continue
        return True
    return False


class PIIDetector:
    """Pattern-based detector for the PII an SA SME bot is most likely to receive.

    Order matters: ID numbers are redacted first so their digit runs are not
    re-matched as phone or bank-account numbers.
    """

    ID_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")
    EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    PHONE_RE = re.compile(r"(?<![\d+])(?:\+27|0027|27|0)[\s-]?[1-8]\d(?:[\s-]?\d){7}(?!\d)")
    BANK_RE = re.compile(r"(?<!\d)\d{8,12}(?!\d)")

    PLACEHOLDERS = {
        "sa_id": "[SA_ID]",
        "email": "[EMAIL]",
        "phone": "[PHONE]",
        "bank_account": "[BANK_ACCOUNT]",
    }

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        taken: list[tuple[int, int]] = []

        def free(s: int, e: int) -> bool:
            return all(e <= ts or s >= te for ts, te in taken)

        for m in self.ID_RE.finditer(text):
            if is_valid_sa_id(m.group()):
                matches.append(PIIMatch("sa_id", m.start(), m.end(), m.group()))
                taken.append((m.start(), m.end()))
        for kind, rx in (("email", self.EMAIL_RE), ("phone", self.PHONE_RE), ("bank_account", self.BANK_RE)):
            for m in rx.finditer(text):
                if free(m.start(), m.end()):
                    matches.append(PIIMatch(kind, m.start(), m.end(), m.group()))
                    taken.append((m.start(), m.end()))
        matches.sort(key=lambda x: x.start)
        return matches

    def redact(self, text: str) -> str:
        out: list[str] = []
        pos = 0
        for m in self.detect(text):
            out.append(text[pos : m.start])
            out.append(self.PLACEHOLDERS[m.kind])
            pos = m.end
        out.append(text[pos:])
        return "".join(out)

    def contains_pii(self, text: str) -> bool:
        return bool(self.detect(text))


# --- errors ---------------------------------------------------------------------


class ComplianceError(PermissionError):
    """Base class for refusals the compliance layer makes."""


class ConsentRequired(ComplianceError):
    """Marketing content attempted without ``consent_recorded``."""


class QuietHoursError(ComplianceError):
    """Proactive send attempted inside the tenant's quiet hours."""


class OptedOutError(ComplianceError):
    """Proactive send attempted to a user who has opted out."""


# --- facade --------------------------------------------------------------------


class Compliance:
    """One object the agent and template sender consult for every policy question."""

    def __init__(
        self,
        store: ConversationStore,
        *,
        quiet_hours: QuietHours | None = None,
        pii: PIIDetector | None = None,
        opt_out_keywords: frozenset[str] | set[str] = DEFAULT_OPT_OUT_KEYWORDS,
        opt_in_keywords: frozenset[str] | set[str] = DEFAULT_OPT_IN_KEYWORDS,
        opt_out_reply: str = "You've been unsubscribed and won't receive further messages. Reply START to opt back in.",
        opt_in_reply: str = "Welcome back — you're opted in again.",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.quiet_hours = quiet_hours or QuietHours()
        self.pii = pii or PIIDetector()
        self.opt_out_keywords = frozenset(normalise_keyword(k) for k in opt_out_keywords)
        self.opt_in_keywords = frozenset(normalise_keyword(k) for k in opt_in_keywords)
        self.opt_out_reply = opt_out_reply
        self.opt_in_reply = opt_in_reply
        self._clock = clock

    # opt-out / opt-in
    def is_opt_out(self, text: str) -> bool:
        return normalise_keyword(text) in self.opt_out_keywords

    def is_opt_in(self, text: str) -> bool:
        return normalise_keyword(text) in self.opt_in_keywords

    def opt_out(self, state: ConversationState) -> str:
        state.opted_out = True
        state.reset_flow()
        self.store.save(state)
        return self.opt_out_reply

    def opt_in(self, state: ConversationState) -> str:
        state.opted_out = False
        self.store.save(state)
        return self.opt_in_reply

    # PII
    def redact(self, text: str) -> str:
        return self.pii.redact(text)

    def redact_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{**m, "content": self.redact(m.get("content", ""))} for m in messages]

    # quiet hours
    def is_quiet_hours(self, now: datetime | None = None) -> bool:
        return self.quiet_hours.is_quiet(now or self._clock())

    def next_send_time(self, now: datetime | None = None) -> datetime:
        return self.quiet_hours.next_allowed(now or self._clock())

    # consent
    def record_consent(self, wa_id: str) -> ConversationState:
        state = self.store.get(wa_id)
        state.consent_recorded = True
        self.store.save(state)
        return state

    def require_consent(self, state: ConversationState) -> None:
        if not state.consent_recorded:
            raise ConsentRequired(
                f"no marketing consent recorded for {state.wa_id}; call record_consent() first"
            )

    # retention
    def purge_older_than(self, days: int) -> int:
        return self.store.purge_older_than(days, now=self._clock())

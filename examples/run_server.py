"""Serve the webhook with the real Cloud API transport, configured from env vars.

    cp .env.example .env   # fill in WA_* values
    uvicorn examples.run_server:app --port 8000

Meta must reach ``https://<your-host>/webhook``; point your Meta app's webhook
config there with the same WA_VERIFY_TOKEN. The flow is the booking bot from
``booking_bot.py`` with its keyword ``MockModel`` — swap ``model`` for any object
implementing ``wa_kit.models.ModelClient`` to use a real LLM.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.booking_bot import HANDOFF_TRIGGERS, build_flow, classify  # noqa: E402
from wa_kit import (  # noqa: E402
    Agent,
    Compliance,
    ConversationStore,
    DedupStore,
    HandoffManager,
    LogSink,
    MetaCloudTransport,
    MockModel,
    QuietHours,
    WebhookSink,
    create_app,
)
from wa_kit.handoff import EscalationSink  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"missing required environment variable {name} (see .env.example)")
    return value


def build() -> tuple[Agent, MetaCloudTransport]:
    db_path = os.environ.get("WA_DB_PATH", "./data/wa_kit.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    store = ConversationStore(db_path)
    transport = MetaCloudTransport(
        phone_number_id=_env("WA_PHONE_NUMBER_ID"),
        access_token=_env("WA_ACCESS_TOKEN"),
        app_secret=_env("WA_APP_SECRET"),
        api_version=os.environ.get("WA_API_VERSION", "v20.0"),
    )
    sinks: list[EscalationSink] = [LogSink()]
    if os.environ.get("WA_ESCALATION_WEBHOOK"):
        sinks.append(WebhookSink(os.environ["WA_ESCALATION_WEBHOOK"]))

    agent = Agent(
        store=store,
        transport=transport,
        flow=build_flow(MockModel(classify)),
        compliance=Compliance(
            store, quiet_hours=QuietHours(tz=os.environ.get("WA_TIMEZONE", "Africa/Johannesburg"))
        ),
        handoff=HandoffManager(store, sinks, triggers=HANDOFF_TRIGGERS),
    )
    return agent, transport


agent, transport = build()
app = create_app(
    transport=transport,
    verify_token=_env("WA_VERIFY_TOKEN"),
    handler=agent.handle,
    dedup=DedupStore(agent.store.connection),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

"""whatsapp-agent-kit — stateful LLM agents for WhatsApp Business, POPIA-aware."""

from wa_kit.agent import Agent
from wa_kit.compliance import (
    Compliance,
    ConsentRequired,
    OptedOutError,
    PIIDetector,
    QuietHours,
    QuietHoursError,
    is_valid_sa_id,
)
from wa_kit.conversation import ConversationState, ConversationStore, HistoryEntry
from wa_kit.flow import (
    Flow,
    FlowRunner,
    LLMDecision,
    LLMStage,
    Stage,
    StepResult,
    is_yes,
    min_length,
    one_of,
    yes_no,
)
from wa_kit.handoff import Escalation, EscalationSink, HandoffManager, LogSink, WebhookSink
from wa_kit.messages import InboundMessage, Location, Media, MessageType
from wa_kit.models import MockModel, ModelClient
from wa_kit.templates import (
    OutsideWindowError,
    Template,
    TemplateNotApproved,
    TemplateNotFound,
    TemplateParamError,
    TemplateRegistry,
    TemplateSender,
)
from wa_kit.transport import (
    MetaCloudTransport,
    MockTransport,
    SendResult,
    Transport,
    TransportError,
)
from wa_kit.webhook import DedupStore, create_app, parse_payload, run_worker

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "Compliance",
    "ConsentRequired",
    "ConversationState",
    "ConversationStore",
    "DedupStore",
    "Escalation",
    "EscalationSink",
    "Flow",
    "FlowRunner",
    "HandoffManager",
    "HistoryEntry",
    "InboundMessage",
    "LLMDecision",
    "LLMStage",
    "Location",
    "LogSink",
    "Media",
    "MessageType",
    "MetaCloudTransport",
    "MockModel",
    "MockTransport",
    "ModelClient",
    "OptedOutError",
    "OutsideWindowError",
    "PIIDetector",
    "QuietHours",
    "QuietHoursError",
    "SendResult",
    "Stage",
    "StepResult",
    "Template",
    "TemplateNotApproved",
    "TemplateNotFound",
    "TemplateParamError",
    "TemplateRegistry",
    "TemplateSender",
    "Transport",
    "TransportError",
    "WebhookSink",
    "create_app",
    "is_valid_sa_id",
    "is_yes",
    "min_length",
    "one_of",
    "parse_payload",
    "run_worker",
    "yes_no",
]

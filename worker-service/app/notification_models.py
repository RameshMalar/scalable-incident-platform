"""Worker-local subscription projection and versioned notification contract.

The Common Event Model is nested unchanged. These models are not used by the
current worker entry point or its Service Bus publisher in Phase 2A.
"""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StringConstraints,
    field_validator,
)

from common_event_model import IncidentEvent


NonEmpty = Annotated[
    str, StringConstraints(strict=True, min_length=1, pattern=r"\S")
]
TelegramChatId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^-?[0-9]{1,20}$")
]


class InvalidSubscription(ValueError):
    """Stored configuration cannot safely be converted into a route."""


class SubscriptionRecord(BaseModel):
    """Read only the routing fields of a Phase 1 Cosmos subscription.

    API timestamps and Cosmos metadata are intentionally ignored. All routing
    fields are required; missing enabled/destination fields never get defaults.
    Event and tenant strings are not normalized: matching remains exact.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: NonEmpty
    tenant_id: NonEmpty
    event_type: NonEmpty
    channel: Literal["telegram"]
    telegram_chat_id: TelegramChatId
    enabled: StrictBool
    # Present only on subscriptions created through account setup; such routes
    # also require app.owner_eligibility. Absent/null keeps legacy behaviour.
    owner_object_id: NonEmpty | None = None


class NotificationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    subscription_id: NonEmpty
    channel: Literal["telegram"]
    telegram_chat_id: TelegramChatId
    incident: IncidentEvent

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

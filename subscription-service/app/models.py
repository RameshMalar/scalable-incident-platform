from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints, model_validator


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ChatId = Annotated[str, StringConstraints(pattern=r"^-?[0-9]{1,20}$")]


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: NonEmpty
    channel: Literal["telegram"] = "telegram"
    telegram_chat_id: ChatId
    enabled: StrictBool = True


class SubscriptionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: NonEmpty | None = None
    channel: Literal["telegram"] | None = None
    telegram_chat_id: ChatId | None = None
    enabled: StrictBool | None = None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("at least one change is required")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("subscription fields cannot be null")
        return self


class Subscription(SubscriptionCreate):
    id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime
    # Set only by authenticated account setup (app.telegram_self_service).
    # Such a subscription is delivered only while its owner stays authorized
    # for the tenant and verified for the chat. Clients cannot set it.
    owner_object_id: str | None = None


class SubscriptionPage(BaseModel):
    items: list[Subscription]
    next_offset: int | None = None

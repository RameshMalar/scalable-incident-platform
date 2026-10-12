"""Version-1 routed contract; IncidentEvent remains unchanged."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from common_event_model import IncidentEvent


TelegramChatId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^-?[0-9]{1,20}$")
]


class NotificationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    subscription_id: Annotated[
        str, StringConstraints(strict=True, min_length=1, pattern=r"\S")
    ]
    channel: Literal["telegram"]
    telegram_chat_id: TelegramChatId
    incident: IncidentEvent

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

"""Support tickets."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import TicketStatus
from app.modules.admin_api.schemas.common import OrmModel


class TicketOut(OrmModel):
    id: int
    user_id: int
    subject: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime


class TicketItem(BaseModel):
    ticket: TicketOut
    username: str | None
    first_name: str | None
    messages: int
    last_message: str | None


class TicketMessageOut(OrmModel):
    id: int
    author_id: int
    is_admin: bool
    text: str | None
    file_id: str | None
    created_at: datetime


class TicketView(BaseModel):
    ticket: TicketOut
    username: str | None
    first_name: str | None
    language: str | None
    messages: list[TicketMessageOut]


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000, description="Plain text; it is shown to the user as is")
    close: bool = False


class ReplyOut(BaseModel):
    message: TicketMessageOut
    delivered: bool


class TicketCounts(BaseModel):
    open: int
    answered: int
    closed: int

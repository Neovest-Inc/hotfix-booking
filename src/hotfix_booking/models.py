"""Pydantic request/response models for the API layer."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BookRequest(BaseModel):
    version: str | None = None
    components: Any = None
    clientEnvironments: Any = None
    bookedBy: str | None = None


class Booking(BaseModel):
    id: str
    version: str
    components: list[str]
    clientEnvironments: list[str]
    bookedBy: str
    bookedAt: str
    status: str


class BookingsResponse(BaseModel):
    bookings: list[Booking] = Field(default_factory=list)

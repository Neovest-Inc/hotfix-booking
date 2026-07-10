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
    parents: list[str] = Field(default_factory=list)
    originalParents: list[str] = Field(default_factory=list)
    rebaseHistory: list[dict] = Field(default_factory=list)


class BookingsResponse(BaseModel):
    bookings: list[Booking] = Field(default_factory=list)


class CancelRequest(BaseModel):
    bookingId: str
    cancelledByEmail: str


class AffectedChild(BaseModel):
    id: str
    version: str
    bookedBy: str
    bookedByEmail: str
    previousParentVersions: list[str] = Field(default_factory=list)
    newParentVersions: list[str] = Field(default_factory=list)


class ActiveCmWarning(BaseModel):
    cmKey: str
    status: str


class CancelResponse(BaseModel):
    cancelled: dict
    affected: list[AffectedChild] = Field(default_factory=list)
    activeCmWarning: ActiveCmWarning | None = None

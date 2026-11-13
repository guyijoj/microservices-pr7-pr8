"""Входные модели HTTP и общего конверта события."""
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix):
    return f"{prefix}-{uuid4()}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Address(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class OrderItem(StrictModel):
    dish_id: str = Field(min_length=1)
    quantity: int = Field(ge=1, le=99, strict=True)


class CreateOrder(StrictModel):
    restaurant_id: str = Field(min_length=1)
    items: list[OrderItem] = Field(min_length=1)
    delivery_address: Address


class CreatePayment(StrictModel):
    order_id: str = Field(min_length=1)


class Command(StrictModel):
    reason: Optional[str] = Field(default=None, min_length=1, max_length=500)


class Event(StrictModel):
    event_id: str
    event_type: Literal["payment.confirmed", "order.confirmed", "order.cancelled",
                        "order.prepared", "delivery.completed"]
    schema_version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    data: dict

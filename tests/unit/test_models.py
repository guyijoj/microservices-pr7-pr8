"""Unit: реальные Pydantic-модели из app/models.py; нет БД и RabbitMQ."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import Address, Command, CreateOrder, Event, OrderItem, new_id, now


@pytest.mark.parametrize("quantity", [1, 2, 99])
def test_order_item_valid_boundaries(quantity):
    assert OrderItem(dish_id="dish-1", quantity=quantity).quantity == quantity


@pytest.mark.parametrize("quantity", [0, 100, -1, 1.5, "2", True])
def test_order_item_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        OrderItem(dish_id="dish-1", quantity=quantity)


@pytest.mark.parametrize("dish_id", ["", None])
def test_order_item_requires_nonempty_dish(dish_id):
    with pytest.raises(ValidationError):
        OrderItem(dish_id=dish_id, quantity=1)


def test_order_item_rejects_unknown_field():
    with pytest.raises(ValidationError):
        OrderItem(dish_id="dish-1", quantity=1, fake_price=1)


@pytest.mark.parametrize("lat,lon", [(-90, -180), (90, 180), (55.75, 37.61)])
def test_address_valid_boundaries(lat, lon):
    result = Address(text="Москва", lat=lat, lon=lon)
    assert result.lat == lat and result.lon == lon


@pytest.mark.parametrize("lat", [-90.01, 90.01])
def test_address_rejects_invalid_latitude(lat):
    with pytest.raises(ValidationError):
        Address(text="Москва", lat=lat, lon=30)


@pytest.mark.parametrize("lon", [-180.01, 180.01])
def test_address_rejects_invalid_longitude(lon):
    with pytest.raises(ValidationError):
        Address(text="Москва", lat=55, lon=lon)


@pytest.mark.parametrize("text", ["", "x" * 501])
def test_address_rejects_invalid_text_length(text):
    with pytest.raises(ValidationError):
        Address(text=text, lat=55, lon=37)


def test_create_order_requires_items(order_body):
    with pytest.raises(ValidationError):
        CreateOrder.model_validate({**order_body, "items": []})


def test_create_order_requires_restaurant(order_body):
    with pytest.raises(ValidationError):
        CreateOrder.model_validate({**order_body, "restaurant_id": ""})


def test_create_order_rejects_extra_fields(order_body):
    with pytest.raises(ValidationError):
        CreateOrder.model_validate({**order_body, "amount_minor": 1})


def test_create_order_has_expected_nested_types(order_body):
    result = CreateOrder.model_validate(order_body)
    assert result.items[0].quantity == 2
    assert isinstance(result.delivery_address, Address)


@pytest.mark.parametrize("reason", ["x", "x" * 500])
def test_command_accepts_reason_boundaries(reason):
    assert Command(reason=reason).reason == reason


@pytest.mark.parametrize("reason", ["", "x" * 501])
def test_command_rejects_reason_boundaries(reason):
    with pytest.raises(ValidationError):
        Command(reason=reason)


def test_command_allows_missing_reason():
    assert Command().reason is None


def test_event_accepts_valid_envelope():
    event = Event.model_validate({
        "event_id": "event-1", "event_type": "payment.confirmed", "schema_version": 1,
        "occurred_at": now(), "correlation_id": "order-1", "data": {"order_id": "order-1"},
    })
    assert event.schema_version == 1
    assert event.occurred_at.tzinfo is not None


@pytest.mark.parametrize("field,value", [
    ("event_type", "payment.refunded"), ("schema_version", 2),
    ("event_id", None), ("occurred_at", "nonsense"),
])
def test_event_rejects_invalid_envelope(field, value):
    payload = {"event_id": "event-1", "event_type": "order.prepared", "schema_version": 1,
               "occurred_at": now(), "correlation_id": "order-1", "data": {"order_id": "order-1"}}
    payload[field] = value
    with pytest.raises(ValidationError):
        Event.model_validate(payload)


def test_generated_ids_have_prefix_and_are_unique():
    first, second = new_id("order"), new_id("order")
    assert first.startswith("order-") and second.startswith("order-") and first != second


def test_now_returns_utc_iso_timestamp():
    value = now()
    assert value.endswith("Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo == timezone.utc

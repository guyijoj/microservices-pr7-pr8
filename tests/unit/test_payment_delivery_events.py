"""Unit: платёж, доставка и подписчики RabbitMQ, вызванные без брокера."""
from copy import deepcopy

import pytest

from app.models import now
from app.services import (DomainError, check_payment, create_payment, delivery_command,
                          handle_event, order_command)

CUSTOMER = ("customer", "user-bocharov")
SERVICE = ("service", "system")
COURIER = ("courier", "courier-1")


def expect_error(status, func, *args):
    with pytest.raises(DomainError) as caught:
        func(*args)
    assert caught.value.status == status


def event(event_type, order_id, data, event_id="event-test"):
    return {"event_id": event_id, "event_type": event_type, "schema_version": 1,
            "occurred_at": now(), "correlation_id": order_id, "data": {"order_id": order_id, **data}}


def test_payment_uses_order_total_and_user(tx, order):
    payment = create_payment(tx, CUSTOMER, order)
    assert payment["amount_minor"] == order["amount_minor"]
    assert payment["user_id"] == order["user_id"]
    assert payment["status"] == "pending"
    assert tx.get("payment", payment["id"]) == payment


def test_cannot_pay_for_someone_else_order(tx, order):
    expect_error(403, create_payment, tx, ("customer", "other"), order)
    assert tx.all("payment") == []


def test_duplicate_payment_for_same_order_rejected(tx, order):
    create_payment(tx, CUSTOMER, order)
    expect_error(409, create_payment, tx, CUSTOMER, order)
    assert len(tx.all("payment")) == 1


def test_payment_only_for_order_awaiting_payment(tx, order):
    order["status"] = "cancelled"
    expect_error(409, create_payment, tx, CUSTOMER, order)


def test_payment_check_creates_one_event_only(tx, order):
    payment = create_payment(tx, CUSTOMER, order)
    first = check_payment(tx, SERVICE, payment["id"])
    assert first["status"] == "confirmed"
    second = check_payment(tx, SERVICE, payment["id"])
    assert second == first
    events = tx.all("_outbox")
    assert len(events) == 1
    assert events[0]["event_type"] == "payment.confirmed"
    assert events[0]["data"]["amount_minor"] == 125000


def test_payment_check_for_unknown_id_returns_404(tx):
    expect_error(404, check_payment, tx, SERVICE, "payment-missing")


def test_payment_check_not_owned_returns_403(tx, order):
    payment = create_payment(tx, CUSTOMER, order)
    expect_error(403, check_payment, tx, ("customer", "other"), payment["id"])


def test_confirmed_payment_event_marks_order_paid(tx, order):
    message = event("payment.confirmed", order["id"],
                    {"amount_minor": order["amount_minor"], "currency": "RUB"})
    handle_event(tx, "orders", message)
    assert tx.get("order", order["id"])["status"] == "paid"
    assert tx.get("_inbox", message["event_id"]) is not None


def test_wrong_payment_amount_does_not_mark_paid(tx, order):
    message = event("payment.confirmed", order["id"],
                    {"amount_minor": 5, "currency": "RUB"})
    with pytest.raises(ValueError, match="Сумма"):
        handle_event(tx, "orders", message)
    assert tx.get("order", order["id"])["status"] == "awaiting_payment"
    assert tx.get("_inbox", message["event_id"]) is None


def test_wrong_payment_currency_does_not_mark_paid(tx, order):
    message = event("payment.confirmed", order["id"],
                    {"amount_minor": order["amount_minor"], "currency": "USD"})
    with pytest.raises(ValueError):
        handle_event(tx, "orders", message)
    assert tx.get("order", order["id"])["status"] == "awaiting_payment"


def test_late_payment_does_not_reopen_cancelled_order(tx, order):
    order_command(tx, ("restaurant", "restaurant-1"), order["id"], "cancel", {"reason": "Нет продуктов"})
    message = event("payment.confirmed", order["id"],
                    {"amount_minor": order["amount_minor"], "currency": "RUB"})
    handle_event(tx, "orders", message)
    assert tx.get("order", order["id"])["status"] == "cancelled"


def test_duplicate_event_id_is_ignored(tx, order):
    message = event("payment.confirmed", order["id"],
                    {"amount_minor": order["amount_minor"], "currency": "RUB"})
    handle_event(tx, "orders", message)
    duplicate = deepcopy(message)
    duplicate["data"]["amount_minor"] = 1  # повтор с тем же ID не применяется заново
    handle_event(tx, "orders", duplicate)
    assert tx.get("order", order["id"])["status"] == "paid"
    assert len(tx.all("_inbox")) == 1


def test_event_wrong_correlation_rejected(tx, order):
    message = event("payment.confirmed", order["id"],
                    {"amount_minor": order["amount_minor"], "currency": "RUB"})
    message["correlation_id"] = "some-other-order"
    with pytest.raises(ValueError, match="correlation_id"):
        handle_event(tx, "orders", message)


def test_unknown_event_type_for_service_rejected(tx, order):
    message = event("order.prepared", order["id"], {})
    with pytest.raises(ValueError, match="Неподдерживаемое"):
        handle_event(tx, "orders", message)


def test_order_prepared_creates_delivery(tx, order):
    message = event("order.prepared", order["id"],
                    {"user_id": order["user_id"], "restaurant_id": order["restaurant_id"],
                     "pickup_address": {"text": "Ресторан", "lat": 55, "lon": 37},
                     "delivery_address": order["delivery_address"]})
    handle_event(tx, "deliveries", message)
    delivery = tx.all("delivery")[0]
    assert delivery["status"] == "assigned"
    assert delivery["courier_id"] == "courier-1"
    assert delivery["order_id"] == order["id"]


def test_second_prepared_event_cannot_create_second_delivery(tx, order):
    data = {"user_id": order["user_id"], "restaurant_id": order["restaurant_id"],
            "pickup_address": {"text": "Ресторан", "lat": 55, "lon": 37},
            "delivery_address": order["delivery_address"]}
    handle_event(tx, "deliveries", event("order.prepared", order["id"], data, "event-1"))
    handle_event(tx, "deliveries", event("order.prepared", order["id"], data, "event-2"))
    assert len(tx.all("delivery")) == 1


@pytest.fixture
def delivery(tx, order):
    item = {"id": "delivery-1", "order_id": order["id"], "user_id": order["user_id"],
            "restaurant_id": "restaurant-1", "courier_id": "courier-1",
            "status": "assigned", "created_at": now(), "delivered_at": None}
    tx.put("delivery", item["id"], item)
    return item


def test_delivery_accept_changes_status(tx, delivery):
    accepted = delivery_command(tx, COURIER, delivery["id"], "accept")
    assert accepted["status"] == "in_transit"
    delivery_command(tx, COURIER, delivery["id"], "accept")
    assert tx.all("_outbox") == []


def test_delivery_cannot_complete_before_acceptance(tx, delivery):
    expect_error(409, delivery_command, tx, COURIER, delivery["id"], "complete")


def test_delivery_other_courier_is_forbidden(tx, delivery):
    expect_error(403, delivery_command, tx, ("courier", "other-courier"), delivery["id"], "accept")


def test_delivery_complete_sets_time_and_emits_once(tx, delivery):
    delivery_command(tx, COURIER, delivery["id"], "accept")
    result = delivery_command(tx, COURIER, delivery["id"], "complete")
    second = delivery_command(tx, COURIER, delivery["id"], "complete")
    assert result["status"] == "delivered"
    assert result["delivered_at"] is not None
    assert second["delivered_at"] == result["delivered_at"]
    assert [e["event_type"] for e in tx.all("_outbox")] == ["delivery.completed"]


@pytest.mark.parametrize("event_type,expected", [
    ("order.confirmed", "подтвердил"),
    ("order.cancelled", "Нет ингредиентов"),
    ("delivery.completed", "доставлен"),
])
def test_notification_text_and_dedup(tx, order, event_type, expected):
    data = {"user_id": order["user_id"], "reason": "Нет ингредиентов"}
    message = event(event_type, order["id"], data)
    handle_event(tx, "notifications", message)
    handle_event(tx, "notifications", message)
    records = tx.all("notification")
    assert len(records) == 1
    assert records[0]["status"] == "sent"
    assert records[0]["source_event_id"] == message["event_id"]
    assert expected in records[0]["text"]

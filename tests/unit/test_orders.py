"""Unit: работа с заказами, права, переходы и Idempotency-Key."""
import pytest

from app.services import (DomainError, can_read, create_order, idempotent,
                          order_command, require, require_access)

CUSTOMER = ("customer", "user-bocharov")
RESTAURANT = ("restaurant", "restaurant-1")


def assert_domain_error(status, fn, *args):
    with pytest.raises(DomainError) as exc:
        fn(*args)
    assert exc.value.status == status
    return exc.value


def test_create_order_calculates_catalog_total(order):
    assert order["amount_minor"] == 2 * 55000 + 15000
    assert order["currency"] == "RUB"
    assert order["items"][0]["name"] == "Пицца Маргарита"
    assert order["items"][1]["price_minor"] == 15000


def test_create_order_starts_awaiting_payment(order):
    assert order["status"] == "awaiting_payment"
    assert order["cancellation_reason"] is None
    assert order["user_id"] == "user-bocharov"
    assert order["id"].startswith("order-")


def test_create_order_is_saved(tx, order):
    assert tx.get("order", order["id"]) == order


def test_invalid_restaurant_is_not_saved(tx, order_body):
    assert_domain_error(404, create_order, tx, CUSTOMER, {**order_body, "restaurant_id": "unknown"})
    assert tx.all("order") == []


def test_invalid_dish_is_not_saved(tx, order_body):
    body = {**order_body, "items": [{"dish_id": "unknown", "quantity": 1}]}
    assert_domain_error(404, create_order, tx, CUSTOMER, body)
    assert tx.all("order") == []


@pytest.mark.parametrize("actor,allowed", [
    (("customer", "user-bocharov"), True),
    (("customer", "other-user"), False),
    (("restaurant", "restaurant-1"), True),
    (("restaurant", "restaurant-2"), False),
    (("courier", "courier-1"), False),
    (("service", "system"), True),
])
def test_order_read_authorization(actor, allowed, order):
    assert can_read(actor, order) is allowed
    if not allowed:
        assert_domain_error(403, require_access, actor, order)


def test_require_missing_object_returns_404(tx):
    assert_domain_error(404, require, tx, "order", "not-found")


def test_confirm_unpaid_order_rejected(tx, order):
    assert_domain_error(409, order_command, tx, RESTAURANT, order["id"], "confirm", {})
    assert tx.get("order", order["id"])["status"] == "awaiting_payment"


def test_confirm_paid_order_publishes_one_event(tx, order):
    order["status"] = "paid"
    tx.put("order", order["id"], order)
    result = order_command(tx, RESTAURANT, order["id"], "confirm", {})
    assert result["status"] == "confirmed"
    assert [e["event_type"] for e in tx.all("_outbox")] == ["order.confirmed"]
    order_command(tx, RESTAURANT, order["id"], "confirm", {})
    assert len(tx.all("_outbox")) == 1


def test_cancel_requires_reason(tx, order):
    assert_domain_error(422, order_command, tx, RESTAURANT, order["id"], "cancel", {})
    assert tx.all("_outbox") == []


def test_cancel_and_repeat_same_reason(tx, order):
    path = (tx, RESTAURANT, order["id"], "cancel", {"reason": "Нет ингредиентов"})
    first = order_command(*path)
    second = order_command(*path)
    assert first["status"] == second["status"] == "cancelled"
    assert first["cancellation_reason"] == "Нет ингредиентов"
    assert len(tx.all("_outbox")) == 1


def test_cancel_with_new_reason_conflicts(tx, order):
    order_command(tx, RESTAURANT, order["id"], "cancel", {"reason": "Нет ингредиентов"})
    assert_domain_error(409, order_command, tx, RESTAURANT, order["id"],
                        "cancel", {"reason": "Другая причина"})


def test_prepared_order_cannot_be_cancelled(tx, order):
    order["status"] = "prepared"
    tx.put("order", order["id"], order)
    assert_domain_error(409, order_command, tx, RESTAURANT, order["id"],
                        "cancel", {"reason": "Поздно"})


def test_non_cancel_command_rejects_reason(tx, order):
    order["status"] = "paid"
    tx.put("order", order["id"], order)
    assert_domain_error(422, order_command, tx, RESTAURANT, order["id"],
                        "confirm", {"reason": "Лишнее"})


def test_preparation_sequence_creates_prepared_event(tx, order):
    order["status"] = "paid"
    tx.put("order", order["id"], order)
    for action in ("confirm", "startPreparation", "markPrepared"):
        order_command(tx, RESTAURANT, order["id"], action, {})
    result = tx.get("order", order["id"])
    assert result["status"] == "prepared"
    events = tx.all("_outbox")
    assert {e["event_type"] for e in events} == {"order.confirmed", "order.prepared"}
    prepared = next(e for e in events if e["event_type"] == "order.prepared")
    assert prepared["data"]["pickup_address"]["text"]
    assert prepared["data"]["delivery_address"] == order["delivery_address"]


def test_start_preparation_before_confirmation_conflicts(tx, order):
    assert_domain_error(409, order_command, tx, RESTAURANT, order["id"], "startPreparation", {})


def test_other_restaurant_cannot_change_order(tx, order):
    assert_domain_error(403, order_command, tx, ("restaurant", "restaurant-2"),
                        order["id"], "cancel", {"reason": "Тест"})


def test_idempotency_uses_previous_result(tx, order_body):
    called = []

    def operation():
        called.append(True)
        return create_order(tx, CUSTOMER, order_body)

    first = idempotent(tx, CUSTOMER, "replay", order_body, operation)
    second = idempotent(tx, CUSTOMER, "replay", dict(order_body), operation)
    assert second == first and len(called) == 1 and len(tx.all("order")) == 1


def test_idempotency_reused_key_different_body_rejected(tx, order_body):
    idempotent(tx, CUSTOMER, "replay", order_body, lambda: {"ok": True})
    assert_domain_error(409, idempotent, tx, CUSTOMER, "replay",
                        {**order_body, "restaurant_id": "changed"}, lambda: {"ok": False})


@pytest.mark.parametrize("key", [None, "", " ", "a" * 129])
def test_idempotency_invalid_key(tx, order_body, key):
    assert_domain_error(422, idempotent, tx, CUSTOMER, key, order_body, lambda: {"ok": True})
    assert tx.all("_idempotency") == []


def test_idempotency_scoped_to_actor(tx, order_body):
    customer2 = ("customer", "another-user")
    first = idempotent(tx, CUSTOMER, "same-key", order_body, lambda: "first")
    second = idempotent(tx, customer2, "same-key", order_body, lambda: "second")
    assert first == "first" and second == "second"
    assert len(tx.all("_idempotency")) == 2

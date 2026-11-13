"""Бизнес-логика. HTTP и RabbitMQ вызывают одни и те же методы."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from .models import new_id, now


class DomainError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


CATALOG = {
    "dish-1": {"name": "Пицца Маргарита", "price_minor": 55000},
    "dish-2": {"name": "Лимонад", "price_minor": 15000},
}
PICKUP = {"text": "Москва, ул. Примерная, 1", "lat": 55.75, "lon": 37.61}


def emit(tx, event_type, data):
    event = dict(event_id=new_id("event"), event_type=event_type,
                 schema_version=1, occurred_at=now(),
                 correlation_id=data["order_id"], data=data)
    tx.put("_outbox", event["event_id"], event)


def require(tx, kind, key):
    value = tx.get(kind, key)
    if value is None:
        raise DomainError(404, "NOT_FOUND", "Объект не найден")
    return value


def can_read(actor, item):
    role, actor_id = actor
    return (role == "service" or item.get("user_id") == actor_id or
            (role == "restaurant" and item.get("restaurant_id") == actor_id) or
            (role == "courier" and item.get("courier_id") == actor_id))


def require_access(actor, item):
    if not can_read(actor, item):
        raise DomainError(403, "FORBIDDEN", "Нет доступа к объекту")


def idempotent(tx, actor, key, body, operation):
    if not key or not key.strip() or len(key) > 128:
        raise DomainError(422, "INVALID_KEY", "Нужен Idempotency-Key длиной 1–128 символов")
    record_key = hashlib.sha256((":".join(actor) + ":" + key).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = tx.get("_idempotency", record_key)
    if previous and datetime.fromisoformat(previous["expires"]) > datetime.now(timezone.utc):
        if previous["fingerprint"] != fingerprint:
            raise DomainError(409, "KEY_REUSED", "Ключ уже использован с другим телом")
        return previous["result"]
    result = operation()
    tx.put("_idempotency", record_key, {
        "fingerprint": fingerprint, "result": result,
        "expires": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()})
    return result


def create_order(tx, actor, body):
    if body["restaurant_id"] != "restaurant-1":
        raise DomainError(404, "RESTAURANT_NOT_FOUND", "В демокаталоге есть restaurant-1")
    items = []
    for item in body["items"]:
        dish = CATALOG.get(item["dish_id"])
        if not dish:
            raise DomainError(404, "DISH_NOT_FOUND", "Блюдо не найдено")
        items.append({**item, **dish})
    order = dict(id=new_id("order"), user_id=actor[1], **body,
                 status="awaiting_payment", cancellation_reason=None,
                 amount_minor=sum(i["quantity"] * i["price_minor"] for i in items),
                 currency="RUB", created_at=now(), updated_at=now())
    order["items"] = items
    tx.put("order", order["id"], order)
    return order


def create_payment(tx, actor, order):
    require_access(actor, order)
    if order["status"] != "awaiting_payment":
        raise DomainError(409, "INVALID_STATE", "Заказ не ожидает оплаты")
    if any(p["order_id"] == order["id"] for p in tx.all("payment")):
        raise DomainError(409, "PAYMENT_EXISTS", "Платёж для заказа уже создан")
    payment = dict(id=new_id("payment"), order_id=order["id"], user_id=order["user_id"],
                   amount_minor=order["amount_minor"], currency=order["currency"],
                   status="pending", created_at=now(), updated_at=now())
    tx.put("payment", payment["id"], payment)
    return payment


def check_payment(tx, actor, payment_id):
    payment = require(tx, "payment", payment_id)
    require_access(actor, payment)
    # Учебный адаптер платёжного провайдера всегда подтверждает оплату.
    if payment["status"] == "pending":
        payment.update(status="confirmed", updated_at=now())
        tx.put("payment", payment_id, payment)
        emit(tx, "payment.confirmed", {"payment_id": payment_id,
             "order_id": payment["order_id"], "amount_minor": payment["amount_minor"],
             "currency": payment["currency"]})
    return payment


TRANSITIONS = {
    "confirm": ({"paid"}, "confirmed", "order.confirmed"),
    "cancel": ({"awaiting_payment", "paid", "confirmed", "preparing"}, "cancelled", "order.cancelled"),
    "startPreparation": ({"confirmed"}, "preparing", None),
    "markPrepared": ({"preparing"}, "prepared", "order.prepared"),
}


def order_command(tx, actor, order_id, action, body):
    order = require(tx, "order", order_id)
    require_access(actor, order)
    before, after, event_type = TRANSITIONS[action]
    reason = body.get("reason")
    if (action == "cancel" and (not reason or not reason.strip())) or (action != "cancel" and reason is not None):
        raise DomainError(422, "INVALID_REASON", "Причина обязательна только для отмены")
    if order["status"] == after:
        if action == "cancel" and order["cancellation_reason"] != reason:
            raise DomainError(409, "INVALID_STATE", "У отмены уже записана другая причина")
        return order
    if order["status"] not in before:
        raise DomainError(409, "INVALID_STATE", "Недопустимый переход состояния")
    order.update(status=after, updated_at=now())
    if action == "cancel":
        order["cancellation_reason"] = reason
    tx.put("order", order_id, order)
    if event_type:
        data = dict(order_id=order_id, user_id=order["user_id"], restaurant_id=order["restaurant_id"])
        if action == "cancel":
            data["reason"] = reason
        if action == "markPrepared":
            data.update(pickup_address=PICKUP, delivery_address=order["delivery_address"])
        emit(tx, event_type, data)
    return order


def delivery_command(tx, actor, delivery_id, action):
    delivery = require(tx, "delivery", delivery_id)
    require_access(actor, delivery)
    before, after = ("assigned", "in_transit") if action == "accept" else ("in_transit", "delivered")
    if delivery["status"] == after:
        return delivery
    if delivery["status"] != before:
        raise DomainError(409, "INVALID_STATE", "Недопустимый переход состояния")
    delivery["status"] = after
    if action == "complete":
        delivery["delivered_at"] = now()
        emit(tx, "delivery.completed", {"order_id": delivery["order_id"],
             "delivery_id": delivery_id, "user_id": delivery["user_id"],
             "delivered_at": delivery["delivered_at"]})
    tx.put("delivery", delivery_id, delivery)
    return delivery


def handle_event(tx, service, event):
    """Inbox и результат обработки записываются одной транзакцией."""
    if tx.get("_inbox", event["event_id"]):
        return
    data, event_type = event["data"], event["event_type"]
    if event["correlation_id"] != data["order_id"]:
        raise ValueError("correlation_id не совпадает с order_id")
    if service == "orders" and event_type == "payment.confirmed":
        order = require(tx, "order", data["order_id"])
        if data["amount_minor"] != order["amount_minor"] or data["currency"] != order["currency"]:
            raise ValueError("Сумма оплаты не совпадает с заказом")
        if order["status"] == "awaiting_payment":
            order.update(status="paid", updated_at=now())
            tx.put("order", order["id"], order)
        # При поздней оплате отменённый заказ не возобновляется.
    elif service == "deliveries" and event_type == "order.prepared":
        if not any(d["order_id"] == data["order_id"] for d in tx.all("delivery")):
            delivery = dict(id=new_id("delivery"), **data, courier_id="courier-1",
                            status="assigned", created_at=now(), delivered_at=None)
            tx.put("delivery", delivery["id"], delivery)
    elif service == "notifications" and event_type in {"order.confirmed", "order.cancelled", "delivery.completed"}:
        messages = {"order.confirmed": "Ресторан подтвердил заказ",
                    "order.cancelled": "Заказ отменён. Причина: " + data.get("reason", ""),
                    "delivery.completed": "Заказ доставлен"}
        notification = dict(id=new_id("notification"), order_id=data["order_id"],
                            user_id=data["user_id"], source_event_id=event["event_id"],
                            event_type=event_type, channel="push", text=messages[event_type],
                            status="sent", created_at=now(), sent_at=now(), failure_reason=None)
        # Учебный адаптер: сохраняем уведомление вместо внешнего push-провайдера.
        tx.put("notification", notification["id"], notification)
    else:
        raise ValueError("Неподдерживаемое событие")
    tx.put("_inbox", event["event_id"], {"processed_at": now()})

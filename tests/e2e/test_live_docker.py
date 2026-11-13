"""Проверки реально запущенных 4 контейнеров, PostgreSQL и RabbitMQ.

Запуск: PR8_LIVE=1 python -m pytest tests/e2e -v
Без PR8_LIVE=1 тесты пропускаются и НЕ считаются выполненными.
"""
import os
import time
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("PR8_LIVE") != "1",
    reason="Нужны реальные Docker-контейнеры: запустите с PR8_LIVE=1",
)

BASE = {
    "orders": os.getenv("PR8_ORDERS_URL", "http://localhost:8001"),
    "payments": os.getenv("PR8_PAYMENTS_URL", "http://localhost:8002"),
    "deliveries": os.getenv("PR8_DELIVERIES_URL", "http://localhost:8003"),
    "notifications": os.getenv("PR8_NOTIFICATIONS_URL", "http://localhost:8004"),
}


def auth(role="customer-bocharov", key=None):
    headers = {"Authorization": f"Bearer {role}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def request(service, method, path, **kwargs):
    url = BASE[service] + path
    try:
        return httpx.request(method, url, timeout=8, **kwargs)
    except httpx.RequestError as exc:
        pytest.fail(f"Нет подключения к {url}: {exc}. Проверь docker compose ps")


def wait_until(callback, predicate, label, timeout=25):
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = callback()
        if predicate(last_value):
            return last_value
        time.sleep(0.35)
    pytest.fail(f"Событие {label} не обработано за {timeout} с. Последний ответ: {last_value}")


@pytest.fixture(scope="session", autouse=True)
def all_services_ready():
    for service, url in BASE.items():
        try:
            result = httpx.get(url + "/openapi.json", timeout=5)
            assert result.status_code == 200, f"{service} вернул HTTP {result.status_code}"
        except (httpx.RequestError, AssertionError) as error:
            pytest.fail(f"Нет готового сервиса {service} на {url}: {error}")


@pytest.mark.parametrize("service", list(BASE))
def test_openapi_live(service):
    response = request(service, "GET", "/openapi.json")
    assert response.status_code == 200
    assert response.json()["paths"]


@pytest.mark.parametrize("service,path", [
    ("orders", "/api/orders"),
    ("payments", "/api/payments/missing"),
    ("deliveries", "/api/deliveries"),
    ("notifications", "/api/notifications"),
])
def test_live_protected_endpoints_require_token(service, path):
    response = request(service, "GET", path)
    assert response.status_code == 401


def new_order():
    payload = {"restaurant_id": "restaurant-1",
               "items": [{"dish_id": "dish-1", "quantity": 2},
                         {"dish_id": "dish-2", "quantity": 1}],
               "delivery_address": {"text": "Москва, Тестовая улица, 1", "lat": 55.75, "lon": 37.61}}
    key = str(uuid4())
    response = request("orders", "POST", "/api/orders", json=payload,
                       headers=auth(key=key))
    assert response.status_code == 201, response.text
    return response.json(), payload, key


def test_live_create_order_price_and_readback():
    order, _, _ = new_order()
    response = request("orders", "GET", "/api/orders/" + order["id"], headers=auth())
    assert response.status_code == 200
    assert response.json()["amount_minor"] == 125000
    assert response.json()["status"] == "awaiting_payment"


def test_live_idempotency_replays_same_order():
    order, payload, key = new_order()
    repeated = request("orders", "POST", "/api/orders", json=payload,
                       headers=auth(key=key))
    assert repeated.status_code == 201
    assert repeated.json()["id"] == order["id"]


def test_live_invalid_quantity_is_rejected():
    body = {"restaurant_id": "restaurant-1", "items": [{"dish_id": "dish-1", "quantity": 0}],
            "delivery_address": {"text": "Москва", "lat": 55.75, "lon": 37.61}}
    response = request("orders", "POST", "/api/orders", json=body,
                       headers=auth(key=str(uuid4())))
    assert response.status_code == 422


def test_live_invalid_order_action_is_blocked():
    order, _, _ = new_order()
    path = f"/api/orders/{order['id']}/commands/confirm"
    assert request("orders", "POST", path, json={}, headers=auth("restaurant-demo")).status_code == 409
    assert request("orders", "POST", path, json={}, headers=auth()).status_code == 403


def test_live_order_cancellation_and_notification():
    order, _, _ = new_order()
    result = request("orders", "POST", f"/api/orders/{order['id']}/commands/cancel",
                     json={"reason": "Заказ отменён тестом PR8"}, headers=auth("restaurant-demo"))
    assert result.status_code == 200, result.text
    notices = wait_until(
        lambda: request("notifications", "GET", "/api/notifications?order_id=" + order["id"],
                        headers=auth()).json(),
        lambda result: result["total"] == 1, "order.cancelled",
    )
    assert notices["items"][0]["event_type"] == "order.cancelled"


def test_live_full_order_payment_delivery_and_notifications():
    order, _, key = new_order()
    pay_response = request("payments", "POST", "/api/payments", json={"order_id": order["id"]},
                           headers=auth(key=key))
    assert pay_response.status_code == 201, pay_response.text
    payment = pay_response.json()
    assert payment["amount_minor"] == 125000

    # Внешняя оплата заменена в проекте №7 учебной командой :check.
    paid = request("payments", "POST", f"/api/payments/{payment['id']}:check",
                   headers=auth("service-demo"))
    assert paid.status_code == 200 and paid.json()["status"] == "confirmed"
    wait_until(
        lambda: request("orders", "GET", "/api/orders/" + order["id"], headers=auth()).json(),
        lambda state: state["status"] == "paid", "payment.confirmed",
    )
    for action in ("confirm", "startPreparation", "markPrepared"):
        result = request("orders", "POST", f"/api/orders/{order['id']}/commands/{action}",
                         json={}, headers=auth("restaurant-demo"))
        assert result.status_code == 200, result.text

    deliveries = wait_until(
        lambda: request("deliveries", "GET", "/api/deliveries?order_id=" + order["id"],
                        headers=auth()).json(),
        lambda data: data["total"] == 1, "order.prepared",
    )
    delivery = deliveries["items"][0]
    assert delivery["courier_id"] == "courier-1"
    for action in ("accept", "complete"):
        response = request("deliveries", "POST", f"/api/deliveries/{delivery['id']}:{action}",
                           headers=auth("courier-demo"))
        assert response.status_code == 200, response.text
    notices = wait_until(
        lambda: request("notifications", "GET", "/api/notifications?order_id=" + order["id"],
                        headers=auth()).json(),
        lambda data: {x["event_type"] for x in data["items"]} ==
                     {"order.confirmed", "delivery.completed"},
        "order.confirmed и delivery.completed",
    )
    assert notices["total"] == 2

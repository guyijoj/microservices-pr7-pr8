"""Компонентные тесты: HTTP нескольких приложений, RabbitMQ заменён доставкой событий."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from .conftest import create_order, headers, transfer


def test_payment_api_gets_order_by_http(api, body):
    order = create_order(api, body)
    result = api[1]["payments"].post("/api/payments", json={"order_id": order["id"]},
                                      headers=headers(key="payment-1"))
    assert result.status_code == 201, result.text
    payment = result.json()
    assert payment["order_id"] == order["id"]
    assert payment["amount_minor"] == order["amount_minor"]
    assert payment["status"] == "pending"
    assert result.headers["location"] == "/api/payments/" + payment["id"]


def test_create_payment_for_missing_order_returns_404(api):
    response = api[1]["payments"].post("/api/payments", json={"order_id": "not-real"},
                                        headers=headers(key="payment-missing"))
    assert response.status_code == 404


def test_customer_cannot_check_payment(api, body):
    order = create_order(api, body)
    payment = api[1]["payments"].post("/api/payments", json={"order_id": order["id"]},
                                      headers=headers(key="payment-1")).json()
    response = api[1]["payments"].post(f"/api/payments/{payment['id']}:check", headers=headers())
    assert response.status_code == 403


def test_check_payment_emits_event_once(api, body):
    apps, clients = api
    order = create_order(api, body)
    payment = clients["payments"].post("/api/payments", json={"order_id": order["id"]},
                                       headers=headers(key="payment-1")).json()
    path = f"/api/payments/{payment['id']}:check"
    assert clients["payments"].post(path, headers=headers("service-demo")).json()["status"] == "confirmed"
    assert clients["payments"].post(path, headers=headers("service-demo")).json()["status"] == "confirmed"
    with apps["payments"].state.repo.transaction() as tx:
        events = tx.all("_outbox")
    assert len(events) == 1
    assert events[0]["event_type"] == "payment.confirmed"


def test_complete_order_to_delivery_with_notifications(api, body):
    apps, clients = api
    order = create_order(api, body)
    payment_response = clients["payments"].post("/api/payments", json={"order_id": order["id"]},
                                                 headers=headers(key="payment-1"))
    assert payment_response.status_code == 201
    payment = payment_response.json()
    assert clients["payments"].post(f"/api/payments/{payment['id']}:check",
                                     headers=headers("service-demo")).status_code == 200
    transfer(apps, "payments", "orders", "payment.confirmed")
    assert clients["orders"].get(f"/api/orders/{order['id']}", headers=headers()).json()["status"] == "paid"

    for action in ("confirm", "startPreparation", "markPrepared"):
        result = clients["orders"].post(f"/api/orders/{order['id']}/commands/{action}",
                                         json={}, headers=headers("restaurant-demo"))
        assert result.status_code == 200
    transfer(apps, "orders", "notifications", "order.confirmed")
    transfer(apps, "orders", "deliveries", "order.prepared")
    deliveries = clients["deliveries"].get("/api/deliveries", headers=headers()).json()
    assert deliveries["total"] == 1
    delivery_id = deliveries["items"][0]["id"]
    assert clients["deliveries"].post(f"/api/deliveries/{delivery_id}:complete",
                                       headers=headers("courier-demo")).status_code == 409
    assert clients["deliveries"].post(f"/api/deliveries/{delivery_id}:accept",
                                       headers=headers("courier-demo")).status_code == 200
    assert clients["deliveries"].post(f"/api/deliveries/{delivery_id}:complete",
                                       headers=headers("courier-demo")).status_code == 200
    transfer(apps, "deliveries", "notifications", "delivery.completed")
    notifications = clients["notifications"].get("/api/notifications", headers=headers()).json()
    assert notifications["total"] == 2
    assert {x["event_type"] for x in notifications["items"]} == {"order.confirmed", "delivery.completed"}


def test_cancelled_order_creates_notification(api, body):
    apps, clients = api
    order = create_order(api, body)
    clients["orders"].post(f"/api/orders/{order['id']}/commands/cancel", json={"reason": "Нет продуктов"},
                            headers=headers("restaurant-demo"))
    transfer(apps, "orders", "notifications", "order.cancelled")
    result = clients["notifications"].get("/api/notifications", headers=headers()).json()
    assert result["total"] == 1
    assert "Нет продуктов" in result["items"][0]["text"]


def test_failed_order_http_dependency_returns_503(tmp_path):
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    app = create_app("payments", database_url=f"sqlite:///{tmp_path / 'failed.db'}",
                     broker_enabled=False, transport=httpx.MockTransport(unavailable))
    with TestClient(app) as client:
        response = client.post("/api/payments", json={"order_id": "any"},
                               headers=headers(key="dependent-1"))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "ORDERS_UNAVAILABLE"
        with app.state.repo.transaction() as tx:
            assert tx.all("payment") == []
            assert tx.all("_idempotency") == []


def test_payment_belongs_to_customer(api, body):
    order = create_order(api, body)
    payment = api[1]["payments"].post("/api/payments", json={"order_id": order["id"]},
                                      headers=headers(key="payment-2")).json()
    assert api[1]["payments"].get(f"/api/payments/{payment['id']}",
                                   headers=headers("courier-demo")).status_code == 403
    assert api[1]["payments"].get(f"/api/payments/{payment['id']}",
                                   headers=headers("service-demo")).status_code == 200

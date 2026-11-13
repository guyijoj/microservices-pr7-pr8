"""Проверяем HTTP, бизнес-правила и обработчики событий с SQLite.

Сеть RabbitMQ в этих тестах заменена доставкой записей outbox в handler.
Реальный транспорт проверяется отдельно demo.py после Docker Compose.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.models import Event
from app.services import handle_event

BODY = {"restaurant_id": "restaurant-1", "items": [{"dish_id": "dish-1", "quantity": 1}],
        "delivery_address": {"text": "Москва", "lat": 55.75, "lon": 37.61}}


def headers(token="customer-bocharov", key="test-1"):
    return {"Authorization": "Bearer " + token, "Idempotency-Key": key}


@pytest.fixture
def system(tmp_path):
    apps = {}
    clients = {}

    def fetch_order(request):
        response = clients["orders"].get(request.url.path, headers=dict(request.headers))
        return httpx.Response(response.status_code, json=response.json())

    for name in ["orders", "payments", "deliveries", "notifications"]:
        apps[name] = create_app(name, f"sqlite:///{tmp_path / (name + '.db')}",
                                broker_enabled=False, transport=httpx.MockTransport(fetch_order))
        clients[name] = TestClient(apps[name])
    yield apps, clients
    for client in clients.values():
        client.close()
    for app in apps.values():
        app.state.repo.engine.dispose()


def events(app):
    with app.state.repo.transaction() as tx:
        return tx.all("_outbox")


def deliver(apps, source, target, event_type):
    event = next(e for e in events(apps[source]) if e["event_type"] == event_type)
    Event.model_validate(event)
    for _ in range(2):  # Повторная доставка одного event_id не меняет результат.
        with apps[target].state.repo.transaction() as tx:
            handle_event(tx, target, event)


def create_paid_order(apps, clients):
    response = clients["orders"].post("/api/orders", json=BODY, headers=headers())
    assert response.status_code == 201
    order = response.json()
    response = clients["payments"].post("/api/payments", json={"order_id": order["id"]}, headers=headers())
    assert response.status_code == 201
    payment = response.json()
    assert payment["amount_minor"] == order["amount_minor"] == 55000
    url = f"/api/payments/{payment['id']}:check"
    assert clients["payments"].post(url, headers=headers("service-demo")).status_code == 200
    assert clients["payments"].post(url, headers=headers("service-demo")).status_code == 200
    assert len(events(apps["payments"])) == 1
    # Создание платежа с прежним ключом повторяется даже после подтверждения.
    repeated = clients["payments"].post("/api/payments", json={"order_id": order["id"]}, headers=headers())
    assert repeated.json() == payment
    deliver(apps, "payments", "orders", "payment.confirmed")
    return order


def test_complete_workflow_and_duplicate_events(system):
    apps, clients = system
    order = create_paid_order(apps, clients)
    oid = order["id"]
    for action in ["confirm", "startPreparation", "markPrepared"]:
        url = f"/api/orders/{oid}/commands/{action}"
        for _ in range(2):
            assert clients["orders"].post(url, json={}, headers=headers("restaurant-demo")).status_code == 200
    assert len(events(apps["orders"])) == 2
    deliver(apps, "orders", "notifications", "order.confirmed")
    deliver(apps, "orders", "deliveries", "order.prepared")
    deliveries = clients["deliveries"].get("/api/deliveries", headers=headers()).json()
    assert deliveries["total"] == 1
    delivery = deliveries["items"][0]
    did = delivery["id"]
    assert delivery["courier_id"] == "courier-1"
    assert clients["deliveries"].post(f"/api/deliveries/{did}:complete", headers=headers("courier-demo")).status_code == 409
    for action in ["accept", "complete"]:
        for _ in range(2):
            response = clients["deliveries"].post(f"/api/deliveries/{did}:{action}", headers=headers("courier-demo"))
            assert response.status_code == 200
    assert len(events(apps["deliveries"])) == 1
    deliver(apps, "deliveries", "notifications", "delivery.completed")
    notices = clients["notifications"].get("/api/notifications", headers=headers()).json()
    assert notices["total"] == 2
    assert {n["event_type"] for n in notices["items"]} == {"order.confirmed", "delivery.completed"}


def test_idempotency_access_and_validation(system):
    apps, clients = system
    client = clients["orders"]
    response = client.post("/api/orders", json=BODY, headers=headers())
    order = response.json()
    assert client.post("/api/orders", json=BODY, headers=headers()).json() == order
    assert client.post("/api/orders", json={**BODY, "restaurant_id": "other"}, headers=headers()).status_code == 409
    assert client.post("/api/orders", json=BODY, headers={"Authorization": "Bearer customer-bocharov"}).status_code == 422
    assert client.get("/api/orders").status_code == 401
    assert client.get("/api/orders/" + order["id"], headers=headers("courier-demo")).status_code == 403
    assert client.post(f"/api/orders/{order['id']}/commands/confirm", json={}, headers=headers()).status_code == 403
    assert client.post(f"/api/orders/{order['id']}/commands/confirm", json={}, headers=headers("restaurant-demo")).status_code == 409
    assert client.get("/api/orders?count=101", headers=headers()).status_code == 422
    assert client.post("/api/orders", json={**BODY, "items": [{"dish_id": "dish-1", "quantity": 0}]}, headers=headers(key="other")).status_code == 422
    # Данные доступны через новый экземпляр приложения с той же БД.
    app2 = create_app("orders", str(apps["orders"].state.repo.engine.url), broker_enabled=False)
    with TestClient(app2) as restarted:
        assert restarted.get("/api/orders/" + order["id"], headers=headers()).json() == order


def test_cancellation_reason_and_rollback(system):
    apps, clients = system
    order = create_paid_order(apps, clients)
    url = f"/api/orders/{order['id']}/commands/cancel"
    assert clients["orders"].post(url, json={}, headers=headers("restaurant-demo")).status_code == 422
    for _ in range(2):
        assert clients["orders"].post(url, json={"reason": "Нет ингредиентов"}, headers=headers("restaurant-demo")).status_code == 200
    assert clients["orders"].post(url, json={"reason": "Иная причина"}, headers=headers("restaurant-demo")).status_code == 409
    assert len(events(apps["orders"])) == 1
    deliver(apps, "orders", "notifications", "order.cancelled")
    notices = clients["notifications"].get("/api/notifications", headers=headers()).json()
    assert notices["total"] == 1
    assert "Нет ингредиентов" in notices["items"][0]["text"]
    assert clients["deliveries"].get("/api/deliveries", headers=headers()).json()["total"] == 0


def test_payment_dependency_failure_and_missing_order(system, tmp_path):
    def failure(request):
        raise httpx.ConnectError("offline", request=request)

    app = create_app("payments", f"sqlite:///{tmp_path / 'offline.db'}", False,
                     transport=httpx.MockTransport(failure))
    with TestClient(app) as client:
        assert client.post("/api/payments", json={"order_id": "missing"}, headers=headers()).status_code == 503
        with app.state.repo.transaction() as tx:
            assert tx.all("payment") == []
            assert tx.all("_idempotency") == []
    _, clients = system
    assert clients["payments"].post("/api/payments", json={"order_id": "missing"}, headers=headers()).status_code == 404


def test_two_to_five_endpoints_per_service(system):
    apps, _ = system
    for app in apps.values():
        routes = [r for r in app.routes if r.path.startswith("/api/")]
        assert 2 <= len(routes) <= 5

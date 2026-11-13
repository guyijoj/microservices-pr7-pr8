"""Компонентные тесты: HTTP через TestClient, SQLite вместо PostgreSQL,
межсервисный HTTP через MockTransport, брокер отключен.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.services import handle_event


@pytest.fixture
def api(tmp_path):
    apps, clients = {}, {}

    def fetch_order(request):
        # Платёж по-прежнему делает реальный HTTP GET в приложение Заказ,
        # но сеть и чужой сервер заменены встроенным транспортом.
        response = clients["orders"].get(request.url.path, headers=dict(request.headers))
        return httpx.Response(response.status_code, json=response.json())

    transport = httpx.MockTransport(fetch_order)
    for name in ("orders", "payments", "deliveries", "notifications"):
        apps[name] = create_app(
            name, database_url=f"sqlite:///{tmp_path / (name + '.sqlite')}",
            broker_enabled=False, transport=transport,
        )
        clients[name] = TestClient(apps[name])
    yield apps, clients
    for client in clients.values():
        client.close()
    for app in apps.values():
        app.state.repo.engine.dispose()


@pytest.fixture
def body():
    return {"restaurant_id": "restaurant-1", "items": [
        {"dish_id": "dish-1", "quantity": 2}, {"dish_id": "dish-2", "quantity": 1},
    ], "delivery_address": {"text": "Москва, Тестовая, 5", "lat": 55.75, "lon": 37.61}}


def headers(token="customer-bocharov", key="pr8-unique"):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def create_order(api, body, key="pr8-order"):
    _, clients = api
    result = clients["orders"].post("/api/orders", headers=headers(key=key), json=body)
    assert result.status_code == 201, result.text
    return result.json()


def transfer(apps, sender, recipient, event_type):
    with apps[sender].state.repo.transaction() as tx:
        candidates = [event for event in tx.all("_outbox") if event["event_type"] == event_type]
    assert candidates, f"Нет события {event_type} в outbox сервиса {sender}"
    event = candidates[-1]
    with apps[recipient].state.repo.transaction() as tx:
        handle_event(tx, recipient, event)
    return event

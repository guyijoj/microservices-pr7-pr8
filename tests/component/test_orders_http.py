"""Компонентные HTTP-проверки сервиса Заказ без реальной сети."""
import pytest

from .conftest import create_order, headers


def test_created_order_contains_calculated_fields(api, body):
    _, clients = api
    result = clients["orders"].post("/api/orders", json=body, headers=headers())
    assert result.status_code == 201
    record = result.json()
    assert result.headers["location"] == "/api/orders/" + record["id"]
    assert record["amount_minor"] == 125000
    assert record["status"] == "awaiting_payment"
    assert record["user_id"] == "user-bocharov"
    assert record["items"][0]["name"] == "Пицца Маргарита"


def test_get_order_returns_same_object(api, body):
    order = create_order(api, body)
    assert api[1]["orders"].get("/api/orders/" + order["id"], headers=headers()).json() == order


def test_repeated_create_with_same_key_returns_same_order(api, body):
    client = api[1]["orders"]
    first = client.post("/api/orders", json=body, headers=headers(key="key-a"))
    second = client.post("/api/orders", json=body, headers=headers(key="key-a"))
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert client.get("/api/orders", headers=headers()).json()["total"] == 1


def test_reused_key_with_another_payload_returns_409(api, body):
    client = api[1]["orders"]
    assert client.post("/api/orders", json=body, headers=headers(key="same")).status_code == 201
    changed = {**body, "items": [{"dish_id": "dish-2", "quantity": 1}]}
    response = client.post("/api/orders", json=changed, headers=headers(key="same"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KEY_REUSED"


def test_missing_idempotency_key_returns_422(api, body):
    response = api[1]["orders"].post("/api/orders", json=body,
                                      headers={"Authorization": "Bearer customer-bocharov"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_KEY"


@pytest.mark.parametrize("changed", [
    {"items": []},
    {"restaurant_id": ""},
    {"items": [{"dish_id": "dish-1", "quantity": 0}]},
    {"items": [{"dish_id": "dish-1", "quantity": 100}]},
    {"delivery_address": {"text": "Москва", "lat": 91, "lon": 37}},
    {"delivery_address": {"text": "Москва", "lat": 55, "lon": 181}},
    {"unexpected": 123},
])
def test_invalid_request_body_returns_422(api, body, changed):
    response = api[1]["orders"].post("/api/orders", json={**body, **changed}, headers=headers())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("changed", [
    {"restaurant_id": "other-restaurant"},
    {"items": [{"dish_id": "missing-dish", "quantity": 1}]},
])
def test_unknown_catalog_item_returns_404(api, body, changed):
    response = api[1]["orders"].post("/api/orders", json={**body, **changed}, headers=headers())
    assert response.status_code == 404


def test_customer_cannot_confirm_order(api, body):
    order = create_order(api, body)
    response = api[1]["orders"].post(f"/api/orders/{order['id']}/commands/confirm",
                                      json={}, headers=headers())
    assert response.status_code == 403


def test_restaurant_cannot_confirm_unpaid_order(api, body):
    order = create_order(api, body)
    response = api[1]["orders"].post(f"/api/orders/{order['id']}/commands/confirm",
                                      json={}, headers=headers("restaurant-demo"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_cancel_requires_reason_and_reuses_previous_result(api, body):
    order = create_order(api, body)
    path = f"/api/orders/{order['id']}/commands/cancel"
    client = api[1]["orders"]
    assert client.post(path, json={}, headers=headers("restaurant-demo")).status_code == 422
    first = client.post(path, json={"reason": "Закрылись"}, headers=headers("restaurant-demo"))
    second = client.post(path, json={"reason": "Закрылись"}, headers=headers("restaurant-demo"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert client.post(path, json={"reason": "Нет курьера"},
                       headers=headers("restaurant-demo")).status_code == 409


def test_list_filters_and_pagination(api, body):
    client = api[1]["orders"]
    first = create_order(api, body, "key-1")
    second = create_order(api, body, "key-2")
    client.post(f"/api/orders/{first['id']}/commands/cancel", json={"reason": "Ошибка"},
                headers=headers("restaurant-demo"))
    result = client.get("/api/orders?status=cancelled&count=1&offset=0", headers=headers()).json()
    assert result["total"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == first["id"]
    assert second["id"] != first["id"]


@pytest.mark.parametrize("query", ["count=0", "count=101", "offset=-1"])
def test_invalid_pagination_returns_422(api, query):
    assert api[1]["orders"].get("/api/orders?" + query, headers=headers()).status_code == 422


def test_nonowner_cannot_read_order(api, body):
    order = create_order(api, body)
    response = api[1]["orders"].get("/api/orders/" + order["id"], headers=headers("courier-demo"))
    assert response.status_code == 403


def test_unknown_order_returns_404(api):
    response = api[1]["orders"].get("/api/orders/unknown", headers=headers())
    assert response.status_code == 404


@pytest.mark.parametrize("service,path", [
    ("orders", "/api/orders"),
    ("payments", "/api/payments/missing"),
    ("deliveries", "/api/deliveries"),
    ("notifications", "/api/notifications"),
])
def test_api_requires_authorization(api, service, path):
    response = api[1][service].get(path)
    assert response.status_code == 401


@pytest.mark.parametrize("service", ["orders", "payments", "deliveries", "notifications"])
def test_swagger_openapi_is_present(api, service):
    response = api[1][service].get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["openapi"].startswith("3.")

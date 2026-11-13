"""Полный сценарий через настоящие HTTP и RabbitMQ после compose up.

Использует только стандартную библиотеку Python, можно запускать Python 3.9.
"""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def request(port, path, method="GET", body=None, token="customer-bocharov", key=None):
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    req = Request(f"http://localhost:{port}/api/{path}",
                  data=json.dumps(body).encode() if body is not None else None,
                  headers=headers, method=method)
    try:
        with urlopen(req, timeout=5) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode()}") from exc


def wait_for(operation, check, label):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = operation()
        if check(result):
            print(label)
            return result
        time.sleep(0.3)
    raise RuntimeError("Истекло время ожидания: " + label)


def main():
    key = str(uuid4())
    order = request(8001, "orders", "POST", {
        "restaurant_id": "restaurant-1",
        "items": [{"dish_id": "dish-1", "quantity": 1}],
        "delivery_address": {"text": "Москва, ул. Студенческая, 10", "lat": 55.74, "lon": 37.6}
    }, key=key)
    oid = order["id"]
    print("1. Создан заказ:", oid, "сумма", order["amount_minor"], "коп.")
    payment = request(8002, "payments", "POST", {"order_id": oid}, key=key)
    print("2. Создан платёж:", payment["id"], "сумма получена от Заказа по HTTP")
    request(8002, "payments/" + payment["id"] + ":check", "POST", token="service-demo")
    wait_for(lambda: request(8001, "orders/" + oid), lambda x: x["status"] == "paid",
             "3. Событие payment.confirmed обработано, заказ оплачен")
    for action in ["confirm", "startPreparation", "markPrepared"]:
        request(8001, f"orders/{oid}/commands/{action}", "POST", {}, token="restaurant-demo")
    delivery = wait_for(lambda: request(8003, "deliveries?order_id=" + oid),
                        lambda x: x["total"] == 1, "4. По order.prepared назначен курьер")["items"][0]
    did = delivery["id"]
    for action in ["accept", "complete"]:
        request(8003, f"deliveries/{did}:{action}", "POST", token="courier-demo")
    notifications = wait_for(lambda: request(8004, "notifications?order_id=" + oid),
                             lambda x: x["total"] == 2, "5. Получены два уведомления")
    for item in notifications["items"]:
        print("  ", item["text"])
    print("Сценарий завершён. Заказ:", oid, "Доставка:", did)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, URLError) as exc:
        raise SystemExit(f"Ошибка: {exc}\nПроверь docker compose ps и docker compose logs --tail=50")

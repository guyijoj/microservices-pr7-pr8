"""Изолированное хранилище вместо PostgreSQL для тестирования бизнес-логики."""
from copy import deepcopy

import pytest


class FakeTx:
    """Минимальный интерфейс UnitOfWork из app.repository без SQL и сети."""

    def __init__(self):
        self.data = {}

    def get(self, kind, key):
        return deepcopy(self.data.get(kind, {}).get(key))

    def all(self, kind):
        return deepcopy(list(self.data.get(kind, {}).values()))

    def put(self, kind, key, value):
        self.data.setdefault(kind, {})[key] = deepcopy(value)

    def remove(self, kind, key):
        self.data.get(kind, {}).pop(key, None)


@pytest.fixture
def tx():
    return FakeTx()


@pytest.fixture
def order_body():
    return {
        "restaurant_id": "restaurant-1",
        "items": [{"dish_id": "dish-1", "quantity": 2}, {"dish_id": "dish-2", "quantity": 1}],
        "delivery_address": {"text": "Москва, улица Тестовая, 5", "lat": 55.75, "lon": 37.61},
    }


@pytest.fixture
def order(tx, order_body):
    from app.services import create_order

    return create_order(tx, ("customer", "user-bocharov"), order_body)

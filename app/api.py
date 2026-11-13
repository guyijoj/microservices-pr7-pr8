"""HTTP-контроллеры. Каждый процесс обслуживает один сервис."""
import os
from contextlib import asynccontextmanager
from typing import Literal, Optional

import httpx
from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .messaging import Broker
from .models import Command, CreateOrder, CreatePayment
from .repository import Repository
from .services import (DomainError, can_read, check_payment, create_order,
                       create_payment, delivery_command, idempotent,
                       order_command, require, require_access)

TOKENS = {
    "customer-bocharov": ("customer", "user-bocharov"),
    "restaurant-demo": ("restaurant", "restaurant-1"),
    "courier-demo": ("courier", "courier-1"),
    "service-demo": ("service", "system"),
}


def actor(authorization: Optional[str] = Header(default=None)):
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if token not in TOKENS or not authorization.startswith("Bearer "):
        raise DomainError(401, "UNAUTHORIZED", "Нужен учебный Bearer-токен из README")
    return TOKENS[token]


def roles(identity, *allowed):
    if identity[0] not in allowed:
        raise DomainError(403, "FORBIDDEN", "Операция недоступна этой роли")


def create_app(service, database_url=None, broker_enabled=None, transport=None):
    database_url = database_url or os.getenv("DATABASE_URL", f"sqlite:///./{service}.db")
    repo = Repository(database_url)
    if broker_enabled is None:
        broker_enabled = os.getenv("BROKER_ENABLED", "true").lower() == "true"

    @asynccontextmanager
    async def lifespan(app):
        broker = None
        if broker_enabled:
            broker = Broker(service, repo, os.getenv("AMQP_URL", "amqp://student:student@localhost:5673/"))
            await broker.start()
        try:
            yield
        finally:
            if broker:
                await broker.close()
            repo.engine.dispose()

    app = FastAPI(title=f"Бочаров — {service}", version="1.0.0", lifespan=lifespan)
    app.state.repo = repo

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={
            "error": {"code": exc.code, "message": exc.message, "details": {}}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={
            "error": {"code": "VALIDATION_ERROR", "message": str(exc), "details": {}}})

    kind = {"orders": "order", "payments": "payment",
            "deliveries": "delivery", "notifications": "notification"}[service]
    prefix = f"/api/{service}"

    @app.get(prefix + "/{object_id}", summary="Получить объект по идентификатору")
    def get_one(object_id: str, identity=Depends(actor)):
        with repo.transaction() as tx:
            result = require(tx, kind, object_id)
            require_access(identity, result)
            return result

    if service != "payments":
        @app.get(prefix, summary="Список доступных пользователю объектов")
        def get_list(identity=Depends(actor), order_id: Optional[str] = None,
                     status: Optional[str] = None, offset: int = Query(0, ge=0),
                     count: int = Query(20, ge=1, le=100)):
            with repo.transaction() as tx:
                values = [v for v in tx.all(kind) if can_read(identity, v)
                          and (not order_id or v.get("order_id", v["id"]) == order_id)
                          and (not status or v["status"] == status)]
            values.sort(key=lambda v: (v["created_at"], v["id"]), reverse=True)
            return dict(items=values[offset:offset + count], offset=offset,
                        count=count, total=len(values))

    if service == "orders":
        @app.post(prefix, status_code=201, summary="Создать заказ")
        def post_order(body: CreateOrder, identity=Depends(actor),
                       idempotency_key: Optional[str] = Header(default=None)):
            roles(identity, "customer")
            with repo.transaction() as tx:
                result = idempotent(tx, identity, idempotency_key, body.model_dump(),
                                    lambda: create_order(tx, identity, body.model_dump()))
            return JSONResponse(result, status_code=201, headers={"Location": prefix + "/" + result["id"]})

        @app.post(prefix + "/{order_id}/commands/{action}", summary="Команда ресторана")
        def command_order(order_id: str,
                          action: Literal["confirm", "cancel", "startPreparation", "markPrepared"],
                          body: Command = Body(default=Command()), identity=Depends(actor)):
            roles(identity, "restaurant")
            with repo.transaction() as tx:
                return order_command(tx, identity, order_id, action, body.model_dump())

    if service == "payments":
        @app.post(prefix, status_code=201, summary="Создать платёж, получив заказ по HTTP")
        def post_payment(body: CreatePayment, identity=Depends(actor),
                         idempotency_key: Optional[str] = Header(default=None)):
            roles(identity, "customer", "service")

            def operation(tx):
                url = os.getenv("ORDERS_URL", "http://localhost:8001")
                try:
                    with httpx.Client(transport=transport, timeout=3) as client:
                        response = client.get(url + "/api/orders/" + body.order_id,
                                              headers={"Authorization": "Bearer service-demo"})
                except httpx.RequestError:
                    raise DomainError(503, "ORDERS_UNAVAILABLE", "Сервис Заказ недоступен")
                if response.status_code == 404:
                    raise DomainError(404, "NOT_FOUND", "Заказ не найден")
                if response.status_code != 200:
                    raise DomainError(503, "ORDERS_UNAVAILABLE", "Ошибка ответа сервиса Заказ")
                return create_payment(tx, identity, response.json())

            with repo.transaction() as tx:
                result = idempotent(tx, identity, idempotency_key, body.model_dump(), lambda: operation(tx))
            return JSONResponse(result, status_code=201, headers={"Location": prefix + "/" + result["id"]})

        @app.post(prefix + "/{payment_id}:check", summary="Проверить оплату у учебного провайдера")
        def payment_check(payment_id: str, identity=Depends(actor)):
            roles(identity, "service")
            with repo.transaction() as tx:
                return check_payment(tx, identity, payment_id)

    if service == "deliveries":
        @app.post(prefix + "/{delivery_id}:accept", summary="Курьер принимает заказ")
        def accept(delivery_id: str, identity=Depends(actor)):
            roles(identity, "courier")
            with repo.transaction() as tx:
                return delivery_command(tx, identity, delivery_id, "accept")

        @app.post(prefix + "/{delivery_id}:complete", summary="Курьер завершает доставку")
        def complete(delivery_id: str, identity=Depends(actor)):
            roles(identity, "courier")
            with repo.transaction() as tx:
                return delivery_command(tx, identity, delivery_id, "complete")

    return app

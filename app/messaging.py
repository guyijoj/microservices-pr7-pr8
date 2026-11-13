"""RabbitMQ: постоянные очереди, ручные ack и публикация из outbox."""
import asyncio
import json
import logging
from contextlib import suppress

import aio_pika

from .models import Event
from .services import DomainError, handle_event

SUBSCRIPTIONS = {
    "orders": ["payment.confirmed"],
    "deliveries": ["order.prepared"],
    "notifications": ["order.confirmed", "order.cancelled", "delivery.completed"],
}
log = logging.getLogger(__name__)


class Broker:
    def __init__(self, service, repo, url):
        self.service, self.repo, self.url = service, repo, url

    async def start(self):
        self.connection = await aio_pika.connect_robust(self.url)
        self.channel = await self.connection.channel(publisher_confirms=True)
        await self.channel.set_qos(prefetch_count=1)
        self.exchange = await self.channel.declare_exchange(
            "food.events", aio_pika.ExchangeType.TOPIC, durable=True)
        # Заранее создаём все подписки: события не теряются при порядке запуска.
        for service, keys in SUBSCRIPTIONS.items():
            queue = await self.channel.declare_queue(f"bocharov.{service}", durable=True)
            for key in keys:
                await queue.bind(self.exchange, routing_key=key)
            if service == self.service:
                await queue.consume(self.receive)
        self.task = asyncio.create_task(self.publish_outbox())

    async def receive(self, message):
        try:
            event = Event.model_validate_json(message.body).model_dump(mode="json")
            if event["event_type"] != message.routing_key:
                raise ValueError("event_type не совпадает с routing_key")
            with self.repo.transaction() as tx:
                handle_event(tx, self.service, event)
        except (ValueError, KeyError, TypeError, DomainError):
            log.exception("Отклонено некорректное событие")
            await message.reject(requeue=False)
        except Exception:
            log.exception("Временная ошибка обработки")
            await asyncio.sleep(1)
            await message.nack(requeue=True)
        else:
            await message.ack()

    async def publish_outbox(self):
        while True:
            try:
                with self.repo.transaction() as tx:
                    events = tx.all("_outbox")
                for event in events:
                    await self.exchange.publish(aio_pika.Message(
                        body=json.dumps(event, ensure_ascii=False).encode(),
                        content_type="application/json", message_id=event["event_id"],
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                        routing_key=event["event_type"])
                    with self.repo.transaction() as tx:
                        tx.remove("_outbox", event["event_id"])
            except Exception:
                log.exception("Публикация не удалась, outbox будет отправлен повторно")
            await asyncio.sleep(0.2)

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        await self.connection.close()

# RabbitMQ Adapter

> **Module:** Messaging — [Module Guide](../modules/messaging.md)
> **Package:** `pyfly.messaging.adapters.rabbitmq`
> **Backend:** aio-pika 9.0+

## Quick Start

### Installation

```bash
uv add "pyfly[rabbitmq]"

# Or install both Kafka and RabbitMQ
uv add "pyfly[eda]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  messaging:
    provider: "rabbitmq"
    rabbitmq:
      url: "amqp://guest:guest@localhost/"
```

### Minimal Example

```python
from pyfly.messaging import message_listener

@message_listener(topic="orders", group="order-service")
async def handle_order(msg: Message) -> None:
    print(f"Received order: {msg.value}")
```

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.messaging.provider` | `str` | `"memory"` | Adapter selection (`auto`, `kafka`, `rabbitmq`, `memory`) |
| `pyfly.messaging.rabbitmq.url` | `str` | `"amqp://guest:guest@localhost/"` | RabbitMQ connection URL (AMQP) |

When `provider` is `"auto"`, PyFly selects the adapter based on which library is installed. If `aio-pika` is found, the RabbitMQ adapter is used.

> **Note:** The exchange name defaults to `"pyfly"` and is not configurable via `pyfly.yaml` in the auto-configured adapter. To customise it, construct `RabbitMQAdapter(url=..., exchange_name=...)` manually as a `@bean`.

---

## Adapter-Specific Features

### RabbitMQAdapter

Implements `MessageBrokerPort` using `aio_pika.connect_robust()`.

- **Exchange:** Uses a single direct exchange (default name: `"pyfly"`) with topics as routing keys
- **Queues:** Declares durable queues per consumer group
- **Publishing:** Publishes persistent messages with a `message_id` and optional headers
- **Subscribing:** Consumes each queue on a channel of its own with a bounded prefetch and manual acknowledgement: a message is acked after the listener's unit of work commits, republished with a delay after a failure, and dead-lettered to `<queue>.dlq` (behind `pyfly.dlx`) after its last attempt. See [Delivery Guarantees](../modules/messaging.md#delivery-guarantees).

### Consumer Groups

Consumer groups are mapped to RabbitMQ queues. Multiple instances with the same `group` share the queue for competing-consumer load balancing.

### Lifecycle

- `start()` — Establishes a robust connection, declares exchange and queues, starts consumers
- `stop()` — Stops the consumers, waits for the deliveries in flight (requeuing the ones it has to cancel), then closes the connection

---

## Testing

When no broker library is installed, PyFly auto-configures `InMemoryMessageBroker` — no RabbitMQ needed for unit tests.

```yaml
# pyfly-test.yaml
pyfly:
  messaging:
    provider: "memory"
```

---

## See Also

- [Messaging Module Guide](../modules/messaging.md) — Full API reference: publishing, consuming, message listeners
- [Kafka Adapter](kafka.md) — Alternative messaging backend
- [Adapter Catalog](README.md)

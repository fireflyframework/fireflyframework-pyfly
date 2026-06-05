# Kafka Adapter

> **Module:** Messaging — [Module Guide](../modules/messaging.md)
> **Package:** `pyfly.messaging.adapters.kafka`
> **Backend:** aiokafka 0.10+

## Quick Start

### Installation

```bash
uv add "pyfly[kafka]"

# Or install both Kafka and RabbitMQ
uv add "pyfly[eda]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  messaging:
    provider: "kafka"
    kafka:
      bootstrap-servers: "localhost:9092"
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
| `pyfly.messaging.kafka.bootstrap-servers` | `str` | `"localhost:9092"` | Comma-separated Kafka broker addresses |

When `provider` is `"auto"`, PyFly selects the adapter based on which library is installed. If `aiokafka` is found, the Kafka adapter is used.

---

## Adapter-Specific Features

### KafkaAdapter

Implements `MessageBrokerPort` using `AIOKafkaProducer` and `AIOKafkaConsumer`.

- **Publishing:** Serializes messages to JSON and sends to the specified topic with optional headers
- **Subscribing:** Creates async consumer loops with consumer group support
- **Headers:** Encodes/decodes message headers (string values)

### Consumer Groups

The `group` parameter on `@message_listener` maps directly to Kafka consumer groups for load-balanced consumption across instances.

### Lifecycle

- `start()` — Starts the Kafka producer and all consumer loops
- `stop()` — Gracefully stops consumers and flushes the producer

---

## Testing

When no broker library is installed, PyFly auto-configures `InMemoryMessageBroker` — no Kafka needed for unit tests.

```yaml
# pyfly-test.yaml
pyfly:
  messaging:
    provider: "memory"
```

---

## See Also

- [Messaging Module Guide](../modules/messaging.md) — Full API reference: publishing, consuming, message listeners
- [RabbitMQ Adapter](rabbitmq.md) — Alternative messaging backend
- [Adapter Catalog](README.md)

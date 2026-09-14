# MQTT publish-subscribe

SMOLPy models application-layer MQTT traffic on top of the standard Ethernet/IP/TCP wire model.

## What is modelled

- **Publisher adapters** call `publishes()` to emit periodic MQTT PUBLISH frames at a fixed rate toward an `MQTTBroker` node.
- **The broker** receives PUBLISH frames and fans out one copy to each registered subscriber per topic (`routes()`). QoS 0 delivers silently; QoS 1 additionally sends a PUBACK frame (58 bytes) back toward the publisher.
- **Subscriber adapters** receive forwarded copies just like normal Ethernet frames; all standard metrics (`throughput`, `latency`, `bytes_received`) apply.
- **`broker_queue`** samples the broker's inbound store depth — unprocessed PUBLISH frames waiting to be forwarded. A non-zero and rising queue indicates the broker or its downstream link is becoming a bottleneck.

## Frame size formula

```
frame_size = 54 (Ethernet+IPv4+TCP) + 2 (MQTT fixed header) + 2 (topic-length field) + len(topic) + (2 if qos > 0 else 0) + payload_bytes
```

A typical small sensor message (`topic="plant/temperature"`, `payload=20`, `qos=1`) produces a 96-byte frame, roughly 16× smaller than a maximum-size bulk frame (1 518 B).

## Dashboard

`MQTTBroker` nodes appear as **purple** circles in the topology panel — see [Dashboard](dashboard.md).

See [`example_mqtt.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_mqtt.py) for a full working scenario, or [Examples](examples.md) for the write-up.

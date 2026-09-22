# DSL Reference

## Topology builders

```python
adapter = net.adapter("name", ip="10.0.0.1")          # NIC / host / server
switch  = net.switch("name",  ports=16, mode="store-and-forward")
hub     = net.hub("name",     ports=8)
broker  = net.mqtt_broker("name", ip="10.0.2.1")       # MQTT message broker
slave   = net.modbus_slave("name", ip="10.0.0.10", unit_id=1)  # Modbus TCP slave
net.link(a, b, speed=1_000, length=10)                 # Mb/s and metres
```

Multiple switches can be chained to model hierarchical topologies:

```python
core_sw = net.switch("core-sw", ports=16, mode="store-and-forward")
edge_sw = net.switch("edge-sw", ports=8,  mode="store-and-forward")
net.link(edge_sw, core_sw, speed=1_000, length=5)      # inter-switch uplink
```

## Traffic

```python
# Basic Ethernet send
src.sends(to=dst, rate=8_000, size=1_518, pattern="constant")

# Delayed start (useful for staggered scenarios)
src.sends(to=dst, rate=8_000, size=1_518, pattern="constant", delay_ms=5_000)

# MQTT publish (sensor-style, constant-rate)
sensor.publishes(to=broker, topic="plant/temp", rate=1.0, payload=20, qos=1)
sensor.publishes(to=broker, topic="plant/temp", rate=1.0, payload=20, qos=0, delay_ms=2_000)

# Broker topic routing — must be called before simulate()
broker.routes("plant/temp", to=[server])

# Modbus TCP poll (master → slave, Read Holding Registers)
plc.polls(slave, register=40001, count=10, rate=1.0)
plc.polls(slave, register=30001, count=5, rate=2.0, delay_ms=500)
```

### `sends()` parameters

| Parameter | Type | Description |
|---|---|---|
| `to` | Adapter | Destination adapter |
| `rate` | float | Frames per second |
| `size` | int \| `"imix"` | Frame size in bytes, or Internet Mix distribution |
| `pattern` | str | `"constant"`, `"poisson"`, or `"bursty"` |
| `delay_ms` | float | Simulation time before this flow starts (default 0) |

### `publishes()` parameters

| Parameter | Type | Description |
|---|---|---|
| `to` | MQTTBroker | Target broker |
| `topic` | str | MQTT topic string |
| `rate` | float | Messages per second (default 1.0) |
| `payload` | int | Payload bytes (default 20) |
| `qos` | int | 0 = fire-and-forget, 1 = PUBACK acknowledgement |
| `delay_ms` | float | Simulation time before publishing starts (default 0) |

### `polls()` parameters

| Parameter | Type | Description |
|---|---|---|
| `slave` | ModbusSlave | Target slave to poll |
| `register` | int | Starting holding-register address |
| `count` | int | Number of registers to read |
| `rate` | float | Polls per second (default 1.0) |
| `delay_ms` | float | Simulation time before polling starts (default 0) |

### Traffic patterns

| Pattern | Description |
|---|---|
| `"constant"` | Fixed inter-frame gap — models a saturated link |
| `"poisson"` | Exponentially distributed gaps — models random/bursty traffic |
| `"bursty"` | Pareto-distributed burst lengths — models ON/OFF sources |

### Frame sizes

| Value | Description |
|---|---|
| integer | Fixed size in bytes (e.g. `512`, `1_518`) |
| `"imix"` | 40 % × 64 B, 57 % × 594 B, 3 % × 1 518 B |

## Observations

```python
net.observe(metric, on=node, every=interval_ms)
```

| Metric | Unit | Observed on |
|---|---|---|
| `throughput` | Mb/s | Adapter |
| `latency` | µs | Adapter |
| `frame_loss` | % | Adapter |
| `bytes_sent` | MB | Adapter (sender) |
| `bytes_received` | MB | Adapter (receiver) |
| `queue_depth` | frames | Switch |
| `utilization` | % | Any node |
| `collision_rate` | /s | Hub |
| `broker_queue` | msgs | MQTTBroker |
| `modbus_latency` | µs | Adapter (master) |

## Simulation

```python
result = net.simulate(duration=30_000)              # headless — silent, fastest
result = net.simulate(duration=30_000, text=True)   # rich text dashboard in terminal
result = net.simulate(duration=30_000, live=True)   # full Dear PyGui desktop window
result = net.simulate(duration=30_000, seed=7)      # seed of the traffic generators (default 42)

result.report()   # print summary table (avg / min / max per metric)
result.plot()     # open static dashboard for a completed result

# Export metric time-series (format inferred from extension)
result.export("results.csv")    # long CSV: time_ms, metric, value
result.export("results.json")   # JSON dict of lists-of-pairs
result.export("out.csv", format="csv")   # explicit format override
```

**Text mode** (`text=True`) displays a live updating table in the terminal — no display server or GUI toolkit required. Ideal for headless servers, SSH sessions, and CI environments.

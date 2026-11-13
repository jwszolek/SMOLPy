# PROFINET RT cyclic I/O

PROFINET RT is the third industrial communication pattern SMOLPy models, alongside Modbus TCP polling and MQTT publish/subscribe: **cyclic producer–consumer exchange**. Controller and device each send their process data every update time, unprompted, and each supervises the other's frames with a watchdog. RT frames are plain Layer-2 Ethernet frames (EtherType `0x8892`, no IP/TCP) tagged with 802.1Q priority 6. Design rationale: [ADR-0005](adr/0005-profinet-rt.md).

```python
plc = net.adapter("plc", ip="10.0.0.1")
io1 = net.profinet_device("io1", ip="10.0.4.1")
net.link(plc, sw, speed=100, length=5)
net.link(io1, sw, speed=100, length=10)

plc.controls(io1, cycle=4, inputs=32, outputs=16, hold=3)

net.observe("pn_latency",           on=plc, every=100)
net.observe("pn_jitter",            on=plc, every=100)
net.observe("pn_watchdog_expiries", on=plc, every=100)
```

The same in a `.smol` file:

```
profinet_device io1 ip=10.0.4.1
control plc -> io1 cycle=4 inputs=32 outputs=16 hold=3
```

## What is modelled

- **Controller** — a regular `Adapter`. `controls()` sets up one Application Relationship (AR) with a `ProfinetDevice`; one controller can run ARs with many devices, and can at the same time be a Modbus master or MQTT publisher.
- **Cyclic data in both directions** — the controller sends output frames, the device sends input frames, each every `cycle` ms (1, 2, 4, … 512), independently of each other.
- **Watchdog** — a consumer that receives no frame for `hold × cycle` counts an expiry. Supervision starts with the first received frame and re-arms when data flows again.
- **Priority** — RT frames carry 802.1Q priority 6. Every egress queue (switch ports and node NICs) serves the eight priority classes strictly: higher first, FIFO within a class, a frame already on the wire is never interrupted. Ordinary traffic has priority 0 unless set with `sends(..., priority=)`.

## Metrics

All three are observed on the **consuming** node: on a controller they cover input frames from all its devices, on a device the output frames from its controller.

| Metric | Unit | Meaning per sampling window |
|---|---|---|
| `pn_latency` | µs | Mean send → receive time |
| `pn_jitter` | µs | Largest deviation of the frame inter-arrival time from the cycle |
| `pn_watchdog_expiries` | count | Cumulative number of watchdog expiries |

## Frame size

```
frame_size = max(60, 14 (Ethernet) + 4 (802.1Q tag) + 2 (FrameID) + io_bytes + 4 (APDU status))
```

`io_bytes` includes the per-submodule IOPS/IOCS status bytes. As elsewhere in SMOLPy, the FCS, preamble and inter-frame gap are not counted.

## Prioritisation on and off

Build a switch with `qos="fifo"` to make it ignore priorities and compare. In [`example_profinet.py`](https://github.com/jwszolek/SMOLPy/blob/main/examples/example_profinet.py), an engineering station on a 1 Gb/s link downloads a project to a PLC in bursts of 100 full frames while the PLC exchanges 2 ms cyclic data with three devices over a 100 Mb/s port (10 s run):

| Switch `qos` | `pn_jitter` max | `pn_latency` max | Watchdog expiries |
|---|---|---|---|
| `strict` | ~0.12 ms | ~0.07 ms | 0 |
| `fifo` | ~18 ms | ~46 ms | 936 |

With prioritisation the RT frames overtake each burst; without it they wait behind ~12 ms of bulk data per burst — longer than the 6 ms watchdog.

Only *bursts* make watchdogs expire. Under smooth overload, frames arrive later and later but still roughly once per cycle, so latency grows while the watchdog stays quiet. This matches the model, because frames are never dropped.

## Limitations

- **RT only.** IRT (isochronous, time-scheduled, sub-millisecond) needs a TSN-style scheduling model and is not modelled.
- **No AR establishment.** DCP naming, RPC connect and parameterisation are not modelled; cyclic exchange starts at `delay_ms`.
- **Watchdog expiry is counted, the AR keeps running.** Real devices abort the AR and take much longer to reconnect.
- **No frame loss.** Queues are unbounded, so frames are delayed but never dropped.
- **8 strict classes.** Real switches often map the 8 priorities onto fewer hardware queues.
- Acyclic record data, alarms, media redundancy (MRP) and shared devices are out of scope.

## Dashboard

`ProfinetDevice` nodes appear as **teal** circles in the topology panel.

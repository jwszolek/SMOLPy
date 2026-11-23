# ADR-0012: Modbus Polling with One Request in Flight

**Status:** Accepted  
**Date:** 2026-11-23  
**Amends:**
- ADR-0002: `poll` gains `outstanding` and `timeout`.
- ADR-0007: two new optional fields of the IR, which stays version 2.
- ADR-0004: the `smol-ns3` line format becomes version 3, and ns-3's TCP settings are set to Linux's.
- ADR-0011: "Protocols when a frame is lost", for Modbus.
- [Model Semantics](../model-semantics.md) §5, §6, §8 and §9.

**Roadmap:** none. It came out of the ADR-0011 testbed.

---

## Context

**Today's `poll` is open loop.** It sends a request every `1/rate` seconds whether or not the previous response has arrived. That holds in all three engines:
- SimPy: `_modbus_poll_gen`;
- Rust: `Periodic` in `rust/smol-engine`;
- ns-3: `Periodic` in `smol-ns3.cc`.

Most real Modbus TCP masters work differently. They have one transaction in flight per connection: they wait for the response, or for their response timeout, before sending the next request.

**The ADR-0011 testbed shows where this matters.** The setup:
- **Switch:** a TP-Link LS108GP. The egress port to the master runs at 100 Mb/s, holds about 60 frames, and has 802.3x PAUSE disabled.
- **Background traffic:** a Raspberry Pi sends Poisson UDP traffic of 1 518 B frames to the master's port, at ρ = 0.985 and 0.996.
- **Polling:** the master polls a Modbus TCP server on the Pi every 10 ms, and waits for each response.

Results with `buffer=91080` (60 frames):

| | Lost Modbus responses | RTT of the affected polls |
|---|---|---|
| hardware | 3 of 1 680 (0.18 %) | about 215 ms |
| ns-3 backend | — | 24–40 ms |
| SimPy / Rust | about 0.75 % of polls never answered | — |

Everything else matches on all three engines, from ρ = 0.25 to 0.996: queueing delay, the RTT's median, p90 and p99, and UDP loss. The difference is in what happens after a lost response:
- **Hardware.** TCP retransmits the lost response after a retransmission timeout. With one request in flight, no later segments follow it, so there are no duplicate ACKs to trigger a fast retransmit.
- **ns-3.** The open-loop polls keep requests and responses flowing. Three duplicate ACKs follow the loss, and the fast retransmit comes after tens of ms.
- **SimPy and Rust.** There is no TCP, so a lost response is simply never answered, and the master keeps polling.

**ns-3's retransmission timeout is not Linux's.** Linux runs on most masters and slaves, and on both ends of the testbed. The ns-3 program sets none of `TcpSocketBase`'s RTO attributes, so it uses ns-3's defaults. The two compute the RTO as follows:
- **ns-3:** `RTO = max(SRTT + max(G, 4·RTTVAR), MinRto)`, with `MinRto` = 1 s (RFC 6298) and the clock granularity `G` = 1 ms (`tcp-socket-base.cc`). After a lost response the slave would retransmit only after 1 s.
- **Linux:** `RTO = SRTT + RTTVAR`, where `tcp_rtt_estimator()` keeps `RTTVAR`, in units of 4 × the deviation, at or above `rto_min` (`TCP_RTO_MIN = HZ/5`, 200 ms; `net/ipv4/tcp_input.c`, `include/net/tcp.h`). So `RTO ≥ SRTT + 200 ms`, which is the hardware's 215 ms: 200 ms plus a few ms of RTT.

### Options considered

**When a poll instant comes while the request is still outstanding:**
- **A: skip it, as a counted cycle overrun.** **Chosen.** The request goes out at the next poll instant after the response or the timeout. A master on a fixed cycle works this way: the PLC task that issues the request runs every cycle and does nothing while its transaction is busy. The schedule stays `start + round(k·T)` (§1, "No drift"), and every request is sent at its own scheduled instant. So when `modbus_latency` starts is not open to question, and how many instants were skipped is simply a count.
- **B: defer it, sending as soon as the response or timeout arrives.** Masters written as `request; wait; sleep(until next)` work this way. Requests then go out at instants set by the network, not the schedule, and the send has to be defined apart from the scheduled instant. With a long outage, B makes a burst after recovery: one request at once, then the schedule again. B can be added later as `overrun=defer`, alongside A.
- **C: catch up, sending every missed request one after another.** With one request in flight, that is a burst of round trips until the master is back on schedule. Few masters do this, and it would add load after an overload. Rejected.

**What the limit applies to:**
- **Per master–slave pair.** **Chosen.** It is one TCP connection, and Modbus TCP's MBAP transaction id matches responses per connection. The ns-3 backend already opens exactly one connection per pair.
- **Per master.** One transaction in flight across all of a master's slaves. That is what serial Modbus RTU does, not Modbus TCP. Rejected.

---

## Decision

### The parameters

```
poll laptop -> server register=0 count=10 rate=100 outstanding=1 timeout=500
```

```python
laptop.polls(server, register=0, count=10, rate=100, outstanding=1, timeout_ms=500)
```

- **`outstanding`:** `1` or `unlimited`. In Python it is `1` or `None`.
    - **The default is unlimited:** today's open loop. A scenario without the parameter means exactly what it meant before: the same results and event counts, the same benchmark hashes (B1–B4) and the same equivalence suite.
- **`timeout`:** the master's response timeout, in ms, the same unit as `delay`.
    - It needs `outstanding=1`. With an unlimited number of requests in flight it would need its own semantics (per request, with late responses discarded); that is left for later.
    - **Without `timeout`, the master waits forever.** On SimPy and Rust a single lost request or response then stops that pair's polling for the rest of the run. That is what a master without a timeout would do without TCP retransmission, and `modbus_skipped` shows it. Documentation recommends a timeout on those backends.
- **Validation:**
    - `outstanding` is `1` or unset. The IR holds an integer, so that a window of `N` can be added later.
    - `timeout` is a positive number, at least 1 ns after rounding (§1) and at most 2^63 − 1 ns, as a duration, and set only with `outstanding=1`.
    - **A pair with `outstanding=1` has one poll statement.** Two statements to the same slave would have to share one transaction slot, and their instants would compete. The rule needed for that, a transaction queue per connection, is left for later. Several register blocks of one slave are one poll with a larger `count`.
- **IR.** `Poll.outstanding: int | None = None` and `Poll.timeout_ms: float | None = None`.

### One request in flight

A pair with `outstanding=1` holds at most one **outstanding** request. That is a request sent and neither answered nor timed out.

- **At a poll instant `t`:**
    - the request is **sent** if no request is outstanding and the previous request did not complete at `t` itself;
    - otherwise the instant is **skipped**: nothing is sent, and the instant is counted in `modbus_skipped`;
    - either way, the next instant is the next one of the schedule.
- **Completed** means its response was accepted or its timeout fired.
- **Same-instant rule.** A response or timeout at exactly a poll instant does not free that instant: the next request goes out at the following instant. The rule depends on no event order, so it holds the same way on every engine, TCP's included. It matters only for exact ties, such as an RTT equal to the period.
- **Transaction ids.** Each request of the pair carries the next MBAP transaction id, and its response carries the same id back. Ids are counted from 0 without wrapping. A real MBAP id has 16 bits, but with one request in flight two live ids are never 65 536 apart.

### The timeout

A request sent at `s`, with timeout `τ` rounded to ns:
- **A response with its id arriving at `a < s + τ`** is **accepted**:
    - it ends the transaction;
    - it counts towards `modbus_latency` (`a − s`);
    - it counts as a response.
- **At `s + τ`, if the request is still outstanding,** it **times out**:
    - it is counted in `modbus_timeouts`;
    - it ends the transaction, so the next poll instant after `s + τ` sends the next request.
- **A response arriving at `a ≥ s + τ`, or with an id that is not outstanding, is discarded.**
    - It is a late response: the master already counted its request as timed out.
    - It still counts as a frame the master's NIC accepted (`frames_received`, `bytes_received`, `throughput`, `latency`).
    - It does not count towards `modbus_latency` or as a response.
- **A response at exactly `s + τ` is late,** whichever event an engine processes first.

### Where `modbus_latency` starts

It starts at **the instant the master sends the request**, which is what a master measures: it timestamps the request when it hands it to its TCP socket. Under rule A, a request is only ever sent at its scheduled instant, so the two instants are the same. As today, the request's origin time is that instant, and the response carries it back.

### On each engine

- **SimPy and Rust have no transport layer.** A lost request or response is not retransmitted. The transaction ends at its timeout, or never if the poll has no timeout.
- **ns-3 runs Modbus over real TCP.**
    - A lost segment is retransmitted: after the RTO, or after a fast retransmit if enough duplicate ACKs arrive. With one request in flight, they usually do not.
    - The timeout matters only when recovery takes longer than `τ`.
    - **After a timeout the connection stays open.** The next request is sent on the same byte stream, behind the old request if that is still being retransmitted. Its response then also waits for the old response's bytes to arrive, so in-order delivery can make it time out too. A real master that keeps its connection behaves the same; many masters reconnect after a timeout, which is not modelled.
- **The engines therefore differ by design under loss.** On SimPy and Rust a loss becomes a timeout. On ns-3 it becomes an RTT of about RTO + RTT, or a timeout if `τ` is shorter. The testbed is the reference for the second.

### Metrics

| Metric | On | Value at a sample | Unit |
|---|---|---|---|
| `modbus_timeouts` | adapter | requests of its polls that timed out so far | polls |
| `modbus_skipped` | adapter | poll instants skipped so far because a request was outstanding | polls |

Both are cumulative counts, like `modbus_unanswered`. On a poll with an unlimited number of requests in flight they stay 0.

**Changed definitions:**
- **`modbus_unanswered`** stays *requests sent less responses accepted*.
    - Skipped instants are not requests.
    - A discarded late response is not a response.
    - So, for a master's polls with `outstanding=1`: `modbus_unanswered = modbus_timeouts + outstanding`, where `outstanding` is the number of its pairs with a request outstanding at the sample.
- **Poll instants.** Over a master's polls with `outstanding=1`, the instants so far are the requests sent plus `modbus_skipped`.

### The IR stays version 2

- `outstanding` and `timeout_ms` are optional fields, as `buffer_bytes` is in ADR-0011. They are written only when set, so a scenario without them is the same JSON byte for byte.
- **Capability feature `"modbus_outstanding"`.** `smolpy.backends.check` rejects a poll with `outstanding=1` on a backend without it, naming the poll. A backend never ignores it silently.
- **The Rust package.** `smolpy[rust]` requires the `smolpy-engine-rs` release that runs it.

### ns-3

**Format 3.** The poll line gains two fields:

```
poll <connection> <request B> <response B> <delay ns> <period num> <period den> <outstanding> <timeout ns>
```

- `outstanding` is 0 for unlimited or 1.
- `timeout` is 0 for none.

**On the client side:**
- **The poll owns the transaction slot.** `Periodic` hands a request to `Connection::Send` only when no request of its pair is outstanding.
- **`Sent` carries the transaction id.** On arrival of a whole response, `Connection::Arrived` accepts it only if its id is the outstanding one and it arrived before `s + τ`.
- **What the application guarantees.** It never has more than one request awaiting a response. Below the application, TCP's own queue (`m_inFlight`) can still hold a timed-out request's bytes; see above.
- **A test checks that `outstanding=1` really keeps one request in flight on the connection,** without loss and with it.

**TCP settings, set to Linux's.** These are `Config::SetDefault` values in `smol-ns3.cc`. Sources: ns-3 3.49 `src/internet/model/tcp-socket.cc`, `tcp-socket-base.cc`, `rtt-estimator.cc`; Linux `include/net/tcp.h`, `net/ipv4/tcp_input.c`, `net/ipv4/tcp_output.c`.

| Setting | ns-3 default | Linux | Set to |
|---|---|---|---|
| `TcpSocketBase::MinRto` | 1 s | 200 ms (`TCP_RTO_MIN`) | **200 ms** |
| `TcpSocketBase::ClockGranularity` | 1 ms | `rto_min` is a floor on `RTTVAR` | **200 ms** |
| `RttEstimator::InitialEstimation`, the first RTO | 1 s | 1 s (`TCP_TIMEOUT_INIT`) | 1 s, unchanged |
| `TcpSocket::ConnTimeout`, the SYN RTO | 3 s | 1 s | **1 s** |
| `TcpSocket::DelAckTimeout` | 200 ms | 40 ms at first (`TCP_ATO_MIN`); adapts up to 200 ms (`TCP_DELACK_MAX`) | **40 ms** |
| `TcpSocket::DelAckCount` | 2 | an ACK at once for more than one full segment | 2, unchanged |
| `TcpSocketBase::Timestamp` | on | on | off, unchanged |
| `TcpSocketBase::Sack` | on | on | on, unchanged |
| `TcpSocket::TcpNoDelay` | on | Nagle by default; Modbus stacks set `TCP_NODELAY` | on, unchanged |
| congestion control, recovery | CUBIC, PRR | CUBIC, PRR | unchanged |

- **Clock granularity.** With `G = 200 ms`, ns-3's RTO becomes `SRTT + max(200 ms, 4·RTTVAR)`. That is Linux's formula, including its addition of the floor to SRTT. `MinRto` alone would give `max(200 ms, SRTT + 4·RTTVAR)`, about 10 ms short of the testbed. Both are set, so the RTO is never under 200 ms.
- **Delayed ACK.** Linux starts its delayed-ACK timeout at 40 ms and adapts it. 40 ms is its value for request–response traffic that does not piggyback the ACK. Linux's quick-ACK mode at the start of a connection is not modelled.
- **Timestamps stay off.** Linux sends them, but SMOL sizes every TCP frame without the 12 B option (Model Semantics §2). Frame sizes then agree across engines, and the RTO's RTT samples come from ACKs alone, by Karn's rule.
- **Tail loss probe.** Linux's TLP is not in ns-3. With one segment in flight, its probe comes at `2·SRTT + rto_min`, capped at the RTO, so it is not earlier than the RTO. A lone lost response is therefore recovered at the RTO on both.
- **The settings change how ns-3's TCP recovers, and so how ns-3 results look under loss.** Without loss, only the delayed ACK can change a result: a pure ACK now leaves after 40 ms instead of 200 ms when nothing piggybacks it. That moves frames on the wire at poll and publish periods above 40 ms. Step 4 records every ns-3 result that changes.

*As built (2026-11-23):*
- **The transaction slot.** It is in `Periodic`, above TCP: a poll whose slot is not free skips the instant, and a timeout is one event, scheduled after the request is handed to TCP. The id and the slot travel with the request's bytes and come back with the response's. A test checks that at every sample at most one request awaits a response, with loss and without a timeout.
- **The rules hold over TCP.** TCP adds 1 ns per send to the round trip. At the exact round trips of the SimPy tests, no response therefore moves across a poll instant or a timeout, and the counts are SimPy's. `poll-timeouts` and `poll-tie` sample at SimPy's instants (`test_over_tcp`).
- **The testbed case** (`python -m benchmarks closed-loop`, scenario T2, `benchmarks/results/2026-11-23-2106-…-one-request-in-flight.json`). 30 s per run, five seeds per load, no timeout and a timeout of 1 s:

    | ρ | engine | polls affected | long RTTs: n, min, median, max (ms) | UDP frames dropped |
    |---|---|---|---|---|
    | 0.985 | ns-3 | 0.28 % | 40: 210.0, 212.9, 216.5 | 0.31 % |
    | 0.99 | ns-3 | 0.34 % | 47: 210.2, 213.3, 217.2 | 0.46 % |
    | 0.996 | ns-3 | 0.61 % | 81: 210.1, 213.2, 217.3 | 0.69 % |
    | 0.985–0.996 | SimPy, Rust, timeout 1 s | 0.21–0.56 %, as timeouts | none | 0.28–0.68 % |
    | 0.985, 0.996 | the testbed | 0.18 % (3 of 1 680) | about 215 | 0.09 % |

    - **On ns-3 every lost response comes back after the RTO,** 210 to 217 ms after its request, as on the hardware. None times out at 1 s, and none is unanswered at the end beyond the one in flight.
    - **For comparison, on the same scenario:**
        - ns-3's own TCP settings give about 1 s;
        - open-loop polls give a fast retransmit after 25 to 29 ms.
    - **On SimPy and Rust a lost response is a timeout.** Without a timeout, polling stops at the first loss: 5 polls of 75 000 instants are unanswered, and the rest are skipped.
    - **The share of polls affected is 1.5 to 3.4 times the hardware's.** It follows the share of frames dropped, which is 3 to 8 times the testbed's (ADR-0011); that gap stays open.
- **Results that change with the TCP settings.** The recorded ns-3 benchmarks were run again against the code before this step:
    - **T1 of ADR-0011 (open loop):** all 90 runs give the same values.
    - **L1, TCP under load:** the same values up to ρ = 1.05. From ρ = 1.1 the uplink queue grows by seconds, and the RTO changes the means by at most 1.2 %. At ρ = 1.35, for example, the Modbus RTT goes from 2.455 s to 2.484 s.
    - **ns-3's event counts grow by a constant** of a few hundred to a few thousand per run. ns-3 counts a cancelled event when its time comes, and each segment cancels and reschedules the retransmission timer. With a minimum RTO of 200 ms instead of 1 s, the timers set in the last 0.8 s of a run fall inside it.
    - **No ns-3 test changed.** The delayed-ACK timeout changes nothing in the suite, whose periods are under 40 ms.

**A backend option, not part of the scenario.** `smolpy.ns3.TcpSettings` holds the values above, with these defaults: minimum RTO, clock granularity, delayed-ACK timeout and SYN RTO. The program reads them from a `tcp` line of the format. `Ns3Backend(tcp=TcpSettings(min_rto_ms=1000))` runs with other values. They are not in the IR, for two reasons:
- SimPy and Rust have no TCP to apply them to;
- they describe the hosts' operating system, not the network.

### Validation and tests

**Unit tests, on SimPy and Rust:**
- with `outstanding=1`, no loss and an RTT shorter than the period, every result equals the open loop's, event counts aside;
- with an RTT longer than the period, the instants are skipped exactly as rule A says;
- a poll instant at exactly the same instant as a response arrival is skipped;
- a timeout fires exactly `τ` after the request;
- a response at or after `s + τ` is discarded and not counted;
- `modbus_unanswered = modbus_timeouts + outstanding`, and instants = requests sent + `modbus_skipped`;
- validation of `outstanding` and `timeout`, the `.smol` syntax, and the capability.

**Cross-engine.** Two deterministic scenarios join the bit-exact set of SimPy and Rust, event counts included:
- **`poll-timeouts`:** a slow path through a bounded buffer, where constant background flows overflow it, responses are lost and timeouts fire;
- **`poll-tie`:** an RTT exactly equal to the poll period, so that each response arrives at the instant of the next poll.

**ns-3, the testbed case:**
- **Setup:**
    - Poisson UDP at ρ ≈ 0.99 through a 100 Mb/s port with `buffer=91080`;
    - Modbus with `outstanding=1`, `rate=100` and no timeout;
    - 30 s per run, several seeds.
- **Expected:**
    - lost responses come back with an RTT of about `MinRto + RTT` (≈ 200–220 ms), not tens of ms;
    - no poll stays unanswered at the end, beyond the one in flight.
- **Reported:**
    - the distribution of the long RTTs across the seeds;
    - the share of polls affected, against the hardware's 0.18 %.

  The model loses more frames than the testbed (ADR-0011: 0.5 % against 0.09 % of UDP frames). So the share of affected polls is expected to be higher than the hardware's, and that gap stays open.

### Steps

1. **The model:**
    - the two optional IR fields, written only when set;
    - `outstanding` and `timeout` in the Python API and in `.smol`;
    - validation;
    - the capability feature, which no backend has yet, so every backend rejects `outstanding=1`, and the Rust reader rejects the fields.
2. **SimPy:**
    - rule A, the timeout, transaction ids and the two metrics;
    - unit tests;
    - Model Semantics updated.
3. **Rust:**
    - the same;
    - the two bit-exact scenarios;
    - the `smolpy-engine-rs` floor raised.
4. **ns-3:**
    - format 3 and the poll state on the client side;
    - `TcpSettings` and the `tcp` line;
    - the testbed case on ns-3, with its results recorded, and on SimPy and Rust for comparison.
5. **Documentation:**
    - `modbus.md`, the language and DSL references, `backends.md`;
    - an example scenario;
    - the roadmap, with `response_delay` (slave processing time) named as the natural next step of the Modbus candidate.

---

## Consequences

**Positive**
- **The usual Modbus TCP master can be modelled:** one transaction per connection, a response timeout, and cycle overruns counted.
- **ns-3 reproduces the testbed's recovery:** an RTO-sized RTT for a lost response, with Linux's minimum RTO, where it now shows a fast retransmit that the hardware cannot have.
- **On SimPy and Rust a loss becomes a timeout,** a bounded and countable event, instead of a poll unanswered for good.
- **ns-3's TCP matches Linux in the settings that shape recovery,** and they are documented and can be changed.

**Negative / trade-offs**
- **A new `smolpy-engine-rs` release and ns-3 format version.**
- **ns-3 results with loss change.** Results without loss can change too, through the delayed-ACK timeout, at poll and publish periods above 40 ms.
- **One poll statement per pair with `outstanding=1`.** Masters that serialize several transactions on one connection are not modelled yet.
- **Not modelled:**
    - masters that defer instead of skipping (option B);
    - reconnecting after a timeout;
    - a window of more than one request.

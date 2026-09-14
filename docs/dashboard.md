# Dashboard

When `live=True` the simulation runs in a background thread while a full-screen Dear PyGui window opens immediately.

## Topology panel (left)

Each node is drawn as a coloured circle:

| Colour | Node type |
|---|---|
| Blue | Adapter (host / server / NIC) |
| Green | Switch |
| Orange | Hub |
| Purple | MQTTBroker |
| Coral red | ModbusSlave |

Node fill changes dynamically during the simulation:

| Appearance | Meaning |
|---|---|
| Dim (faded) | Node idle — no traffic yet |
| Pulsing bright fill | Node actively **transmitting** (`bytes_sent > 0`) |
| Solid bright fill | Node forwarding traffic (switch / hub) |
| Pulsing amber outer ring | Node actively **receiving** data (`bytes_received > 0`) |

Animated particles flow along every link to show live traffic direction.

## Metrics panel (right)

One chart per observed metric. All series update in real time. Axes auto-scale to fit the data.

## Simulation controls

Three controls appear in the title bar during a live simulation:

| Control | Effect |
|---|---|
| **⏸ Pause** | Freezes simulation time; dashboard stays interactive. Click again to resume. |
| **▶ Resume** | Continues from the exact pause point. |
| **⏹ Stop** | Ends the simulation early; plots freeze at the last collected sample. |

Status indicator:

- **● Simulating…** — running
- **● Paused** — paused by user
- **● Done** — completed normally
- **● Stopped** — ended by user

## Text mode

Pass `text=True` (or run `smolpy run script.py --text`) for a live updating table in the terminal instead — no display server or GUI toolkit required. Ideal for headless servers, SSH sessions, and CI environments.

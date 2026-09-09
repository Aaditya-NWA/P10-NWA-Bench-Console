# NWA Bench Console

**Motor Torque & Thrust Test Stand**
*Nautical Wings Aerospace Pvt. Ltd.*

Real-time console for a 100 kgf flight thrust test stand. An ESP32-S3 reads five HX711
load cells (four beam cells for torque, one S-type for thrust), an ADS1115 for
current/voltage, and an IR RPM sensor, and streams JSON telemetry over serial to a Python
backend, which relays it to a React dashboard over WebSocket.

---

## Architecture

```
ESP32-S3  (UART0 @ 115200, JSON lines, ~20 Hz)
    │      GPIO43 TX / GPIO44 RX via an external USB-UART adapter
    ▼
Python backend  (FastAPI + pyserial)
    │      WebSocket ws://localhost:8000/ws
    ▼
React frontend  (Vite + Recharts)
    └─ Browser at http://localhost:3000
```

Serial transport is **UART0 via an external USB-UART adapter**, not the ESP32-S3's native
USB-Serial/JTAG peripheral — the board's native USB port is physically damaged.

---

## Where your data goes

Everything the console writes for the operator lives under **Documents**, outside the
repo, so a `git clean` can never delete a test log:

```
<Documents>/NWA Bench Console/
├── CSV/        one .csv per recording (+ a .meta.json with operator and calibration)
├── Login/      login.json — operator accounts
├── Logs/       append-only audit trails (throttle, calibration)
└── Recovery/   per-run .jsonl, written as frames arrive — survives a crash
```

A recording streams to disk **as it happens**. If the backend is killed mid-run, whatever
reached the disk is still a complete, replayable record up to that instant.

---

## Getting started

### 1. Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate                 # Windows
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Two things that have bitten before:

- **Run from `backend/`, not the repo root** — `main:app` resolves relative to the
  working directory.
- **Use `python -m uvicorn`, not bare `uvicorn`.** Windows console-script shims hardcode
  the interpreter path at install time, so renaming any parent folder breaks them with
  `Fatal error in launcher`. `python -m` bypasses the shims entirely.

Run the tests with `python -m pytest tests -q`.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000
npm test           # vitest
```

### 3. First run

The console opens on a **create-account** screen. The account is stored on this machine
at `Documents/NWA Bench Console/Login/login.json`; nothing is sent anywhere. Its purpose
is attribution, not secrecy — every throttle command and calibration change is recorded
against whoever is signed in.

Sessions live in the backend's memory, so restarting the backend signs everyone out.

### 4. Firmware (ESP-IDF v6.0.2)

```bash
idf.py build
idf.py -p COM6 flash
idf.py -p COM6 monitor
```

`idf.py monitor`, `tools/log_to_csv.py`, and the backend cannot hold the same COM port at
once — flash without `monitor`, then run whichever one you need.

The build tree is **outside this repo**: `esp32/main.c`, `hx711.c` and `hx711.h` are
copied into the IDF project's `main/` directory before building (see `idfcmd.txt`).
Editing the repo copy alone does not change what gets flashed.

---

## Running without hardware

The whole stack can run with no ESP32 attached — useful for UI work and for exercising
the recording pipeline.

```bash
NWA_BENCH_ALLOW_REPLAY=1 python -m uvicorn main:app --port 8000
```

Then either simulate a bench that responds to the throttle, or replay a captured run:

```bash
curl -X POST localhost:8000/replay/start -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" -d '{"mode":"simulate","rate_hz":20}'

curl -X POST localhost:8000/replay/start -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"mode":"file","file":"C:/.../Recovery/run.jsonl"}'
```

Replayed frames travel the **real** serial reader, so history, health counters and the
recording pipeline are all genuinely exercised. It is **off by default on purpose**: synthetic
telemetry is indistinguishable from real telemetry once it is in a CSV, and this machine
produces test reports.

---

## Hardware

| Component | Detail |
|---|---|
| MCU | ESP32-S3 (ESP-IDF v6.0.2) |
| Beam cells | 4× 60 kg, cross pattern — S1 top, S2 bottom, S3 right, S4 left |
| Thrust cell | 1× S-type, axial |
| ADC | 5× HX711 (one per cell) + ADS1115 for current/voltage |
| Beam cell pins | S1 4/5, S2 6/7, S3 15/16, S4 17/18 (DT/SCK) |
| Thrust cell pins | 36/35 (DT/SCK) |
| RPM sensor | GPIO1, via level shifter |
| ESC PWM | GPIO21 |
| ADS1115 | I²C, SDA 40 / SCL 41 |
| Console UART | TX 43 / RX 44 @ 115200 |

---

## Dashboard

**Live Bench** tab

| Feature | Detail |
|---|---|
| Torque / Thrust / RPM / Current / Voltage / Power | Large readouts; torque carries a colour-coded CW ↻ / CCW ↺ badge from the firmware's own hysteresis-filtered direction |
| Unit toggle | Switch readouts between SI (N, N·m) and kgf (kgf, kgf·cm). **Display only** — recorded and exported data is always SI |
| RPM divergence | Shows raw sensor RPM and its delta from the filtered value; turns red when the IR pickup looks noisy |
| Link health | Live frame rate, and a persistent banner when the port is open but no telemetry is arriving |
| STABLE / SETTLING badge | Driven by the ESP32's stability detection; shows N/A on firmware that doesn't report it |
| Tare | Re-zeros the four beam cells, the thrust cell, and the current sensor |
| Throttle slider + exact entry | Manual ESC control, 1000–2000 µs |
| **CUT THROTTLE** | Also bound to the **spacebar**, which works anywhere on the page |
| Auto profile | Ramp → hold → next step → ramp down, sequenced by the backend so it survives a page refresh or a closed tab |
| Chart | 30 s rolling torque + thrust, with dashed markers at every profile phase change |
| Beam cell grid | S1–S4 readings, flagging any cell that disagrees sharply with the other three |
| Logging | Names the run and streams it to CSV in your Documents folder as it records; Stop reports where it was saved |
| Calibration settings | Edits the ESP32's runtime constants over serial, showing old → new before saving |
| Dark / light theme | Saved in localStorage |

**Data Analysis** tab — upload rig logs and benchmark reference files, auto-pair them,
detect steady-state plateaus, interpolate the rig's thrust/torque at each benchmark RPM,
and export a styled multi-sheet workbook. Entirely independent of the live tab.

---

## Telemetry format

One JSON line per cycle (~20 Hz):

```json
{"torque":10.3412,"thrust":45.231,"s1":1.203,"s2":-0.412,"s3":0.891,"s4":0.034,
 "stable":true,"dir":"CW","rpm":12450.0,"rpm_raw":12470.0,
 "current_a":1.85,"voltage_v":22.1,"current_a2":0.00,"voltage_v2":0.00,
 "throttle_us":1300}
```

`load` and `f_arm` were removed on 2026-08-03 — the firmware no longer publishes either.
`current_a2` / `voltage_v2` are the second ADS1115 pair; they read a hard `0.00` until
that hardware is fitted and `SENSOR2_ENABLED` is turned on.

Commands (dashboard → backend → ESP32):

| Command | Wire | Effect |
|---|---|---|
| Tare | `T\n` | Re-zero all cells and the current sensor |
| Throttle | `S1300\n` | Set throttle; firmware clamps to [1000, 2000] |
| Read calibration | `C\n` | Device replies with `CAL` and `CAL_DEFAULTS` event lines |
| Set calibration | `C<key>=<value>\n` | Applies live and commits to NVS |
| Reset calibration | `C!\n` | Restores firmware defaults and wipes NVS |

---

## API

Every endpoint except `/auth/*` requires a session token
(`Authorization: Bearer <token>`; the WebSocket takes `?token=`).

| Endpoint | Method | Description |
|---|---|---|
| `/auth/{status,setup,login,logout,me,users,password}` | GET/POST | Operator accounts and sessions |
| `/ws` | WebSocket | Live telemetry + tare/throttle command channel |
| `/status` | GET | Serial state, link health, recorder state, profile progress |
| `/history` | GET | The 30-second rolling buffer |
| `/ports`, `/connect`, `/disconnect` | GET/POST | Serial port management |
| `/recording/{start,stop,status}` | POST/GET | Logging; stop returns the file paths |
| `/recordings` | GET | Runs already written to the CSV folder |
| `/profile/{start,stop,status}` | POST/GET | Automated throttle profile |
| `/calibration`, `/calibration/reset` | GET/POST | Runtime calibration relay |
| `/audit/{throttle,calibration}` | GET | Append-only audit trails |
| `/paths` | GET | Where the console writes the operator's files |
| `/replay/{start,status}` | POST/GET | Simulated / replayed source (gated) |
| `/analysis/run`, `/analysis/export-excel` | POST | Tab 2 |

---

## Migrating to WiFi

Only the backend changes: swap `serial_reader_thread` for a UDP/TCP listener. The
frontend only knows about the backend's WebSocket/REST API, never the transport to the
ESP32, so it needs no changes.

---

*Built by Nautical Wings Aerospace Pvt. Ltd. — Internal tooling*

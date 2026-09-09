import { useState, useEffect, useRef, useCallback } from "react";
import { api, wsUrl } from "../api";
import {
  EP, EVT_AUTH_EXPIRED, METHOD_POST, WS_UNAUTHORIZED,
  MSG, ACTION, FW_EVENT, TARE_STATUS, CAL_STATUS,
  MSG_BACKEND_UNREACHABLE,
} from "../constants";

const RECONNECT_DELAY_MS   = 2000;  // Before retrying the browser↔backend WebSocket
const STATUS_POLL_MS       = 2000;
const PROFILE_POLL_MS      = 250;   // Only while a profile is running
const TARE_STATUS_CLEAR_MS = 3000;
const CAL_SAVED_CLEAR_MS   = 2500;
const RECORDING_NOTICE_MS  = 12000;
const HISTORY_SIZE         = 600;   // 30 s @ ~20 Hz — must match backend HISTORY_SIZE

/**
 * useSerialData
 * Owns the WebSocket to the backend, rolling history, and the tare /
 * port / recording / profile / calibration controls. "connect" and
 * "disconnect" here refer to the backend's serial link to the ESP32,
 * not the browser's link to the backend.
 *
 * `enabled` gates ALL network activity: React runs hooks before the
 * caller's auth early-return, so without it this hook would open its
 * socket and poll /status while the sign-in screen is still showing —
 * every request 401ing and redrawing a login error twice a second.
 */
export function useSerialData({ enabled = true } = {}) {
  const [latest, setLatest]         = useState(null);
  const [history, setHistory]       = useState([]);
  const [connected, setConnected]   = useState(false);       // Browser ↔ backend
  const [tareStatus, setTareStatus] = useState(null);
  // Updated the instant a THROTTLE_SET event arrives rather than on the
  // next telemetry frame, which cost an extra full cycle of lag.
  const [confirmedThrottleUs, setConfirmedThrottleUs] = useState(null);

  const [ports, setPorts]                     = useState([]);
  const [serialConnected, setSerialConnected] = useState(false); // Backend ↔ ESP32
  const [serialPort, setSerialPort]           = useState("");
  const [serialError, setSerialError]         = useState("");
  // Frame rate, newest-frame age, malformed count, stalled verdict.
  // This is what tells an open-but-dead port apart from a healthy one.
  const [health, setHealth]                   = useState(null);

  const [recording, setRecording]             = useState(false);
  const [frameCount, setFrameCount]           = useState(0);
  const [recorderInfo, setRecorderInfo]       = useState(null); // On-disk paths for the run
  const [recordingError, setRecordingError]   = useState("");
  const [recordingNotice, setRecordingNotice] = useState("");

  // Mirrored from the backend, which owns the actual sequencing.
  const [profile, setProfile] = useState(null);
  const [profileError, setProfileError] = useState("");

  // The ESP32 owns the calibration values; this is a mirror of what it
  // reports. `fields` is the registry the backend serves (labels,
  // groups, ranges) so the panel carries no third copy of the table.
  const [calibration, setCalibration] = useState({ fields: [], values: {}, defaults: {} });
  const [calibrationStatus, setCalibrationStatus] = useState(CAL_STATUS.IDLE);
  const [calibrationError, setCalibrationError] = useState("");

  const wsRef        = useRef(null);
  const reconnectRef = useRef(null);

  // BUG FIX: `recording` used to be a dependency of connect(), so the
  // mount effect's cleanup tore down the WebSocket on every start/stop
  // of a recording — dropping frames at exactly the wrong moment.
  // Reading the flag through a ref keeps connect() stable.
  const recordingRef = useRef(false);
  useEffect(() => { recordingRef.current = recording; }, [recording]);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      clearTimeout(reconnectRef.current);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);

        if (msg.type === MSG.HISTORY) {
          setHistory(msg.data);
          if (msg.data.length > 0) {
            const lastFrame = msg.data[msg.data.length - 1];
            setLatest(lastFrame);
            // Seeded too, so a refresh mid-session shows the right value
            if (typeof lastFrame.throttle_us === "number") {
              setConfirmedThrottleUs(lastFrame.throttle_us);
            }
          }
          return;
        }

        // BUG FIX: this used to fire for ANY ack — including throttle
        // acks, which arrive every ~80 ms while dragging the slider, so
        // each one overwrote tareStatus and the Tare button flickered
        // through "Command Sent" during an unrelated drag.
        if (msg.type === MSG.ACK && msg.action === ACTION.TARE) {
          setTareStatus(msg.status);
          setTimeout(() => setTareStatus(null), TARE_STATUS_CLEAR_MS);
          return;
        }
        // Throttle acks need no state — the confirmed readout comes from
        // THROTTLE_SET and the telemetry frame's throttle_us below.
        if (msg.type === MSG.ACK) {
          return;
        }

        if (msg.event === FW_EVENT.TARE_DONE) {
          setTareStatus(TARE_STATUS.DONE);
          setTimeout(() => setTareStatus(null), TARE_STATUS_CLEAR_MS);
          return;
        }

        // Fires the moment the firmware applies a throttle command,
        // independent of the next telemetry frame.
        if (msg.event === FW_EVENT.THROTTLE_SET) {
          setConfirmedThrottleUs(msg.us);
          return;
        }

        // The firmware answers a "C" request with two lines: values in
        // use, then compiled-in defaults. Mirroring them here (not only
        // in the /calibration REST reply) keeps an open settings panel
        // truthful when the change came from somewhere else.
        if (msg.event === FW_EVENT.CAL) {
          setCalibration((prev) => ({ ...prev, values: msg.values ?? {} }));
          return;
        }
        if (msg.event === FW_EVENT.CAL_DEFAULTS) {
          setCalibration((prev) => ({ ...prev, defaults: msg.defaults ?? {} }));
          return;
        }
        if (msg.event === FW_EVENT.CAL_SET) {
          // Surfaced so a value the firmware refused (or accepted but
          // couldn't persist) is visible rather than looking saved.
          if (msg.ok === false) {
            setCalibrationError(`Device rejected ${msg.key}: ${msg.error ?? "unknown reason"}`);
          } else if (msg.warning) {
            setCalibrationError(`${msg.key} applied, but not saved to flash — it will revert on reboot`);
          } else if (msg.key) {
            setCalibration((prev) => ({
              ...prev,
              values: { ...prev.values, [msg.key]: msg.value },
            }));
          }
          return;
        }
        if (msg.event === FW_EVENT.CAL_RESET) {
          if (msg.ok === false) {
            setCalibrationError("Values reverted to firmware defaults, but the device could not clear its saved copy");
          }
          return;
        }

        // Pushed on every phase change. The countdown within a hold is
        // filled in by /profile/status polling below rather than
        // broadcast every tick, to keep this socket carrying telemetry.
        if (msg.type === MSG.PROFILE) {
          setProfile(msg.profile);
          return;
        }

        setLatest(msg);
        // Kept in sync from telemetry too, in case a THROTTLE_SET was missed
        if (typeof msg.throttle_us === "number") {
          setConfirmedThrottleUs(msg.throttle_us);
        }
        setHistory((prev) => {
          const next = [...prev, msg];
          return next.length > HISTORY_SIZE ? next.slice(-HISTORY_SIZE) : next;
        });
        if (recordingRef.current) {
          setFrameCount((c) => c + 1);
        }

      } catch {
        // Unparseable message — ignore rather than drop the connection
      }
    };

    ws.onclose = (ev) => {
      setConnected(false);
      // Retrying every 2 s with a dead token would spin forever.
      if (ev.code === WS_UNAUTHORIZED) {
        window.dispatchEvent(new CustomEvent(EVT_AUTH_EXPIRED));
        return;
      }
      reconnectRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    ws.onerror = () => {
      ws.close();                                  // Route the failure through onclose
    };
  }, []);                                          // Stable for the hook's lifetime — see recordingRef

  useEffect(() => {
    if (!enabled) return;
    connect();
    return () => {
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect, enabled]);

  useEffect(() => {
    const pollStatus = async () => {
      try {
        const data = await api(EP.STATUS);
        setSerialConnected(data.serial?.connected ?? false);
        setSerialPort(data.serial?.port ?? "");
        setSerialError(data.serial?.error ?? "");
        setHealth(data.health ?? null);
        setRecorderInfo(data.recorder ?? null);
        if (data.profile) setProfile(data.profile);  // In case a WS push was missed
      } catch {
        // Backend unreachable — leave the last known state as-is
      }
    };
    if (!enabled) return;
    pollStatus();
    const interval = setInterval(pollStatus, STATUS_POLL_MS);
    return () => clearInterval(interval);
  }, [enabled]);

  // The 2 s poll above is too coarse for a hold countdown or a live
  // ramp position, but polling at 4 Hz all the time would be wasteful.
  // This effect only exists while a profile is in flight.
  useEffect(() => {
    if (!profile?.running) return;
    const tick = async () => {
      try {
        const data = await api(EP.PROFILE_STATUS);
        if (data.profile) setProfile(data.profile);
      } catch {
        // The WS push will re-sync when the backend returns
      }
    };
    const interval = setInterval(tick, PROFILE_POLL_MS);
    return () => clearInterval(interval);
  }, [profile?.running]);

  const sendTare = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: ACTION.TARE }));
      setTareStatus(TARE_STATUS.SENDING);
    }
  }, []);

  const sendThrottle = useCallback((us) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: ACTION.THROTTLE, us }));
    }
  }, []);

  // The backend owns the sequencing; this just hands over the plan.
  const startProfile = useCallback(async (steps, rampRateUsPerS) => {
    setProfileError("");
    try {
      const data = await api(EP.PROFILE_START, {
        method: METHOD_POST,
        body: { steps, ramp_rate_us_per_s: rampRateUsPerS },
      });
      if (!data.ok) {
        setProfileError(data.error ?? "Could not start profile");
        return false;
      }
      return true;
    } catch (e) {
      setProfileError(e.message ?? MSG_BACKEND_UNREACHABLE);
      return false;
    }
  }, []);

  const stopProfile = useCallback(async () => {
    try {
      await api(EP.PROFILE_STOP, { method: METHOD_POST });
    } catch {
      setProfileError("Could not reach backend — use CUT THROTTLE");
    }
  }, []);

  // All three calibration actions go through the backend, which relays
  // them to the ESP32 and waits for it to confirm. Nothing is cached in
  // the browser, so a value that failed to apply can't look like it stuck.
  const applyCalPayload = useCallback((data) => {
    setCalibration({
      fields:   data.fields   ?? [],
      values:   data.values   ?? {},
      defaults: data.defaults ?? {},
    });
  }, []);

  const fetchCalibration = useCallback(async () => {
    setCalibrationStatus(CAL_STATUS.LOADING);
    setCalibrationError("");
    try {
      const data = await api(EP.CALIBRATION);
      applyCalPayload(data);
      // A cached answer is still the last thing the device reported and
      // worth showing — but say so rather than passing it off as live.
      if (data.error) setCalibrationError(data.error);
      setCalibrationStatus(data.error ? CAL_STATUS.ERROR : CAL_STATUS.IDLE);
    } catch (e) {
      setCalibrationError(e.message ?? MSG_BACKEND_UNREACHABLE);
      setCalibrationStatus(CAL_STATUS.ERROR);
    }
  }, [applyCalPayload]);

  const saveCalibration = useCallback(async (values) => {
    setCalibrationStatus(CAL_STATUS.SAVING);
    setCalibrationError("");
    try {
      const data = await api(EP.CALIBRATION, { method: METHOD_POST, body: { values } });
      if (!data.ok) {
        setCalibrationError(data.error ?? "Save failed");
        setCalibrationStatus(CAL_STATUS.ERROR);
        return false;
      }
      applyCalPayload(data);
      if (data.error) {                            // Saved, but no read-back confirmation
        setCalibrationError(data.error);
        setCalibrationStatus(CAL_STATUS.ERROR);
        return true;
      }
      setCalibrationStatus(CAL_STATUS.SAVED);
      setTimeout(() => setCalibrationStatus(CAL_STATUS.IDLE), CAL_SAVED_CLEAR_MS);
      return true;
    } catch (e) {
      setCalibrationError(e.message ?? MSG_BACKEND_UNREACHABLE);
      setCalibrationStatus(CAL_STATUS.ERROR);
      return false;
    }
  }, [applyCalPayload]);

  const resetCalibration = useCallback(async () => {
    setCalibrationStatus(CAL_STATUS.SAVING);
    setCalibrationError("");
    try {
      const data = await api(EP.CALIBRATION_RESET, { method: METHOD_POST });
      if (!data.ok) {
        setCalibrationError(data.error ?? "Reset failed");
        setCalibrationStatus(CAL_STATUS.ERROR);
        return false;
      }
      applyCalPayload(data);
      setCalibrationStatus(CAL_STATUS.SAVED);
      setTimeout(() => setCalibrationStatus(CAL_STATUS.IDLE), CAL_SAVED_CLEAR_MS);
      return true;
    } catch (e) {
      setCalibrationError(e.message ?? MSG_BACKEND_UNREACHABLE);
      setCalibrationStatus(CAL_STATUS.ERROR);
      return false;
    }
  }, [applyCalPayload]);

  const refreshPorts = useCallback(async () => {
    try {
      const data = await api(EP.PORTS);
      setPorts(data.ports ?? []);
    } catch {
      setPorts([]);
    }
  }, []);

  const connectSerial = useCallback(async (port) => {
    try {
      const data = await api(EP.CONNECT, { method: METHOD_POST, body: { port } });
      if (data.ok) {
        setSerialConnected(true);                  // Optimistic; /status confirms shortly
        setSerialPort(port);
        setSerialError("");
      } else {
        setSerialError(data.error ?? "Connection failed");
      }
    } catch (e) {
      setSerialError(e.message ?? MSG_BACKEND_UNREACHABLE);
    }
  }, []);

  const disconnectSerial = useCallback(async () => {
    try {
      await api(EP.DISCONNECT, { method: METHOD_POST });
      setSerialConnected(false);
      setSerialPort("");
    } catch (e) {
      setSerialError(e.message ?? MSG_BACKEND_UNREACHABLE);
    }
  }, []);

  // testName flows through to the on-disk CSV filename and the run's
  // metadata, so a folder of runs is identifiable without opening each.
  const startRecording = useCallback(async (testName = "") => {
    setRecordingError("");
    try {
      const data = await api(EP.RECORDING_START, {
        method: METHOD_POST,
        body: { test_name: testName },
      });
      setRecording(true);
      setFrameCount(0);
      setRecorderInfo(data ?? null);
      // The backend still records in memory if the disk write failed, so
      // this is a warning rather than a failure to start.
      if (data?.error) setRecordingError(data.error);
      return true;
    } catch (e) {
      setRecordingError(e.message ?? "Could not start recording");
      return false;
    }
  }, []);

  // No download: the run is already written to the operator's Documents
  // folder as it happens. This closes the files and reports where.
  const stopRecording = useCallback(async () => {
    try {
      const data = await api(EP.RECORDING_STOP, { method: METHOD_POST });
      setRecorderInfo(data ?? null);
      if (data?.csv_path) {
        setRecordingNotice(
          `Saved ${data.frame_count ?? 0} frames to ${data.csv_path}`
        );
        setTimeout(() => setRecordingNotice(""), RECORDING_NOTICE_MS);
      }
    } catch (e) {
      setRecordingError(e.message ?? "Could not stop the recording cleanly");
    } finally {
      setRecording(false);
    }
  }, []);

  return {
    latest, history, connected, tareStatus, sendTare, sendThrottle,
    confirmedThrottleUs,
    ports, refreshPorts, serialConnected, serialPort, serialError,
    connectSerial, disconnectSerial,
    health,
    recording, frameCount, startRecording, stopRecording,
    recorderInfo, recordingError, recordingNotice,
    profile, profileError, startProfile, stopProfile,
    calibration, calibrationStatus, calibrationError,
    fetchCalibration, saveCalibration, resetCalibration,
  };
}

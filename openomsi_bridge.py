# -*- coding: utf-8 -*-
"""OpenOMSI bridge for OMSI Auto-Informant.

The companion app decodes user audio into WAV files, then openOMSI's plugin audio
engine performs the final spatial playback. Route, navigation and settings use
separate atomic JSON files so navigation messages cannot overwrite audio requests.
"""
from __future__ import annotations

import hashlib
from array import array
import json
import math
import os
import socket
import struct
import shutil
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Callable


class OpenOMSIBridgeError(RuntimeError):
    """An error communicating with the openOMSI plugin."""


class _Waiter:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.error: str | None = None
        self.last_event: dict[str, Any] | None = None


class OpenOMSIBridge:
    DEFAULTS: dict[str, Any] = {
        "enabled": True,
        "plugin_dir": "",
        "udp_port": 47821,
        "source_mode": "ceiling",  # ceiling | bus | fixed
        "volume": 0.9,
        "range": 7.0,
        "speaker_forward": -1.65,
        "speaker_right": 0.35,
        "speaker_up": 2.25,
        # 0 = clean, 100 = noticeably damaged / tiny loudspeaker.
        "speaker_effect": 45,
        "informant_gain": 1.0,
        # Multiplier relative to the selected Auto-Informant volume while camera is outside.
        "exterior_volume": 0.25,
        "exterior_range_factor": 0.55,
        "exterior_range": 45.0,
        "config_version": 3,
        "auto_start_matching_trip": True,
        "follow_game_progress": True,
        "allow_manual_ibis_sync": True,
        "start_timeout": 8.0,
        "playback_timeout_extra": 30.0,
        "fixed_x": 0.0,
        "fixed_y": 0.0,
        "fixed_z": 0.0,
        # The PA/informant uses a distributed ceiling speaker; music uses the forward radio speaker.
        "music_source_mode": "ceiling",
        "music_range": 9.0,
        "music_speaker_forward": 2.10,
        "music_speaker_right": 0.70,
        "music_speaker_up": 1.25,
        "music_exterior_volume": 0.30,
        "music_fixed_x": 0.0,
        "music_fixed_y": 0.0,
        "music_fixed_z": 0.0,
        "informant_output_level": 1.0,
        "music_output_level": 1.0,
    }

    def __init__(
        self,
        config: dict[str, Any],
        on_key: Callable[[str], None] | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        merged = dict(self.DEFAULTS)
        supplied = dict(config or {})
        # Backward-compatible migration of v1's boolean radio_filter setting.
        if "speaker_effect" not in supplied:
            supplied["speaker_effect"] = 45 if supplied.get("radio_filter", True) else 0
        merged.update(supplied)
        self.config = merged
        self.enabled = bool(merged.get("enabled", True))
        raw_dir = str(merged.get("plugin_dir") or "").strip()
        if self.enabled and not raw_dir:
            raise OpenOMSIBridgeError("plugin_dir is empty in openomsi_bridge_config.json")
        self.plugin_dir = Path(raw_dir or ".").expanduser().resolve()
        self.port = int(merged.get("udp_port", 47821))
        if not 1024 <= self.port <= 65535:
            raise OpenOMSIBridgeError("udp_port must be in range 1024..65535")
        self.on_key = on_key
        self.on_state = on_state
        self.command_file = self.plugin_dir / "bridge_command.json"
        self.music_command_file = self.plugin_dir / "bridge_music.json"
        self.route_file = self.plugin_dir / "bridge_route.json"
        self.nav_file = self.plugin_dir / "bridge_nav.json"
        self.settings_file = self.plugin_dir / "bridge_settings.json"
        self.cache_dir = self.plugin_dir / "audio" / "cache"
        self.stream_cache_dir = self.plugin_dir / "audio" / "stream_cache"
        self._socket: socket.socket | None = None
        self._receiver: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._write_lock = threading.RLock()
        self._conversion_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._waiters: dict[str, _Waiter] = {}
        self._last_seen = 0.0
        self._current_id: str | None = None
        self._started = False
        self._last_game_state: dict[str, Any] | None = None
        self._last_state_signature = ""
        self._pending_state_chunks: dict[str, dict[str, Any]] = {}
        self._last_app_stop_index: int | None = None
        self._last_app_step: int | None = None

    @classmethod
    def from_config_file(
        cls,
        path: str | os.PathLike[str],
        on_key: Callable[[str], None] | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> "OpenOMSIBridge":
        config_path = Path(path).expanduser().resolve()
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OpenOMSIBridgeError(f"cannot read bridge config {config_path}: {exc}") from exc
        if not isinstance(config, dict):
            raise OpenOMSIBridgeError("bridge config must be a JSON object")
        return cls(config, on_key=on_key, on_state=on_state)

    @property
    def connected(self) -> bool:
        return self.enabled and (time.monotonic() - self._last_seen) < 8.0

    @property
    def last_game_state(self) -> dict[str, Any] | None:
        with self._state_lock:
            return dict(self._last_game_state) if self._last_game_state else None

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        main_lua = self.plugin_dir / "main.lua"
        if not main_lua.is_file():
            raise OpenOMSIBridgeError(f"main.lua not found in plugin directory: {self.plugin_dir}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", self.port))
        sock.settimeout(0.25)
        self._socket = sock
        self._shutdown.clear()
        self._receiver = threading.Thread(target=self._receive_loop, name="openOMSI-bridge", daemon=True)
        self._receiver.start()
        self._started = True
        # These are separate channels: the plugin polls them independently.
        self._write_json(self.settings_file, self._settings_payload())
        self._write_command({"id": uuid.uuid4().hex, "action": "noop"})

    def close(self) -> None:
        if not self.enabled:
            return
        try:
            self.stop()
        except Exception:
            pass
        self._shutdown.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._receiver
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.6)
        self._started = False

    def _notify_state(self, state: dict[str, Any]) -> None:
        callback = self.on_state
        if callback:
            try:
                callback(dict(state))
            except Exception as exc:
                print(f"[OpenOMSI bridge] state callback error: {exc}")

    def _receive_loop(self) -> None:
        while not self._shutdown.is_set():
            sock = self._socket
            if sock is None:
                break
            try:
                data, address = sock.recvfrom(64 * 1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if address[0] not in ("127.0.0.1", "::1"):
                continue
            try:
                event = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            self._last_seen = time.monotonic()
            event_type = str(event.get("type") or "")
            event_id = event.get("id")
            if event_type in {"door_close_request", "door_close_warning", "music_started", "music_finished", "music_stopped", "music_error", "native_radio_station_changed"}:
                self._notify_state(event)
            if event_type == "started":
                with self._state_lock:
                    waiter = self._waiters.get(str(event_id))
                    if waiter:
                        waiter.last_event = event
                        waiter.started.set()
            elif event_type in ("finished", "stopped", "error"):
                with self._state_lock:
                    waiter = self._waiters.get(str(event_id))
                    if waiter:
                        waiter.last_event = event
                        if event_type == "error":
                            waiter.error = str(event.get("message") or "plugin playback error")
                        waiter.finished.set()
                # Surface plugin-side errors even if they are not audio waiters.
                if event_type == "error":
                    self._notify_state(event)
            elif event_type == "hotkey" and str(event.get("key", "")).upper() == "Q":
                callback = self.on_key
                if callback:
                    try:
                        callback("Q")
                    except Exception as exc:
                        print(f"[OpenOMSI bridge] key callback error: {exc}")
            elif event_type == "game_state":
                self._handle_game_state_event(event)
            elif event_type == "duty_stop_chunk":
                self._handle_duty_stop_chunk(event)
            elif event_type in ("ready", "plugin_started", "route_status", "nav_ack", "control_status"):
                self._notify_state(event)

    def _publish_game_state(self, event: dict[str, Any]) -> None:
        signature = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._state_lock:
            self._last_game_state = dict(event)
        if signature != self._last_state_signature:
            self._last_state_signature = signature
            self._notify_state(event)

    def _handle_game_state_event(self, event: dict[str, Any]) -> None:
        if not event.get("stops_chunked"):
            self._publish_game_state(event)
            return
        sequence = str(event.get("state_seq") or "")
        try:
            expected = int(event.get("duty_stop_chunks") or 0)
        except (TypeError, ValueError):
            expected = 0
        if not sequence or expected < 1 or expected > 512:
            # Empty/unusable chunk metadata: publish the basic state with an empty stop list.
            partial = dict(event)
            partial.pop("stops_chunked", None)
            partial.pop("duty_stop_chunks", None)
            partial.pop("duty_stop_count", None)
            partial["duty_stop_objects"] = []
            partial["duty_stops"] = []
            self._publish_game_state(partial)
            return
        now = time.monotonic()
        for key, pending in list(self._pending_state_chunks.items()):
            if now - float(pending.get("created", now)) > 4.0:
                self._pending_state_chunks.pop(key, None)
        # Keep a bounded number of in-flight snapshots if a packet was lost.
        while len(self._pending_state_chunks) >= 4:
            oldest = min(self._pending_state_chunks, key=lambda k: self._pending_state_chunks[k].get("created", 0.0))
            self._pending_state_chunks.pop(oldest, None)
        self._pending_state_chunks[sequence] = {
            "base": dict(event), "expected": expected, "chunks": {}, "created": now,
        }

    def _handle_duty_stop_chunk(self, event: dict[str, Any]) -> None:
        sequence = str(event.get("state_seq") or "")
        pending = self._pending_state_chunks.get(sequence)
        if pending is None:
            return
        try:
            index = int(event.get("chunk_index") or 0)
            expected = int(pending.get("expected") or 0)
        except (TypeError, ValueError):
            return
        stops = event.get("stops")
        if not (1 <= index <= expected) or not isinstance(stops, list):
            return
        pending["chunks"][index] = stops
        if len(pending["chunks"]) < expected:
            return
        merged_stops: list[dict[str, Any]] = []
        for chunk_index in range(1, expected + 1):
            chunk = pending["chunks"].get(chunk_index)
            if not isinstance(chunk, list):
                return
            merged_stops.extend(item for item in chunk if isinstance(item, dict))
        result = dict(pending["base"])
        result.pop("stops_chunked", None)
        result.pop("duty_stop_chunks", None)
        result.pop("duty_stop_count", None)
        result["duty_stop_objects"] = merged_stops
        result["duty_stops"] = [str(stop.get("name") or "") for stop in merged_stops]
        result["stops_chunked"] = False
        self._pending_state_chunks.pop(sequence, None)
        self._publish_game_state(result)

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    def _write_command(self, command: dict[str, Any]) -> None:
        if not self.enabled:
            raise OpenOMSIBridgeError("openOMSI bridge is disabled")
        with self._write_lock:
            self._write_json(self.command_file, command)

    def _settings_payload(self) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "action": "settings",
            "config_version": 3,
            "speaker_effect": max(0, min(100, int(self.config.get("speaker_effect", 45)))),
            "exterior_volume": self._clamp_float(self.config.get("exterior_volume", 0.25), 0.0, 1.0, 0.25),
            "music_exterior_volume": self._clamp_float(self.config.get("music_exterior_volume", 0.30), 0.0, 1.0, 0.30),
            "exterior_range_factor": self._clamp_float(self.config.get("exterior_range_factor", 0.55), 0.20, 1.0, 0.55),
            "exterior_range": self._clamp_float(self.config.get("exterior_range", 45.0), 2.0, 100.0, 45.0),
            "auto_start_matching_trip": bool(self.config.get("auto_start_matching_trip", True)),
            "follow_game_progress": bool(self.config.get("follow_game_progress", True)),
            "allow_manual_ibis_sync": bool(self.config.get("allow_manual_ibis_sync", True)),
            "range": self._clamp_float(self.config.get("range", 7.0), 0.5, 100.0, 7.0),
            "source_mode": str(self.config.get("source_mode", "ceiling")),
            "speaker_forward": self._clamp_float(self.config.get("speaker_forward", -1.65), -8.0, 8.0, -1.65),
            "speaker_right": self._clamp_float(self.config.get("speaker_right", 0.35), -8.0, 8.0, 0.35),
            "speaker_up": self._clamp_float(self.config.get("speaker_up", 2.25), -2.0, 8.0, 2.25),
            "informant_output_level": self._clamp_float(self.config.get("informant_output_level", 1.0), 0.0, 1.0, 1.0),
            "music_output_level": self._clamp_float(self.config.get("music_output_level", 1.0), 0.0, 1.0, 1.0),
        }

    @staticmethod
    def _clamp_float(value: Any, lo: float, hi: float, fallback: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return max(lo, min(hi, number))

    def update_config(self, config: dict[str, Any]) -> None:
        """Apply new app settings without restarting the Python app or game."""
        if not isinstance(config, dict):
            return
        self.config.update(config)
        if self.enabled and self._started:
            try:
                with self._write_lock:
                    self._write_json(self.settings_file, self._settings_payload())
            except Exception as exc:
                print(f"[OpenOMSI bridge] unable to publish settings: {exc}")

    def set_app_route(self, map_name: str, route_name: str, stops: list[str]) -> None:
        """Publish the selected app route; the plugin can match it to a timetable trip."""
        if not self.enabled:
            return
        names = [str(x).strip() for x in stops if str(x).strip()]
        self._last_app_stop_index = None
        self._last_app_step = None
        payload = {
            "id": uuid.uuid4().hex,
            "action": "set_route",
            "map": str(map_name),
            "route": str(route_name),
            "stops": names,
        }
        with self._write_lock:
            self._write_json(self.route_file, payload)

    def sync_step(self, target_index: int, target_name: str, step: int) -> None:
        """Sync one announced stop with IBIS; target_index is zero-based in the live duty stop list from openOMSI."""
        if not self.enabled:
            return
        try:
            target_index = int(target_index)
        except (TypeError, ValueError):
            return
        if target_index < 0:
            return
        step = int(step)
        previous_index = self._last_app_stop_index
        previous_step = self._last_app_step
        target_delta = 0 if previous_index is None else max(-1000, min(1000, target_index - previous_index))
        if previous_step is None or step == previous_step:
            step_delta = 0
        else:
            step_delta = 1 if step > previous_step else -1
        # If moving between the app's arrival/departure phases keeps the same target
        # stop, preserve the user's button direction for the manual-IBIS fallback.
        delta = target_delta if target_delta != 0 else step_delta
        self._last_app_stop_index = target_index
        self._last_app_step = step
        payload = {
            "id": uuid.uuid4().hex,
            "action": "navigate",
            "target_index": target_index,
            "target_name": str(target_name),
            "step": step,
            "delta": delta,
            "target_delta": target_delta,
            "step_delta": step_delta,
            "allow_manual_ibis_sync": bool(self.config.get("allow_manual_ibis_sync", True)),
        }
        with self._write_lock:
            self._write_json(self.nav_file, payload)

    def stop(self) -> None:
        if not self.enabled:
            return
        with self._state_lock:
            current_id = self._current_id
            waiter = self._waiters.get(current_id) if current_id else None
            if waiter is not None:
                waiter.last_event = {"type": "stopped", "reason": "local stop requested"}
                waiter.started.set()
                waiter.finished.set()
        try:
            self._write_command({"id": uuid.uuid4().hex, "action": "stop"})
        except Exception as exc:
            print(f"[OpenOMSI bridge] stop command failed: {exc}")

    def set_output_levels(self, informant_level: float, music_level: float) -> None:
        """Set independent post-source faders through the dedicated settings channel.

        Do not use bridge_command.json for this: that file is a one-shot playback
        mailbox, and a frequent slider update could overwrite a pending play/stop.
        The Lua plugin polls bridge_settings.json independently.
        """
        try:
            informant = self._clamp_float(informant_level, 0.0, 1.0, 1.0)
            music = self._clamp_float(music_level, 0.0, 1.0, 1.0)
        except (TypeError, ValueError) as exc:
            raise OpenOMSIBridgeError("output levels must be numeric") from exc
        self.update_config({
            "informant_output_level": informant,
            "music_output_level": music,
        })

    @staticmethod
    def _ffmpeg_binary() -> str | None:
        """Resolve the app-managed FFmpeg without requiring an audio output device."""
        try:
            import imageio_ffmpeg
            candidate = imageio_ffmpeg.get_ffmpeg_exe()
            if candidate and Path(candidate).is_file():
                return str(candidate)
        except Exception:
            pass
        return shutil.which("ffmpeg")

    @staticmethod
    def _speaker_ffmpeg_filters(intensity: float) -> str:
        """Audible but intelligible small-speaker coloration; avoids the old blanket muffling."""
        intensity = max(0.0, min(1.0, float(intensity)))
        if intensity <= 0.001:
            return ""
        # Keep substantially more top end than the old v1.7 filter and soften the crusher.
        highpass = min(180.0, 55.0 + 125.0 * intensity)
        lowpass = max(7800.0, 19000.0 - 11200.0 * (intensity ** 1.10))
        bits = max(12, min(16, int(round(16.0 - 4.0 * intensity))))
        crusher_mix = 0.02 + 0.10 * intensity
        ratio = 1.6 + 0.8 * intensity
        makeup = 1.45 + 0.45 * intensity
        chain = [
            f"highpass=f={highpass:.1f}",
            f"acompressor=threshold=0.68:ratio={ratio:.2f}:attack=10:release=110:makeup={makeup:.2f}",
            "asoftclip=type=atan",
            f"lowpass=f={lowpass:.1f}",
            f"acrusher=bits={bits}:mix={crusher_mix:.2f}",
            "volume=1.55",
            "alimiter=limit=0.97",
        ]
        return ",".join(chain)

    @staticmethod
    def _exterior_ffmpeg_filters() -> str:
        """Muffle the exterior copy in FFmpeg rather than iterating over samples in Python."""
        return "highpass=f=85,lowpass=f=1450,acompressor=threshold=0.55:ratio=2.2:attack=15:release=110,asoftclip=type=atan,lowpass=f=1450"

    def _decode_source_to_pcm_wav(
        self, source: Path, destination: Path, *, speaker_effect: int = 0, exterior: bool = False, extra_gain: float = 1.0
    ) -> None:
        """Decode/downmix to mono 44.1-kHz PCM, optionally applying native FFmpeg DSP."""
        ffmpeg = self._ffmpeg_binary()
        if not ffmpeg:
            raise OpenOMSIBridgeError("FFmpeg is unavailable; launch with run_app.bat to install bundled dependencies")
        destination.parent.mkdir(parents=True, exist_ok=True)
        effect = max(0, min(100, int(speaker_effect)))
        if exterior:
            audio_filters = self._exterior_ffmpeg_filters()
        else:
            audio_filters = self._speaker_ffmpeg_filters(effect)
        base = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:a:0", "-vn",
        ]
        extra_gain = max(1.0, min(2.0, float(extra_gain)))
        if extra_gain > 1.001:
            gain_filter = f"volume={extra_gain:.3f},alimiter=limit=0.97"
            audio_filters = f"{audio_filters},{gain_filter}" if audio_filters else gain_filter
        if audio_filters:
            attempts = [audio_filters]
            # acrusher is available in the bundled FFmpeg but not every system build.
            if not exterior and "acrusher=" in audio_filters:
                attempts.append(",".join(x for x in audio_filters.split(",") if not x.startswith("acrusher=")))
        else:
            attempts = [""]
        last_detail = "no FFmpeg diagnostic"
        try:
            for audio_filter in attempts:
                destination.unlink(missing_ok=True)
                command = list(base)
                if audio_filter:
                    command.extend(["-af", audio_filter])
                command.extend(["-ac", "1", "-ar", "44100", "-acodec", "pcm_s16le", str(destination)])
                try:
                    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                            timeout=45 if source.stat().st_size < 2 * 1024 * 1024 else 240, check=False)
                except subprocess.TimeoutExpired as exc:
                    raise OpenOMSIBridgeError(f"FFmpeg timed out while decoding {source.name}") from exc
                if result.returncode == 0 and destination.is_file():
                    return
                last_detail = (result.stderr or "no FFmpeg diagnostic").strip()[-1200:]
            destination.unlink(missing_ok=True)
            raise OpenOMSIBridgeError(f"FFmpeg could not decode/process {source.name}: {last_detail}")
        except OpenOMSIBridgeError:
            destination.unlink(missing_ok=True)
            raise

    def _prepare_wav(self, source: Path, *, ephemeral: bool = False, extra_gain: float | None = None) -> tuple[Path, float]:
        """Prepare a mono PCM16 WAV for positional playback, without pygame/audio-device init."""
        source = source.expanduser().resolve()
        if not source.is_file():
            raise OpenOMSIBridgeError(f"audio file not found: {source}")
        effect = int(max(0, min(100, int(self.config.get("speaker_effect", 45)))))
        if extra_gain is None:
            extra_gain = max(1.0, min(2.0, float(self.config.get("informant_gain", 1.0))))
        else:
            extra_gain = max(1.0, min(2.0, float(extra_gain)))
        if ephemeral:
            self.stream_cache_dir.mkdir(parents=True, exist_ok=True)
            destination = self.stream_cache_dir / (uuid.uuid4().hex + ".wav")
        else:
            digest = hashlib.sha256()
            with source.open("rb") as src:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(f"\0small-speaker-ffmpeg-v12-bright-mono-spatial={effect}-gain={extra_gain:.3f}".encode("ascii"))
            filename = digest.hexdigest()[:32] + ".wav"
            destination = self.cache_dir / filename
        if not ephemeral and destination.is_file():
            try:
                with wave.open(str(destination), "rb") as wav:
                    if wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 44100:
                        duration = wav.getnframes() / max(1, wav.getframerate())
                        return destination, duration
            except Exception:
                pass
            destination.unlink(missing_ok=True)

        decode_tmp = destination.with_name(destination.stem + ".decode.tmp.wav")
        final_tmp = destination.with_suffix(".tmp.wav")
        try:
            self._decode_source_to_pcm_wav(source, decode_tmp, speaker_effect=effect, extra_gain=extra_gain)
            with wave.open(str(decode_tmp), "rb") as decoded:
                channels = decoded.getnchannels()
                sample_width = decoded.getsampwidth()
                sample_rate = decoded.getframerate()
                raw = decoded.readframes(decoded.getnframes())
            if channels not in (1, 2) or sample_width != 2 or sample_rate != 44100:
                raise OpenOMSIBridgeError(
                    f"FFmpeg returned unexpected audio format: {channels}ch, {sample_width * 8}bit, {sample_rate}Hz"
                )
            if not raw:
                raise OpenOMSIBridgeError(f"decoded audio is empty: {source.name}")
            # FFmpeg already applied the speaker DSP (when enabled) and downmixed
            # into mono, so app-capture chunks no longer pass through Python sample loops.
            output = self._downmix_pcm16_mono(raw, channels) if channels == 2 else raw
            destination.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(final_tmp), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(44100)
                wav.writeframes(output)
            os.replace(final_tmp, destination)
            with wave.open(str(destination), "rb") as wav:
                duration = wav.getnframes() / max(1, wav.getframerate())
            return destination, duration
        except OpenOMSIBridgeError:
            destination.unlink(missing_ok=True)
            raise
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise OpenOMSIBridgeError(f"cannot convert {source.name} to plugin WAV: {exc}") from exc
        finally:
            decode_tmp.unlink(missing_ok=True)
            final_tmp.unlink(missing_ok=True)

    @staticmethod
    def _pcm_to_mono16(raw: bytes, sample_width: int, channels: int) -> bytes:
        """Convert uncompressed little-endian 8/16/24/32-bit PCM to signed mono PCM16."""
        if channels not in (1, 2):
            raise OpenOMSIBridgeError(f"PCM conversion supports mono/stereo only, got {channels}")
        if sample_width == 2:
            return OpenOMSIBridge._downmix_pcm16_mono(raw, channels)
        if sample_width not in (1, 3, 4):
            raise OpenOMSIBridgeError(f"unsupported PCM sample width: {sample_width} bytes")
        bytes_per_frame = sample_width * channels
        frames = len(raw) // bytes_per_frame
        output = bytearray(frames * 2)
        bits = sample_width * 8
        sign_bit = 1 << (bits - 1)
        modulus = 1 << bits
        shift = bits - 16
        for frame in range(frames):
            base = frame * bytes_per_frame
            values = []
            for channel in range(channels):
                chunk = raw[base + channel * sample_width:base + (channel + 1) * sample_width]
                if sample_width == 1:
                    # WAV 8-bit PCM uses unsigned samples.
                    sample = (chunk[0] - 128) << 8
                else:
                    sample = int.from_bytes(chunk, "little", signed=False)
                    if sample & sign_bit:
                        sample -= modulus
                    sample >>= shift
                values.append(sample)
            mono = sum(values) // channels
            struct.pack_into("<h", output, frame * 2, max(-32768, min(32767, mono)))
        return bytes(output)

    @staticmethod
    def _downmix_pcm16_mono(raw: bytes, channels: int) -> bytes:
        """Downmix signed 16-bit PCM to mono for true positional playback."""
        if channels == 1:
            return raw
        if channels != 2:
            raise OpenOMSIBridgeError(f"mono downmix supports mono/stereo only, got {channels}")
        samples_count = len(raw) // 2
        samples = struct.unpack("<" + "h" * samples_count, raw[:samples_count * 2]) if samples_count else ()
        frames = len(samples) // 2
        output = bytearray(frames * 2)
        for frame in range(frames):
            mono = int((int(samples[frame * 2]) + int(samples[frame * 2 + 1])) / 2)
            struct.pack_into("<h", output, frame * 2, max(-32768, min(32767, mono)))
        return bytes(output)

    @staticmethod
    def _small_speaker_pcm(raw: bytes, sample_rate: int, channels: int, intensity: float) -> bytes:
        """Model a narrow-band bus radio / PA speaker at intensity 0..1.

        Uses second-order band-limiting, stronger compression/saturation and a
        controllable bit-depth reduction. At 100% it should be unmistakably less
        hi-fi than the source, including for streamed WAV segments.
        """
        if channels not in (1, 2):
            raise OpenOMSIBridgeError(f"small-speaker processing supports mono/stereo only, got {channels}")
        if not raw:
            return b""
        intensity = max(0.0, min(1.0, float(intensity)))
        sample_rate = max(1000, int(sample_rate))
        samples_count = len(raw) // 2
        samples = struct.unpack("<" + "h" * samples_count, raw[: samples_count * 2])
        frames = samples_count // channels
        if frames == 0:
            return b""
        # Retain intelligible presence even at maximum effect. The previous 800-Hz
        # low-pass and a second identical post-saturation filter made all audio dull.
        hp_cutoff = 55.0 + 135.0 * intensity
        lp_cutoff = 18000.0 - 12600.0 * (intensity ** 1.15)
        lp_cutoff = min(lp_cutoff, sample_rate * 0.45)
        hp_cutoff = min(hp_cutoff, sample_rate * 0.20)

        def biquad_coeffs(kind: str, cutoff: float):
            omega = 2.0 * math.pi * min(cutoff, sample_rate * 0.45) / sample_rate
            cosine = math.cos(omega)
            sine = math.sin(omega)
            alpha = sine / (2.0 * 0.70710678)
            if kind == "highpass":
                b0 = (1.0 + cosine) * 0.5
                b1 = -(1.0 + cosine)
                b2 = (1.0 + cosine) * 0.5
            else:
                b0 = (1.0 - cosine) * 0.5
                b1 = 1.0 - cosine
                b2 = (1.0 - cosine) * 0.5
            a0 = 1.0 + alpha
            a1 = -2.0 * cosine
            a2 = 1.0 - alpha
            return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)

        hp = biquad_coeffs("highpass", hp_cutoff)
        lp = biquad_coeffs("lowpass", lp_cutoff)
        # Stereo becomes progressively narrow before the positional source is downmixed.
        width = 1.0 - 0.998 * intensity
        drive = 1.0 + 2.0 * intensity
        threshold = 0.90 - 0.18 * intensity
        compression = 0.08 + 0.38 * intensity
        quant_bits = max(9, min(15, int(round(15 - 6 * intensity))))
        quant_levels = float(2 ** (quant_bits - 1))
        result = bytearray(frames * channels * 2)
        hp_b0, hp_b1, hp_b2, hp_a1, hp_a2 = hp
        lp_b0, lp_b1, lp_b2, lp_a1, lp_a2 = lp
        # Biquad state arrays use [x1, x2, y1, y2] per channel.
        hp_state = [[0.0, 0.0, 0.0, 0.0] for _ in range(channels)]
        post_lp_state = [[0.0, 0.0, 0.0, 0.0] for _ in range(channels)]
        out_index = 0
        for frame in range(frames):
            base = frame * channels
            values = [samples[base + ch] / 32768.0 for ch in range(channels)]
            if channels == 2:
                mono = (values[0] + values[1]) * 0.5
                values = [mono + (value - mono) * width for value in values]
            for ch, value in enumerate(values):
                hstate = hp_state[ch]
                high = hp_b0 * value + hp_b1 * hstate[0] + hp_b2 * hstate[1] - hp_a1 * hstate[2] - hp_a2 * hstate[3]
                hstate[1], hstate[0], hstate[3], hstate[2] = hstate[0], value, hstate[2], high
                pushed = high * (1.0 + 0.20 * intensity)
                magnitude = abs(pushed)
                if magnitude > threshold:
                    magnitude = threshold + (magnitude - threshold) * (1.0 - compression)
                # Strong soft clipping creates the compressed/strained character of an
                # overdriven tiny speaker. Normalize only by tanh(drive), not by drive,
                # otherwise the distortion would just make the signal quiet.
                signed = math.copysign(magnitude, pushed)
                saturated = math.tanh(signed * drive) / max(1e-6, math.tanh(drive))
                # A single post-saturation low-pass keeps consonants and presence.
                pstate = post_lp_state[ch]
                post = lp_b0 * saturated + lp_b1 * pstate[0] + lp_b2 * pstate[1] - lp_a1 * pstate[2] - lp_a2 * pstate[3]
                pstate[1], pstate[0], pstate[3], pstate[2] = pstate[0], saturated, pstate[2], post
                saturated = min(0.985, max(-0.985, post * (1.15 + 0.25 * intensity)))
                if intensity >= 0.35:
                    saturated = round(saturated * quant_levels) / quant_levels
                packed = int(max(-0.985, min(0.985, saturated)) * 32767.0)
                struct.pack_into("<h", result, out_index, packed)
                out_index += 2
        return bytes(result)

    def _prepare_exterior_wav(self, source: Path, *, ephemeral: bool = False) -> tuple[Path, float]:
        """Make a softly muffled mono variant for playback through the body/windows."""
        source = source.expanduser().resolve()
        if not source.is_file():
            raise OpenOMSIBridgeError(f"audio file not found: {source}")
        if ephemeral:
            self.stream_cache_dir.mkdir(parents=True, exist_ok=True)
            destination = self.stream_cache_dir / (uuid.uuid4().hex + "_outside.wav")
        else:
            digest = hashlib.sha256()
            with source.open("rb") as src:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0outside-window-muffle-ffmpeg-v5=hp85-lp1450-mono")
            destination = self.cache_dir / (digest.hexdigest()[:32] + ".wav")
        if not ephemeral and destination.is_file():
            try:
                with wave.open(str(destination), "rb") as wav:
                    if wav.getnchannels() == 1 and wav.getsampwidth() == 2:
                        return destination, wav.getnframes() / max(1, wav.getframerate())
            except Exception:
                destination.unlink(missing_ok=True)
        tmp = destination.with_suffix(".tmp.wav")
        try:
            # Same-rate decode plus filtering is much faster than Python's old
            # sample-by-sample exterior muffle on streaming audio chunks.
            self._decode_source_to_pcm_wav(source, tmp, exterior=True)
            os.replace(tmp, destination)
            with wave.open(str(destination), "rb") as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise OpenOMSIBridgeError("FFmpeg returned unexpected outside WAV format")
                duration = wav.getnframes() / max(1, wav.getframerate())
            return destination, duration
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise OpenOMSIBridgeError(f"cannot create outside-muffled WAV for {source.name}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _exterior_muffle_pcm(raw: bytes, sample_rate: int, channels: int) -> bytes:
        """Mono, low-pass-through-window coloration: a distant murmur, not a second clear voice."""
        if channels not in (1, 2):
            raise OpenOMSIBridgeError(f"outside muffle supports mono/stereo only, got {channels}")
        samples_count = len(raw) // 2
        if samples_count == 0:
            return b""
        samples = struct.unpack("<" + "h" * samples_count, raw[:samples_count * 2])
        frames = samples_count // channels
        dt = 1.0 / max(1, sample_rate)
        hp_cutoff = 85.0
        lp_cutoff = 1450.0
        hp_rc = 1.0 / (2.0 * math.pi * hp_cutoff)
        lp_rc = 1.0 / (2.0 * math.pi * lp_cutoff)
        hp_alpha = hp_rc / (hp_rc + dt)
        lp_alpha = dt / (lp_rc + dt)
        previous_input = 0.0
        previous_high = 0.0
        low_state = 0.0
        output = bytearray(frames * 2)
        out_index = 0
        for frame in range(frames):
            base = frame * channels
            if channels == 2:
                value = (samples[base] + samples[base + 1]) / (2.0 * 32768.0)
            else:
                value = samples[base] / 32768.0
            high = hp_alpha * (previous_high + value - previous_input)
            previous_input = value
            previous_high = high
            low_state += lp_alpha * (high - low_state)
            pushed = low_state * 1.05
            magnitude = abs(pushed)
            if magnitude > 0.50:
                magnitude = 0.50 + (magnitude - 0.50) * 0.28
            shaped = math.copysign(math.tanh(magnitude * 1.15) / math.tanh(1.15), pushed)
            struct.pack_into("<h", output, out_index, int(max(-0.92, min(0.92, shaped)) * 32767.0))
            out_index += 2
        return bytes(output)

    def _spatial_payload(self, plugin_wav: Path, outside_wav: Path | None, volume: float, *, music: bool = False) -> dict[str, Any]:
        try:
            relative = plugin_wav.relative_to(self.plugin_dir).as_posix()
            outside_relative = outside_wav.relative_to(self.plugin_dir).as_posix() if outside_wav else ""
        except ValueError as exc:
            raise OpenOMSIBridgeError("converted WAV ended up outside the plugin folder") from exc
        if music:
            source_mode = self.config.get("music_source_mode", "ceiling")
            source_range = self.config.get("music_range", 9.0)
            forward = self.config.get("music_speaker_forward", 2.10)
            right = self.config.get("music_speaker_right", 0.70)
            up = self.config.get("music_speaker_up", 1.25)
            fixed_x = self.config.get("music_fixed_x", 0.0)
            fixed_y = self.config.get("music_fixed_y", 0.0)
            fixed_z = self.config.get("music_fixed_z", 0.0)
        else:
            source_mode = self.config.get("source_mode", "ceiling")
            source_range = self.config.get("range", 7.0)
            forward = self.config.get("speaker_forward", -1.65)
            right = self.config.get("speaker_right", 0.35)
            up = self.config.get("speaker_up", 2.25)
            fixed_x = self.config.get("fixed_x", 0.0)
            fixed_y = self.config.get("fixed_y", 0.0)
            fixed_z = self.config.get("fixed_z", 0.0)
        return {
            "file": relative,
            "outside_file": outside_relative,
            "volume": max(0.0, min(1.0, float(volume))),
            "exterior_volume": self._clamp_float(
                self.config.get("music_exterior_volume", 0.30) if music else self.config.get("exterior_volume", 0.25),
                0.0, 1.0, 0.30 if music else 0.25,
            ),
            "exterior_range_factor": self._clamp_float(self.config.get("exterior_range_factor", 0.55), 0.20, 1.0, 0.55),
            "exterior_range": self._clamp_float(self.config.get("exterior_range", 45.0), 2.0, 100.0, 45.0),
            "source": str(source_mode),
            "range": self._clamp_float(source_range, 0.5, 100.0, 9.0 if music else 7.0),
            "forward": self._clamp_float(forward, -8.0, 8.0, 2.10 if music else -1.65),
            "right": self._clamp_float(right, -8.0, 8.0, 0.70 if music else 0.35),
            "up": self._clamp_float(up, -2.0, 8.0, 1.25 if music else 2.25),
            "x": fixed_x,
            "y": fixed_y,
            "z": fixed_z,
        }

    def prepare_music(self, source: str | os.PathLike[str], volume: float = 0.55, *, ephemeral: bool = False) -> dict[str, Any]:
        """Prepare music before playback; ephemeral chunks are deleted after they leave the queue."""
        if not self.enabled:
            raise OpenOMSIBridgeError("openOMSI playback is disabled")
        if not self._started:
            self.start()
        source_path = Path(source).expanduser().resolve()
        with self._conversion_lock:
            effective_gain = max(0.0, min(2.0, float(volume)))
            plugin_wav, _duration = self._prepare_wav(source_path, ephemeral=ephemeral, extra_gain=max(1.0, effective_gain))
            outside_wav = None
            if ephemeral:
                # Segmented streams are restarted as separate game audio sources.
                # Tiny edge ramps suppress discontinuity clicks at each hand-off.
                self._fade_wav_edges(plugin_wav, fade_ms=7.0)
            try:
                outside_wav, _ = self._prepare_exterior_wav(plugin_wav, ephemeral=ephemeral)
                if ephemeral and outside_wav is not None:
                    self._fade_wav_edges(outside_wav, fade_ms=7.0)
            except Exception as exc:
                # Exterior variant is optional. A failed DSP pass must never disable the 3D source.
                print(f"[OpenOMSIBridge] exterior audio variant skipped: {exc}")
        payload = self._spatial_payload(plugin_wav, outside_wav, volume, music=True)
        payload["track"] = source_path.name
        if ephemeral:
            payload["_cleanup_files"] = [str(path) for path in (plugin_wav, outside_wav) if path is not None]
        return payload

    @staticmethod
    def _fade_wav_edges(path: Path, *, fade_ms: float = 7.0) -> None:
        """Apply short linear edge ramps to a temporary PCM16 WAV chunk."""
        path = Path(path)
        temp_path = path.with_name(path.stem + ".edge.tmp.wav")
        try:
            with wave.open(str(path), "rb") as source:
                channels = source.getnchannels()
                sample_width = source.getsampwidth()
                sample_rate = source.getframerate()
                frames = source.getnframes()
                raw = source.readframes(frames)
            if sample_width != 2 or channels not in (1, 2) or frames < 4:
                return
            samples = array("h")
            samples.frombytes(raw)
            if samples.itemsize != 2:
                return
            # WAV PCM is little-endian; array('h') follows host byte order.
            import sys
            if sys.byteorder != "little":
                samples.byteswap()
            fade_frames = min(max(1, int(round(sample_rate * max(0.0, fade_ms) / 1000.0))), frames // 2)
            if fade_frames <= 1:
                return
            for frame in range(fade_frames):
                fade_in = frame / float(fade_frames - 1)
                fade_out = (fade_frames - frame - 1) / float(fade_frames - 1)
                start = frame * channels
                end = (frames - fade_frames + frame) * channels
                for channel in range(channels):
                    samples[start + channel] = int(samples[start + channel] * fade_in)
                    samples[end + channel] = int(samples[end + channel] * fade_out)
            if sys.byteorder != "little":
                samples.byteswap()
            with wave.open(str(temp_path), "wb") as target:
                target.setnchannels(channels)
                target.setsampwidth(sample_width)
                target.setframerate(sample_rate)
                target.writeframes(samples.tobytes())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def cleanup_prepared_music(self, payload: dict[str, Any] | None) -> None:
        """Delete only temporary chunk files created for the live radio stream."""
        if not isinstance(payload, dict):
            return
        try:
            base = self.stream_cache_dir.resolve()
        except OSError:
            return
        for value in payload.get("_cleanup_files", []):
            try:
                path = Path(str(value)).resolve()
                if path.parent == base:
                    path.unlink(missing_ok=True)
            except Exception:
                continue
        try:
            self.stream_cache_dir.rmdir()
        except OSError:
            pass

    def start_prepared_music(self, payload: dict[str, Any]) -> str:
        """Start a previously decoded file through the independent game music source."""
        command_id = uuid.uuid4().hex
        command = dict(payload)
        command.pop("_cleanup_files", None)
        command.update({"id": command_id, "action": "play_music"})
        self._write_json(self.music_command_file, command)
        return command_id

    def queue_prepared_music(self, payload: dict[str, Any]) -> str:
        """Queue the next prepared radio segment; Lua starts it on the audio-finished frame."""
        command_id = uuid.uuid4().hex
        command = dict(payload)
        command.pop("_cleanup_files", None)
        command.update({"id": command_id, "action": "queue_music"})
        self._write_json(self.music_command_file, command)
        return command_id

    def start_music(self, source: str | os.PathLike[str], volume: float = 0.55) -> None:
        """Start independent, spatial music playback; it does not block announcement audio."""
        self.start_prepared_music(self.prepare_music(source, volume))

    def start_native_radio(self, station_index: int, volume: float = 0.55, station_name: str = "Интернет-радио") -> str:
        """Hand a station to openOMSI's native stream player for gapless spatial radio.

        Returns the command ID so the UI can discard delayed events from a prior source.
        """
        if not self.enabled:
            raise OpenOMSIBridgeError("openOMSI playback is disabled")
        if not self._started:
            self.start()
        if int(station_index) < 1:
            raise OpenOMSIBridgeError("native radio station index must be >= 1")
        command_id = uuid.uuid4().hex
        self._write_json(self.music_command_file, {
            "id": command_id, "action": "native_radio",
            "station_index": int(station_index),
            "volume": max(0.0, min(1.0, float(volume))),
            "track": str(station_name or "Интернет-радио"),
        })
        return command_id

    def stop_music(self) -> None:
        if not self.enabled:
            return
        try:
            self._write_json(self.music_command_file, {"id": uuid.uuid4().hex, "action": "stop_music"})
        except Exception as exc:
            print(f"[OpenOMSIBridge] stop music command failed: {exc}")

    def play_and_wait(
        self,
        source: str | os.PathLike[str],
        volume: float = 0.9,
        stop_flag: threading.Event | None = None,
        then_close_doors: bool = False,
    ) -> None:
        """Ask the plugin to play a source and block the caller until completion/stop."""
        if not self.enabled:
            raise OpenOMSIBridgeError("openOMSI playback is disabled")
        if not self._started:
            self.start()
        source_path = Path(source)
        with self._conversion_lock:
            plugin_wav, duration = self._prepare_wav(source_path)
            outside_wav = None
            try:
                outside_wav, outside_duration = self._prepare_exterior_wav(plugin_wav)
                if abs(outside_duration - duration) > 0.1:
                    outside_wav = None
            except Exception as exc:
                # Do not let a failed muffled variant break the main spatial sound.
                print(f"[OpenOMSIBridge] exterior audio variant skipped: {exc}")
        try:
            relative = plugin_wav.relative_to(self.plugin_dir).as_posix()
            outside_relative = outside_wav.relative_to(self.plugin_dir).as_posix() if outside_wav else ""
        except ValueError as exc:
            raise OpenOMSIBridgeError("converted WAV ended up outside the plugin folder") from exc

        command_id = uuid.uuid4().hex
        waiter = _Waiter()
        with self._state_lock:
            self._waiters[command_id] = waiter
            self._current_id = command_id
        command = {
            "id": command_id,
            "action": "play",
            "file": relative,
            "outside_file": outside_relative,
            "volume": max(0.0, min(1.0, float(volume))),
            "exterior_volume": self._clamp_float(self.config.get("exterior_volume", 0.25), 0.0, 1.0, 0.25),
            "exterior_range_factor": self._clamp_float(self.config.get("exterior_range_factor", 0.55), 0.20, 1.0, 0.55),
            "exterior_range": self._clamp_float(self.config.get("exterior_range", 45.0), 2.0, 100.0, 45.0),
            "source": str(self.config.get("source_mode", "ceiling")),
            "range": self._clamp_float(self.config.get("range", 16.0), 0.5, 100.0, 16.0),
            "forward": self._clamp_float(self.config.get("speaker_forward", -1.20), -8.0, 8.0, -1.20),
            "right": self._clamp_float(self.config.get("speaker_right", 0.0), -8.0, 8.0, 0.0),
            "up": self._clamp_float(self.config.get("speaker_up", 2.35), -2.0, 8.0, 2.35),
            "x": self.config.get("fixed_x", 0.0),
            "y": self.config.get("fixed_y", 0.0),
            "z": self.config.get("fixed_z", 0.0),
            "then_close_doors": bool(then_close_doors),
        }
        try:
            self._write_command(command)
            start_timeout = max(1.0, float(self.config.get("start_timeout", 8.0)))
            if not waiter.started.wait(start_timeout):
                if waiter.error:
                    raise OpenOMSIBridgeError(waiter.error)
                raise OpenOMSIBridgeError(
                    "openOMSI plugin did not acknowledge playback. Check that openOMSI is running, "
                    "the AutoInformant Lua plugin is loaded, and UDP port matches."
                )

            deadline = time.monotonic() + duration + max(
                5.0, float(self.config.get("playback_timeout_extra", 30.0))
            )
            while not waiter.finished.is_set():
                if stop_flag is not None and stop_flag.is_set():
                    self.stop()
                    return
                if time.monotonic() >= deadline:
                    self.stop()
                    raise OpenOMSIBridgeError(f"timed out waiting for playback to finish: {Path(source).name}")
                waiter.finished.wait(0.05)

            if waiter.error:
                raise OpenOMSIBridgeError(waiter.error)
        finally:
            with self._state_lock:
                self._waiters.pop(command_id, None)
                if self._current_id == command_id:
                    self._current_id = None

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoInformant Pro — standalone automatic stop announcer for openOMSI.

This app does not patch or import the legacy OMSI Auto-Informant application.
It follows the live duty/stops supplied by the openOMSI plugin API.
"""
from __future__ import annotations

import json
import os
import queue
import datetime as _datetime
import re
import tempfile
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, simpledialog
from typing import Any

from informant_core import (
    AUDIO_EXTS, AUX_ALIASES, AudioLibrary, estimated_time_to_next_stop,
    find_current_stop_index, is_announceable_stop as core_is_announceable_stop,
    normalize_stop_objects, normalize_text, route_identity, safe_float, folder_key,
    stop_key, announcement_schedule_allows,
)
from pack_system import PackManager
from openomsi_bridge import OpenOMSIBridge, OpenOMSIBridgeError
from yandex_music_service import (YandexMusicService, YandexMusicError, WindowsDpapiTokenStore,
                                  make_segment_marker, parse_segment_marker)

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "autoinformant_pro_settings.json"
PLUGIN_SOURCE = "plugin/AutoInformantPro/main.lua"
AUDIO_EXTENSIONS = tuple(sorted(AUDIO_EXTS))

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 12,
    "pack_enabled": {},
    "pack_priorities": {},
    "active_map_packs": {},
    "active_category_profiles": {},
    "audio_root": str(APP_DIR / "maps"),
    "plugin_dir": "",
    "udp_port": 47821,
    "auto_enabled": True,
    "auto_arrival": True,
    "auto_departure": True,
    "arrival_radius": 18.0,
    "max_arrival_speed": 2.5,
    "exit_speed": 4.0,
    "play_next_prefix": True,
    "play_terminus": True,
    "anons_enabled": True,
    "anons_mode": "stops",  # stops | minutes
    "anons_interval": 3,
    "average_speed_kmh": 25.0,
    "anons_safety_seconds": 5.0,
    "volume": 0.90,
    "informant_volume": 1.00,
    "speaker_effect": 45,
    "exterior_volume_pct": 25.0,
    "music_exterior_volume_pct": 30.0,
    "exterior_range": 45.0,
    "interior_range": 7.0,
    "source_mode": "ceiling",
    # Coordinates are relative to the vehicle origin (x east, y north, z up).
    # The announcer is a cabin PA/ceiling source; radio/music comes from the front radio speaker.
    "speaker_forward": -1.65,
    "speaker_right": 0.35,
    "speaker_up": 2.25,
    "auto_install_plugin": False,
    "overrides": {},
    # Optional direct audio bank per in-game map. Keys are map folder names;
    # values may point to any folder name (including a voice/vehicle variant).
    "map_audio_folders": {},
    "vehicle_audio_folders": {},
    "play_jingle": True,
    "announcement_schedules": {},
    "door_close_enabled_maps": {},
    "music_volume": 0.65,
    # Quick output faders on the Overview page. These are post-source gains
    # controlled live by the Lua bridge; advanced source gains above remain intact.
    "informant_output_level": 1.0,
    "informant_output_last_nonzero": 1.0,
    "music_output_level": 1.0,
    "music_output_last_nonzero": 1.0,
    # Independent music source: front radio/dashboard speaker, distinct from the ceiling PA.
    "music_source_mode": "ceiling",
    "music_range": 9.0,
    "music_speaker_forward": 2.10,
    "music_speaker_right": 0.70,
    "music_speaker_up": 1.25,
    "music_fixed_x": 0.0,
    "music_fixed_y": 0.0,
    "music_fixed_z": 0.0,
    "music_playlist": [],
    "radio_stations": [],
    "ffmpeg_path": "",
}

# ---------- Portable paths and settings ----------
def resource_path(relative: str) -> Path:
    candidates = [APP_DIR / relative]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / relative)
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def openomsi_radio_cfg_path() -> Path:
    """Official openOMSI global internet-radio list."""
    return Path.home() / ".openomsi" / "radio.cfg"


def parse_openomsi_radio_cfg(path: Path | None = None) -> list[dict[str, str]]:
    """Read openOMSI's documented `name = URL` station lines, ignoring volume/frequency rows."""
    path = path or openomsi_radio_cfg_path()
    stations: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return stations
    seen: set[str] = set()
    for line in lines:
        if "=" not in line:
            continue
        name, address = line.split("=", 1)
        name, address = name.strip(), address.strip()
        url = address.split("|", 1)[0].strip()
        if not name or name.casefold() == "volume" or not url.lower().startswith(("http://", "https://")):
            continue
        key = url.rstrip("/").casefold()
        if key in seen:
            continue
        seen.add(key)
        stations.append({"name": name, "url": url})
    return stations


def _normal_url(url: str) -> str:
    return str(url or "").strip().split("|", 1)[0].rstrip("/").casefold()


def sync_openomsi_radio_cfg(stations: list[dict[str, Any]], path: Path | None = None) -> tuple[bool, str]:
    """Update names for known URLs and append missing URLs without deleting user entries."""
    path = path or openomsi_radio_cfg_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing_text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            existing_text = ""
        lines = existing_text.splitlines()
        desired_by_url: dict[str, str] = {}
        for item in stations:
            url = str(item.get("url") or "").strip()
            name = str(item.get("name") or "Станция").strip()
            if url.lower().startswith(("http://", "https://")):
                desired_by_url[_normal_url(url)] = name
        known: set[str] = set()
        changed = False
        for i, line in enumerate(lines):
            if "=" not in line:
                continue
            old_name, raw_address = line.split("=", 1)
            address = raw_address.strip()
            key = _normal_url(address)
            if not key or not address.lower().startswith(("http://", "https://")):
                continue
            known.add(key)
            desired_name = desired_by_url.get(key)
            if desired_name and desired_name != old_name.strip():
                lines[i] = f"{desired_name} = {address}"
                changed = True
        additions: list[str] = []
        for item in stations:
            name = str(item.get("name") or "Станция").strip()
            url = str(item.get("url") or "").strip()
            key = _normal_url(url)
            if not key or not url.lower().startswith(("http://", "https://")) or key in known:
                continue
            additions.append(f"{name} = {url}")
            known.add(key)
        if additions:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(additions)
            changed = True
        if changed:
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return changed, str(path)
    except OSError as exc:
        return False, f"{path}: {exc}"


def _detect_installed_plugin_dir() -> str:
    """Find the existing Lua plugin after a damaged/missing settings file."""
    home = Path.home()
    candidates: list[Path] = [
        home / ".openomsi" / "plugins" / "AutoInformantPro",
        home / "Documents" / "openOMSI" / "plugins" / "AutoInformantPro",
        APP_DIR / "plugins" / "AutoInformantPro",
        APP_DIR / "AutoInformantPro",
    ]
    roots: list[Path] = []
    for env_key in ("APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "ProgramW6432"):
        base = os.environ.get(env_key)
        if base:
            root = Path(base)
            roots.append(root)
            for casing in ("openOMSI", "openomsi"):
                candidates.extend([
                    root / casing / "plugins" / "AutoInformantPro",
                    root / casing / "Plugins" / "AutoInformantPro",
                    root / "Steam" / "steamapps" / "common" / casing / "plugins" / "AutoInformantPro",
                ])
    # If the simulator is currently running, use its actual executable directory.
    try:
        import psutil
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if "openomsi" in str(proc.info.get("name") or "").casefold():
                    exe = Path(str(proc.info.get("exe") or ""))
                    if exe.is_file():
                        root = exe.parent
                        candidates.extend([root / "plugins" / "AutoInformantPro", root / "Plugins" / "AutoInformantPro", root / "AutoInformantPro"])
            except (psutil.Error, OSError, ValueError):
                continue
    except Exception:
        pass
    # Discover Steam libraries via libraryfolders.vdf (including secondary drives).
    steam_roots = set(roots)
    steam_roots.update({
        home / "AppData" / "Local" / "Steam",
        home / ".steam" / "steam" ,
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Steam",
    })
    for steam in steam_roots:
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw_path in re.findall(r'"path"\s+"([^"]+)"', text):
            library = Path(raw_path.replace("\\\\", "\\"))
            for casing in ("openOMSI", "openomsi"):
                candidates.append(library / "steamapps" / "common" / casing / "plugins" / "AutoInformantPro")
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            if (candidate / "main.lua").is_file():
                return str(candidate.resolve())
        except OSError:
            pass
    return ""


def load_settings() -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    raw: dict[str, Any] = {}
    try:
        if SETTINGS_PATH.is_file():
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
                out.update(raw)
    except Exception:
        pass

    # v1 shipped with TERMINUS disabled by default. That default was easy to miss,
    # especially while the v1 settings dialog failed to save. Migrate once to the
    # intended automatic behaviour; subsequent explicit user changes are respected.
    try:
        old_version = int(raw.get("version", 1))
    except (TypeError, ValueError):
        old_version = 1
    if old_version < 2:
        # v1 had the terminus option off by default; if settings were never saved,
        # migrate it to the expected real-world behaviour.
        if "play_terminus" not in raw or not bool(raw.get("play_terminus", False)):
            out["play_terminus"] = True
        if raw.get("speaker_forward") == 0.65:
            out["speaker_forward"] = DEFAULT_SETTINGS["speaker_forward"]
        if raw.get("speaker_right") == -0.35:
            out["speaker_right"] = DEFAULT_SETTINGS["speaker_right"]
        if raw.get("speaker_up") == 2.30:
            out["speaker_up"] = DEFAULT_SETTINGS["speaker_up"]
        if raw.get("exterior_range") == 24.0:
            out["exterior_range"] = DEFAULT_SETTINGS["exterior_range"]

    if old_version < 3:
        # v2's default virtual point sat close to the driver-camera origin, which
        # sounded almost centred. Shift only unchanged defaults; preserve calibration.
        if raw.get("speaker_forward") in (None, 1.45):
            out["speaker_forward"] = DEFAULT_SETTINGS["speaker_forward"]
        if raw.get("speaker_right") in (None, -0.70):
            out["speaker_right"] = DEFAULT_SETTINGS["speaker_right"]
        if raw.get("speaker_up") in (None, 1.90):
            out["speaker_up"] = DEFAULT_SETTINGS["speaker_up"]
        if raw.get("interior_range") in (None, 12.0):
            out["interior_range"] = DEFAULT_SETTINGS["interior_range"]
        if "map_audio_folders" not in raw:
            out["map_audio_folders"] = {}
        out["version"] = 3

    if old_version < 6:
        # v1.2 had the PA and radio defaults reversed, and a 0.8% exterior level
        # that made both sources effectively inaudible outside. Migrate only untouched defaults.
        if (raw.get("speaker_forward"), raw.get("speaker_right"), raw.get("speaker_up")) == (0.25, 1.10, 2.55):
            out["speaker_forward"], out["speaker_right"], out["speaker_up"] = -1.20, 0.00, 2.35
        if (raw.get("music_speaker_forward"), raw.get("music_speaker_right"), raw.get("music_speaker_up")) == (-1.20, 0.00, 2.35):
            out["music_speaker_forward"], out["music_speaker_right"], out["music_speaker_up"] = 1.10, 0.25, 1.55
        if raw.get("exterior_volume_pct") == 0.8:
            out["exterior_volume_pct"] = 8.0
        if "music_exterior_volume_pct" not in raw:
            out["music_exterior_volume_pct"] = 12.0
        out["version"] = 6

    if old_version < 7:
        # v1.2's tiny range and very low outside mix made the source hard to locate
        # and nearly inaudible outside. Update untouched defaults only; keep calibration.
        if raw.get("exterior_volume_pct") == 8.0 or (old_version < 6 and raw.get("exterior_volume_pct") == 0.8):
            out["exterior_volume_pct"] = 25.0
        if raw.get("music_exterior_volume_pct") == 12.0 or "music_exterior_volume_pct" not in raw:
            out["music_exterior_volume_pct"] = 30.0
        if raw.get("exterior_range") == 30.0:
            out["exterior_range"] = 45.0
        if raw.get("interior_range") == 5.0:
            out["interior_range"] = 16.0
        if raw.get("music_range") == 10.0:
            out["music_range"] = 16.0
        out["version"] = 8

    # v1.4.0 accidentally shipped a developer/test settings file with /mnt/data paths,
    # an empty plugin_dir, and an empty radio list. Repair those values on affected installs.
    audio_root = str(out.get("audio_root") or "").strip()
    try:
        audio_root_exists = bool(audio_root) and Path(audio_root).expanduser().is_dir()
    except (OSError, ValueError):
        audio_root_exists = False
    if not audio_root_exists or "/mnt/data/" in audio_root.replace("\\", "/").casefold():
        fallback_audio_root = APP_DIR / "maps"
        out["audio_root"] = str(fallback_audio_root if fallback_audio_root.is_dir() else APP_DIR)
    plugin_dir = str(out.get("plugin_dir") or "").strip()
    if not plugin_dir or not (Path(plugin_dir).expanduser() / "main.lua").is_file():
        out["plugin_dir"] = _detect_installed_plugin_dir()
    if not isinstance(out.get("radio_stations"), list):
        out["radio_stations"] = []
    if not out.get("radio_stations"):
        # Recover stations from openOMSI's own radio list if 1.4.0 erased the app list.
        out["radio_stations"] = parse_openomsi_radio_cfg()
    if not isinstance(out.get("overrides"), dict):
        out["overrides"] = {}
    if not isinstance(out.get("map_audio_folders"), dict):
        out["map_audio_folders"] = {}
    if not isinstance(out.get("vehicle_audio_folders"), dict):
        out["vehicle_audio_folders"] = {}
    if not isinstance(out.get("announcement_schedules"), dict):
        out["announcement_schedules"] = {}
    if not isinstance(out.get("door_close_enabled_maps"), dict):
        out["door_close_enabled_maps"] = {}
    if not isinstance(out.get("music_playlist"), list):
        out["music_playlist"] = []
    if not isinstance(out.get("radio_stations"), list):
        out["radio_stations"] = []
    # v1.8 adds a master gain plus dedicated source gains. Preserve the old music
    # level under the new master*source composition and only migrate untouched stock
    # speaker positions/ranges; hand-calibrated values are left alone.
    if old_version < 10:
        try:
            old_master = float(raw.get("volume", 0.90))
        except (TypeError, ValueError):
            old_master = 0.90
        try:
            old_music = float(raw.get("music_volume", 0.55))
        except (TypeError, ValueError):
            old_music = 0.55
        if old_master > 0.01 and "music_volume" in raw:
            out["music_volume"] = max(0.0, min(2.0, old_music / old_master))
        if (raw.get("speaker_forward"), raw.get("speaker_right"), raw.get("speaker_up")) in {
            (-1.20, 0.00, 2.35), (0.25, 1.10, 2.55), (0.65, -0.35, 2.30),
        }:
            out["speaker_forward"], out["speaker_right"], out["speaker_up"] = -1.65, 0.35, 2.25
        if (raw.get("music_speaker_forward"), raw.get("music_speaker_right"), raw.get("music_speaker_up")) in {
            (1.10, 0.25, 1.55), (-1.20, 0.0, 2.35),
        }:
            out["music_speaker_forward"], out["music_speaker_right"], out["music_speaker_up"] = 2.10, 0.70, 1.25
        if raw.get("interior_range") == 16.0 or (old_version < 8 and raw.get("interior_range") == 5.0):
            out["interior_range"] = 7.0
        if raw.get("music_range") == 16.0 or (old_version < 8 and raw.get("music_range") == 10.0):
            out["music_range"] = 9.0
    # v1.8 adds per-source gains. Existing values are preserved; a new install
    # gets a slightly healthier music starting level and a dedicated informant gain.
    try:
        out["informant_volume"] = max(0.0, min(2.0, float(out.get("informant_volume", 1.0))))
    except (TypeError, ValueError):
        out["informant_volume"] = 1.0
    try:
        out["music_volume"] = max(0.0, min(2.0, float(out.get("music_volume", 0.65))))
    except (TypeError, ValueError):
        out["music_volume"] = 0.65
    if not isinstance(out.get("pack_enabled"), dict):
        out["pack_enabled"] = {}
    if not isinstance(out.get("pack_priorities"), dict):
        out["pack_priorities"] = {}
    if not isinstance(out.get("active_map_packs"), dict):
        out["active_map_packs"] = {}
    if not isinstance(out.get("active_category_profiles"), dict):
        out["active_category_profiles"] = {}
    out.pop("mute_captured_app", None)
    out["version"] = 12
    if out.get("music_source_mode") not in {"ceiling", "bus", "fixed"}:
        out["music_source_mode"] = DEFAULT_SETTINGS["music_source_mode"]
    # Keep the port isolated from the previous patch-based integration.
    out["udp_port"] = 47821
    return out


def save_settings(settings: dict[str, Any]) -> None:
    # Preserve the pre-v1.9 settings once before the first schema-v12 write.
    # This is deliberately local to the user's installation and is never shipped
    # inside the release archive.
    if SETTINGS_PATH.is_file():
        try:
            current_version = int(settings.get("version", 1))
        except (TypeError, ValueError):
            current_version = 1
        if current_version >= 12:
            backup_path = SETTINGS_PATH.with_name("autoinformant_pro_settings.pre-v1.9.json")
            if not backup_path.exists():
                try:
                    previous = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                    previous_version = int(previous.get("version", 1)) if isinstance(previous, dict) else 1
                except Exception:
                    previous_version = 1
                if previous_version < 12:
                    try:
                        shutil.copy2(SETTINGS_PATH, backup_path)
                    except OSError:
                        # A backup failure must not prevent the user from saving
                        # current settings; atomic replacement below still applies.
                        pass
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


def should_preserve_terminal_phrase(engine, player, old_map_name: str, new_map_name: str,
                                    old_vehicle_key: str, new_vehicle_key: str,
                                    terminal_pending: bool = False) -> bool:
    """Keep a queued terminal phrase through duty rollovers on the same bus/map."""
    phrase_marked = bool(terminal_pending or (engine is not None and getattr(engine, "terminal_phrase_queued", False)))
    return bool(
        phrase_marked and player is not None and getattr(player, "is_busy", False)
        and old_map_name and folder_key(old_map_name) == folder_key(new_map_name)
        and old_vehicle_key and old_vehicle_key == new_vehicle_key
    )


def safe_path_name(name: str) -> str:
    return str(name or "").strip().replace("\x00", "")


# ---------- Worker that plays every item through openOMSI ----------
class PlaybackWorker:
    def __init__(self, get_bridge, get_volume, log):
        self.get_bridge = get_bridge
        self.get_volume = get_volume
        self.log = log
        self._queue: queue.Queue[tuple[Path, str, threading.Event, float, bool] | None] = queue.Queue()
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name="AutoInformant-audio", daemon=True)
        self._thread.start()

    @property
    def is_busy(self) -> bool:
        return bool(self._queue.unfinished_tasks)

    def enqueue(self, path: Path, label: str, volume: float | None = None, *, then_close_doors: bool = False) -> None:
        if not path or not path.is_file():
            return
        with self._lock:
            if self._cancel.is_set():
                self._cancel = threading.Event()
            token = self._cancel
        v = max(0.0, min(1.0, float(self.get_volume() if volume is None else volume)))
        self._queue.put((Path(path), label, token, v, bool(then_close_doors)))

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._queue.task_done()
            except Exception:
                pass

    def stop(self, clear_queue: bool = True) -> None:
        with self._lock:
            self._cancel.set()
        bridge = self.get_bridge()
        if bridge:
            try:
                bridge.stop()
            except Exception:
                pass
        if clear_queue:
            self._drain()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            path, label, token, volume, then_close_doors = item
            try:
                if token.is_set():
                    continue
                bridge = self.get_bridge()
                if not bridge:
                    self.log("Звук не запущен: сначала установи плагин openOMSI в настройках.")
                    continue
                bridge.play_and_wait(path, volume=volume, stop_flag=token, then_close_doors=then_close_doors)
            except OpenOMSIBridgeError as exc:
                self.log(f"Ошибка игрового звука «{path.name}»: {exc}")
            except Exception as exc:
                self.log(f"Ошибка воспроизведения: {exc}")
                self.log(traceback.format_exc())
            finally:
                self._queue.task_done()

    def close(self) -> None:
        self.stop(clear_queue=True)
        self._queue.put(None)


# ---------- Automatic announcement state machine ----------
class AutoAnnouncementEngine:
    """GPS-like arrival/departure detection + ad timing; no route.txt dependency."""
    def __init__(self, app: "AutoInformantApp"):
        self.app = app
        self.route_key = ""
        self.was_in_zone = False
        self.zone_index: int | None = None
        self.arrival_key: str | None = None
        self.last_arrival_index: int | None = None
        self.departures_since_anon = 0
        self.last_anon_clock: float | None = None
        self.anon_history: list[str] = []
        self._last_missing: set[str] = set()
        self._last_anon_log = ""
        self.last_seen_stop_index: int | None = None
        self.last_departure_to_index: int | None = None
        # Guard against stale at_stop flags while crawling in traffic after leaving a stop.
        self.last_departed_from_index: int | None = None
        self._departure_pairs: set[tuple[int, int]] = set()
        # A target stop gets its NEXT announcement at most once per duty. This guards
        # against transient at_stop/index glitches while braking in traffic.
        self._announced_next_indices: set[int] = set()
        self._low_speed_since: float | None = None
        self._low_speed_index: int | None = None
        self._api_at_stop_since: float | None = None
        self._api_at_stop_index: int | None = None
        self._needs_at_stop_clear = False
        self.terminal_phrase_queued = False

    def reset_route(self, key: str, state: dict[str, Any]) -> None:
        self.route_key = key
        self.was_in_zone = False
        self.zone_index = None
        self.arrival_key = None
        self.last_arrival_index = None
        self.departures_since_anon = 0
        clock = safe_float(state.get("game_clock"))
        self.last_anon_clock = clock
        self._last_missing.clear()
        self.last_seen_stop_index = None
        self.last_departure_to_index = None
        self.last_departed_from_index = None
        self._departure_pairs.clear()
        self._announced_next_indices.clear()
        self._low_speed_since = None
        self._low_speed_index = None
        self._api_at_stop_since = None
        self._api_at_stop_index = None
        self._needs_at_stop_clear = False
        self.terminal_phrase_queued = False

    def _setting(self, key: str, default: Any = None) -> Any:
        resolver = getattr(self.app, "profile_setting", None)
        if callable(resolver):
            try:
                return resolver(key, default)
            except Exception:
                pass
        return self.app.settings.get(key, default)

    def _announceable(self, stop: dict[str, Any], index: int, map_name: str) -> bool:
        resolver = getattr(self.app, "is_announceable_stop", None)
        if callable(resolver):
            try:
                return bool(resolver(stop, index, map_name))
            except Exception:
                pass
        return core_is_announceable_stop(stop)

    def _schedules(self) -> dict[str, dict[str, Any]]:
        resolver = getattr(self.app, "current_announcement_schedules", None)
        if callable(resolver):
            try:
                return resolver()
            except Exception:
                pass
        return self.app.settings.get("announcement_schedules", {})

    @staticmethod
    def _final_announceable_index(stops: list[dict[str, Any]], state: dict[str, Any], map_name: str = "",
                                  announceable=None) -> int | None:
        """Return the final passenger stop, excluding pass-through/ignored points."""
        if callable(announceable):
            eligible = [i for i, stop in enumerate(stops) if announceable(stop, i, map_name)]
        else:
            eligible = [i for i, stop in enumerate(stops) if core_is_announceable_stop(stop)]
        if not eligible:
            return None
        destination = normalize_text(state.get("terminus") or state.get("destination"))
        if destination:
            matches = [i for i in eligible if normalize_text(stops[i].get("name")) == destination]
            if matches:
                return matches[-1]
        return eligible[-1]

    def _queue_terminus(self, state: dict[str, Any], stops: list[dict[str, Any]],
                        map_name: str, index: int, *, context: str) -> None:
        if not self._setting("play_terminus", True):
            return
        if index != self._final_announceable_index(stops, state, map_name, announceable=self._announceable):
            return
        aux = self.app.library.aux_file(map_name, state, AUX_ALIASES["terminus"])
        if aux:
            self.app.enqueue_audio(aux, "Конечная")
            self.app.log(f"TERMINUS добавлен в очередь после названия остановки ({context}): {aux.name}")
        else:
            key = f"aux:terminus:{map_name}:{state.get('line')}:{state.get('tour')}"
            if key not in self._last_missing:
                self._last_missing.add(key)
                active_folder = self.app.library._map_dir(map_name)
                self.app.log(f"Не найден TERMINUS для карты «{map_name}». Проверь выбранную аудиопапку: {active_folder}")

    def _queue_jingle(self, state: dict[str, Any], map_name: str, phrase_label: str) -> None:
        if not self._setting("play_jingle", True):
            return
        cue = self.app.library.jingle_file(map_name, state)
        if cue is not None:
            self.app.enqueue_audio(cue, f"JINGLE перед фразой: {phrase_label}")

    def update(self, state: dict[str, Any], stops: list[dict[str, Any]], map_name: str) -> None:
        if not self.app.settings.get("auto_enabled", True) or not stops:
            return
        current_index = find_current_stop_index(state, stops)
        if current_index is None:
            return
        current_stop = stops[current_index]
        raw_dist = safe_float(state.get("next_stop_distance"))
        speed = abs(safe_float(state.get("speed")) or 0.0)
        radius = float(self._setting("arrival_radius", 18.0))
        max_speed = float(self._setting("max_arrival_speed", 2.5))
        exit_speed = float(self._setting("exit_speed", 4.0))
        api_at_stop = state.get("at_stop") is True
        if self._needs_at_stop_clear and not api_at_stop:
            self._needs_at_stop_clear = False
        # openOMSI can keep at_stop=true for a short time after the bus has departed.
        # Latch the stop we just left until the duty advances; otherwise braking in a
        # traffic queue can re-enter the same stop zone and replay NEXT over and over.
        stale_departure_state = self.last_departed_from_index == current_index
        if self.last_departed_from_index is not None and current_index != self.last_departed_from_index:
            self.last_departed_from_index = None

        # Distance/speed alone are only a fallback when the API does not assert at_stop.
        # Require the bus to be almost stopped, very close to the stop, and stable for a
        # few seconds. A brief brake or creeping traffic must not count as an arrival.
        fallback_close = raw_dist is not None and raw_dist <= min(radius, 6.0)
        fallback_slow = speed <= min(max_speed, 1.0)
        if current_index != self._low_speed_index:
            self._low_speed_index = current_index
            self._low_speed_since = None
        if fallback_close and fallback_slow and not stale_departure_state:
            if self._low_speed_since is None:
                self._low_speed_since = time.monotonic()
        else:
            self._low_speed_since = None
        fallback_dwell = (self._low_speed_since is not None
                          and time.monotonic() - self._low_speed_since >= 3.0)
        approximate_zone = fallback_dwell and not api_at_stop

        # Treat at_stop as a signal that must persist briefly, and only close to the
        # upcoming stop. Some buses leave at_stop latched while braking in a queue; a
        # single low-speed telemetry frame must not create a phantom arrival/departure.
        api_close = raw_dist is None or raw_dist <= min(radius, 8.0)
        api_candidate = (api_at_stop and speed < exit_speed and api_close
                         and not stale_departure_state and not self._needs_at_stop_clear)
        if current_index != self._api_at_stop_index:
            self._api_at_stop_index = current_index
            self._api_at_stop_since = None
        if api_candidate:
            if self._api_at_stop_since is None:
                self._api_at_stop_since = time.monotonic()
        else:
            self._api_at_stop_since = None
        api_dwell = (self._api_at_stop_since is not None
                     and time.monotonic() - self._api_at_stop_since >= 0.65)
        # On app startup, announce immediately if the bus is already at a confirmed
        # stop; subsequent arrivals must pass the debounce above.
        initial_at_stop = self.last_arrival_index is None and api_candidate
        enter_zone = initial_at_stop or api_dwell or approximate_zone

        # If the duty's next-stop index jumps over a stop while moving quickly, the usual
        # arrival/departure zone never triggers. Catch up once: announce the passed stop,
        # then the actual next stop. A stop already announced on normal departure is skipped.
        previous_seen = self.last_seen_stop_index
        if (previous_seen is not None and current_index > previous_seen and not self.was_in_zone
                and self.last_departure_to_index != current_index):
            missed_candidates = [i for i in range(previous_seen, min(current_index, len(stops)))
                                 if self._announceable(stops[i], i, map_name)]
            if missed_candidates:
                missed_index = missed_candidates[-1]
                self._arrival(state, stops, map_name, missed_index)
                self._departure(state, stops, map_name, missed_index, current_index)
                self.last_departure_to_index = current_index

        if enter_zone:
            # A skipped/teleported duty index can leave the previous stop without a sampled
            # outside frame; still run the departure transition exactly once.
            if self.was_in_zone and self.zone_index is not None and current_index != self.zone_index:
                self._departure(state, stops, map_name, self.zone_index, current_index)
                self.was_in_zone = False
            if not self.was_in_zone or self.zone_index != current_index:
                self.zone_index = current_index
                self.was_in_zone = True
                self._arrival(state, stops, map_name, current_index)
            self.last_seen_stop_index = current_index
            return

        if self.was_in_zone:
            # Hysteresis prevents noisy distance values from firing arrival/departure repeatedly.
            beyond_exit_radius = raw_dist is not None and raw_dist > radius + 9.0
            actually_moving = speed >= exit_speed
            if actually_moving or beyond_exit_radius:
                previous = self.zone_index
                self.was_in_zone = False
                if previous is not None:
                    target = current_index if current_index > previous else previous + 1
                    self._departure(state, stops, map_name, previous, target)
        self.last_seen_stop_index = current_index

    def _arrival(self, state: dict[str, Any], stops: list[dict[str, Any]], map_name: str, index: int) -> None:
        stop = stops[index]
        key = stop_key(map_name, stop, index)
        self.last_arrival_index = index
        if self.arrival_key == key:
            return
        self.arrival_key = key
        if not self._announceable(stop, index, map_name):
            self.app.log(f"GPS: точка «{stop['name']}» помечена как игнорируемая/проездная — без объявления.")
            return

        stop_audio_queued = False
        if self._setting("auto_arrival", True):
            path = self.app.resolve_stop_audio(stop, index)
            if path:
                self._queue_jingle(state, map_name, f"Прибытие: {stop['name']}")
                self.app.enqueue_audio(path, f"Прибытие: {stop['name']}")
                stop_audio_queued = True
                self.app.log(f"Прибытие: {stop['name']}")
            else:
                self._log_missing(stop, key)

        # Terminal cue follows the actual stop-name clip at arrival, not before it.
        # If the stop-name audio is missing, stay silent rather than playing an orphan cue.
        if stop_audio_queued:
            if index == self._final_announceable_index(stops, state, map_name, announceable=self._announceable):
                self.terminal_phrase_queued = True
            self._queue_terminus(state, stops, map_name, index, context="прибытие")

    def _departure(self, state: dict[str, Any], stops: list[dict[str, Any]], map_name: str,
                   from_index: int, target_index: int) -> None:
        if not stops:
            return
        target_index = max(0, target_index)
        while target_index < len(stops) and not self._announceable(stops[target_index], target_index, map_name):
            target_index += 1
        target = stops[target_index] if target_index < len(stops) else None
        if target is not None:
            self.last_departure_to_index = target_index
            self.last_departed_from_index = from_index
            self._needs_at_stop_clear = True
            self._api_at_stop_since = None
            self._low_speed_since = None
            pair = (int(from_index), int(target_index))
            # Index-based deduplication is stronger than a (from, to) pair: stale
            # telemetry can briefly invent a false arrival at an intermediate stop,
            # then try to announce the same target again from a different origin.
            if pair in self._departure_pairs or target_index in self._announced_next_indices:
                self.app.log(f"NEXT для остановки «{target.get('name', target_index + 1)}» уже объявлялся; повтор пропущен.")
                return
            self._departure_pairs.add(pair)
            self._announced_next_indices.add(target_index)
            # Keep only recent transitions, not an unbounded route-long history.
            if len(self._departure_pairs) > 64:
                self._departure_pairs = set(sorted(self._departure_pairs)[-32:])
            if len(self._announced_next_indices) > 128:
                # A single duty is unlikely to contain 128 stops; retain the most recent
                # indices if an unusually long looping duty is reported by the game.
                self._announced_next_indices = set(sorted(self._announced_next_indices)[-64:])
        speech_duration = 0.0
        if target is not None and self._setting("auto_departure", True):
            stop_audio = self.app.resolve_stop_audio(target, target_index)
            # Never play just a generic NEXT clip when the actual stop audio is missing.
            if stop_audio:
                self._queue_jingle(state, map_name, f"Следующая остановка: {target['name']}")
                if self._setting("play_next_prefix", True):
                    prefix = self.app.library.aux_file(map_name, state, AUX_ALIASES["next"])
                    if prefix:
                        self.app.enqueue_audio(prefix, "Следующая остановка — сигнал")
                        speech_duration += self.app.library.duration(prefix) or 0.0
                self.app.enqueue_audio(stop_audio, f"Следующая остановка: {target['name']}")
                speech_duration += self.app.library.duration(stop_audio) or 0.0
                self.app.log(f"Следующая остановка: {target['name']}")
                # As in the legacy informant: on departure, say NEXT + final stop name
                # + TERMINUS. It is intentionally also played once more upon arrival.
                if target_index == self._final_announceable_index(stops, state, map_name, announceable=self._announceable) and self._setting("play_terminus", True):
                    term = self.app.library.aux_file(map_name, state, AUX_ALIASES["terminus"])
                    if term:
                        self.app.enqueue_audio(term, "Конечная (следующая остановка)")
                        speech_duration += self.app.library.duration(term) or 0.0
                        self.app.log(f"TERMINUS добавлен после названия конечной: {term.name}")
                        self.terminal_phrase_queued = True
                    else:
                        self._queue_terminus(state, stops, map_name, target_index, context="отъезд")
            else:
                key = stop_key(map_name, target, target_index)
                self._log_missing(target, key)

        if 0 <= from_index < len(stops) and self._announceable(stops[from_index], from_index, map_name):
            self.departures_since_anon += 1
        if 0 <= from_index < len(stops) and self._announceable(stops[from_index], from_index, map_name) and self._is_anon_due(state):
            eta = estimated_time_to_next_stop(
                state, stops, target_index,
                average_speed_kmh=float(self._setting("average_speed_kmh", 25.0)),
            )
            available = max(0.0, (eta or 0.0) - speech_duration)
            files = self.app.library.announcement_files(map_name, state)
            schedules = self._schedules()
            files = [p for p in files if announcement_schedule_allows(p, state, schedules)]
            chosen = self.app.library.choose_announcement(
                files, available,
                self.anon_history,
                margin_seconds=float(self._setting("anons_safety_seconds", 5.0)),
            ) if self._setting("anons_enabled", True) else None
            if chosen:
                self._queue_jingle(state, map_name, f"Анонс: {chosen.stem}")
                self.app.enqueue_audio(chosen, f"Анонс: {chosen.stem}")
                self.anon_history.append(str(chosen.resolve()))
                if len(self.anon_history) > 200:
                    self.anon_history = self.anon_history[-100:]
                clock = safe_float(state.get("game_clock"))
                if clock is not None:
                    self.last_anon_clock = clock
                self.departures_since_anon = 0
                self.app.log(f"Анонс поставлен в очередь ({self.app.library.duration(chosen):.1f} с; до остановки ~{eta:.0f} с): {chosen.name}")
            elif self._setting("anons_enabled", True):
                msg = "Анонс пропущен: между остановками нет подходящего по длительности файла или ETA неизвестно."
                if msg != self._last_anon_log:
                    self.app.log(msg)
                    self._last_anon_log = msg

    def _is_anon_due(self, state: dict[str, Any]) -> bool:
        if not self._setting("anons_enabled", True):
            return False
        interval = max(1, int(self._setting("anons_interval", 3)))
        mode = self._setting("anons_mode", "stops")
        if mode == "minutes":
            now = safe_float(state.get("game_clock"))
            if now is None:
                return False
            if self.last_anon_clock is None:
                self.last_anon_clock = now
                return False
            elapsed = (now - self.last_anon_clock) % 86400.0
            return elapsed >= interval * 60.0
        return self.departures_since_anon >= interval

    def _log_missing(self, stop: dict[str, Any], key: str) -> None:
        if key not in self._last_missing:
            self._last_missing.add(key)
            self.app.log(f"Нет аудиофайла для «{stop['name']}» — пропускаю объявление без звука.")


# ---------- GUI ----------
class ModernSwitch(tk.Frame):
    """Small flat pill switch used instead of native Windows checkbox widgets."""

    def __init__(self, master, text: str, variable: tk.BooleanVar, command=None, *,
                 background="#202328", foreground="#f3f4f6", accent="#f0a629",
                 muted="#454b54", font=("Segoe UI", 9), padx=0, pady=0):
        super().__init__(master, bg=background, bd=0, highlightthickness=0,
                         padx=padx, pady=pady)
        self.variable = variable
        self.command = command
        self.background = background
        self.foreground = foreground
        self.accent = accent
        self.muted = muted
        self.track = tk.Canvas(self, width=42, height=24, bg=background,
                               bd=0, highlightthickness=0, cursor="hand2")
        self.track.pack(side="left", padx=(0, 9))
        self.label = tk.Label(self, text=text, bg=background, fg=foreground,
                              font=font, anchor="w", justify="left")
        self.label.pack(side="left", fill="x", expand=True)
        for widget in (self, self.track, self.label):
            widget.bind("<Button-1>", self._toggle)
            widget.bind("<space>", self._toggle)
            widget.configure(cursor="hand2")
        self.variable.trace_add("write", self._render)
        self._render()

    def _toggle(self, _event=None):
        self.variable.set(not bool(self.variable.get()))
        if callable(self.command):
            self.command()
        return "break"

    def _render(self, *_args):
        self.track.delete("all")
        enabled = bool(self.variable.get())
        track_color = self.accent if enabled else self.muted
        x0, y0, x1, y1 = 2, 3, 40, 21
        self.track.create_oval(x0, y0, x0 + 18, y1, fill=track_color, outline=track_color)
        self.track.create_rectangle(x0 + 9, y0, x1 - 9, y1, fill=track_color, outline=track_color)
        self.track.create_oval(x1 - 18, y0, x1, y1, fill=track_color, outline=track_color)
        thumb_x = 25 if enabled else 5
        self.track.create_oval(thumb_x, 5, thumb_x + 14, 19,
                               fill="#ffffff", outline="#ffffff")


class AutoInformantApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.pack_manager = PackManager(APP_DIR, self.settings, on_change=self._on_pack_change)
        try:
            _changed, _radio_cfg_status = sync_openomsi_radio_cfg(self.settings.get("radio_stations", []))
        except Exception:
            pass
        self.title("AutoInformant Pro for openOMSI")
        self.geometry("1120x760")
        self.minsize(980, 650)
        self.configure(bg="#101117")
        self._set_icon()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.report_callback_exception = self._tk_error

        self.current_state: dict[str, Any] = {}
        self.stops: list[dict[str, Any]] = []
        self.map_name = ""
        self.current_route_key = ""
        self.current_stop_index: int | None = None
        self._tree_item_by_index: dict[int, str] = {}
        self._last_scrolled_stop_index: int | None = None
        self._refresh_after_id = None
        self._last_state_signature = ""
        self._status_message = "Плагин не настроен — открой настройки и установи его в openOMSI."
        self._bridge: OpenOMSIBridge | None = None
        self._bridge_lock = threading.RLock()
        self._suppress_settings_update = False
        self._last_vehicle_bank_key = ""
        self._terminal_phrase_handover_active = False
        self.music_playlist: list[Path] = [Path(p) for p in self.settings.get("music_playlist", []) if Path(str(p)).is_file()]
        self.music_index = -1
        self.music_mode = "stopped"  # stopped | local | radio | yandex
        self.music_active = False
        self._music_finished_event = threading.Event()
        self._music_started_event = threading.Event()
        self._radio_stop_event = threading.Event()
        self._radio_stream_proc: subprocess.Popen | None = None
        self._radio_stream_dir: Path | None = None
        self._radio_stream_thread: threading.Thread | None = None
        self._music_window = None
        self._music_local_tree = None
        self._music_radio_tree = None
        self.radio_station_index = -1
        self._active_native_radio_id: str | None = None
        self._music_worker_lock = threading.RLock()
        self._music_source_generation = 0
        self._radio_stream_generation = 0
        self._radio_retry_count = 0
        self._quick_volume_after: dict[str, str | None] = {"informant": None, "music": None}
        self._quick_volume_pending: dict[str, float] = {}
        self._quick_volume_flush_id: str | None = None
        self._quick_volume_save_after: str | None = None
        self._quick_volume_vars: dict[str, tk.DoubleVar] = {}
        self._quick_volume_canvases: dict[str, tk.Canvas] = {}
        self._quick_volume_buttons: dict[str, ttk.Button] = {}
        self._quick_volume_values: dict[str, ttk.Label] = {}
        self._plugin_live_volume_supported = False
        self._plugin_version_notice = None
        # Yandex Music is an independent source feeding the existing music bridge.
        self.yandex_service = YandexMusicService()
        self.yandex_profile: dict[str, Any] | None = None
        self.yandex_results: list[dict[str, Any]] = []
        self.yandex_queue: list[dict[str, Any]] = []
        self.yandex_queue_index = -1
        self.yandex_queue_source = "manual"  # manual | wave | favorite_wave
        self.yandex_current_track: dict[str, Any] | None = None
        self.yandex_current_source: str | None = None
        self.yandex_track_started_at = 0.0
        self.yandex_wave_mode = "discover"
        self.yandex_auto_next = True
        self._yandex_wave_generation = 0
        self._yandex_wave_prefetch_lock = threading.RLock()
        self._yandex_wave_prefetch_event = threading.Event()
        self._yandex_wave_prefetch_event.set()
        self._yandex_wave_prefetch_started = False
        self._yandex_wave_prefetch_rows: list[dict[str, Any]] | None = None
        self._yandex_wave_prefetch_error: str | None = None
        self._yandex_final_segment_index: int | None = None
        self._yandex_final_segment_generation = 0
        self._yandex_song_finished_event = threading.Event()
        self._yandex_login_cancel_event: threading.Event | None = None
        self._yandex_login_dialog = None
        self._yandex_code_url = ""
        self._yandex_code_value = ""
        self._yandex_restore_attempted = False
        self._yandex_tree = None
        self._yandex_queue_button = None
        self._yandex_status_label = None
        self._yandex_now_label = None
        self._yandex_profile_label = None
        self._yandex_search_var = None
        self._yandex_search_kind = None
        self._yandex_result_title = None
        self._yandex_progress = None
        self._yandex_time_label = None
        self._yandex_progress_after = None
        self._yandex_profile_busy = False
        self._yandex_tab_ready = False

        self.library = AudioLibrary(
            self.settings.get("audio_root") or APP_DIR,
            self.settings["overrides"],
            self.settings.get("map_audio_folders", {}),
            self.settings.get("vehicle_audio_folders", {}),
        )
        self.library.set_pack_manager(self.pack_manager)
        self._active_page = "home"
        self._settings_wheel_bindings = []
        self._build_ui()
        self.engine = AutoAnnouncementEngine(self)
        self.player = PlaybackWorker(lambda: self._bridge, lambda: float(self.settings.get("volume", 0.9)) * min(1.0, max(0.0, float(self.settings.get("informant_volume", 1.0)))), self.log)
        self._try_connect_bridge()
        self._schedule_heartbeat()

    def _on_pack_change(self):
        """Refresh only package-backed lookup state; playback and the Lua bridge stay untouched."""
        try:
            save_settings(self.settings)
        except Exception:
            pass
        library = getattr(self, "library", None)
        if library is not None:
            library._index_cache.clear()
            library._duration_cache.clear()
        tree = getattr(self, "tree", None)
        if tree is not None:
            try:
                self._rebuild_stop_rows()
            except Exception:
                pass

    def _current_pack_profiles(self):
        return self.pack_manager.effective_profiles(self.map_name or "", self.current_state)

    def _set_icon(self):
        ico = APP_DIR / "logo.ico"
        png = APP_DIR / "logo.png"
        if not ico.exists():
            ico = resource_path("logo.ico")
        if not png.exists():
            png = resource_path("logo.png")
        try:
            if ico.is_file() and os.name == "nt":
                self.iconbitmap(str(ico))
            elif png.is_file():
                img = tk.PhotoImage(file=str(png))
                self.iconphoto(True, img)
                self._icon_image = img
            else:
                self.iconname("AutoInformant Pro")
        except Exception:
            pass

    def _place_dialog(self, win, parent=None, *, modal=False):
        """Place owned dialogs predictably and keep them above their owner."""
        owner = parent or self
        try:
            win.transient(owner)
            win.update_idletasks()
            width = max(320, win.winfo_width())
            height = max(220, win.winfo_height())
            # Centre over the owner, then clamp to the usable screen bounds.
            x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
            y = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x = max(0, min(x, sw - width))
            y = max(0, min(y, sh - height))
            win.geometry(f"{width}x{height}+{x}+{y}")
            win.lift(owner)
            if modal:
                win.grab_set()
            win.focus_set()
        except tk.TclError:
            pass

    def _build_ui(self):
        """Single-window navigation shell, inspired by the openOMSI launcher."""
        try:
            import ttkbootstrap as tb
            # Keep an explicit reference: ttkbootstrap owns the active Tcl theme.
            # A discarded Style instance can otherwise be replaced by a later
            # default ttk.Style() in some frozen/embedded launch environments.
            self._theme_style = tb.Style(theme="darkly")
            style = self._theme_style
        except Exception:
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass
        self._style = style
        # Flat, modern charcoal theme.  Use the same palette whether darkly or the
        # bundled ttk "clam" fallback is active, so missing optional themes never
        # fall back to the old Windows bevel/border look.
        bg, sidebar_bg, panel, panel_raised, field, fg, dim, accent = (
            "#111315", "#191b1f", "#202328", "#272b31", "#2c3037",
            "#f3f4f6", "#9da3ad", "#f0a629",
        )
        self._ui_colors = {
            "bg": bg, "sidebar": sidebar_bg, "panel": panel,
            "panel_raised": panel_raised, "field": field,
            "fg": fg, "dim": dim, "accent": accent,
            "border": "#30353d", "success": "#65c18c", "danger": "#e06a70",
        }
        try:
            self.configure(bg=bg)
            self.option_add("*Font", ("Segoe UI", 10))
            self.option_add("*TCombobox*Listbox.background", field)
            self.option_add("*TCombobox*Listbox.foreground", fg)
            self.option_add("*TCombobox*Listbox.selectBackground", "#574321")
            self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        except tk.TclError:
            pass

        # ttkbootstrap/darkly is used when available; clam gets a complete flat
        # restyle below and deliberately avoids classic raised widget borders.
        style.configure("TFrame", background=bg)
        style.configure("Sidebar.TFrame", background=sidebar_bg)
        style.configure("Content.TFrame", background=bg)
        style.configure("Card.TFrame", background=panel)
        style.configure("CardRaised.TFrame", background=panel_raised)
        style.configure("Hero.TFrame", background=panel)
        style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=bg, foreground=dim, font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=panel, foreground=fg, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=panel, foreground=fg, font=("Segoe UI", 12, "bold"))
        style.configure("CardMuted.TLabel", background=panel, foreground=dim, font=("Segoe UI", 9))
        style.configure("HeroTitle.TLabel", background=panel, foreground=fg, font=("Segoe UI", 21, "bold"))
        style.configure("HeroSubtitle.TLabel", background=panel, foreground=dim, font=("Segoe UI", 9))
        style.configure("PageTitle.TLabel", background=bg, foreground=fg, font=("Segoe UI", 21, "bold"))
        style.configure("Section.TLabel", background=panel, foreground=accent, font=("Segoe UI", 9, "bold"))
        style.configure("Status.TLabel", background=panel_raised, foreground="#efbd5b", font=("Segoe UI Semibold", 9), padding=(9, 5))

        style.configure("TButton", font=("Segoe UI Semibold", 9), padding=(13, 8),
                        borderwidth=0, relief="flat", background=panel_raised,
                        foreground=fg, focusthickness=0, focuscolor=panel_raised)
        style.map("TButton",
                  background=[("disabled", panel), ("pressed", "#3b4048"), ("active", "#343941")],
                  foreground=[("disabled", "#68707b"), ("active", "#ffffff")],
                  relief=[("pressed", "flat"), ("!pressed", "flat")])
        try:
            style.layout("TButton", [("Button.padding", {"sticky": "nswe", "children": [
                ("Button.label", {"sticky": "nswe"})]})])
        except tk.TclError:
            pass
        style.configure("Nav.TButton", background=sidebar_bg, foreground="#b9bec7",
                        anchor="w", padding=(15, 12), font=("Segoe UI", 10, "bold"),
                        borderwidth=0, relief="flat")
        style.map("Nav.TButton", background=[("active", "#272b31"), ("pressed", "#2f333a")],
                  foreground=[("active", "#ffffff")])
        style.configure("NavActive.TButton", background="#30343b", foreground=accent,
                        anchor="w", padding=(15, 12), font=("Segoe UI", 10, "bold"),
                        borderwidth=0, relief="flat")
        style.map("NavActive.TButton", background=[("active", "#383d45")], foreground=[("active", accent)])
        style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg,
                        padding=(10, 8), borderwidth=0, relief="flat")
        style.map("TEntry", fieldbackground=[("focus", "#343941")],
                  bordercolor=[("focus", accent)])
        style.configure("TCombobox", fieldbackground=field, background=field, foreground=fg,
                        arrowcolor=dim, padding=(9, 7), borderwidth=0, relief="flat")
        style.map("TCombobox", fieldbackground=[("readonly", field), ("focus", "#343941")],
                  foreground=[("readonly", fg)], selectbackground=[("readonly", field)],
                  selectforeground=[("readonly", fg)])
        style.configure("TSpinbox", fieldbackground=field, background=field, foreground=fg,
                        padding=(8, 6), borderwidth=0, relief="flat", arrowsize=12)
        style.configure("Accent.TButton", background=accent, foreground="#20180a",
                        padding=(14, 9), font=("Segoe UI", 9, "bold"), borderwidth=0, relief="flat")
        style.map("Accent.TButton", background=[("pressed", "#c9871f"), ("active", "#ffc04f")],
                  foreground=[("active", "#20180a")])
        style.configure("Danger.TButton", background="#48282c", foreground="#ffdddd",
                        padding=(12, 8), borderwidth=0, relief="flat")
        style.map("Danger.TButton", background=[("pressed", "#68333a"), ("active", "#5b3037")])
        style.configure("VolumeIcon.TButton", background="#343941", foreground=accent,
                        padding=(8, 6), font=("Segoe UI Symbol", 14, "bold"), borderwidth=0, relief="flat")
        style.map("VolumeIcon.TButton", background=[("active", "#444b55"), ("pressed", "#4b535e")],
                  foreground=[("active", "#ffd27a")])

        # Custom flat tabs are used instead of ttk.Notebook: several Windows themes
        # draw a bright native border around Notebook.client even with borderwidth=0.
        style.configure("MusicTab.TButton", background=panel_raised, foreground=dim,
                        padding=(16, 10), font=("Segoe UI Semibold", 9), borderwidth=0, relief="flat")
        style.map("MusicTab.TButton", background=[("active", "#343941")],
                  foreground=[("active", fg)])
        style.configure("MusicTabActive.TButton", background=accent, foreground="#20180a",
                        padding=(16, 10), font=("Segoe UI Semibold", 9, "bold"), borderwidth=0, relief="flat")
        style.map("MusicTabActive.TButton", background=[("active", "#ffc04f")],
                  foreground=[("active", "#20180a")])
        style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg,
                        rowheight=36, borderwidth=0, relief="flat", font=("Segoe UI", 9),
                        bordercolor=panel, lightcolor=panel, darkcolor=panel)
        style.map("Treeview", background=[("selected", "#574321")], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=panel_raised, foreground=dim,
                        padding=(10, 9), borderwidth=0, relief="flat",
                        font=("Segoe UI Semibold", 8, "bold"))
        style.map("Treeview.Heading", background=[("active", "#343941")], foreground=[("active", fg)])
        style.configure("TLabelframe", background=panel, foreground=fg, borderwidth=0, relief="flat", padding=10)
        style.configure("TLabelframe.Label", background=panel, foreground=dim,
                        font=("Segoe UI", 8, "bold"), padding=(3, 0))
        style.configure("TCheckbutton", background=bg, foreground=fg,
                        font=("Segoe UI", 9), padding=(3, 5), indicatorcolor=field)
        style.map("TCheckbutton", foreground=[("disabled", "#68707b"), ("active", "#ffffff")],
                  background=[("active", bg)], indicatorcolor=[("selected", accent)])
        style.configure("Card.TCheckbutton", background=panel, foreground=fg,
                        font=("Segoe UI", 9), padding=(3, 5), indicatorcolor=field)
        style.map("Card.TCheckbutton", background=[("active", panel)], indicatorcolor=[("selected", accent)])
        style.configure("Horizontal.TScale", background=panel, troughcolor="#3a4048",
                        sliderlength=20, borderwidth=0, relief="flat", troughrelief="flat")
        style.map("Horizontal.TScale", background=[("active", panel)])
        style.configure("Vertical.TScrollbar", background="#343a43", troughcolor=sidebar_bg,
                        bordercolor=sidebar_bg, lightcolor="#343a43", darkcolor="#343a43",
                        arrowcolor="#343a43", relief="flat", arrowsize=1, width=9)
        style.configure("Horizontal.TScrollbar", background="#343a43", troughcolor=sidebar_bg,
                        bordercolor=sidebar_bg, lightcolor="#343a43", darkcolor="#343a43",
                        arrowcolor="#343a43", relief="flat", arrowsize=1, width=9)
        style.configure("TPanedwindow", background=bg, sashrelief="flat", sashwidth=6)
        # Remove old-style arrow buttons from clam scrollbars; darkly uses its own
        # compatible layout and simply ignores this fallback if the elements differ.
        try:
            style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
                "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"unit": 1, "sticky": "nswe"})]
            })])
            style.layout("Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {
                "sticky": "we", "children": [("Horizontal.Scrollbar.thumb", {"unit": 1, "sticky": "nswe"})]
            })])
        except tk.TclError:
            pass
        # Clam draws bright one-pixel borders around Treeview/Entry elements even
        # when borderwidth=0. Flatten those layouts so the UI keeps the launcher-like
        # uninterrupted surfaces on systems without ttkbootstrap.
        try:
            style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
            style.layout("TEntry", [("Entry.field", {"sticky": "nswe", "border": 0, "children": [
                ("Entry.padding", {"sticky": "nswe", "children": [
                    ("Entry.textarea", {"sticky": "nswe"})]})]})])
            style.configure("TEntry", fieldbackground=field, background=field, foreground=fg,
                            bordercolor=field, lightcolor=field, darkcolor=field, borderwidth=0)
            style.configure("TCombobox", bordercolor=field, lightcolor=field, darkcolor=field, borderwidth=0)
            style.configure("Sash", background="#30353d", sashthickness=4, gripcount=0, relief="flat")
            style.configure("TPanedwindow", background=bg, sashrelief="flat", sashwidth=4)
        except tk.TclError:
            pass

        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=212, padding=(10, 14))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(8, 6, 8, 18))
        brand.pack(fill="x")
        ttk.Label(brand, text="◉  AutoInformant", background=sidebar_bg, foreground="#ffffff",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(brand, text="Аудиосистема openOMSI", background=sidebar_bg, foreground="#92949d",
                  font=("Segoe UI", 9)).pack(anchor="w", padx=(31, 0), pady=(2, 0))
        ttk.Label(sidebar, text="РАБОТА", background=sidebar_bg, foreground="#777982",
                  font=("Segoe UI Semibold", 8, "bold")).pack(anchor="w", padx=10, pady=(5, 4))
        self._nav_buttons = {}
        for key, title in (("home", "▣  Обзор"), ("packs", "▦  Аудиопаки"), ("music", "♫  Музыка и радио"), ("settings", "⚙  Настройки")):
            btn = ttk.Button(sidebar, text=title, style="Nav.TButton", command=lambda k=key: self._show_page(k))
            btn.pack(fill="x", pady=2)
            self._nav_buttons[key] = btn
        tk.Frame(sidebar, height=1, bg="#30353d").pack(fill="x", pady=14)
        ttk.Label(sidebar, text="СВЯЗЬ", background=sidebar_bg, foreground="#777982",
                  font=("Segoe UI Semibold", 8, "bold")).pack(anchor="w", padx=10, pady=(0, 5))
        self.lbl_sidebar_connection = ttk.Label(sidebar, text="● ждём openOMSI", style="Status.TLabel",
                                                font=("Segoe UI", 9), wraplength=175)
        self.lbl_sidebar_connection.pack(anchor="w", padx=10)
        ttk.Label(sidebar, text=f"v2.0.2\n{APP_DIR}", background=sidebar_bg, foreground="#777982",
                  font=("Segoe UI", 8), wraplength=180).pack(side="bottom", anchor="w", padx=9, pady=(10, 2))

        content = ttk.Frame(shell, style="Content.TFrame", padding=(22, 16, 22, 16))
        content.pack(side="left", fill="both", expand=True)
        topbar = ttk.Frame(content)
        topbar.pack(fill="x", pady=(0, 12))
        self.lbl_page_title = ttk.Label(topbar, text="Обзор", style="PageTitle.TLabel",
                                        font=("Segoe UI", 21, "bold"))
        self.lbl_page_title.pack(side="left")
        self.lbl_connection = ttk.Label(topbar, text="● нет связи", style="Status.TLabel")
        self.lbl_connection.pack(side="right")
        self.page_area = ttk.Frame(content)
        self.page_area.pack(fill="both", expand=True)
        self._pages: dict[str, ttk.Frame] = {}
        self._page_builders = {
            "home": self._build_dashboard_page,
            "packs": self._build_pack_page,
            "music": self._build_music_page,
            "settings": self._build_settings_page,
            "categories": self._build_category_editor_page,
        }
        self.var_auto = tk.BooleanVar(value=bool(self.settings.get("auto_enabled", True)))
        self._ensure_page("home")
        self._show_page("home")

    def _ensure_page(self, key: str):
        if key in self._pages:
            return self._pages[key]
        page = ttk.Frame(self.page_area)
        self._pages[key] = page
        self._page_builders[key](page)
        return page

    def _show_page(self, key: str):
        if key not in getattr(self, "_page_builders", {}):
            return
        # Rebuild settings whenever the user re-enters the page. A map/duty may
        # have loaded while another page was open; profile controls must then show
        # the current pack's values rather than stale pre-connection widget values.
        if key == "settings" and self._active_page != "settings" and key in self._pages:
            old_page = self._pages.pop(key)
            old_page.destroy()
        if self._active_page == "settings" and key != "settings":
            for sequence, binding_id in getattr(self, "_settings_wheel_bindings", []):
                try: self.unbind_all(sequence)
                except tk.TclError: pass
            self._settings_wheel_bindings = []
        for page in self._pages.values():
            page.pack_forget()
        page = self._ensure_page(key)
        page.pack(fill="both", expand=True)
        self._active_page = key
        titles = {"home": "Обзор", "packs": "Аудиопаки", "music": "Музыка и радио", "settings": "Настройки", "categories": "Редактор категорий"}
        self.lbl_page_title.configure(text=titles.get(key, "AutoInformant Pro"))
        for name, button in self._nav_buttons.items():
            button.configure(style="NavActive.TButton" if name == key else "Nav.TButton")
        if key == "packs":
            self._refresh_packs_page()
        elif key == "home":
            self._rebuild_stop_rows()
            self._refresh_quick_volume_controls()
        elif key == "categories":
            self._refresh_category_editor()

    def _build_dashboard_page(self, page):
        hero = ttk.Frame(page, style="Hero.TFrame", padding=(18, 14))
        hero.pack(fill="x", pady=(0, 12))
        ttk.Label(hero, text="Автоматический информатор", style="HeroTitle.TLabel",
                  font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(hero, text="Маршруты и остановки берутся из активного duty openOMSI. Файлы route.txt для определения рейса не используются.",
                  style="HeroSubtitle.TLabel", wraplength=1100).pack(anchor="w", pady=(4, 0))
        route_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        route_card.pack(fill="x", pady=(0, 10))
        ttk.Label(route_card, text="ТЕКУЩИЙ РЕЙС", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 4))
        self.lbl_route = ttk.Label(route_card, text="Жду активный рейс/расписание…", style="CardTitle.TLabel", font=("Segoe UI Semibold", 12, "bold"))
        self.lbl_route.pack(anchor="w")
        self.lbl_next = ttk.Label(route_card, text="Следующая остановка: —", style="CardMuted.TLabel")
        self.lbl_next.pack(anchor="w", pady=(4, 0))
        self.lbl_sync = ttk.Label(route_card, text=self._status_message, style="CardMuted.TLabel")
        self.lbl_sync.pack(anchor="w", pady=(2, 0))

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 10))
        ModernSwitch(bar, "Автоматические объявления", self.var_auto, command=self._toggle_auto,
                     background=self._ui_colors["bg"], foreground=self._ui_colors["fg"],
                     accent=self._ui_colors["accent"]).pack(side="left", pady=3)
        ttk.Button(bar, text="Обновить файлы", command=self.refresh_audio_library).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="▶ Объявить текущую", command=self.manual_current_announcement).pack(side="right", padx=(5, 0))
        ttk.Button(bar, text="🎲 Анонс", command=self.play_random_anon).pack(side="right", padx=(5, 0))
        ttk.Button(bar, text="■ Стоп", command=self.stop_playback).pack(side="right", padx=(5, 0))
        ttk.Button(bar, text="Обновить плагин", command=self.install_plugin).pack(side="right", padx=(5, 0))

        # Compact live faders, similar to the quick mixer found in modern media apps.
        mixer = ttk.Frame(page)
        mixer.pack(fill="x", pady=(0, 10))
        mixer.columnconfigure(0, weight=1)
        mixer.columnconfigure(1, weight=1)
        self._build_quick_volume_card(mixer, "informant", "ИНФОРМАТОР", "◖))", "◖×", 0)
        self._build_quick_volume_card(mixer, "music", "МУЗЫКА И РАДИО", "♫", "♪×", 1)

        main = ttk.PanedWindow(page, orient="vertical")
        main.pack(fill="both", expand=True)
        list_frame = ttk.Frame(main, style="Card.TFrame", padding=10)
        main.add(list_frame, weight=5)
        ttk.Label(list_frame, text="ОСТАНОВКИ АКТИВНОГО РЕЙСА", style="CardMuted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        cols = ("number", "stop", "audio", "state")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse")
        for key, title in (("number", "№"), ("stop", "Остановка из игры"), ("audio", "Аудиофайл"), ("state", "Статус")):
            self.tree.heading(key, text=title)
        self.tree.column("number", width=48, anchor="center", stretch=False)
        self.tree.column("stop", width=320, anchor="w")
        self.tree.column("audio", width=390, anchor="w")
        self.tree.column("state", width=150, anchor="w")
        tree_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        tree_xscroll = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=tree_xscroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        tree_scroll.grid(row=1, column=1, sticky="ns")
        tree_xscroll.grid(row=2, column=0, sticky="ew")
        list_frame.rowconfigure(1, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("missing", foreground="#f87171")
        self.tree.tag_configure("pass", foreground="#9a9da5")
        self.tree.tag_configure("ignored", foreground="#a4a5ab", background="#2b2c31")
        self.tree.tag_configure("arrived", background="#2e3a32")
        self.tree.bind("<Button-3>", self._stop_context_menu)
        self.tree.bind("<Double-1>", lambda _e: self.play_selected_stop())
        ttk.Label(list_frame, text="Двойной клик — прослушать · ПКМ — назначить звук или игнорировать остановку",
                  style="CardMuted.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(7, 0))

        log_frame = ttk.Frame(main, style="Card.TFrame", padding=9)
        main.add(log_frame, weight=2)
        ttk.Label(log_frame, text="ЖУРНАЛ", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 5))
        self.log_text = tk.Text(log_frame, height=6, bg="#171b20", fg="#d4d9e0", insertbackground="#f3f4f6",
                                font=("Consolas", 9), wrap="word", relief="flat", bd=0, highlightthickness=0,
                                padx=8, pady=6, selectbackground="#574321", selectforeground="#ffffff")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _quick_level_key(self, kind: str) -> str:
        return "informant_output_level" if kind == "informant" else "music_output_level"

    def _quick_last_key(self, kind: str) -> str:
        return "informant_output_last_nonzero" if kind == "informant" else "music_output_last_nonzero"

    def _build_quick_volume_card(self, parent, kind: str, title: str, active_icon: str, muted_icon: str, column: int):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(13, 9))
        card.grid(row=0, column=column, sticky="ew", padx=(0, 5) if column == 0 else (5, 0))
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        value = max(0.0, min(1.0, float(self.settings.get(self._quick_level_key(kind), 1.0) or 0.0)))
        button = ttk.Button(top, text=active_icon if value > 0.001 else muted_icon,
                            style="VolumeIcon.TButton", width=3,
                            command=lambda k=kind: self._toggle_quick_mute(k))
        button.pack(side="left", padx=(0, 9))
        ttk.Label(top, text=title, style="Section.TLabel").pack(side="left")
        value_label = ttk.Label(top, text=f"{round(value * 100):.0f}%", style="CardTitle.TLabel", width=5, anchor="e")
        value_label.pack(side="right")
        variable = tk.DoubleVar(value=value * 100.0)
        slider = tk.Canvas(card, height=24, bg=self._ui_colors["panel"], bd=0,
                           highlightthickness=0, cursor="hand2")
        slider.pack(fill="x", pady=(6, 0))
        slider.bind("<Configure>", lambda _e, k=kind: self._draw_quick_volume_slider(k))
        slider.bind("<Button-1>", lambda e, k=kind: self._set_quick_volume_from_event(k, e))
        slider.bind("<B1-Motion>", lambda e, k=kind: self._set_quick_volume_from_event(k, e))
        self._quick_volume_vars[kind] = variable
        self._quick_volume_canvases[kind] = slider
        self._quick_volume_buttons[kind] = button
        self._quick_volume_values[kind] = value_label
        self.after_idle(lambda k=kind: self._draw_quick_volume_slider(k))

    def _draw_quick_volume_slider(self, kind: str):
        canvas = self._quick_volume_canvases.get(kind)
        variable = self._quick_volume_vars.get(kind)
        if canvas is None or variable is None or not canvas.winfo_exists():
            return
        width = max(80, canvas.winfo_width())
        height = max(20, canvas.winfo_height())
        left, right = 9, width - 9
        y = height / 2
        ratio = max(0.0, min(1.0, variable.get() / 100.0))
        pos = left + (right - left) * ratio
        canvas.delete("all")
        # Rounded track and fill, with a clean white thumb like the openOMSI UI.
        canvas.create_line(left, y, right, y, fill="#3c424b", width=5, capstyle="round")
        if pos > left:
            canvas.create_line(left, y, pos, y, fill=self._ui_colors["accent"], width=5, capstyle="round")
        thumb = 7 if canvas.winfo_height() >= 22 else 6
        canvas.create_oval(pos-thumb, y-thumb, pos+thumb, y+thumb,
                           fill="#f4f5f6", outline=self._ui_colors["accent"], width=2)

    def _set_quick_volume_from_event(self, kind: str, event):
        canvas = self._quick_volume_canvases.get(kind)
        if canvas is None:
            return
        width = max(80, canvas.winfo_width())
        left, right = 9, width - 9
        ratio = max(0.0, min(1.0, (float(event.x) - left) / max(1.0, right - left)))
        value = ratio * 100.0
        variable = self._quick_volume_vars.get(kind)
        if variable is not None:
            variable.set(value)
        self._on_quick_volume_changed(kind, str(value))

    def _refresh_quick_volume_controls(self):
        for kind in ("informant", "music"):
            key = self._quick_level_key(kind)
            value = max(0.0, min(1.0, float(self.settings.get(key, 1.0) or 0.0)))
            variable = self._quick_volume_vars.get(kind)
            if variable is not None and abs(variable.get() - value * 100.0) > 0.5:
                variable.set(value * 100.0)
            value_label = self._quick_volume_values.get(kind)
            if value_label is not None:
                value_label.configure(text=f"{value * 100:.0f}%")
            button = self._quick_volume_buttons.get(kind)
            if button is not None:
                active, muted = (("◖))", "◖×") if kind == "informant" else ("♫", "♪×"))
                button.configure(text=active if value > 0.001 else muted)
            self._draw_quick_volume_slider(kind)

    def _on_quick_volume_changed(self, kind: str, raw_value: str):
        try:
            value = max(0.0, min(1.0, float(raw_value) / 100.0))
        except (TypeError, ValueError):
            return
        self.settings[self._quick_level_key(kind)] = value
        if value > 0.001:
            self.settings[self._quick_last_key(kind)] = value
        self._quick_volume_pending[kind] = value
        self._refresh_quick_volume_controls()
        if self._quick_volume_flush_id is None:
            self._quick_volume_flush_id = self.after(160, self._flush_quick_volume)

    def _toggle_quick_mute(self, kind: str):
        key, last_key = self._quick_level_key(kind), self._quick_last_key(kind)
        current = max(0.0, min(1.0, float(self.settings.get(key, 1.0) or 0.0)))
        if current > 0.001:
            self.settings[last_key] = current
            new_value = 0.0
        else:
            try:
                new_value = max(0.05, min(1.0, float(self.settings.get(last_key, 1.0) or 1.0)))
            except (TypeError, ValueError):
                new_value = 1.0
        self.settings[key] = new_value
        variable = self._quick_volume_vars.get(kind)
        if variable is not None:
            variable.set(new_value * 100.0)
        self._quick_volume_pending[kind] = new_value
        self._refresh_quick_volume_controls()
        if self._quick_volume_flush_id is None:
            self._quick_volume_flush_id = self.after(30, self._flush_quick_volume)

    def _flush_quick_volume(self):
        self._quick_volume_flush_id = None
        if self._bridge:
            try:
                self._bridge.set_output_levels(
                    float(self.settings.get("informant_output_level", 1.0)),
                    float(self.settings.get("music_output_level", 1.0)),
                )
            except Exception as exc:
                self.log(f"Не удалось применить быстрые уровни громкости: {exc}")
        self._quick_volume_pending.clear()
        if self._quick_volume_save_after:
            try:
                self.after_cancel(self._quick_volume_save_after)
            except tk.TclError:
                pass
        self._quick_volume_save_after = self.after(450, self._save_quick_volume_settings)

    def _save_quick_volume_settings(self):
        self._quick_volume_save_after = None
        try:
            save_settings(self.settings)
        except Exception as exc:
            self.log(f"Не удалось сохранить громкость: {exc}")

    def _build_pack_page(self, page):
        intro = ttk.Frame(page, style="Hero.TFrame", padding=(16, 12))
        intro.pack(fill="x", pady=(0, 12))
        ttk.Label(intro, text="Библиотека переносимых аудиопаков", style="HeroTitle.TLabel",
                  font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(intro, text="Пак хранит аудио и настройки вместе. Несколько паков одной карты могут быть установлены одновременно; активный вариант выбирается отдельно.",
                  style="HeroSubtitle.TLabel", wraplength=1100).pack(anchor="w", pady=(4, 0))
        body = ttk.PanedWindow(page, orient="horizontal")
        body.pack(fill="both", expand=True)
        list_panel = ttk.Frame(body, style="Card.TFrame", padding=10)
        detail_panel = ttk.Frame(body, style="Card.TFrame", padding=10)
        body.add(list_panel, weight=3); body.add(detail_panel, weight=2)
        # Establish a useful initial split; the detail panel keeps its own vertical
        # scroll so long category descriptions never disappear below the window.
        body.after(150, lambda: body.sashpos(0, max(500, int(body.winfo_width() * 0.59))))
        ttk.Label(list_panel, text="УСТАНОВЛЕННЫЕ ПАКИ", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 3))
        self.prepared_packs_label = ttk.Label(list_panel, text="Подготовленные паки: —", style="CardMuted.TLabel")
        self.prepared_packs_label.pack(anchor="w", pady=(0, 6))
        tree_frame = ttk.Frame(list_panel, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.pack_tree = ttk.Treeview(tree_frame, columns=("name", "author", "profiles", "status"), show="headings", selectmode="browse")
        for key, title in (("name", "Название"), ("author", "Автор"), ("profiles", "Профили"), ("status", "Состояние")):
            self.pack_tree.heading(key, text=title)
        self.pack_tree.column("name", width=210); self.pack_tree.column("author", width=100)
        self.pack_tree.column("profiles", width=95, anchor="center"); self.pack_tree.column("status", width=100, anchor="center")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.pack_tree.yview)
        self.pack_tree.configure(yscrollcommand=scroll.set)
        self.pack_tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        self.pack_tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected_pack_details())
        actions = ttk.Frame(list_panel, style="Card.TFrame"); actions.pack(fill="x", pady=(8, 0))
        actions.columnconfigure(0, weight=1); actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="Импортировать .aipack", style="Accent.TButton", command=self.import_audio_pack).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        ttk.Button(actions, text="Импортировать подготовленные", command=self.import_prepared_packs).grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        ttk.Button(actions, text="Создать .aipack из папки…", style="Accent.TButton", command=self.create_pack_from_folder).grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        ttk.Button(actions, text="Экспортировать .aipack", command=self.export_selected_pack).grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        ttk.Button(actions, text="Экспортировать в папку…", command=self.export_selected_pack_to_folder).grid(row=2, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(actions, text="Удалить", style="Danger.TButton", command=self.remove_selected_pack).grid(row=2, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(detail_panel, text="СВЕДЕНИЯ И ДЕЙСТВИЯ", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 8))
        detail_body = ttk.Frame(detail_panel, style="Card.TFrame")
        detail_body.pack(fill="both", expand=True)
        detail_canvas = tk.Canvas(detail_body, highlightthickness=0, bd=0, background="#202126")
        detail_scroll = ttk.Scrollbar(detail_body, orient="vertical", command=detail_canvas.yview)
        detail_canvas.configure(yscrollcommand=detail_scroll.set)
        detail_canvas.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        detail_inner = ttk.Frame(detail_canvas, style="Card.TFrame")
        detail_inner_id = detail_canvas.create_window((0, 0), window=detail_inner, anchor="nw")
        detail_inner.bind("<Configure>", lambda _e: detail_canvas.configure(scrollregion=detail_canvas.bbox("all")))
        detail_canvas.bind("<Configure>", lambda e: detail_canvas.itemconfigure(detail_inner_id, width=e.width))
        self.pack_detail_canvas = detail_canvas
        self.pack_detail_inner = detail_inner

        self.pack_detail_title = ttk.Label(detail_inner, text="Выбери пак", style="CardTitle.TLabel", font=("Segoe UI", 16, "bold"), wraplength=390, justify="left")
        self.pack_detail_title.pack(anchor="w", fill="x")
        self.pack_detail_meta = ttk.Label(detail_inner, text="", style="CardMuted.TLabel", wraplength=390, justify="left")
        self.pack_detail_meta.pack(anchor="w", fill="x", pady=(6, 12))
        self.pack_detail_desc = ttk.Label(detail_inner, text="", style="Card.TLabel", wraplength=390, justify="left")
        self.pack_detail_desc.pack(anchor="w", fill="x", pady=(0, 12))
        self.pack_icon_label = ttk.Label(detail_inner, text="◉", style="CardTitle.TLabel", font=("Segoe UI", 28), foreground="#efa52b")
        self.pack_icon_label.pack(anchor="w", pady=(0, 8))
        self.pack_enable_var = tk.BooleanVar(value=True)
        ModernSwitch(detail_inner, "Пак включён", self.pack_enable_var,
                     command=self.toggle_selected_pack_enabled,
                     background=self._ui_colors["panel"], foreground=self._ui_colors["fg"],
                     accent=self._ui_colors["accent"]).pack(anchor="w", pady=(0, 8))
        detail_actions = ttk.Frame(detail_inner, style="Card.TFrame"); detail_actions.pack(fill="x", pady=(3, 0))
        ttk.Button(detail_actions, text="Изменить название / автора / описание", command=self.edit_selected_pack).pack(fill="x", pady=3)
        ttk.Button(detail_actions, text="Изменить иконку…", command=self.change_selected_pack_icon).pack(fill="x", pady=3)
        ttk.Button(detail_actions, text="Сделать основным для текущей карты", command=self.activate_selected_pack_for_current_map).pack(fill="x", pady=3)
        ttk.Button(detail_actions, text="Управлять категориями…", style="Accent.TButton", command=self.open_category_editor).pack(fill="x", pady=(7, 3))
        ttk.Button(detail_actions, text="Назначить текущий транспорт категории", command=self.assign_current_vehicle_to_category).pack(fill="x", pady=3)
        self.pack_detail_help = ttk.Label(detail_inner, style="CardMuted.TLabel", text="Категории — самостоятельные аудиослои. При назначении модели категория полностью заменяет звуки карты для этого транспорта, включая остановки, NEXT, TERMINUS, SIGNAL, JINGLE, DOORS и анонсы. Если подходящих категорий несколько, последняя назначенная текущей модели становится активной.",
                  wraplength=390, justify="left")
        self.pack_detail_help.pack(anchor="w", fill="x", pady=(14, 8))
        def _resize_detail(event):
            wrap = max(220, event.width - 18)
            for widget in (self.pack_detail_title, self.pack_detail_meta, self.pack_detail_desc, self.pack_detail_help):
                widget.configure(wraplength=wrap)
        detail_canvas.bind("<Configure>", _resize_detail, add="+")
        self.pack_detail_frame = detail_panel

    # ---------- Category editor: all changes stay inside the selected .aipack ----------
    def open_category_editor(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            messagebox.showinfo("Пак не выбран", "Сначала выбери аудиопак.", parent=self)
            return
        manifest = self.pack_manager.pack_for_profile(pack_id)
        if not manifest or not manifest.get("map_profiles"):
            messagebox.showwarning("Нет профиля карты", "Категории можно создавать внутри пака, у которого есть хотя бы один профиль карты.", parent=self)
            return
        self._category_editor_pack_id = pack_id
        if "categories" not in self._pages:
            self._ensure_page("categories")
        # The page builder initializes the editor controls; restore the requested
        # pack after first construction, before the page refreshes its selection.
        self._category_editor_pack_id = pack_id
        self._show_page("categories")

    def _build_category_editor_page(self, page):
        header = ttk.Frame(page, style="Hero.TFrame", padding=(16, 10))
        header.pack(fill="x", pady=(0, 9))
        header_top = ttk.Frame(header, style="Hero.TFrame")
        header_top.pack(fill="x")
        self.category_page_title = ttk.Label(header_top, text="Категории аудиопака", style="HeroTitle.TLabel",
                                             font=("Segoe UI", 20, "bold"))
        self.category_page_title.pack(side="left", anchor="w")
        ttk.Button(header_top, text="← К аудиопакам", command=lambda: self._show_page("packs")).pack(side="right")
        ttk.Label(header, text="Отдельная аудиобиблиотека для выбранных моделей транспорта; полностью заменяет основную библиотеку карты.",
                  style="HeroSubtitle.TLabel", wraplength=1100).pack(anchor="w", pady=(4, 0))

        split = ttk.PanedWindow(page, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, style="Card.TFrame", padding=10)
        right = ttk.Frame(split, style="Card.TFrame", padding=10)
        split.add(left, weight=2)
        split.add(right, weight=4)
        split.after(120, lambda: split.sashpos(0, max(260, int(split.winfo_width() * 0.34))))

        ttk.Label(left, text="КАТЕГОРИИ", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 8))
        cat_tree_frame = ttk.Frame(left, style="Card.TFrame")
        cat_tree_frame.pack(fill="both", expand=True)
        self.category_tree = ttk.Treeview(cat_tree_frame, columns=("vehicles",), show="tree headings", selectmode="browse")
        self.category_tree.heading("#0", text="Название")
        self.category_tree.heading("vehicles", text="Моделей")
        self.category_tree.column("#0", width=170, minwidth=110)
        self.category_tree.column("vehicles", width=65, minwidth=55, anchor="center", stretch=False)
        cat_scroll = ttk.Scrollbar(cat_tree_frame, orient="vertical", command=self.category_tree.yview)
        self.category_tree.configure(yscrollcommand=cat_scroll.set)
        self.category_tree.pack(side="left", fill="both", expand=True)
        cat_scroll.pack(side="right", fill="y")
        self.category_tree.bind("<<TreeviewSelect>>", lambda _e: self._load_category_editor_selection())
        ttk.Button(left, text="＋ Создать категорию", style="Accent.TButton", command=self._create_category_dialog).pack(fill="x", pady=(10, 0))

        self.category_editor_empty = ttk.Label(right, text="Выбери категорию слева или создай новую.",
                                               style="CardMuted.TLabel", wraplength=640, justify="left")
        self.category_editor_empty.pack(anchor="w", pady=(0, 12))
        form = ttk.Frame(right, style="Card.TFrame")
        form.pack(fill="x")
        ttk.Label(form, text="Название категории", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.category_name_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.category_name_var).grid(row=1, column=0, sticky="ew")
        ttk.Label(form, text="Карта, для которой используется категория", style="CardMuted.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 5))
        self.category_map_var = tk.StringVar()
        self.category_map_combo = ttk.Combobox(form, textvariable=self.category_map_var, state="readonly")
        self.category_map_combo.grid(row=3, column=0, sticky="ew")
        form.columnconfigure(0, weight=1)
        meta_actions = ttk.Frame(right, style="Card.TFrame")
        meta_actions.pack(fill="x", pady=(7, 8))
        ttk.Button(meta_actions, text="Сохранить изменения", style="Accent.TButton", command=self._save_category_editor).pack(side="left")
        ttk.Button(meta_actions, text="Удалить категорию", style="Danger.TButton", command=self._delete_selected_category).pack(side="right")

        tk.Frame(right, height=1, bg="#30353d").pack(fill="x", pady=(2, 12))
        asset_header = ttk.Frame(right, style="Card.TFrame")
        asset_header.pack(fill="x")
        ttk.Label(asset_header, text="АУДИОБИБЛИОТЕКА", style="CardMuted.TLabel").pack(side="left")
        self.category_asset_path_label = ttk.Label(asset_header, text="Папка пакета", style="CardMuted.TLabel")
        self.category_asset_path_label.pack(side="right")
        asset_actions = ttk.Frame(right, style="Card.TFrame")
        asset_actions.pack(fill="x", pady=(6, 8))
        ttk.Button(asset_actions, text="Открыть папку звуков", command=self._open_selected_category_folder).pack(side="left")
        ttk.Button(asset_actions, text="Импортировать папку звуков…", command=self._import_category_audio_folder).pack(side="left", padx=(7, 0))
        ttk.Label(right, text="Для точечных правок открой папку; импорт заменяет её содержимое. Исходная папка не изменяется.",
                  style="CardMuted.TLabel", wraplength=720, justify="left").pack(anchor="w", pady=(0, 7))

        tk.Frame(right, height=1, bg="#30353d").pack(fill="x", pady=(0, 8))
        ttk.Label(right, text="НАЗНАЧЕННЫЕ МОДЕЛИ ТРАНСПОРТА", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 5))
        assignment_frame = ttk.Frame(right, style="Card.TFrame")
        assignment_frame.pack(fill="x", expand=False)
        self.category_assignment_tree = ttk.Treeview(assignment_frame, columns=("model",), show="headings", selectmode="browse", height=2)
        self.category_assignment_tree.heading("model", text="Производитель | модель из телеметрии openOMSI")
        self.category_assignment_tree.column("model", width=500, anchor="w")
        assignment_scroll = ttk.Scrollbar(assignment_frame, orient="vertical", command=self.category_assignment_tree.yview)
        self.category_assignment_tree.configure(yscrollcommand=assignment_scroll.set)
        self.category_assignment_tree.pack(side="left", fill="both", expand=True)
        assignment_scroll.pack(side="right", fill="y")
        assignment_actions = ttk.Frame(right, style="Card.TFrame")
        assignment_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(assignment_actions, text="Назначить текущий транспорт сюда", style="Accent.TButton", command=self._category_assign_current_vehicle).pack(side="left")
        ttk.Button(assignment_actions, text="Добавить модель вручную…", command=self._category_add_manual_vehicle).pack(side="left", padx=(7, 0))
        ttk.Button(assignment_actions, text="Удалить выбранное", style="Danger.TButton", command=self._category_remove_vehicle).pack(side="right")
        ttk.Label(right, text="Ручной формат: «Производитель | Модель». Точнее — назначить транспорт из телеметрии openOMSI.",
                  style="CardMuted.TLabel", wraplength=720, justify="left").pack(anchor="w", pady=(5, 0))

        self._category_editor_pack_id = None
        self._category_editor_selected_id = None

    def _category_manifest_and_profile(self):
        pack_id = getattr(self, "_category_editor_pack_id", None)
        category_id = getattr(self, "_category_editor_selected_id", None)
        manifest = self.pack_manager.pack_for_profile(pack_id) if pack_id else None
        profile = next((p for p in (manifest or {}).get("category_profiles", []) if p.get("id") == category_id), None)
        return pack_id, manifest, profile

    def _refresh_category_editor(self, select_id: str | None = None):
        if not getattr(self, "category_tree", None):
            return
        pack_id = getattr(self, "_category_editor_pack_id", None)
        manifest = self.pack_manager.pack_for_profile(pack_id) if pack_id else None
        if not manifest:
            self.category_page_title.configure(text="Категории аудиопака")
            self.category_tree.delete(*self.category_tree.get_children())
            self._category_editor_selected_id = None
            self._load_category_editor_selection()
            return
        self.category_page_title.configure(text=f"Категории · {manifest.get('name') or pack_id}")
        current = select_id or getattr(self, "_category_editor_selected_id", None)
        self.category_tree.delete(*self.category_tree.get_children())
        for profile in manifest.get("category_profiles", []):
            category_id = str(profile.get("id") or "")
            assignments = profile.get("vehicle_assignments", [])
            self.category_tree.insert("", "end", iid=category_id,
                                      text=f"▸  {profile.get('name') or category_id}",
                                      values=(len(assignments),))
        if current and self.category_tree.exists(current):
            self.category_tree.selection_set(current)
            self.category_tree.see(current)
            self._category_editor_selected_id = current
        elif self.category_tree.get_children():
            first = self.category_tree.get_children()[0]
            self.category_tree.selection_set(first)
            self._category_editor_selected_id = first
        else:
            self._category_editor_selected_id = None
        self._load_category_editor_selection()

    def _load_category_editor_selection(self):
        selected = self.category_tree.selection() if getattr(self, "category_tree", None) else ()
        if selected:
            self._category_editor_selected_id = str(selected[0])
        pack_id, manifest, profile = self._category_manifest_and_profile()
        if not profile:
            self.category_editor_empty.configure(text="В этом паке пока нет категорий. Нажми «Создать категорию», чтобы добавить отдельную аудиобиблиотеку, например «Троллейбусы», «Трамваи» или «Электробусы». Затем импортируй папку звуков и назначь модели транспорта. Категория заменяет основную библиотеку карты для назначенной модели, а не смешивает их.")
            self.category_name_var.set("")
            self.category_map_var.set("")
            self.category_asset_path_label.configure(text="Папка не создана")
            if getattr(self, "category_assignment_tree", None):
                self.category_assignment_tree.delete(*self.category_assignment_tree.get_children())
            return
        aliases = list(profile.get("map_aliases", []))
        self.category_editor_empty.configure(text=f"Категория «{profile.get('name') or profile.get('id')}» изолирована: её звуки используются только назначенными моделями.")
        self.category_name_var.set(str(profile.get("name") or ""))
        map_profiles = manifest.get("map_profiles", [])
        labels = []
        self._category_map_profile_by_label = {}
        for base in map_profiles:
            base_aliases = list(base.get("map_aliases", []))
            label = f"{', '.join(base_aliases) or 'Все карты'}  ·  {base.get('id')}"
            labels.append(label)
            self._category_map_profile_by_label[label] = base
            if set(base_aliases) == set(aliases):
                self.category_map_var.set(label)
        self.category_map_combo.configure(values=labels)
        if labels and not self.category_map_var.get():
            self.category_map_var.set(labels[0])
        root = self.pack_manager.pack_directory(pack_id)
        asset_root = str(profile.get("asset_root") or "assets")
        self.category_asset_path_label.configure(text=asset_root)
        self.category_assignment_tree.delete(*self.category_assignment_tree.get_children())
        for index, assignment in enumerate(profile.get("vehicle_assignments", [])):
            self.category_assignment_tree.insert("", "end", iid=str(index), values=(str(assignment),))

    def _create_category_dialog(self):
        pack_id = getattr(self, "_category_editor_pack_id", None)
        manifest = self.pack_manager.pack_for_profile(pack_id) if pack_id else None
        if not manifest:
            return
        map_profiles = manifest.get("map_profiles", [])
        if not map_profiles:
            messagebox.showwarning("Нет профиля карты", "Сначала добавь в пак профиль карты.", parent=self)
            return
        dlg = tk.Toplevel(self)
        dlg.title("Новая категория")
        dlg.geometry("480x270")
        dlg.minsize(420, 240)
        dlg.configure(bg=self._ui_colors["bg"])
        self._place_dialog(dlg, self, modal=True)
        body = ttk.Frame(dlg, style="Card.TFrame", padding=18)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(body, text="Создание аудиокатегории", style="HeroTitle.TLabel",
                  font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(body, text="Например: Троллейбусы, Трамваи, Электробусы. Для категории создастся отдельная папка внутри пакета.",
                  style="CardMuted.TLabel", wraplength=410).pack(anchor="w", pady=(5, 12))
        ttk.Label(body, text="Название", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 4))
        name_var = tk.StringVar()
        name_entry = ttk.Entry(body, textvariable=name_var)
        name_entry.pack(fill="x")
        ttk.Label(body, text="Для какого профиля карты", style="CardMuted.TLabel").pack(anchor="w", pady=(12, 4))
        labels = []
        by_label = {}
        for base in map_profiles:
            label = f"{', '.join(base.get('map_aliases', [])) or 'Все карты'}  ·  {base.get('id')}"
            labels.append(label); by_label[label] = base
        map_var = tk.StringVar(value=labels[0])
        ttk.Combobox(body, textvariable=map_var, values=labels, state="readonly").pack(fill="x")
        actions = ttk.Frame(body, style="Card.TFrame")
        actions.pack(fill="x", pady=(16, 0))
        def create():
            try:
                category_id = self.pack_manager.create_category(pack_id, name=name_var.get(),
                                                                map_profile_id=by_label[map_var.get()].get("id"))
                dlg.destroy()
                self._refresh_category_editor(category_id)
                self._refresh_packs_page(pack_id)
                self.log(f"Создана категория «{name_var.get().strip()}» в паке «{manifest.get('name') or pack_id}».")
            except Exception as exc:
                messagebox.showerror("Не удалось создать категорию", str(exc), parent=dlg)
        ttk.Button(actions, text="Отмена", command=dlg.destroy).pack(side="right")
        ttk.Button(actions, text="Создать", style="Accent.TButton", command=create).pack(side="right", padx=(0, 7))
        name_entry.focus_set()

    def _save_category_editor(self):
        pack_id, manifest, profile = self._category_manifest_and_profile()
        if not profile:
            messagebox.showinfo("Категория не выбрана", "Сначала выбери категорию или создай новую.", parent=self)
            return
        label = self.category_map_var.get()
        base = getattr(self, "_category_map_profile_by_label", {}).get(label)
        if not base:
            messagebox.showerror("Не выбрана карта", "Выбери профиль карты для этой категории.", parent=self)
            return
        try:
            self.pack_manager.update_category(pack_id, profile["id"], name=self.category_name_var.get(),
                                              map_profile_id=base.get("id"))
            self._refresh_category_editor(profile["id"])
            self._refresh_packs_page(pack_id)
            self.refresh_audio_library()
            self.log(f"Обновлена категория «{self.category_name_var.get().strip()}».")
        except Exception as exc:
            messagebox.showerror("Не удалось сохранить категорию", str(exc), parent=self)

    def _delete_selected_category(self):
        pack_id, _manifest, profile = self._category_manifest_and_profile()
        if not profile:
            return
        name = str(profile.get("name") or profile.get("id"))
        if not messagebox.askyesno("Удалить категорию", f"Удалить категорию «{name}» вместе со всеми её аудиофайлами и назначениями?\n\nОстальные категории и основная библиотека не затрагиваются.", parent=self):
            return
        try:
            self.pack_manager.delete_category(pack_id, profile["id"])
            self._category_editor_selected_id = None
            self._refresh_category_editor()
            self._refresh_packs_page(pack_id)
            self.refresh_audio_library()
        except Exception as exc:
            messagebox.showerror("Не удалось удалить категорию", str(exc), parent=self)

    def _open_selected_category_folder(self):
        pack_id, _manifest, profile = self._category_manifest_and_profile()
        if not profile:
            messagebox.showinfo("Категория не выбрана", "Сначала выбери категорию.", parent=self)
            return
        root = self.pack_manager.pack_directory(pack_id)
        folder = (root / str(profile.get("asset_root") or "assets")).resolve()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self)

    def _import_category_audio_folder(self):
        pack_id, _manifest, profile = self._category_manifest_and_profile()
        if not profile:
            messagebox.showinfo("Категория не выбрана", "Сначала выбери категорию.", parent=self)
            return
        source = filedialog.askdirectory(title="Выбери папку с аудиобиблиотекой категории", parent=self)
        if not source:
            return
        if not messagebox.askyesno("Заменить содержимое категории?",
                                   "Все текущие файлы выбранной категории будут заменены копией выбранной папки.\n\nИсходная папка останется нетронутой.", parent=self):
            return
        try:
            target = self.pack_manager.import_category_audio(pack_id, profile["id"], source)
            self.library._index_cache.clear(); self.library._duration_cache.clear()
            self._refresh_category_editor(profile["id"])
            self.refresh_audio_library()
            self.log(f"Импортирована аудиопапка категории «{profile.get('name')}»: {target}")
            messagebox.showinfo("Аудиобиблиотека импортирована", f"Файлы скопированы внутрь пака:\n{target}", parent=self)
        except Exception as exc:
            messagebox.showerror("Не удалось импортировать папку", str(exc), parent=self)

    def _category_assign_current_vehicle(self):
        pack_id, _manifest, profile = self._category_manifest_and_profile()
        if not profile:
            messagebox.showinfo("Категория не выбрана", "Сначала выбери категорию.", parent=self)
            return
        if not self.map_name or self.map_name == "Неизвестная карта":
            messagebox.showinfo("Карта не загружена", "Сначала загрузи нужную карту и транспорт в openOMSI.", parent=self)
            return
        manufacturer = str(self.current_state.get("vehicle_manufacturer") or "").strip()
        model = str(self.current_state.get("vehicle_model") or "").strip()
        if not manufacturer and not model:
            messagebox.showinfo("Транспорт не определён", "Выбери транспорт в openOMSI и дождись обновления телеметрии.", parent=self)
            return
        try:
            self.pack_manager.set_active_category_for_vehicle(pack_id, profile["id"], self.map_name, manufacturer, model)
            self._refresh_category_editor(profile["id"])
            self._refresh_packs_page(pack_id)
            self.refresh_audio_library()
            self.log(f"{manufacturer} | {model} назначен категории «{profile.get('name')}».")
        except Exception as exc:
            messagebox.showerror("Не удалось назначить транспорт", str(exc), parent=self)

    def _category_add_manual_vehicle(self):
        pack_id, _manifest, profile = self._category_manifest_and_profile()
        if not profile:
            messagebox.showinfo("Категория не выбрана", "Сначала выбери категорию.", parent=self)
            return
        value = simpledialog.askstring("Добавить модель транспорта", "Введи производителя и модель в формате:\nПроизводитель | Модель\n\nПример: ZiU | 682G g00", parent=self)
        if not value:
            return
        try:
            self.pack_manager.add_vehicle_assignment(pack_id, profile["id"], value)
            self._refresh_category_editor(profile["id"])
            self._refresh_packs_page(pack_id)
        except Exception as exc:
            messagebox.showerror("Не удалось добавить модель", str(exc), parent=self)

    def _category_remove_vehicle(self):
        pack_id, _manifest, profile = self._category_manifest_and_profile()
        if not profile:
            return
        selected = self.category_assignment_tree.selection()
        if not selected:
            messagebox.showinfo("Модель не выбрана", "Выбери назначение в списке.", parent=self)
            return
        assignment = str(self.category_assignment_tree.item(selected[0], "values")[0])
        if not messagebox.askyesno("Удалить назначение", f"Убрать «{assignment}» из категории «{profile.get('name')}»?", parent=self):
            return
        self.pack_manager.remove_vehicle_assignment(pack_id, profile["id"], assignment)
        self._refresh_category_editor(profile["id"])
        self._refresh_packs_page(pack_id)
        self.refresh_audio_library()

    def _selected_pack_id(self) -> str | None:
        if not getattr(self, "pack_tree", None):
            return None
        selected = self.pack_tree.selection()
        return str(selected[0]) if selected else None

    def _refresh_packs_page(self, select_id: str | None = None):
        if not getattr(self, "pack_tree", None):
            return
        previous = select_id or self._selected_pack_id()
        self.pack_manager.refresh()
        self.pack_tree.delete(*self.pack_tree.get_children())
        for item in self.pack_manager.list_packs():
            manifest = item["manifest"]
            status = "выключен" if not item["enabled"] else "установлен"
            map_aliases = [alias for profile in manifest.get("map_profiles", []) for alias in profile.get("map_aliases", [])]
            active = self.pack_manager.map_profile(self.map_name or "") if self.map_name else None
            if active and active[0] == item["id"]:
                status = "основной"
            profile_text = f"{item['map_profiles']} карт. · {item['category_profiles']} кат."
            self.pack_tree.insert("", "end", iid=item["id"], values=(item["name"], item["author"] or "—", profile_text, status))
        if previous and self.pack_tree.exists(previous):
            self.pack_tree.selection_set(previous)
            self.pack_tree.see(previous)
        elif self.pack_tree.get_children():
            first = self.pack_tree.get_children()[0]
            self.pack_tree.selection_set(first)
        self._show_selected_pack_details()
        prepared = self.pack_manager.exported_files()
        if hasattr(self, "prepared_packs_label"):
            self.prepared_packs_label.configure(text=f"Подготовленных пакетов рядом: {len(prepared)}")

    def _show_selected_pack_details(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            self.pack_detail_title.configure(text="Выбери пак")
            self.pack_detail_meta.configure(text="")
            self.pack_detail_desc.configure(text="")
            self.pack_icon_label.configure(image="", text="◉")
            self._pack_icon_image = None
            self.pack_enable_var.set(False)
            return
        item = next((row for row in self.pack_manager.list_packs() if row["id"] == pack_id), None)
        if not item:
            return
        manifest = item["manifest"]
        self.pack_detail_title.configure(text=item["name"])
        maps = [alias for profile in manifest.get("map_profiles", []) for alias in profile.get("map_aliases", [])]
        categories = [str(profile.get("name") or profile.get("id")) for profile in manifest.get("category_profiles", [])]
        meta = [f"Автор: {item['author'] or 'не указан'}", f"ID: {pack_id}",
                "Карты: " + (", ".join(maps) if maps else "нет"),
                "Категории: " + (", ".join(categories) if categories else "нет")]
        self.pack_detail_meta.configure(text="\n".join(meta))
        self.pack_detail_desc.configure(text=item["description"] or "Описание не добавлено.")
        self.pack_enable_var.set(item["enabled"])
        self._pack_icon_image = None
        icon_path = item["icon"]
        try:
            from PIL import Image, ImageTk
            image = Image.open(icon_path).convert("RGBA")
            image.thumbnail((72, 72))
            self._pack_icon_image = ImageTk.PhotoImage(image)
            self.pack_icon_label.configure(image=self._pack_icon_image, text="")
        except Exception:
            self.pack_icon_label.configure(image="", text="◉", font=("Segoe UI", 28), foreground="#efa52b")

    def create_pack_from_folder(self):
        """Create a portable .aipack from a legacy audio folder or repack a full package folder."""
        source_name = filedialog.askdirectory(title="Выбери папку аудиотеки карты или распакованный пакет", parent=self)
        if not source_name:
            return
        source = Path(source_name).expanduser().resolve()
        try:
            info = self.pack_manager.inspect_audio_folder(source)
        except Exception as exc:
            messagebox.showerror("Не удалось прочитать папку", str(exc), parent=self)
            return

        if info["is_package_directory"]:
            try:
                manifest = self.pack_manager._validate_package_directory(source)
            except Exception as exc:
                messagebox.showerror("Папка не является корректным пакетом", str(exc), parent=self)
                return
            default_file = f"{manifest.get('id') or source.name}.aipack"
            filename = filedialog.asksaveasfilename(
                title="Сохранить .aipack из распакованной папки",
                initialdir=str(self.pack_manager.export_dir), initialfile=default_file,
                defaultextension=".aipack", filetypes=[("AutoInformant Pack", "*.aipack")], parent=self,
            )
            if not filename:
                return
            try:
                if Path(filename).exists() and not messagebox.askyesno(
                    "Файл уже существует", f"Заменить файл?\n\n{filename}", parent=self
                ):
                    return
                result = self.pack_manager.create_pack_from_package_folder(source, filename, overwrite=True)
                self.log(f"Упакована готовая папка «{manifest.get('name') or source.name}»: {result}")
                messagebox.showinfo("Пак создан", f"Полная структура пакета сохранена без изменения:\n\n{result}", parent=self)
                self._ask_install_created_pack(result)
            except Exception as exc:
                messagebox.showerror("Не удалось создать .aipack", str(exc), parent=self)
            return

        if info["file_count"] <= 0:
            messagebox.showwarning("Папка пустая", "В выбранной папке нет файлов, которые можно включить в аудиопак.", parent=self)
            return

        dlg = tk.Toplevel(self)
        dlg.title("Создать аудиопак из папки")
        dlg.geometry("790x690")
        dlg.minsize(680, 570)
        dlg.configure(bg=self._ui_colors["bg"])
        self._place_dialog(dlg, self, modal=True)
        body = ttk.Frame(dlg, style="Card.TFrame", padding=16)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Label(body, text="Создание переносимого аудиопака", style="HeroTitle.TLabel",
                  font=("Segoe UI", 19, "bold")).pack(anchor="w")
        size_mb = info["total_bytes"] / (1024 * 1024)
        ttk.Label(body, text=f"Источник: {source}\nНайдено файлов: {info['file_count']} · Размер: {size_mb:.1f} МБ. Исходная папка останется нетронутой.",
                  style="CardMuted.TLabel", wraplength=730, justify="left").pack(anchor="w", pady=(5, 12))

        form = ttk.Frame(body, style="Card.TFrame")
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)
        name_var = tk.StringVar(value=f"Информатор {source.name}")
        author_var = tk.StringVar()
        map_var = tk.StringVar(value=source.name)
        ttk.Label(form, text="Название пака", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        ttk.Entry(form, textvariable=name_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(form, text="Автор", style="CardMuted.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0), pady=(0, 4))
        ttk.Entry(form, textvariable=author_var).grid(row=1, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(form, text="Имя карты (map alias из openOMSI)", style="CardMuted.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(10, 4))
        ttk.Entry(form, textvariable=map_var).grid(row=3, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(form, text="Описание", style="CardMuted.TLabel").grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 4))
        description = tk.Text(form, height=3, wrap="word", relief="flat", bd=0,
                              background="#17191e", foreground="#e8e8eb", insertbackground="#ffffff",
                              font=("Segoe UI", 9), padx=8, pady=6)
        description.grid(row=3, column=1, sticky="ew", padx=(8, 0))

        category_card = ttk.Frame(body, style="Card.TFrame")
        category_card.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(category_card, text="КАТЕГОРИАЛЬНЫЕ ПАПКИ", style="CardMuted.TLabel").pack(anchor="w")
        ttk.Label(category_card,
                  text="Отмеченные папки станут отдельными категориями и будут заменять основную аудиотеку для назначенных моделей. All, «Анонсы» и числовые папки маршрутов автоматически остаются в основной библиотеке.",
                  style="CardMuted.TLabel", wraplength=735, justify="left").pack(anchor="w", pady=(4, 7))

        category_rows = ttk.Frame(category_card, style="Card.TFrame")
        category_rows.pack(fill="both", expand=True)
        cat_canvas = tk.Canvas(category_rows, background="#202126", highlightthickness=0, bd=0)
        cat_scroll = ttk.Scrollbar(category_rows, orient="vertical", command=cat_canvas.yview)
        cat_canvas.configure(yscrollcommand=cat_scroll.set)
        cat_canvas.pack(side="left", fill="both", expand=True)
        cat_scroll.pack(side="right", fill="y")
        cat_inner = ttk.Frame(cat_canvas, style="Card.TFrame")
        cat_window = cat_canvas.create_window((0, 0), window=cat_inner, anchor="nw")
        cat_inner.bind("<Configure>", lambda _e: cat_canvas.configure(scrollregion=cat_canvas.bbox("all")))
        cat_canvas.bind("<Configure>", lambda e: cat_canvas.itemconfigure(cat_window, width=e.width))

        all_directories = list(info["directories"])
        category_vars: dict[str, tk.BooleanVar] = {}
        assignment_vars: dict[str, tk.StringVar] = {}
        row_widgets: dict[str, ttk.Frame] = {}

        def add_category_row(folder_name: str, selected: bool = True):
            if folder_name in category_vars:
                category_vars[folder_name].set(True)
                return
            if folder_name not in all_directories:
                messagebox.showwarning("Нет такой папки", "Категория должна быть подпапкой первого уровня выбранной аудиотеки.", parent=dlg)
                return
            row = ttk.Frame(cat_inner, style="Card.TFrame")
            row.pack(fill="x", pady=3)
            enabled = tk.BooleanVar(value=selected)
            assignment = tk.StringVar()
            category_vars[folder_name] = enabled
            assignment_vars[folder_name] = assignment
            ttk.Checkbutton(row, text=folder_name, variable=enabled).pack(side="left", padx=(2, 10))
            ttk.Label(row, text="Модели (необязательно):", style="CardMuted.TLabel").pack(side="left")
            ttk.Entry(row, textvariable=assignment).pack(side="left", fill="x", expand=True, padx=(7, 2))
            row_widgets[folder_name] = row

        suggestions = list(info["suggested_categories"])
        for folder in suggestions:
            add_category_row(folder, selected=True)
        if not suggestions:
            ttk.Label(cat_inner, text="Подходящих папок-категорий автоматически не найдено. Можно добавить любую подпапку вручную ниже или создать категории после установки пака.",
                      style="CardMuted.TLabel", wraplength=700).pack(anchor="w", pady=8)

        add_row = ttk.Frame(category_card, style="Card.TFrame")
        add_row.pack(fill="x", pady=(7, 0))
        extra_folder_var = tk.StringVar()
        extras = [name for name in all_directories if name not in category_vars]
        extra_combo = ttk.Combobox(add_row, textvariable=extra_folder_var, values=extras, state="readonly")
        extra_combo.pack(side="left", fill="x", expand=True)
        def add_manual_category():
            selected_folder = extra_folder_var.get()
            if not selected_folder:
                return
            add_category_row(selected_folder, selected=True)
            extra_combo.configure(values=[name for name in all_directories if name not in category_vars])
            extra_folder_var.set("")
        ttk.Button(add_row, text="＋ Добавить категорию из папки", command=add_manual_category).pack(side="left", padx=(8, 0))

        footer = ttk.Frame(body, style="Card.TFrame")
        footer.pack(fill="x", pady=(14, 0))
        ttk.Label(footer, text="Назначения можно оставить пустыми и настроить позже в редакторе категорий. Старые папки маршрутов не переименовываются и не анализируются.",
                  style="CardMuted.TLabel", wraplength=510).pack(side="left", fill="x", expand=True)

        def create():
            pack_name = name_var.get().strip()
            map_alias = map_var.get().strip()
            if not pack_name or not map_alias:
                messagebox.showwarning("Заполни обязательные поля", "Нужно указать название пака и имя карты.", parent=dlg)
                return
            selected_categories = [name for name, variable in category_vars.items() if variable.get()]
            assignments = {name: value.get() for name, value in assignment_vars.items() if value.get().strip()}
            filename = filedialog.asksaveasfilename(
                title="Сохранить новый аудиопак", initialdir=str(self.pack_manager.export_dir),
                initialfile=f"{map_alias}.aipack", defaultextension=".aipack",
                filetypes=[("AutoInformant Pack", "*.aipack")], parent=dlg,
            )
            if not filename:
                return
            try:
                if Path(filename).exists() and not messagebox.askyesno(
                    "Файл уже существует", f"Заменить существующий архив?\n\n{filename}", parent=dlg
                ):
                    return
                result = self.pack_manager.create_pack_from_audio_folder(
                    source, filename, name=pack_name, author=author_var.get(),
                    description=description.get("1.0", "end").strip(), map_alias=map_alias,
                    category_folders=selected_categories, category_assignments=assignments, overwrite=True,
                )
                dlg.destroy()
                self.log(f"Создан аудиопак из папки «{source.name}»: {result}")
                messagebox.showinfo("Аудиопак создан", f"Создан переносимый пакет:\n\n{result}\n\nИсходная папка не изменена.", parent=self)
                self._ask_install_created_pack(result)
            except Exception as exc:
                messagebox.showerror("Не удалось создать аудиопак", str(exc), parent=dlg)

        ttk.Button(footer, text="Отмена", command=dlg.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="Создать .aipack", style="Accent.TButton", command=create).pack(side="right")
        self._place_dialog(dlg, self, modal=True)

    def _ask_install_created_pack(self, archive_path: str | Path):
        if not messagebox.askyesno("Установить созданный пак?", "Хочешь сразу установить созданный .aipack в эту программу?", parent=self):
            return
        import zipfile
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
            pack_id = str(manifest.get("id") or "")
            overwrite = False
            if self.pack_manager.pack_directory(pack_id):
                overwrite = messagebox.askyesno(
                    "Пак уже установлен",
                    f"Пак «{manifest.get('name') or pack_id}» уже установлен.\n\nЗаменить его? Локальные изменения внутри установленной копии будут потеряны.",
                    parent=self,
                )
                if not overwrite:
                    return
            self.pack_manager.import_pack(archive_path, overwrite=overwrite)
            self._refresh_packs_page(pack_id)
            self.log(f"Установлен созданный аудиопак «{manifest.get('name') or pack_id}».")
        except Exception as exc:
            messagebox.showerror("Не удалось установить созданный пак", str(exc), parent=self)

    def export_selected_pack_to_folder(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            messagebox.showinfo("Пак не выбран", "Сначала выбери пак для экспорта.", parent=self)
            return
        item = next((row for row in self.pack_manager.list_packs() if row["id"] == pack_id), None)
        if not item:
            return
        manifest = item["manifest"]
        map_profiles = manifest.get("map_profiles", [])
        dlg = tk.Toplevel(self)
        dlg.title("Экспортировать аудиопак в папку")
        dlg.geometry("620x430")
        dlg.minsize(560, 390)
        dlg.configure(bg=self._ui_colors["bg"])
        self._place_dialog(dlg, self, modal=True)
        body = ttk.Frame(dlg, style="Card.TFrame", padding=18)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Label(body, text="Экспорт в папочную структуру", style="HeroTitle.TLabel",
                  font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"Пак: {item['name']}\nВыбери, какую структуру восстановить.",
                  style="CardMuted.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=(5, 14))
        mode_var = tk.StringVar(value="audio")
        ttk.Radiobutton(body, text="Обычная папка аудиотеки — звуки карты и категориальные подпапки, без манифеста и настроек пака.",
                        variable=mode_var, value="audio").pack(anchor="w", pady=4)
        ttk.Radiobutton(body, text="Полная папка пакета — manifest.json, иконка, настройки, назначения и все аудиофайлы. Её можно снова упаковать в .aipack.",
                        variable=mode_var, value="package").pack(anchor="w", pady=4)
        categories_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Включить категориальные папки при экспорте обычной аудиотеки",
                        variable=categories_var).pack(anchor="w", pady=(7, 2))
        profile_var = tk.StringVar()
        profile_by_label = {}
        if len(map_profiles) > 1:
            labels = []
            for profile in map_profiles:
                label = f"{', '.join(profile.get('map_aliases', [])) or 'Карта'} · {profile.get('id')}"
                labels.append(label); profile_by_label[label] = profile
            profile_var.set(labels[0])
            ttk.Label(body, text="Профиль карты", style="CardMuted.TLabel").pack(anchor="w", pady=(9, 3))
            ttk.Combobox(body, textvariable=profile_var, values=labels, state="readonly").pack(fill="x")
        parent_var = tk.StringVar(value=str(Path.home()))
        folder_var = tk.StringVar(value=str((map_profiles[0].get("map_aliases") or [item["id"]])[0]) if map_profiles else item["id"])
        ttk.Label(body, text="Папка назначения", style="CardMuted.TLabel").pack(anchor="w", pady=(11, 4))
        path_row = ttk.Frame(body, style="Card.TFrame")
        path_row.pack(fill="x")
        ttk.Entry(path_row, textvariable=parent_var).pack(side="left", fill="x", expand=True)
        def browse_parent():
            selected = filedialog.askdirectory(title="Выбери родительскую папку", initialdir=parent_var.get(), parent=dlg)
            if selected:
                parent_var.set(selected)
        ttk.Button(path_row, text="Обзор…", command=browse_parent).pack(side="left", padx=(7, 0))
        ttk.Label(body, text="Имя создаваемой папки", style="CardMuted.TLabel").pack(anchor="w", pady=(9, 4))
        ttk.Entry(body, textvariable=folder_var).pack(fill="x")
        actions = ttk.Frame(body, style="Card.TFrame")
        actions.pack(fill="x", pady=(15, 0))
        ttk.Button(actions, text="Отмена", command=dlg.destroy).pack(side="right")

        def export():
            from pack_system import PackManager as _PackManager
            parent = Path(parent_var.get()).expanduser()
            folder_name = folder_var.get().strip()
            try:
                component = _PackManager._safe_folder_component(folder_name)
                destination = parent / component
                if len(map_profiles) > 1:
                    selected_profile = profile_by_label.get(profile_var.get())
                    if not selected_profile:
                        raise ValueError("Выбери профиль карты")
                    selected_profile_id = selected_profile.get("id")
                else:
                    selected_profile_id = map_profiles[0].get("id") if map_profiles else None
                overwrite = False
                if destination.exists():
                    if not destination.is_dir():
                        raise ValueError("По выбранному пути уже существует файл, а нужна папка.")
                    overwrite = messagebox.askyesno(
                        "Папка уже существует",
                        f"Полностью заменить содержимое папки?\n\n{destination}\n\nВсе существующие файлы внутри неё будут удалены.",
                        parent=dlg,
                    )
                    if not overwrite:
                        return
                result = self.pack_manager.export_pack_to_folder(
                    pack_id, destination, mode=mode_var.get(), map_profile_id=selected_profile_id,
                    include_categories=bool(categories_var.get()), overwrite=overwrite,
                )
                dlg.destroy()
                self.log(f"Пак «{item['name']}» экспортирован в папку: {result} ({mode_var.get()})")
                detail = ("Полная структура пакета сохранена; папку можно снова превратить в .aipack." if mode_var.get() == "package"
                          else "Восстановлена обычная аудиотека. Настройки, назначения транспорта и ручные переопределения остаются в пакете и в эту папку не включаются.")
                messagebox.showinfo("Экспорт завершён", f"{detail}\n\n{result}", parent=self)
            except Exception as exc:
                messagebox.showerror("Не удалось экспортировать в папку", str(exc), parent=dlg)
        ttk.Button(actions, text="Экспортировать", style="Accent.TButton", command=export).pack(side="right", padx=(0, 8))

    def import_audio_pack(self):
        filename = filedialog.askopenfilename(title="Импортировать аудиопак AutoInformant", filetypes=[("AutoInformant Pack", "*.aipack"), ("ZIP", "*.zip"), ("Все файлы", "*.*")], parent=self)
        if not filename:
            return
        try:
            import zipfile
            import json as _json
            with zipfile.ZipFile(filename, "r") as archive:
                manifest = _json.loads(archive.read("manifest.json").decode("utf-8-sig"))
            pack_id = str(manifest.get("id") or "")
            exists = self.pack_manager.pack_directory(pack_id) is not None
            overwrite = False
            if exists:
                overwrite = messagebox.askyesno("Пак уже установлен", f"Пак «{manifest.get('name') or pack_id}» уже установлен.\n\nЗаменить его содержимое? Пользовательские изменения внутри текущей копии могут быть потеряны.", parent=self)
                if not overwrite:
                    return
            imported_id = self.pack_manager.import_pack(filename, overwrite=overwrite)
            self._refresh_packs_page(imported_id)
            self.log(f"Импортирован аудиопак: {manifest.get('name') or imported_id}")
        except Exception as exc:
            messagebox.showerror("Не удалось импортировать пак", f"{exc}", parent=self)

    def import_prepared_packs(self):
        archives = self.pack_manager.exported_files()
        if not archives:
            messagebox.showinfo("Нет подготовленных паков", "В папке packs/exports сейчас нет .aipack файлов.", parent=self)
            return
        if not messagebox.askyesno("Импортировать стартовые паки", f"Найдено пакетов: {len(archives)}.\nИмпортировать все ещё не установленные? Уже установленные версии будут пропущены.", parent=self):
            return
        imported, skipped, errors = [], [], []
        for archive in archives:
            try:
                import zipfile, json as _json
                with zipfile.ZipFile(archive, "r") as zf:
                    manifest = _json.loads(zf.read("manifest.json").decode("utf-8-sig"))
                pack_id = str(manifest.get("id") or "")
                if self.pack_manager.pack_directory(pack_id):
                    skipped.append(manifest.get("name") or pack_id)
                    continue
                self.pack_manager.import_pack(archive)
                imported.append(manifest.get("name") or pack_id)
            except Exception as exc:
                errors.append(f"{archive.name}: {exc}")
        self._refresh_packs_page()
        summary = [f"Импортировано: {len(imported)}", f"Пропущено (уже установлены): {len(skipped)}"]
        if errors:
            summary.append("Ошибки:\n" + "\n".join(errors))
        messagebox.showinfo("Импорт подготовленных паков", "\n".join(summary), parent=self)

    def export_selected_pack(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            messagebox.showinfo("Пак не выбран", "Сначала выбери пак для экспорта.", parent=self)
            return
        item = next((row for row in self.pack_manager.list_packs() if row["id"] == pack_id), None)
        if not item:
            return
        filename = filedialog.asksaveasfilename(title="Экспортировать аудиопак", initialfile=f"{pack_id}.aipack", defaultextension=".aipack", filetypes=[("AutoInformant Pack", "*.aipack")], parent=self)
        if not filename:
            return
        try:
            result = self.pack_manager.export_pack(pack_id, filename)
            messagebox.showinfo("Пак экспортирован", f"Создан файл:\n{result}", parent=self)
            self.log(f"Экспортирован пак «{item['name']}»: {result}")
        except Exception as exc:
            messagebox.showerror("Ошибка экспорта", str(exc), parent=self)

    def remove_selected_pack(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            return
        item = next((row for row in self.pack_manager.list_packs() if row["id"] == pack_id), None)
        if not item:
            return
        if not messagebox.askyesno("Удалить аудиопак", f"Удалить установленный пак «{item['name']}»?\n\nЭкспортированные .aipack файлы и пользовательские музыкальные плейлисты не затрагиваются.", parent=self):
            return
        self.pack_manager.remove_pack(pack_id)
        self._refresh_packs_page()

    def toggle_selected_pack_enabled(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            return
        try:
            self.pack_manager.set_enabled(pack_id, bool(self.pack_enable_var.get()))
            self._refresh_packs_page(pack_id)
        except Exception as exc:
            messagebox.showerror("Не удалось изменить состояние", str(exc), parent=self)

    def edit_selected_pack(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            return
        item = next((row for row in self.pack_manager.list_packs() if row["id"] == pack_id), None)
        if not item:
            return
        dlg = tk.Toplevel(self); dlg.title("Сведения об аудиопаке"); dlg.geometry("520x330"); dlg.minsize(460, 300)
        self._place_dialog(dlg, self, modal=True)
        body = ttk.Frame(dlg, padding=18); body.pack(fill="both", expand=True)
        fields = {}
        for row, (label, key) in enumerate((("Название", "name"), ("Автор", "author"), ("Описание", "description"))):
            ttk.Label(body, text=label).grid(row=row*2, column=0, sticky="w", pady=(5, 3))
            var = tk.StringVar(value=str(item[key] or "")); fields[key] = var
            if key == "description":
                widget = tk.Text(body, height=5, wrap="word", bg="#202127", fg="#eeeeef", insertbackground="white", relief="flat")
                widget.insert("1.0", var.get()); widget.grid(row=row*2+1, column=0, sticky="nsew")
                fields["description_widget"] = widget
            else:
                ttk.Entry(body, textvariable=var).grid(row=row*2+1, column=0, sticky="ew")
        body.columnconfigure(0, weight=1); body.rowconfigure(5, weight=1)
        def save_meta():
            try:
                description = fields["description_widget"].get("1.0", "end").strip()
                self.pack_manager.edit_metadata(pack_id, name=fields["name"].get(), author=fields["author"].get(), description=description)
                dlg.destroy(); self._refresh_packs_page(pack_id)
            except Exception as exc:
                messagebox.showerror("Не удалось сохранить", str(exc), parent=dlg)
        buttons = ttk.Frame(body); buttons.grid(row=6, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Сохранить", style="Accent.TButton", command=save_meta).pack(side="right")
        ttk.Button(buttons, text="Отмена", command=dlg.destroy).pack(side="right", padx=6)

    def change_selected_pack_icon(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            return
        filename = filedialog.askopenfilename(title="Выбрать иконку пакета", filetypes=[("Изображения", "*.png *.jpg *.jpeg *.webp")], parent=self)
        if not filename:
            return
        try:
            self.pack_manager.set_icon(pack_id, filename)
            self._refresh_packs_page(pack_id)
        except Exception as exc:
            messagebox.showerror("Не удалось изменить иконку", str(exc), parent=self)

    def activate_selected_pack_for_current_map(self):
        pack_id = self._selected_pack_id()
        map_name = str(self.map_name or "").strip()
        if not pack_id:
            return
        if not map_name or map_name == "Неизвестная карта":
            messagebox.showinfo("Нет активной карты", "Сначала загрузи карту и duty в openOMSI. После этого можно выбрать основной пак для неё.", parent=self)
            return
        try:
            self.pack_manager.set_active_pack_for_map(map_name, pack_id)
            self._refresh_packs_page(pack_id)
            self.refresh_audio_library()
            self.log(f"Основной аудиопак для карты «{map_name}»: {self.pack_manager.pack_for_profile(pack_id).get('name')}")
        except Exception as exc:
            messagebox.showerror("Не удалось активировать пак", str(exc), parent=self)

    def assign_current_vehicle_to_category(self):
        pack_id = self._selected_pack_id()
        if not pack_id:
            return
        if not self.map_name or self.map_name == "Неизвестная карта":
            messagebox.showinfo("Карта не загружена", "Сначала загрузи карту и транспорт в openOMSI, чтобы назначение сохранилось для правильной карты.", parent=self)
            return
        manufacturer = str(self.current_state.get("vehicle_manufacturer") or "").strip()
        model = str(self.current_state.get("vehicle_model") or "").strip()
        if not manufacturer and not model:
            messagebox.showinfo("Транспорт не определён", "Подключи openOMSI и выбери автобус/троллейбус. Модель будет взята из телеметрии игры, вручную придумывать её не нужно.", parent=self)
            return
        manifest = self.pack_manager.pack_for_profile(pack_id) or {}
        categories = manifest.get("category_profiles", [])
        if not categories:
            messagebox.showinfo("Категорий нет", "В выбранном паке нет категорийных аудиобиблиотек.", parent=self)
            return
        if len(categories) == 1:
            chosen = categories[0]
        else:
            dlg = tk.Toplevel(self); dlg.title("Выбрать категорию"); dlg.geometry("400x180")
            self._place_dialog(dlg, self, modal=True)
            body = ttk.Frame(dlg, padding=16); body.pack(fill="both", expand=True)
            ttk.Label(body, text="К какой аудиокатегории назначить текущую модель?").pack(anchor="w", pady=(0, 8))
            names = {str(c.get("name") or c.get("id")): c for c in categories}
            choice = tk.StringVar(value=next(iter(names)))
            ttk.Combobox(body, textvariable=choice, values=list(names), state="readonly").pack(fill="x")
            def apply():
                try:
                    category = names[choice.get()]
                    self.pack_manager.set_active_category_for_vehicle(pack_id, category["id"], self.map_name, manufacturer, model)
                    dlg.destroy(); self._refresh_packs_page(pack_id); self.refresh_audio_library()
                    self.log(f"Транспорт {manufacturer} | {model} назначен и переключён на категорию {category.get('name')}")
                except Exception as exc: messagebox.showerror("Ошибка назначения", str(exc), parent=dlg)
            ttk.Button(body, text="Назначить", style="Accent.TButton", command=apply).pack(anchor="e", pady=(10, 0))
            return
        try:
            self.pack_manager.set_active_category_for_vehicle(pack_id, chosen["id"], self.map_name, manufacturer, model)
            self._refresh_packs_page(pack_id)
            self.refresh_audio_library()
            self.log(f"Транспорт {manufacturer} | {model} назначен и переключён на категорию «{chosen.get('name') or chosen.get('id')}». Для него эта категория заменяет базовую библиотеку карты.")
        except Exception as exc:
            messagebox.showerror("Ошибка назначения", str(exc), parent=self)

    def _build_music_page(self, page):
        hero = ttk.Frame(page, style="Hero.TFrame", padding=(18, 13))
        hero.pack(fill="x", pady=(0, 10))
        ttk.Label(hero, text="Музыка и интернет-радио", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(hero, text="Музыка с ПК, интернет-радио и Яндекс Музыка — через единый игровой аудиоканал.", style="HeroSubtitle.TLabel").pack(anchor="w", pady=(3, 0))
        music_panel = self._ui_colors["panel"]
        music_shell = tk.Frame(page, bg=music_panel, highlightthickness=0, bd=0)
        music_shell.pack(fill="both", expand=True)
        tab_bar = tk.Frame(music_shell, bg=music_panel, highlightthickness=0, bd=0)
        tab_bar.pack(fill="x", padx=12, pady=(10, 0))
        tab_content = tk.Frame(music_shell, bg=music_panel, highlightthickness=0, bd=0)
        tab_content.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        local_tab = ttk.Frame(tab_content, style="Card.TFrame", padding=12)
        radio_tab = ttk.Frame(tab_content, style="Card.TFrame", padding=12)
        yandex_tab = ttk.Frame(tab_content, style="Card.TFrame", padding=12)
        active_music_tab = {"name": "local"}
        tab_defs = {}
        def show_music_tab(name):
            active_music_tab["name"] = name
            for tab in (local_tab, radio_tab, yandex_tab):
                tab.pack_forget()
            {"local": local_tab, "radio": radio_tab, "yandex": yandex_tab}[name].pack(fill="both", expand=True)
            for tab_name, button in tab_defs.items():
                button.configure(style="MusicTabActive.TButton" if tab_name == name else "MusicTab.TButton")
            if name == "yandex":
                self._yandex_on_tab_opened()
        for name, title in (("local", "Музыка с ПК"), ("radio", "Интернет-радио"), ("yandex", "Яндекс Музыка")):
            button = ttk.Button(tab_bar, text=title, style="MusicTabActive.TButton" if name == "local" else "MusicTab.TButton",
                                command=lambda value=name: show_music_tab(value))
            button.pack(side="left", padx=(0, 5))
            tab_defs[name] = button
        local_tab_button = tab_defs["local"]
        radio_tab_button = tab_defs["radio"]
        yandex_tab_button = tab_defs["yandex"]
        local_tab.configure(style="Card.TFrame")
        radio_tab.configure(style="Card.TFrame")
        yandex_tab.configure(style="Card.TFrame")
        local_tab.pack(fill="both", expand=True)
        ttk.Label(local_tab, text="Локальная музыка звучит отдельным 3D-источником в openOMSI; объявления могут звучать поверх.",
                  style="CardMuted.TLabel", wraplength=900).pack(anchor="w", pady=(0, 8))
        local_frame = ttk.Frame(local_tab, style="Card.TFrame"); local_frame.pack(fill="both", expand=True)
        self._music_local_tree = ttk.Treeview(local_frame, columns=("title", "path"), show="headings", selectmode="browse")
        self._music_local_tree.heading("title", text="Трек")
        self._music_local_tree.heading("path", text="Файл")
        self._music_local_tree.column("title", width=220)
        self._music_local_tree.column("path", width=600)
        local_scroll = ttk.Scrollbar(local_frame, orient="vertical", command=self._music_local_tree.yview)
        self._music_local_tree.configure(yscrollcommand=local_scroll.set)
        self._music_local_tree.pack(side="left", fill="both", expand=True); local_scroll.pack(side="right", fill="y")
        def refill_local():
            self._music_local_tree.delete(*self._music_local_tree.get_children())
            for idx, path in enumerate(self.music_playlist):
                self._music_local_tree.insert("", "end", iid=str(idx), values=(path.stem, str(path)))
        self._refill_local_music = refill_local
        refill_local()
        local_buttons = ttk.Frame(local_tab, style="Card.TFrame"); local_buttons.pack(fill="x", pady=(8, 0))
        def add_files():
            names = filedialog.askopenfilenames(title="Добавить музыкальные файлы", filetypes=[("Аудио", "*.mp3 *.wav *.ogg *.flac *.m4a *.aac *.opus"), ("Все файлы", "*.*")], parent=self)
            changed = False
            for name in names:
                path = Path(name).resolve()
                if path.is_file() and path not in self.music_playlist:
                    self.music_playlist.append(path); changed = True
            if changed: self._save_music_playlist(); refill_local()
        def scan_folder():
            name = filedialog.askdirectory(title="Сканировать папку с музыкой", parent=self)
            if not name: return
            root = Path(name); excluded = {"cache", "__pycache__"}; found = []
            for path in root.rglob("*"):
                try:
                    if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and not any(part.lower() in excluded for part in path.parts):
                        found.append(path.resolve())
                except OSError: pass
            known = {str(p).casefold() for p in self.music_playlist}
            self.music_playlist.extend(p for p in sorted(found, key=lambda p: str(p).casefold()) if str(p).casefold() not in known)
            self._save_music_playlist(); refill_local()
            self._music_log(f"сканирование завершено, новых треков: {len({str(p).casefold() for p in self.music_playlist}) - len(known)}")
        def remove_track():
            selected = self._music_local_tree.selection()
            if not selected: return
            idx = int(selected[0])
            if 0 <= idx < len(self.music_playlist):
                self.music_playlist.pop(idx); self._save_music_playlist(); refill_local()
        ttk.Button(local_buttons, text="Добавить файлы…", command=add_files).pack(side="left")
        ttk.Button(local_buttons, text="Сканировать папку…", command=scan_folder).pack(side="left", padx=5)
        ttk.Button(local_buttons, text="Убрать", command=remove_track).pack(side="left")
        ttk.Button(local_buttons, text="▶ Играть", command=self._play_selected_local_music).pack(side="right")
        ttk.Button(local_buttons, text="Следующий ▶", command=self.next_music_track).pack(side="right", padx=5)
        ttk.Button(local_buttons, text="◀ Предыдущий", command=self.previous_music_track).pack(side="right", padx=5)
        ttk.Button(local_buttons, text="■ Стоп", command=self.stop_music).pack(side="right", padx=5)

        ttk.Label(radio_tab, text="Вставляй прямой HTTP(S) URL аудиопотока (MP3/AAC/Ogg/HLS — если поддерживается установленным FFmpeg). Ссылки на страницы веб-плееров не являются прямыми потоками.",
                  style="CardMuted.TLabel", wraplength=900).pack(anchor="w", pady=(0, 8))
        radio_frame = ttk.Frame(radio_tab, style="Card.TFrame"); radio_frame.pack(fill="both", expand=True)
        self._music_radio_tree = ttk.Treeview(radio_frame, columns=("name", "url"), show="headings", selectmode="browse")
        self._music_radio_tree.heading("name", text="Станция"); self._music_radio_tree.heading("url", text="URL потока")
        self._music_radio_tree.column("name", width=200); self._music_radio_tree.column("url", width=650)
        radio_scroll = ttk.Scrollbar(radio_frame, orient="vertical", command=self._music_radio_tree.yview)
        self._music_radio_tree.configure(yscrollcommand=radio_scroll.set)
        self._music_radio_tree.pack(side="left", fill="both", expand=True); radio_scroll.pack(side="right", fill="y")
        stations = [station for station in (self._normalise_radio_station(x) for x in self.settings.get("radio_stations", [])) if station]
        self.settings["radio_stations"] = stations
        def refill_radio():
            self._music_radio_tree.delete(*self._music_radio_tree.get_children())
            for idx, station in enumerate(stations):
                self._music_radio_tree.insert("", "end", iid=str(idx), values=(station["name"], station["url"]))
        refill_radio()
        def station_dialog(index=None):
            existing = stations[index] if index is not None and 0 <= index < len(stations) else None
            dlg = tk.Toplevel(self)
            dlg.title("Изменить радиостанцию" if existing else "Добавить радиостанцию")
            dlg.geometry("520x240"); dlg.minsize(460, 220)
            self._place_dialog(dlg, self, modal=True)
            body = ttk.Frame(dlg, padding=18); body.pack(fill="both", expand=True)
            ttk.Label(body, text="Название станции").grid(row=0, column=0, sticky="w", pady=(0, 5))
            name_var = tk.StringVar(value=(existing or {}).get("name", ""))
            name_entry = ttk.Entry(body, textvariable=name_var); name_entry.grid(row=1, column=0, sticky="ew", pady=(0, 12))
            ttk.Label(body, text="Прямой URL аудиопотока (HTTP или HTTPS)").grid(row=2, column=0, sticky="w", pady=(0, 5))
            url_var = tk.StringVar(value=(existing or {}).get("url", ""))
            url_entry = ttk.Entry(body, textvariable=url_var); url_entry.grid(row=3, column=0, sticky="ew", pady=(0, 14))
            body.columnconfigure(0, weight=1)
            def save_station():
                name, url = name_var.get().strip(), url_var.get().strip()
                if not name:
                    messagebox.showerror("Не указано название", "Введи название радиостанции.", parent=dlg); return
                if not url.lower().startswith(("http://", "https://")):
                    messagebox.showerror("Неверный URL", "Нужна прямая ссылка HTTP(S) на аудиопоток, а не страница веб-плеера.", parent=dlg); return
                station = {"name": name, "url": url}
                if existing: stations[index] = station
                else:
                    stations.append(station); self.radio_station_index = len(stations) - 1
                self.settings["radio_stations"] = stations; save_settings(self.settings); refill_radio()
                selected_index = index if existing else len(stations) - 1
                self._music_radio_tree.selection_set(str(selected_index)); self._music_radio_tree.see(str(selected_index)); dlg.destroy()
                try:
                    changed, status = sync_openomsi_radio_cfg(stations)
                    if changed: self._music_log(f"обновлён список станций openOMSI: {status}; игра перечитает его при следующем запуске")
                except Exception as exc: self._music_log(f"не удалось обновить radio.cfg: {exc}")
            buttons = ttk.Frame(body); buttons.grid(row=4, column=0, sticky="e")
            ttk.Button(buttons, text="Сохранить", style="Accent.TButton", command=save_station).pack(side="right")
            ttk.Button(buttons, text="Отмена", command=dlg.destroy).pack(side="right", padx=6)
            dlg.bind("<Return>", lambda _e: save_station()); name_entry.focus_set()
        def edit_station(_event=None):
            selected = self._music_radio_tree.selection()
            if not selected:
                messagebox.showinfo("Выбери станцию", "Выбери радиостанцию, которую нужно изменить.", parent=self); return
            station_dialog(int(selected[0]))
        def remove_station():
            selected = self._music_radio_tree.selection()
            if not selected: return
            idx = int(selected[0])
            if self.music_mode == "radio" and self.radio_station_index == idx: self.stop_music()
            stations.pop(idx); self.settings["radio_stations"] = stations; save_settings(self.settings); refill_radio()
            self.radio_station_index = min(self.radio_station_index, len(stations) - 1)
        self._music_radio_tree.bind("<Double-1>", edit_station)
        ttk.Button(radio_tab, text="Добавить станцию…", command=lambda: station_dialog()).pack(side="left", pady=(8, 0))
        ttk.Button(radio_tab, text="Изменить…", command=edit_station).pack(side="left", padx=5, pady=(8, 0))
        ttk.Button(radio_tab, text="Удалить", command=remove_station, style="Danger.TButton").pack(side="left", padx=2, pady=(8, 0))
        ttk.Button(radio_tab, text="▶ Играть", command=self._play_selected_radio).pack(side="right", pady=(8, 0))
        ttk.Button(radio_tab, text="■ Стоп", command=self.stop_music).pack(side="right", padx=5, pady=(8, 0))
        ttk.Label(radio_tab, text="Радио использует встроенный плеер openOMSI, если станция уже есть в radio.cfg. Shift+R остаётся горячей клавишей игры.",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(8, 0))
        self._build_yandex_tab(yandex_tab)

    def _build_yandex_tab(self, parent):
        """Build an in-app Yandex Music client UI. All API work runs off the Tk thread."""
        top = ttk.Frame(parent, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 9))
        self._yandex_profile_label = ttk.Label(top, text="Яндекс ID не подключён", style="CardTitle.TLabel")
        self._yandex_profile_label.pack(side="left", anchor="w")
        self._yandex_status_label = ttk.Label(top, text="Не авторизован", style="CardMuted.TLabel")
        self._yandex_status_label.pack(side="left", padx=(12, 0))
        ttk.Button(top, text="Войти через Яндекс", style="Accent.TButton", command=self._yandex_start_login).pack(side="right")
        ttk.Button(top, text="Выйти", command=self._yandex_logout).pack(side="right", padx=5)
        ttk.Button(top, text="Обновить", command=self._yandex_restore_saved_login).pack(side="right", padx=5)

        controls = ttk.Frame(parent, style="Card.TFrame")
        controls.pack(fill="x", pady=(0, 9))
        self._yandex_search_var = tk.StringVar()
        self._yandex_search_kind = tk.StringVar(value="Треки")
        search_entry = ttk.Entry(controls, textvariable=self._yandex_search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        search_entry.bind("<Return>", lambda _e: self._yandex_search())
        kind_box = ttk.Combobox(controls, textvariable=self._yandex_search_kind, state="readonly", width=13,
                                values=("Треки", "Исполнители", "Альбомы", "Плейлисты"))
        kind_box.pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Найти", style="Accent.TButton", command=self._yandex_search).pack(side="left")

        quick = ttk.Frame(parent, style="Card.TFrame")
        quick.pack(fill="x", pady=(0, 9))
        ttk.Button(quick, text="♥ Мне нравится", command=self._yandex_load_liked).pack(side="left")
        ttk.Button(quick, text="☷ Плейлисты", command=self._yandex_load_playlists).pack(side="left", padx=4)
        ttk.Button(quick, text="▣ Альбомы", command=self._yandex_load_liked_albums).pack(side="left", padx=4)
        ttk.Button(quick, text="♟ Исполнители", command=self._yandex_load_liked_artists).pack(side="left", padx=4)
        ttk.Button(quick, text="◉ Моя волна", style="Accent.TButton", command=lambda: self._yandex_start_wave("discover")).pack(side="left", padx=(8, 4))
        ttk.Button(quick, text="♥ Волна — Любимое", command=lambda: self._yandex_start_wave("favorite")).pack(side="left")

        player = ttk.Frame(parent, style="Card.TFrame")
        player.pack(side="bottom", fill="x", pady=(9, 0))
        now_wrap = ttk.Frame(player, style="Card.TFrame")
        now_wrap.pack(side="left", fill="x", expand=True)
        self._yandex_now_label = ttk.Label(now_wrap, text="Ничего не воспроизводится", style="CardTitle.TLabel")
        self._yandex_now_label.pack(anchor="w", fill="x")
        self._yandex_progress = ttk.Progressbar(now_wrap, mode="indeterminate", maximum=100, length=220)
        self._yandex_progress.pack(anchor="w", fill="x", pady=(5, 0))
        self._yandex_time_label = ttk.Label(now_wrap, text="—:— / —:—", style="CardMuted.TLabel")
        self._yandex_time_label.pack(anchor="w", pady=(2, 0))
        ttk.Button(player, text="♥", width=3, command=self._yandex_toggle_like).pack(side="right", padx=3)
        self._yandex_queue_button = ttk.Button(player, text="Очередь (0)", command=self._yandex_show_queue)
        self._yandex_queue_button.pack(side="right", padx=3)
        ttk.Button(player, text="В очередь", command=self._yandex_add_selected_to_queue).pack(side="right", padx=3)
        ttk.Button(player, text="Следующий ▶", command=self._yandex_next).pack(side="right", padx=3)
        ttk.Button(player, text="◀ Предыдущий", command=self._yandex_previous).pack(side="right", padx=3)
        ttk.Button(player, text="■ Стоп", command=self.stop_music).pack(side="right", padx=3)
        ttk.Button(player, text="▶ Играть", style="Accent.TButton", command=self._yandex_play_selected).pack(side="right", padx=3)

        results_panel = ttk.Frame(parent, style="Card.TFrame")
        results_panel.pack(fill="both", expand=True)
        self._yandex_result_title = ttk.Label(results_panel, text="Найди музыку или открой свою коллекцию", style="CardTitle.TLabel")
        self._yandex_result_title.pack(anchor="w", pady=(0, 7))
        table_wrap = ttk.Frame(results_panel, style="Card.TFrame")
        table_wrap.pack(fill="both", expand=True)
        self._yandex_tree = ttk.Treeview(table_wrap, columns=("title", "artist", "album", "duration"),
                                         show="headings", selectmode="browse")
        self._yandex_tree.heading("title", text="Название")
        self._yandex_tree.heading("artist", text="Исполнитель")
        self._yandex_tree.heading("album", text="Альбом / тип")
        self._yandex_tree.heading("duration", text="Время")
        self._yandex_tree.column("title", width=280, minwidth=160, stretch=True)
        self._yandex_tree.column("artist", width=220, minwidth=130, stretch=True)
        self._yandex_tree.column("album", width=220, minwidth=100, stretch=True)
        self._yandex_tree.column("duration", width=90, minwidth=75, stretch=False, anchor="e")
        scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self._yandex_tree.yview)
        self._yandex_tree.configure(yscrollcommand=scroll.set)
        self._yandex_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._yandex_tree.bind("<Double-1>", lambda _e: self._yandex_open_selected())

        self._yandex_tree.bind("<Return>", lambda _e: self._yandex_open_selected())
        self._yandex_tab_ready = True

    def _yandex_set_status(self, text: str, *, profile: str | None = None):
        if self._yandex_status_label and self._yandex_status_label.winfo_exists():
            self._yandex_status_label.configure(text=text)
        if profile is not None and self._yandex_profile_label and self._yandex_profile_label.winfo_exists():
            self._yandex_profile_label.configure(text=profile)

    def _yandex_on_tab_opened(self):
        if not self._yandex_restore_attempted:
            self._yandex_restore_attempted = True
            self._yandex_restore_saved_login(silent=True)

    def _yandex_run_async(self, work, success, *, label="Запрос к Яндексу"):
        def worker():
            try:
                result = work()
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda msg=message: self._yandex_async_error(label, msg))
            else:
                self.after(0, lambda value=result: success(value))
        threading.Thread(target=worker, daemon=True, name="AutoInformant-Yandex-API").start()

    def _yandex_async_error(self, label: str, message: str):
        self._yandex_set_status("Ошибка подключения")
        self._music_log(f"Яндекс: {label}: {message}")
        if self._yandex_now_label and self._yandex_now_label.winfo_exists():
            self._yandex_now_label.configure(text=f"{label}: {message}")

    def _yandex_restore_saved_login(self, silent: bool = False):
        if self.yandex_profile or self._yandex_profile_busy:
            return
        self._yandex_profile_busy = True
        if not silent: self._yandex_set_status("Проверяю сохранённый вход…")
        def work():
            token = self.yandex_service.load_saved_token()
            if not token:
                return None
            return self.yandex_service.connect(token)
        def done(profile):
            self._yandex_profile_busy = False
            if profile:
                self.yandex_profile = profile
                self._yandex_set_status("Подключён", profile=profile.get("display_name") or profile.get("login"))
                self._music_log(f"Яндекс подключён: {profile.get('login')}")
            else:
                self._yandex_set_status("Не авторизован", profile="Яндекс ID не подключён")
        def failed(label, message):
            self._yandex_profile_busy = False
            self._yandex_set_status("Нужен повторный вход")
            self._music_log(f"Яндекс: сохранённая авторизация недоступна: {message}")
        def worker():
            try:
                token = self.yandex_service.load_saved_token()
                profile = self.yandex_service.connect(token) if token else None
            except Exception as exc:
                self.after(0, lambda msg=str(exc): failed("Восстановление", msg))
            else:
                self.after(0, lambda value=profile: done(value))
        threading.Thread(target=worker, daemon=True, name="AutoInformant-Yandex-restore").start()

    def _yandex_start_login(self):
        if not self.yandex_service.library_available():
            messagebox.showerror("Не установлена библиотека Яндекса", "Закрой AutoInformant Pro и запусти run_app.bat. Он установит yandex-music вместе с остальными зависимостями.", parent=self)
            return
        if self._yandex_login_dialog and self._yandex_login_dialog.winfo_exists():
            self._yandex_login_dialog.lift(); return
        cancel_event = threading.Event()
        self._yandex_login_cancel_event = cancel_event
        dlg = tk.Toplevel(self); self._yandex_login_dialog = dlg
        dlg.title("Вход в Яндекс ID")
        dlg.geometry("560x300"); dlg.minsize(500, 270)
        self._place_dialog(dlg, self, modal=True)
        body = ttk.Frame(dlg, padding=18); body.pack(fill="both", expand=True)
        ttk.Label(body, text="Подключение Яндекс Музыки", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="Открой страницу Яндекса и выбери удобный способ входа: QR-код, телефон, приложение или другой доступный вариант.",
                  style="CardMuted.TLabel", wraplength=500).pack(anchor="w", pady=(8, 14))
        code_label = ttk.Label(body, text="Запрашиваю код авторизации…", style="CardTitle.TLabel")
        code_label.pack(anchor="w", pady=(0, 8))
        url_label = ttk.Label(body, text="", style="CardMuted.TLabel", wraplength=500)
        url_label.pack(anchor="w")
        status_label = ttk.Label(body, text="После подтверждения входа подключение завершится автоматически.", style="CardMuted.TLabel")
        status_label.pack(anchor="w", pady=(12, 0))
        buttons = ttk.Frame(body); buttons.pack(fill="x", side="bottom", pady=(16, 0))
        def cancel():
            cancel_event.set()
            try: dlg.grab_release()
            except Exception: pass
            dlg.destroy(); self._yandex_login_dialog = None
        ttk.Button(buttons, text="Отмена", command=cancel).pack(side="right")
        def on_code(code):
            url = str(getattr(code, "verification_url", "") or "")
            value = str(getattr(code, "user_code", "") or "")
            self._yandex_code_url, self._yandex_code_value = url, value
            def show():
                if not dlg.winfo_exists(): return
                code_label.configure(text=f"Код подтверждения: {value or '—'}")
                url_label.configure(text=url or "Открой страницу авторизации из окна программы.")
                ttk.Button(buttons, text="Открыть Яндекс", command=lambda: __import__("webbrowser").open(url) if url else None).pack(side="left")
                ttk.Button(buttons, text="Копировать код", command=lambda: (self.clipboard_clear(), self.clipboard_append(value))).pack(side="left", padx=5)
                if url:
                    try: __import__("webbrowser").open(url)
                    except Exception: pass
            self.after(0, show)
        def worker():
            try:
                profile = self.yandex_service.device_auth(on_code, cancel_event)
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda: status_label.configure(text=f"Не удалось войти: {msg}") if dlg.winfo_exists() else None)
                return
            def done():
                self.yandex_profile = profile
                self._yandex_set_status("Подключён", profile=profile.get("display_name") or profile.get("login"))
                self._music_log(f"Яндекс подключён: {profile.get('login')}")
                if dlg.winfo_exists():
                    try: dlg.grab_release()
                    except Exception: pass
                    dlg.destroy()
                self._yandex_login_dialog = None
                self._yandex_load_liked()
            self.after(0, done)
        threading.Thread(target=worker, daemon=True, name="AutoInformant-Yandex-login").start()
        dlg.protocol("WM_DELETE_WINDOW", cancel)

    def _yandex_logout(self):
        if not self.yandex_profile and not self.yandex_service.token_store.path.exists():
            self._yandex_set_status("Не авторизован", profile="Яндекс ID не подключён"); return
        if self.music_mode == "yandex": self.stop_music()
        try:
            self.yandex_service.disconnect()
        except Exception as exc:
            messagebox.showerror("Не удалось выйти", str(exc), parent=self); return
        self.yandex_profile = None; self.yandex_results = []; self.yandex_queue = []; self.yandex_queue_index = -1
        self._yandex_show_rows([], "Войдите в аккаунт, чтобы открыть музыку")
        self._yandex_set_status("Не авторизован", profile="Яндекс ID не подключён")
        if self._yandex_now_label: self._yandex_now_label.configure(text="Ничего не воспроизводится")
        self._music_log("выход из аккаунта Яндекса; локальный токен удалён")

    @staticmethod
    def _yandex_duration(value):
        seconds = max(0, int((int(value or 0) + 500) / 1000))
        return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "—"

    def _yandex_show_rows(self, rows, title="Результаты"):
        self.yandex_results = list(rows or [])
        if not self._yandex_tree or not self._yandex_tree.winfo_exists(): return
        self._yandex_tree.delete(*self._yandex_tree.get_children())
        for idx, row in enumerate(self.yandex_results):
            kind = row.get("kind", "track")
            title_text = row.get("title", "Без названия")
            artist = row.get("artist", "")
            album = row.get("album", "")
            duration = self._yandex_duration(row.get("duration_ms", 0))
            if kind == "playlist": album = "Плейлист"
            elif kind == "artist": album = "Исполнитель"
            elif kind == "album": album = "Альбом"
            self._yandex_tree.insert("", "end", iid=str(idx), values=(title_text, artist, album, duration))
        if self._yandex_result_title and self._yandex_result_title.winfo_exists():
            self._yandex_result_title.configure(text=f"{title} · {len(self.yandex_results)}")

    def _yandex_search(self):
        if not self.yandex_profile:
            messagebox.showinfo("Сначала войди", "Подключи свой Яндекс ID, чтобы искать музыку.", parent=self); return
        query = self._yandex_search_var.get().strip() if self._yandex_search_var else ""
        if not query: return
        kinds = {"Треки": "track", "Исполнители": "artist", "Альбомы": "album", "Плейлисты": "playlist"}
        kind = kinds.get(self._yandex_search_kind.get(), "track")
        self._yandex_set_status("Ищу…")
        self._yandex_run_async(lambda: self.yandex_service.search(query, kind),
            lambda rows: (self._yandex_show_rows(rows, f"Поиск: {query}"), self._yandex_set_status("Подключён")), label="Поиск")

    def _yandex_load_liked(self):
        if not self.yandex_profile:
            messagebox.showinfo("Сначала войди", "Подключи свой Яндекс ID.", parent=self); return
        self._yandex_set_status("Загружаю понравившиеся треки…")
        self._yandex_run_async(self.yandex_service.liked_tracks,
            lambda rows: (self._yandex_show_rows(rows, "Мне нравится"), self._yandex_set_status("Подключён")), label="Мне нравится")

    def _yandex_load_playlists(self):
        if not self.yandex_profile:
            messagebox.showinfo("Сначала войди", "Подключи свой Яндекс ID.", parent=self); return
        self._yandex_set_status("Загружаю плейлисты…")
        self._yandex_run_async(self.yandex_service.playlists,
            lambda rows: (self._yandex_show_rows(rows, "Мои плейлисты"), self._yandex_set_status("Подключён")), label="Плейлисты")

    def _yandex_load_liked_albums(self):
        if not self.yandex_profile:
            messagebox.showinfo("Сначала войди", "Подключи свой Яндекс ID.", parent=self); return
        self._yandex_set_status("Загружаю понравившиеся альбомы…")
        self._yandex_run_async(self.yandex_service.liked_albums,
            lambda rows: (self._yandex_show_rows(rows, "Мои альбомы"), self._yandex_set_status("Подключён")),
            label="Понравившиеся альбомы")

    def _yandex_load_liked_artists(self):
        if not self.yandex_profile:
            messagebox.showinfo("Сначала войди", "Подключи свой Яндекс ID.", parent=self); return
        self._yandex_set_status("Загружаю понравившихся исполнителей…")
        self._yandex_run_async(self.yandex_service.liked_artists,
            lambda rows: (self._yandex_show_rows(rows, "Мои исполнители"), self._yandex_set_status("Подключён")),
            label="Понравившиеся исполнители")

    def _yandex_selected(self):
        if not self._yandex_tree: return None
        selected = self._yandex_tree.selection()
        if not selected: return None
        try: return self.yandex_results[int(selected[0])]
        except (ValueError, IndexError): return None

    def _yandex_open_selected(self):
        row = self._yandex_selected()
        if not row: return
        kind = row.get("kind", "track")
        if kind == "track": self._yandex_play_selected(); return
        if not self.yandex_profile: return
        if kind == "playlist":
            work = lambda: self.yandex_service.playlist_tracks(row.get("id"), row.get("owner_id") or None)
            title = f"Плейлист: {row.get('title')}"
        elif kind == "album":
            work = lambda: self.yandex_service.album_tracks(row.get("id"))
            title = f"Альбом: {row.get('title')}"
        elif kind == "artist":
            work = lambda: self.yandex_service.artist_tracks(row.get("id"))
            title = f"Исполнитель: {row.get('title')}"
        else: return
        self._yandex_set_status("Загружаю…")
        def done(rows):
            self._yandex_show_rows(rows, title)
            self._yandex_set_status("Подключён")
        self._yandex_run_async(work, done, label="Загрузка содержимого")

    def _yandex_play_selected(self):
        row = self._yandex_selected()
        if not row: return
        if row.get("kind", "track") != "track":
            self._yandex_open_selected(); return
        tracks = [r for r in self.yandex_results if r.get("kind", "track") == "track"]
        index = tracks.index(row) if row in tracks else 0
        self.yandex_queue = tracks
        self.yandex_queue_index = index
        self.yandex_queue_source = "manual"
        self.yandex_auto_next = True
        self._yandex_update_queue_button()
        self._yandex_start_current_track()

    def _yandex_add_selected_to_queue(self):
        row = self._yandex_selected()
        if not row or row.get("kind", "track") != "track": return
        self.yandex_queue.append(row)
        self._yandex_update_queue_button()
        self._music_log(f"добавлено в очередь Яндекс: {row.get('artist')} — {row.get('title')}")

    def _yandex_update_queue_button(self):
        button = getattr(self, "_yandex_queue_button", None)
        if button is not None:
            try:
                if button.winfo_exists():
                    count = max(0, len(self.yandex_queue) - max(0, self.yandex_queue_index + 1))
                    button.configure(text=f"Очередь ({count})")
            except Exception:
                pass

    def _yandex_show_queue(self):
        dlg = tk.Toplevel(self)
        dlg.title("Очередь Яндекс Музыки")
        dlg.geometry("700x480"); dlg.minsize(560, 360)
        self._place_dialog(dlg, self, modal=False)
        body = ttk.Frame(dlg, padding=14); body.pack(fill="both", expand=True)
        ttk.Label(body, text="Очередь воспроизведения", style="HeroTitle.TLabel").pack(anchor="w", pady=(0, 8))
        wrap = ttk.Frame(body); wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=("title", "artist", "state"), show="headings", selectmode="browse")
        tree.heading("title", text="Композиция"); tree.heading("artist", text="Исполнитель"); tree.heading("state", text="Статус")
        tree.column("title", width=280, minwidth=150); tree.column("artist", width=230, minwidth=120); tree.column("state", width=100, minwidth=80, anchor="center")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview); tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        def refill():
            tree.delete(*tree.get_children())
            for index, item in enumerate(self.yandex_queue):
                state = "Сейчас играет" if index == self.yandex_queue_index and self.music_mode == "yandex" else ("Уже сыграл" if index < self.yandex_queue_index else "Ожидает")
                tree.insert("", "end", iid=str(index), values=(item.get("title", "—"), item.get("artist", "—"), state))
            self._yandex_update_queue_button()
        def play_selected():
            sel = tree.selection()
            if not sel: return
            index = int(sel[0])
            if 0 <= index < len(self.yandex_queue):
                self.yandex_queue_index = index
                self.yandex_queue_source = "manual"
                self._yandex_start_current_track(); refill()
        def remove_selected():
            sel = tree.selection()
            if not sel: return
            index = int(sel[0])
            if index == self.yandex_queue_index and self.music_mode == "yandex":
                messagebox.showinfo("Текущий трек", "Сначала останови воспроизведение или переключись на другой трек.", parent=dlg); return
            if 0 <= index < len(self.yandex_queue):
                self.yandex_queue.pop(index)
                if index < self.yandex_queue_index: self.yandex_queue_index -= 1
                refill()
        def clear_upcoming():
            current = self.yandex_queue_index if self.music_mode == "yandex" else -1
            if current >= 0:
                self.yandex_queue = self.yandex_queue[:current + 1]
            else:
                self.yandex_queue = []; self.yandex_queue_index = -1
            refill()
        tree.bind("<Double-1>", lambda _e: play_selected())
        buttons = ttk.Frame(body); buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Играть выбранный", style="Accent.TButton", command=play_selected).pack(side="left")
        ttk.Button(buttons, text="Убрать выбранный", command=remove_selected).pack(side="left", padx=5)
        ttk.Button(buttons, text="Очистить будущие", command=clear_upcoming).pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=dlg.destroy).pack(side="right")
        refill()

    def _yandex_reset_wave_prefetch(self, *, new_session: bool = False) -> None:
        with self._yandex_wave_prefetch_lock:
            if new_session:
                self._yandex_wave_generation += 1
            self._yandex_wave_prefetch_event = threading.Event()
            self._yandex_wave_prefetch_event.set()
            self._yandex_wave_prefetch_started = False
            self._yandex_wave_prefetch_rows = None
            self._yandex_wave_prefetch_error = None

    def _yandex_prefetch_wave_next(self) -> None:
        """Ask Rotor for the next batch while the tail of the current batch plays."""
        if self.yandex_queue_source not in {"wave", "favorite_wave"}:
            return
        if not self.music_active or self.music_mode != "yandex":
            return
        # Fetch on the final track, after the previous four tracks have already
        # reported their completion to Rotor, but while this track is still playing.
        if self.yandex_queue_index < max(0, len(self.yandex_queue) - 1):
            return
        with self._yandex_wave_prefetch_lock:
            if self._yandex_wave_prefetch_started:
                return
            generation = self._yandex_wave_generation
            event = self._yandex_wave_prefetch_event
            event.clear()
            self._yandex_wave_prefetch_started = True
            self._yandex_wave_prefetch_rows = None
            self._yandex_wave_prefetch_error = None

        def worker():
            try:
                rows = self.yandex_service.wave_next()
                error = None
            except Exception as exc:
                rows = None
                error = str(exc)
            with self._yandex_wave_prefetch_lock:
                if generation != self._yandex_wave_generation:
                    return
                self._yandex_wave_prefetch_rows = list(rows or []) if error is None else None
                self._yandex_wave_prefetch_error = error
                event.set()
            if error:
                self.after(0, lambda msg=error: self._music_log(f"не удалось заранее получить следующую партию «Моей волны»: {msg}"))
            elif rows:
                self.after(0, lambda count=len(rows): self._music_log(f"следующая партия «Моей волны» подготовлена: {count} треков"))
        threading.Thread(target=worker, daemon=True, name="AutoInformant-Yandex-wave-prefetch").start()

    def _yandex_take_prefetched_wave_rows(self, timeout: float = 30.0):
        """Wait off the UI thread for a preloaded batch; return None when none exists."""
        with self._yandex_wave_prefetch_lock:
            if not self._yandex_wave_prefetch_started:
                return None
            event = self._yandex_wave_prefetch_event
            generation = self._yandex_wave_generation
        if not event.wait(max(0.0, float(timeout))):
            raise YandexMusicError("Запрос следующей партии «Моей волны» не завершился вовремя. Попробуй переключить трек ещё раз.")
        with self._yandex_wave_prefetch_lock:
            if generation != self._yandex_wave_generation:
                return None
            rows = self._yandex_wave_prefetch_rows
            error = self._yandex_wave_prefetch_error
            self._yandex_wave_prefetch_started = False
            self._yandex_wave_prefetch_rows = None
            self._yandex_wave_prefetch_error = None
            self._yandex_wave_prefetch_event = threading.Event()
            self._yandex_wave_prefetch_event.set()
        if error:
            self._music_log(f"предзагрузка «Моей волны» завершилась ошибкой: {error}; повторяю запрос при переходе")
            return None
        return list(rows or [])

    def _yandex_start_wave(self, diversity: str):
        if not self.yandex_profile:
            messagebox.showinfo("Сначала войди", "Подключи свой Яндекс ID, чтобы запустить волну.", parent=self); return
        previous_track = dict(self.yandex_current_track) if self.music_mode == "yandex" and self.yandex_current_track else None
        previous_source = self.yandex_current_source or self.yandex_queue_source
        previous_elapsed = max(0.0, time.monotonic() - self.yandex_track_started_at) if previous_track and self.yandex_track_started_at else 0.0
        self.stop_music(yandex_feedback=False)
        self.yandex_queue = []; self.yandex_queue_index = -1
        self._yandex_reset_wave_prefetch(new_session=True)
        self.yandex_queue_source = "favorite_wave" if diversity == "favorite" else "wave"
        self.yandex_wave_mode = diversity
        self._yandex_update_queue_button()
        self._yandex_set_status("Запрашиваю персональную волну…")
        def work():
            # Close an earlier radio session before replacing it with a new one.
            if previous_track and previous_source in {"wave", "favorite_wave"}:
                self.yandex_service.wave_track_skipped(previous_track, previous_elapsed)
            return self.yandex_service.wave_start(diversity)
        def done(rows):
            self._yandex_set_status("Подключён")
            if not rows:
                self._yandex_async_error("Моя волна", "Яндекс не вернул доступных треков для этой волны.")
                return
            self.yandex_queue = rows
            self.yandex_queue_index = 0
            self._yandex_show_rows(rows, "Моя волна — Любимое" if diversity == "favorite" else "Моя волна")
            if self._yandex_tree: self._yandex_tree.selection_set("0")
            self._yandex_update_queue_button()
            self._yandex_start_current_track()
        self._yandex_run_async(work, done, label="Моя волна")

    def _yandex_start_current_track(self, *, skipped=False):
        if not (0 <= self.yandex_queue_index < len(self.yandex_queue)): return
        track = self.yandex_queue[self.yandex_queue_index]
        ffmpeg = self._resolve_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("FFmpeg недоступен", "Перезапусти run_app.bat, чтобы установить аудиозависимости.", parent=self); return
        if not self._bridge or not self._bridge.enabled:
            messagebox.showwarning("Нет соединения", "Сначала запусти openOMSI и подключи Lua-плагин AutoInformantPro.", parent=self); return
        previous_track = dict(self.yandex_current_track) if self.music_mode == "yandex" and self.yandex_current_track else None
        previous_source = self.yandex_current_source or self.yandex_queue_source
        next_source = self.yandex_queue_source
        previous_elapsed = max(0.0, time.monotonic() - self.yandex_track_started_at) if previous_track and self.yandex_track_started_at else 0.0
        self.stop_music(yandex_feedback=False)
        self._radio_stop_event = threading.Event()
        self._radio_stream_generation += 1
        generation = self._radio_stream_generation
        self.music_mode = "yandex"; self.music_active = True
        self.yandex_current_track = dict(track)
        self.yandex_current_source = next_source
        self.yandex_track_started_at = 0.0
        self._yandex_song_finished_event.clear()
        self._yandex_final_segment_index = None
        self._yandex_final_segment_generation = generation
        title = f"{track.get('artist', '—')} — {track.get('title', 'Трек')}"
        if self._yandex_now_label and self._yandex_now_label.winfo_exists():
            self._yandex_now_label.configure(text=f"Подключаю: {title}")
        if self._yandex_progress and self._yandex_progress.winfo_exists():
            self._yandex_progress.stop()
        if hasattr(self, "_yandex_progress") and self._yandex_progress is not None:
            try: self._yandex_progress.configure(mode="indeterminate"); self._yandex_progress.start(12)
            except Exception: pass
        stop_event = self._radio_stop_event
        def launch_stream():
            # When manually skipping a wave track, send Rotor feedback before asking
            # Yandex for another started event. This preserves session event ordering.
            if previous_track and previous_source in {"wave", "favorite_wave"}:
                try: self.yandex_service.wave_track_skipped(previous_track, previous_elapsed)
                except Exception as exc: self._music_log(f"не удалось отправить пропуск в Мою волну: {exc}")
            self._yandex_stream_worker(dict(track), ffmpeg, generation, stop_event)
        self._radio_stream_thread = threading.Thread(
            target=launch_stream, daemon=True, name="AutoInformant-Yandex-stream")
        self._radio_stream_thread.start()
        self._yandex_update_queue_button()

    def _yandex_stream_worker(self, track, ffmpeg, generation, stop_event):
        """Fetch a short-lived signed URL and feed finite WAV chunks into the same Lua music queue."""
        temp_dir = Path(tempfile.mkdtemp(prefix="aip-yandex-"))
        self._radio_stream_dir = temp_dir
        log_path = temp_dir / "ffmpeg.err"
        pattern = temp_dir / "track_%05d.wav"
        proc = None; log_file = None
        current_payload = next_payload = preloaded_payload = None
        title = f"{track.get('artist', '')} — {track.get('title', '')}"
        def is_current():
            return (not stop_event.is_set() and self.music_active and self.music_mode == "yandex"
                    and generation == self._radio_stream_generation)
        def wait_segment(index, timeout=8.0, *, eof_ok=False):
            path = temp_dir / f"track_{index:05d}.wav"
            deadline = time.monotonic() + timeout
            while is_current():
                if path.is_file() and path.stat().st_size > 44: return True
                if proc is not None and proc.poll() is not None:
                    if path.is_file() and path.stat().st_size > 44: return True
                    if eof_ok: return False
                    raise RuntimeError("Яндекс/FFmpeg завершил поток до появления аудиосегмента.")
                if time.monotonic() >= deadline:
                    if eof_ok and proc is not None and proc.poll() is not None: return False
                    raise RuntimeError("Яндекс-поток не выдал следующий аудиосегмент вовремя.")
                time.sleep(0.035)
            return False
        try:
            if not self.yandex_service.client:
                raise YandexMusicError("Сессия Яндекса потеряна. Войди в аккаунт заново.")
            url, preview, codec = self.yandex_service.stream_url(track)
            # URL is signed and short-lived. Never write it to settings, logs or diagnostics.
            command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-rw_timeout", "15000000",
                       "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
                       "-user_agent", "AutoInformantPro/2.0.2", "-i", url, "-vn",
                       "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1", "-f", "segment",
                       "-segment_time", "8", "-reset_timestamps", "1", "-segment_format", "wav", str(pattern)]
            log_file = log_path.open("w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_file,
                                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            self._radio_stream_proc = proc
            if not wait_segment(0, timeout=25.0):
                raise RuntimeError("Не удалось получить первый фрагмент аудио.")
            # The segment muxer creates a WAV as soon as it starts writing it.
            # Seeing segment N is not proof that it is complete. Wait for N+1
            # (or process EOF) before reading segment N into the game bridge.
            # This first sentinel makes segments 0 and 1 safe to prepare.
            wait_segment(2, timeout=30.0, eof_ok=True)
            has_1 = (temp_dir / "track_00001.wav").is_file() and (temp_dir / "track_00001.wav").stat().st_size > 44
            volume = float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))
            first_path = temp_dir / "track_00000.wav"
            current_payload = self._bridge.prepare_music(first_path, volume, ephemeral=True)
            current_payload["track"] = make_segment_marker(generation, 0)
            if has_1:
                next_payload = self._bridge.prepare_music(temp_dir / "track_00001.wav", volume, ephemeral=True)
                next_payload["track"] = make_segment_marker(generation, 1)
            elif proc.poll() is not None:
                self._yandex_final_segment_index = 0
                self._yandex_final_segment_generation = generation
            for index in range(2):
                (temp_dir / f"track_{index:05d}.wav").unlink(missing_ok=True)
            self._music_started_event.clear(); self._music_finished_event.clear()
            with self._music_worker_lock:
                if not is_current(): return
                self._bridge.start_prepared_music(current_payload)
            if not self._music_started_event.wait(12.0):
                if is_current(): raise RuntimeError("openOMSI не подтвердил запуск Яндекс Музыки.")
                return
            self.after(0, lambda: self._yandex_track_started_ui(track, preview, codec) if is_current() else None)
            current_index = 0
            while is_current() and next_payload is not None:
                self._music_started_event.clear()
                with self._music_worker_lock:
                    if not is_current(): break
                    self._bridge.queue_prepared_music(next_payload)
                if not self._music_started_event.wait(12.0):
                    if is_current(): raise RuntimeError("openOMSI не запустил следующий фрагмент Яндекс Музыки.")
                    break
                old_payload = current_payload
                current_payload = next_payload
                next_payload = None
                if self._bridge: self._bridge.cleanup_prepared_music(old_payload)
                current_index += 1
                # Only now prepare the following segment. The currently playing WAV
                # gives us one whole segment of time to decode/DSP the next one.
                next_index = current_index + 1
                if is_current():
                    # A following segment is fully closed only after the muxer
                    # has opened the segment after it. At EOF, the last segment
                    # is also closed and can be prepared safely.
                    has_sentinel = wait_segment(next_index + 1, timeout=24.0, eof_ok=True)
                    next_path = temp_dir / f"track_{next_index:05d}.wav"
                    has_next = next_path.is_file() and next_path.stat().st_size > 44
                    if has_sentinel or (proc is not None and proc.poll() is not None and has_next):
                        next_payload = self._bridge.prepare_music(next_path, volume, ephemeral=True)
                        next_payload["track"] = make_segment_marker(generation, next_index)
                        next_path.unlink(missing_ok=True)
                        if proc is not None and proc.poll() is not None:
                            self._yandex_final_segment_index = next_index
                            self._yandex_final_segment_generation = generation
                    elif proc is not None and proc.poll() is not None:
                        self._yandex_final_segment_index = current_index
                        self._yandex_final_segment_generation = generation
                        break
            # A finite Yandex track is different from an endless radio stream:
            # keep the final prepared WAV until openOMSI confirms that it finished.
            if self._yandex_final_segment_index is not None and self._yandex_final_segment_generation == generation:
                expected = max(30.0, min(900.0, float(track.get("duration_ms") or 0) / 1000.0 + 20.0))
                deadline = time.monotonic() + expected
                while is_current() and not self._yandex_song_finished_event.wait(0.2):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("openOMSI не подтвердил окончание трека; воспроизведение остановлено для защиты аудиокэша.")
        except Exception as exc:
            message = str(exc)
            if is_current():
                try:
                    if self._bridge: self._bridge.stop_music()
                except Exception: pass
                self.after(0, lambda text=message: self._yandex_track_failed(text, title))
        finally:
            if not is_current():
                # Manual stop/new source: give the plugin a short polling window to
                # consume stop_music before removing WAV cache files it may still use.
                time.sleep(0.75)
            for payload in (current_payload, next_payload, preloaded_payload):
                try:
                    if self._bridge: self._bridge.cleanup_prepared_music(payload)
                except Exception: pass
            if proc and proc.poll() is None:
                try: proc.terminate(); proc.wait(timeout=1.0)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
            if log_file:
                try: log_file.close()
                except Exception: pass
            try:
                for path in temp_dir.glob("*"): path.unlink(missing_ok=True)
                temp_dir.rmdir()
            except Exception: pass
            if self._radio_stream_proc is proc: self._radio_stream_proc = None
            if self._radio_stream_dir == temp_dir: self._radio_stream_dir = None

    def _yandex_track_started_ui(self, track, preview=False, codec=""):
        if self.music_mode != "yandex": return
        self.yandex_track_started_at = time.monotonic()
        if self._yandex_progress and self._yandex_progress.winfo_exists():
            try:
                self._yandex_progress.stop()
                self._yandex_progress.configure(mode="determinate", maximum=max(1, float(track.get("duration_ms") or 0) / 1000.0), value=0)
            except Exception: pass
        if self._yandex_time_label and self._yandex_time_label.winfo_exists():
            self._yandex_time_label.configure(text=f"0:00 / {self._yandex_duration(track.get('duration_ms'))}")
        if self._yandex_progress_after:
            try: self.after_cancel(self._yandex_progress_after)
            except Exception: pass
            self._yandex_progress_after = None
        self._yandex_update_progress_loop()
        suffix = " · ознакомительный фрагмент" if preview else ""
        title = f"Сейчас играет: {track.get('artist', '—')} — {track.get('title', 'Трек')}"
        if suffix: title += suffix
        if self._yandex_now_label and self._yandex_now_label.winfo_exists(): self._yandex_now_label.configure(text=title)
        if self._yandex_tree and self._yandex_tree.winfo_exists() and 0 <= self.yandex_queue_index < len(self.yandex_queue):
            try: self._yandex_tree.selection_set(str(self.yandex_queue_index)); self._yandex_tree.see(str(self.yandex_queue_index))
            except Exception: pass
        if self.yandex_current_source in {"wave", "favorite_wave"}:
            self._yandex_run_async(
                lambda: self.yandex_service.wave_track_started(track),
                lambda _r: self._yandex_prefetch_wave_next(),
                label="Отправка начала прослушивания",
            )
        self._music_log(f"Яндекс играет: {track.get('artist')} — {track.get('title')} ({codec or 'аудио'})")

    def _yandex_update_progress_loop(self):
        if self.music_mode != "yandex" or not self.music_active or not self.yandex_current_track:
            self._yandex_progress_after = None
            return
        elapsed = max(0.0, time.monotonic() - self.yandex_track_started_at) if self.yandex_track_started_at else 0.0
        duration = max(0.0, float(self.yandex_current_track.get("duration_ms") or 0) / 1000.0)
        if self._yandex_progress and self._yandex_progress.winfo_exists() and duration > 0:
            try: self._yandex_progress.configure(mode="determinate", maximum=duration, value=min(duration, elapsed))
            except Exception: pass
        if self._yandex_time_label and self._yandex_time_label.winfo_exists():
            def fmt(seconds): return f"{int(seconds)//60}:{int(seconds)%60:02d}"
            self._yandex_time_label.configure(text=f"{fmt(elapsed)} / {fmt(duration)}" if duration else f"{fmt(elapsed)} / —:—")
        self._yandex_progress_after = self.after(500, self._yandex_update_progress_loop)

    def _yandex_track_failed(self, message, title):
        self.music_active = False; self.music_mode = "stopped"
        self.yandex_current_track = None; self.yandex_current_source = None
        if self._yandex_progress_after:
            try: self.after_cancel(self._yandex_progress_after)
            except Exception: pass
            self._yandex_progress_after = None
        if self._yandex_progress and self._yandex_progress.winfo_exists(): self._yandex_progress.stop()
        if self._yandex_now_label and self._yandex_now_label.winfo_exists():
            self._yandex_now_label.configure(text=f"Не удалось воспроизвести: {title} · {message}")
        self._music_log(f"ошибка Яндекс-потока: {message}")

    def _yandex_send_feedback(self, event_type):
        track = self.yandex_current_track
        if not track or (self.yandex_current_source or self.yandex_queue_source) not in {"wave", "favorite_wave"}: return
        elapsed = max(0.0, time.monotonic() - self.yandex_track_started_at) if self.yandex_track_started_at else 0.0
        def work():
            if event_type == "skip": return self.yandex_service.wave_track_skipped(track, elapsed)
            return self.yandex_service.wave_track_finished(track, elapsed)
        self._yandex_run_async(work, lambda _r: None, label="Обратная связь волны")

    def _yandex_on_track_finished(self):
        if self.music_mode != "yandex" or not self.music_active:
            return
        finished_track = self.yandex_current_track
        finished_source = self.yandex_current_source or self.yandex_queue_source
        elapsed = max(0.0, time.monotonic() - self.yandex_track_started_at) if self.yandex_track_started_at else 0.0
        self.yandex_current_track = None
        self.yandex_current_source = None
        if self._yandex_progress_after:
            try: self.after_cancel(self._yandex_progress_after)
            except Exception: pass
            self._yandex_progress_after = None
        self._yandex_update_queue_button()
        if self._yandex_progress and self._yandex_progress.winfo_exists():
            self._yandex_progress.stop()
        next_index = self.yandex_queue_index + 1
        if next_index < len(self.yandex_queue):
            if finished_track and finished_source in {"wave", "favorite_wave"}:
                # Rotor feedback must reach Yandex before the next track is marked
                # started; serialize the two events to keep recommendation state sane.
                def start_next(_result=None):
                    if self.music_mode != "yandex" or not self.music_active:
                        return
                    self.yandex_queue_index = next_index
                    self._yandex_start_current_track()
                self._yandex_run_async(
                    lambda: self.yandex_service.wave_track_finished(finished_track, elapsed),
                    start_next, label="Завершение трека в Моей волне")
            else:
                self.yandex_queue_index = next_index
                self._yandex_start_current_track()
            return
        if finished_source in {"wave", "favorite_wave"}:
            self._yandex_set_status("Получаю следующие рекомендации…")
            diversity = self.yandex_wave_mode
            def work():
                # Send the final completion event first. The next batch may already
                # have been fetched while the penultimate track was playing.
                if finished_track:
                    self.yandex_service.wave_track_finished(finished_track, elapsed)
                prefetched = self._yandex_take_prefetched_wave_rows(timeout=30.0)
                if prefetched is not None:
                    return prefetched
                return self.yandex_service.wave_next()
            def done(rows):
                if not rows:
                    self.stop_music()
                    self._yandex_set_status("Волна завершена")
                    return
                self.yandex_queue = rows
                self.yandex_queue_index = 0
                self._yandex_show_rows(rows, "Моя волна — Любимое" if diversity == "favorite" else "Моя волна")
                self._yandex_start_current_track()
                self._yandex_set_status("Подключён")
            self._yandex_run_async(work, done, label="Следующая партия волны")
        else:
            self.music_active = False
            self.music_mode = "stopped"
            if self._yandex_now_label and self._yandex_now_label.winfo_exists():
                self._yandex_now_label.configure(text="Очередь Яндекс Музыки завершена")
            self._music_log("очередь Яндекс Музыки завершена")

    def _yandex_next(self):
        if self.yandex_queue_index + 1 < len(self.yandex_queue):
            self.yandex_queue_index += 1; self._yandex_start_current_track()
        elif self.yandex_queue_source in {"wave", "favorite_wave"}:
            self._yandex_set_status("Получаю следующие рекомендации…")
            current = dict(self.yandex_current_track) if self.yandex_current_track else None
            elapsed = max(0.0, time.monotonic() - self.yandex_track_started_at) if self.yandex_track_started_at else 0.0
            # Stop the current audio immediately; the Rotor skip must be sent before
            # requesting another recommendation batch.
            self.stop_music(yandex_feedback=False)
            def work():
                if current: self.yandex_service.wave_track_skipped(current, elapsed)
                prefetched = self._yandex_take_prefetched_wave_rows(timeout=30.0)
                if prefetched is not None:
                    return prefetched
                return self.yandex_service.wave_next()
            self._yandex_run_async(work,
                lambda rows: self._yandex_append_wave_rows(rows), label="Следующая партия волны")
        else:
            self._music_log("в очереди больше нет треков")

    def _yandex_append_wave_rows(self, rows):
        self._yandex_reset_wave_prefetch(new_session=False)
        if rows:
            self.yandex_queue = list(rows); self.yandex_queue_index = 0
            self._yandex_show_rows(rows, "Моя волна — Любимое" if self.yandex_wave_mode == "favorite" else "Моя волна")
            self._yandex_update_queue_button()
            self._yandex_start_current_track()
        self._yandex_set_status("Подключён")

    def _yandex_previous(self):
        if self.yandex_queue_index > 0:
            self.yandex_queue_index -= 1; self._yandex_start_current_track()

    def _yandex_toggle_like(self):
        # Prefer the selected catalogue item; only fall back to the currently
        # playing track when the results list has no selection.
        track = self._yandex_selected() or self.yandex_current_track
        if not track or track.get("kind", "track") not in {"track", "album", "artist"}: return
        new_value = not bool(track.get("liked", False))
        self._yandex_set_status("Обновляю лайк…")
        def done(result):
            track["liked"] = new_value
            self._yandex_set_status("Подключён")
            self._music_log(("лайк поставлен: " if new_value else "лайк снят: ") + f"{track.get('artist')} — {track.get('title')}")
        self._yandex_run_async(lambda: self.yandex_service.set_liked(track, new_value), done, label="Изменение лайка")

    def _yandex_refresh_search_after_error(self):
        self._yandex_set_status("Нужен повторный вход")

    def _build_settings_page(self, page):
        # The full settings editor is kept in its own in-app page and remains scrollable.
        self._build_settings_editor(page)

    def _tk_error(self, exc, val, tb):
        traceback.print_exception(exc, val, tb)
        try:
            messagebox.showerror("Ошибка AutoInformant Pro", f"{type(val).__name__}: {val}\n\n{''.join(traceback.format_exception(exc, val, tb))}", parent=self)
        except Exception:
            pass

    def log(self, message: str):
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        if threading.current_thread() is not threading.main_thread():
            try:
                self.after(0, lambda: self.log(message))
            except Exception:
                pass
            return
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            print(line)

    def _plugin_source(self) -> Path:
        path = resource_path(PLUGIN_SOURCE)
        if not path.is_file():
            raise FileNotFoundError(f"Не найден файл Lua-плагина: {path}\nРаспакуй приложение целиком или пересобери EXE с файлами plugin.")
        return path

    def install_plugin(self):
        chosen = filedialog.askdirectory(title="Выбери папку plugins openOMSI (или корень игры)", parent=self)
        if not chosen:
            return
        selected = Path(chosen).resolve()
        if selected.name.casefold() == "autoinformantpro":
            target = selected
        elif selected.name.casefold() == "plugins":
            target = selected / "AutoInformantPro"
        else:
            # If the user selected game/content root, place it under its plugins directory.
            target = selected / "plugins" / "AutoInformantPro"
        try:
            target.mkdir(parents=True, exist_ok=True)
            source = self._plugin_source()
            dest = target / "main.lua"
            if dest.exists():
                backup = dest.with_name(f"main.lua.backup-{time.strftime('%Y%m%d-%H%M%S')}")
                shutil.copy2(dest, backup)
                self.log(f"Резервная копия старого плагина: {backup}")
            shutil.copy2(source, dest)
            self.settings["plugin_dir"] = str(target)
            self.settings["udp_port"] = 47821
            save_settings(self.settings)
            self._configure_bridge(restart=True)
            messagebox.showinfo("Плагин установлен", f"AutoInformant Pro установлен сюда:\n{target}\n\nПерезапусти openOMSI, если он уже был открыт.", parent=self)
            self.log(f"Плагин установлен: {dest}")
        except Exception as exc:
            traceback.print_exc()
            messagebox.showerror("Ошибка установки", f"{exc}\n\n{traceback.format_exc()}", parent=self)

    def _configure_bridge(self, restart: bool = False):
        with self._bridge_lock:
            old = self._bridge
            if old and restart:
                try:
                    old.close()
                except Exception:
                    pass
                self._bridge = None
            plugin_dir = str(self.settings.get("plugin_dir") or "").strip()
            if not plugin_dir:
                self.lbl_connection.configure(text="● нет плагина", foreground="#f6bd60")
                return
            try:
                if self._bridge is None:
                    cfg = {
                        "enabled": True,
                        "plugin_dir": plugin_dir,
                        "udp_port": 47821,
                        "source_mode": self.settings.get("source_mode", "ceiling"),
                        "volume": float(self.settings.get("volume", 0.9)),
                        "range": float(self.settings.get("interior_range", 7.0)),
                        "speaker_forward": float(self.settings.get("speaker_forward", -1.65)),
                        "speaker_right": float(self.settings.get("speaker_right", 0.35)),
                        "speaker_up": float(self.settings.get("speaker_up", 2.25)),
                        "speaker_effect": int(self.settings.get("speaker_effect", 45)),
                        "informant_gain": max(1.0, float(self.settings.get("informant_volume", 1.0))),
                        "exterior_volume": float(self.settings.get("exterior_volume_pct", 25.0)) / 100.0,
                        "music_exterior_volume": float(self.settings.get("music_exterior_volume_pct", 30.0)) / 100.0,
                        "exterior_range": float(self.settings.get("exterior_range", 45.0)),
                        "exterior_range_factor": 0.55,
                        "music_source_mode": self.settings.get("music_source_mode", "ceiling"),
                        "music_range": float(self.settings.get("music_range", 9.0)),
                        "music_speaker_forward": float(self.settings.get("music_speaker_forward", 2.10)),
                        "music_speaker_right": float(self.settings.get("music_speaker_right", 0.70)),
                        "music_speaker_up": float(self.settings.get("music_speaker_up", 1.25)),
                        "music_fixed_x": float(self.settings.get("music_fixed_x", 0.0)),
                        "music_fixed_y": float(self.settings.get("music_fixed_y", 0.0)),
                        "music_fixed_z": float(self.settings.get("music_fixed_z", 0.0)),
                        "informant_output_level": float(self.settings.get("informant_output_level", 1.0)),
                        "music_output_level": float(self.settings.get("music_output_level", 1.0)),
                        "auto_start_matching_trip": False,
                        "follow_game_progress": False,
                        "allow_manual_ibis_sync": False,
                        "start_timeout": 8.0,
                        "playback_timeout_extra": 30.0,
                    }
                    self._bridge = OpenOMSIBridge(cfg, on_key=self._on_plugin_key, on_state=self._on_plugin_state)
                    self._bridge.start()
                else:
                    self._bridge.update_config(self._bridge_config())
            except Exception as exc:
                self.log(f"Не удалось подключить плагин: {exc}")
                self._status_message = f"Плагин не отвечает: {exc}"
                self.lbl_sync.configure(text=self._status_message)

    def _bridge_config(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "plugin_dir": str(self.settings.get("plugin_dir") or ""),
            "udp_port": 47821,
            "source_mode": self.settings.get("source_mode", "ceiling"),
            "volume": float(self.settings.get("volume", 0.9)),
            "range": float(self.settings.get("interior_range", 7.0)),
            "speaker_forward": float(self.settings.get("speaker_forward", -1.65)),
            "speaker_right": float(self.settings.get("speaker_right", 0.35)),
            "speaker_up": float(self.settings.get("speaker_up", 2.25)),
            "speaker_effect": int(self.settings.get("speaker_effect", 45)),
            "informant_gain": max(1.0, float(self.settings.get("informant_volume", 1.0))),
            "exterior_volume": float(self.settings.get("exterior_volume_pct", 25.0)) / 100.0,
            "music_exterior_volume": float(self.settings.get("music_exterior_volume_pct", 30.0)) / 100.0,
            "exterior_range": float(self.settings.get("exterior_range", 45.0)),
            "exterior_range_factor": 0.55,
            "music_source_mode": self.settings.get("music_source_mode", "ceiling"),
            "music_range": float(self.settings.get("music_range", 9.0)),
            "music_speaker_forward": float(self.settings.get("music_speaker_forward", 2.10)),
            "music_speaker_right": float(self.settings.get("music_speaker_right", 0.70)),
            "music_speaker_up": float(self.settings.get("music_speaker_up", 1.25)),
            "music_fixed_x": float(self.settings.get("music_fixed_x", 0.0)),
            "music_fixed_y": float(self.settings.get("music_fixed_y", 0.0)),
            "music_fixed_z": float(self.settings.get("music_fixed_z", 0.0)),
            "informant_output_level": float(self.settings.get("informant_output_level", 1.0)),
            "music_output_level": float(self.settings.get("music_output_level", 1.0)),
            "auto_start_matching_trip": False,
            "follow_game_progress": False,
            "allow_manual_ibis_sync": False,
        }

    def _try_connect_bridge(self):
        self._configure_bridge(restart=True)

    def _on_plugin_key(self, key: str):
        key = str(key or "").upper()
        try:
            if key == "Q":
                self.after(0, self.manual_current_announcement)
        except Exception:
            pass

    def _on_plugin_state(self, state: dict[str, Any]):
        try:
            self.after(0, lambda s=dict(state): self._handle_plugin_state(s))
        except Exception:
            pass

    def _update_plugin_capabilities(self, state: dict[str, Any]):
        try:
            version = int(state.get("version", 1))
        except (TypeError, ValueError):
            version = 1
        self._plugin_live_volume_supported = version >= 2
        if self._plugin_live_volume_supported:
            if self._plugin_version_notice != version:
                self.log("Плагин поддерживает быстрый микшер громкости. Уровни применяются через отдельный канал настроек.")
                self._plugin_version_notice = version
        elif self._plugin_version_notice != version:
            self.log("Для быстрого микшера нужно обновить Lua-плагин через кнопку «Обновить плагин» и перезапустить openOMSI.")
            self._plugin_version_notice = version

    def _handle_plugin_state(self, state: dict[str, Any]):
        event_type = state.get("type")
        if event_type == "error":
            self.log(f"Ошибка плагина: {state.get('message', '?')}")
            return
        if event_type == "plugin_started":
            self.log("Плагин AutoInformant Pro загружен в openOMSI.")
            self._update_plugin_capabilities(state)
            return
        if event_type == "door_close_request":
            self._handle_door_close_request()
            return
        if event_type in {"music_finished", "music_started", "music_stopped", "music_error", "native_radio_station_changed"}:
            self._handle_music_event(state)
            return
        if event_type == "door_close_warning":
            self.log(f"DOORS: {state.get('message', 'не удалось закрыть двери после звука')}")
            return
        if event_type == "ready":
            self._update_plugin_capabilities(state)
            return
        if event_type != "game_state":
            return
        signature = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)
        # Still process continuous telemetry (distance/speed), but avoid redrawing every frame.
        self.current_state = state
        # Keep a terminal handover marker across rapid consecutive duty updates,
        # but retire it as soon as the announcement worker has drained its queue.
        if getattr(self, "_terminal_phrase_handover_active", False) and hasattr(self, "player") and not self.player.is_busy:
            self._terminal_phrase_handover_active = False
        previous_vehicle_key = self._last_vehicle_bank_key
        vehicle_key = self.library.vehicle_bank_key(state)
        if vehicle_key != self._last_vehicle_bank_key:
            self._last_vehicle_bank_key = vehicle_key
            self.library._index_cache.clear()
            if self.stops:
                self._rebuild_stop_rows()
        map_path = str(state.get("map_path") or "")
        if map_path:
            map_path_obj = Path(map_path)
            map_name = map_path_obj.parent.name if map_path_obj.name.casefold() == "global.cfg" else map_path_obj.name
        else:
            map_name = str(state.get("map") or "Неизвестная карта")
        map_name = map_name or str(state.get("map") or "Неизвестная карта")
        new_stops = normalize_stop_objects(state)
        new_key = route_identity(state, map_name)
        if new_key != self.current_route_key:
            old_map_name = self.map_name
            old_vehicle_key = previous_vehicle_key
            same_map = bool(old_map_name) and folder_key(old_map_name) == folder_key(map_name)
            same_vehicle = bool(old_vehicle_key) and old_vehicle_key == vehicle_key
            finish_terminal = should_preserve_terminal_phrase(
                getattr(self, "engine", None), getattr(self, "player", None),
                old_map_name, map_name, old_vehicle_key, vehicle_key,
                terminal_pending=getattr(self, "_terminal_phrase_handover_active", False),
            )
            self._terminal_phrase_handover_active = bool(finish_terminal)
            if hasattr(self, "player") and not finish_terminal:
                self.player.stop(clear_queue=True)
            elif finish_terminal:
                self.log("Рейс переключился на следующую работу, но фраза конечной уже в очереди — даю ей полностью доиграть.")
            self.current_route_key = new_key
            self.map_name = map_name
            self.stops = new_stops
            self.current_stop_index = find_current_stop_index(state, self.stops)
            self.engine.reset_route(new_key, state) if hasattr(self, "engine") else None
            self._rebuild_stop_rows()
            if self.stops:
                self._status_message = f"Автоматический список из openOMSI: {len(self.stops)} точек расписания; route.txt не используется."
                self.log(f"Рейс изменён: {map_name} · линия {state.get('line') or '—'} · рейс {state.get('trip_name') or state.get('trip') or '—'} · остановок/точек {len(self.stops)}")
            else:
                self._status_message = "В игре нет активного расписания с последовательностью остановок. Выбери duty/рейс в openOMSI."
                self.lbl_route.configure(text="Нет активного рейса/последовательности остановок")
                self.lbl_next.configure(text="Следующая остановка: —")
                self.log(self._status_message)
        else:
            if self._stops_changed(new_stops, self.stops):
                self.stops = new_stops
                self._rebuild_stop_rows()
            self.current_stop_index = find_current_stop_index(state, self.stops)
        self._update_route_summary(state)
        self._highlight_current_stop()
        if hasattr(self, "engine"):
            self.engine.update(state, self.stops, self.map_name)
        self._last_state_signature = signature

    @staticmethod
    def _stops_changed(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
        if len(a) != len(b):
            return True
        for x, y in zip(a, b):
            if (x.get("id"), x.get("name"), x.get("passed"), x.get("stops")) != (y.get("id"), y.get("name"), y.get("passed"), y.get("stops")):
                return True
        return False

    def _update_route_summary(self, state: dict[str, Any]):
        line = state.get("line") or "—"
        tour = state.get("tour") or "—"
        trip = state.get("trip_name") or state.get("trip") or "—"
        destination = state.get("terminus") or state.get("destination") or "—"
        self.lbl_route.configure(text=f"{self.map_name or 'Карта'}  ·  линия {line}  ·  рейс {trip}  ·  конечная {destination}")
        idx = self.current_stop_index
        if idx is not None and 0 <= idx < len(self.stops):
            nextname = self.stops[idx].get("name") or "—"
            dist = safe_float(state.get("next_stop_distance"))
            eta = estimated_time_to_next_stop(state, self.stops, idx, float(self.settings.get("average_speed_kmh", 25)))
            parts = [f"Следующая остановка: {nextname}"]
            if state.get("at_stop"):
                parts.append("у остановки")
            elif dist is not None:
                parts.append(f"{dist:.0f} м")
            if eta is not None:
                parts.append(f"ETA ~{int(eta)} с")
            self.lbl_next.configure(text=" · ".join(parts))
        else:
            self.lbl_next.configure(text="Следующая остановка: —")
        sync = "подключён" if self._bridge and self._bridge.connected else "нет связи с плагином"
        self.lbl_sync.configure(text=f"{sync} · {self._status_message}")

    def _rebuild_stop_rows(self):
        self.tree.delete(*self.tree.get_children())
        self._tree_item_by_index.clear()
        for idx, stop in enumerate(self.stops):
            audio = self.resolve_stop_audio(stop, idx)
            ignored = self.pack_manager.is_stop_ignored(self.map_name, stop, idx, self.current_state)
            pass_through = not core_is_announceable_stop(stop)
            if audio:
                status = "игнорируется" if ignored else ("проездная точка" if pass_through else "аудио найдено")
                tag = "ignored" if ignored else ("pass" if pass_through else "")
                audio_name = str(audio.relative_to(self.library.root)) if self._is_relative_to(audio, self.library.root) else audio.name
            else:
                status = "игнорируется" if ignored else ("проездная точка" if pass_through else "нет файла — тишина")
                tag = "ignored" if ignored else ("pass" if pass_through else "missing")
                audio_name = "—"
            iid = f"stop_{idx}_{abs(hash((stop.get('id'), stop.get('name'))) % 999999999)}"
            self.tree.insert("", "end", iid=iid, values=(idx + 1, stop.get("name", ""), audio_name, status), tags=(tag,) if tag else ())
            self._tree_item_by_index[idx] = iid

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    def profile_setting(self, key: str, default: Any = None) -> Any:
        packed = self.pack_manager.get_setting(self.map_name or "", self.current_state, key, None)
        return self.settings.get(key, default) if packed is None else packed

    def current_announcement_schedules(self) -> dict[str, dict[str, Any]]:
        packed = self.pack_manager.current_schedules(self.map_name or "", self.current_state)
        # An active package owns its schedules, including an intentionally empty set.
        # Do not leak old absolute-path schedules from the previous installation.
        if self.pack_manager.has_profile(self.map_name or "", self.current_state):
            return packed
        return self.settings.get("announcement_schedules", {})

    def is_announceable_stop(self, stop: dict[str, Any], index: int, map_name: str | None = None) -> bool:
        map_name = map_name or self.map_name or ""
        return (core_is_announceable_stop(stop)
                and not self.pack_manager.is_stop_ignored(map_name, stop, index, self.current_state))

    def resolve_stop_audio(self, stop: dict[str, Any], index: int) -> Path | None:
        packed_override = self.pack_manager.stop_override_path(self.map_name or "", stop, index, self.current_state)
        if packed_override is not None:
            return packed_override
        return self.library.find_stop_audio(self.map_name or "", self.current_state, stop, index)

    def enqueue_audio(self, path: Path | None, label: str):
        if path and path.is_file():
            self.player.enqueue(path, label)

    def _highlight_current_stop(self):
        self.tree.tag_configure("arrived", background="#2b3c35")
        for idx, iid in self._tree_item_by_index.items():
            tags = list(self.tree.item(iid, "tags"))
            tags = [tag for tag in tags if tag != "arrived"]
            if self.current_stop_index == idx:
                tags.append("arrived")
            self.tree.item(iid, tags=tuple(tags))
        if self.current_stop_index is not None and self.current_stop_index != self._last_scrolled_stop_index:
            iid = self._tree_item_by_index.get(self.current_stop_index)
            if iid:
                self.tree.see(iid)
            self._last_scrolled_stop_index = self.current_stop_index

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        for idx, iid in self._tree_item_by_index.items():
            if iid == selection[0]:
                return idx
        return None

    def _stop_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        idx = next((i for i, iid in self._tree_item_by_index.items() if iid == row), None)
        if idx is None:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="▶ Прослушать файл остановки", command=self.play_selected_stop)
        assign_menu = tk.Menu(menu, tearoff=0)
        assign_menu.add_command(
            label="Выбрать из активного аудиопака…",
            command=self.assign_selected_audio_from_pack,
        )
        assign_menu.add_command(
            label="Выбрать на компьютере и импортировать в пак…",
            command=self.import_selected_audio_to_pack,
        )
        menu.add_cascade(label="Назначить аудиофайл", menu=assign_menu)
        stop = self.stops[idx]
        key = stop_key(self.map_name, stop, idx)
        has_override = (self.pack_manager.stop_override_path(self.map_name, stop, idx, self.current_state) is not None
                        or key in self.settings.get("overrides", {}))
        if has_override:
            menu.add_command(label="Сбросить ручное назначение", command=self.clear_selected_override)
        menu.add_separator()
        ignored = self.pack_manager.is_stop_ignored(self.map_name, stop, idx, self.current_state)
        menu.add_command(label="Перестать игнорировать остановку" if ignored else "Игнорировать остановку",
                         command=lambda i=idx, value=not ignored: self.set_stop_ignored(i, value))
        menu.tk_popup(event.x_root, event.y_root)

    def set_stop_ignored(self, index: int, ignored: bool):
        if index < 0 or index >= len(self.stops):
            return
        stop = self.stops[index]
        saved = self.pack_manager.set_stop_ignored(self.map_name, stop, index, self.current_state, ignored)
        if not saved:
            messagebox.showinfo("Пак не выбран", "Сначала импортируй и активируй пак этой карты. Список остановок игры при этом не изменяется.", parent=self)
            return
        self._rebuild_stop_rows()
        action = "будет пропускаться" if ignored else "снова будет озвучиваться"
        self.log(f"Остановка «{stop.get('name')}» {action}. Правило сохранено в аудиопаке.")

    def play_selected_stop(self):
        idx = self._selected_index()
        if idx is None or idx < 0 or idx >= len(self.stops):
            messagebox.showinfo("Остановка не выбрана", "Выбери остановку из списка или дождись активного рейса.", parent=self)
            return
        stop = self.stops[idx]
        path = self.resolve_stop_audio(stop, idx)
        if not path:
            messagebox.showinfo("Файл не найден", f"Для остановки «{stop.get('name')}» файл не найден.\nАвтоматически приложение в таком случае молчит.", parent=self)
            return
        if self.profile_setting("play_jingle", True):
            cue = self.library.jingle_file(self.map_name, self.current_state)
            self.enqueue_audio(cue, f"JINGLE перед остановкой: {stop.get('name')}")
        self.enqueue_audio(path, f"Тест остановки: {stop.get('name')}")

    def manual_current_announcement(self):
        idx = self.current_stop_index
        if idx is None or idx < 0 or idx >= len(self.stops):
            self.log("Нельзя объявить остановку: в openOMSI нет активного рейса.")
            return
        stop = self.stops[idx]
        if not self.is_announceable_stop(stop, idx):
            self.log(f"Текущая точка «{stop.get('name')}» проездная или игнорируемая; объявление не воспроизводится.")
            return
        path = self.resolve_stop_audio(stop, idx)
        if not path:
            self.log(f"Нет файла для «{stop.get('name')}» — оставляю тишину.")
            return
        if self.profile_setting("play_jingle", True):
            cue = self.library.jingle_file(self.map_name, self.current_state)
            self.enqueue_audio(cue, f"JINGLE перед остановкой: {stop.get('name')}")
        self.enqueue_audio(path, f"Ручное объявление: {stop.get('name')}")

    def play_random_anon(self):
        state = self.current_state
        if not state or not self.map_name:
            self.log("Нет состояния игры для выбора анонсов.")
            return
        files = self.library.announcement_files(self.map_name, state)
        # Manual test button allows any valid file; auto mode additionally checks estimated ETA.
        schedules = self.current_announcement_schedules()
        available = [p for p in files if p.suffix.lower() in AUDIO_EXTS
                     and not (set().union(*AUX_ALIASES.values()) & {normalize_text(p.stem)})
                     and announcement_schedule_allows(p, state, schedules)]
        history = getattr(self.engine, "anon_history", [])
        fresh = [p for p in available if str(p.resolve()) not in history[-max(1, len(available)-1):]]
        if not fresh:
            fresh = [p for p in available if not history or str(p.resolve()) != history[-1]]
        if not fresh:
            fresh = available
        if not fresh:
            self.log("Папка «Анонсы» не содержит подходящих аудиофайлов.")
            return
        path = random.choice(fresh)
        self.engine.anon_history.append(str(path.resolve()))
        if self.profile_setting("play_jingle", True):
            cue = self.library.jingle_file(self.map_name, state)
            self.enqueue_audio(cue, f"JINGLE перед анонсом: {path.stem}")
        self.enqueue_audio(path, f"Тестовый анонс: {path.stem}")

    def stop_playback(self):
        self.player.stop(clear_queue=True)
        self.log("Воспроизведение остановлено, очередь очищена.")

    def _selected_stop_for_audio_assignment(self):
        idx = self._selected_index()
        if idx is None or idx < 0 or idx >= len(self.stops):
            return None, None
        return idx, self.stops[idx]

    def _active_profile_for_stop_assignment(self):
        info = self.pack_manager.active_profile_info(self.map_name or "", self.current_state)
        if not info:
            messagebox.showinfo(
                "Нет активного аудиопака",
                "Переназначение остановок хранится внутри .aipack. "
                "Сначала импортируй и активируй пак для текущей карты "
                "(или категорию для текущей модели транспорта).",
                parent=self,
            )
            return None
        return info

    def assign_selected_audio_from_pack(self):
        idx, stop = self._selected_stop_for_audio_assignment()
        if stop is None:
            return
        info = self._active_profile_for_stop_assignment()
        if not info:
            return
        files = self.pack_manager.list_active_profile_audio_files(self.map_name or "", self.current_state)
        if not files:
            messagebox.showinfo(
                "В активном профиле нет аудио",
                f"В профиле «{info['profile_name']}» пакета «{info['pack_name']}» "
                "пока нет поддерживаемых аудиофайлов. Можно импортировать файл с компьютера.",
                parent=self,
            )
            return
        path = filedialog.askopenfilename(
            title=f"Выбрать файл из «{info['pack_name']}» для «{stop.get('name')}»",
            initialdir=str(info["root"]),
            filetypes=[("Аудио", "".join(f"*{ext} " for ext in AUDIO_EXTENSIONS).strip()),
                       ("Все файлы", "*.*")],
            parent=self,
        )
        if path:
            self._save_selected_stop_audio(idx, stop, Path(path), source_label="активного аудиопака")

    def import_selected_audio_to_pack(self):
        idx, stop = self._selected_stop_for_audio_assignment()
        if stop is None:
            return
        info = self._active_profile_for_stop_assignment()
        if not info:
            return
        path = filedialog.askopenfilename(
            title=f"Импортировать файл для «{stop.get('name')}» в «{info['pack_name']}»",
            initialdir=str(self.library.root if self.library.root.is_dir() else APP_DIR),
            filetypes=[("Аудио", "".join(f"*{ext} " for ext in AUDIO_EXTENSIONS).strip()),
                       ("Все файлы", "*.*")],
            parent=self,
        )
        if path:
            self._save_selected_stop_audio(idx, stop, Path(path), source_label="компьютера")

    def _save_selected_stop_audio(self, idx: int, stop: dict[str, Any], source: Path, *, source_label: str):
        source = source.expanduser().resolve()
        if not source.is_file():
            messagebox.showerror("Файл не найден", str(source), parent=self)
            return
        if source.suffix.lower() not in AUDIO_EXTS:
            messagebox.showerror(
                "Неподдерживаемый формат",
                f"Формат {source.suffix or 'без расширения'} не поддерживается. "
                f"Допустимые форматы: {', '.join(AUDIO_EXTENSIONS)}.",
                parent=self,
            )
            return
        try:
            stored = self.pack_manager.set_stop_override(
                self.map_name or "", stop, idx, self.current_state, source
            )
            if stored is None:
                raise RuntimeError(
                    "Активный профиль пакета больше не доступен. Файл не был назначен. "
                    "Проверь активацию .aipack для текущей карты и транспорта."
                )
        except Exception as exc:
            messagebox.showerror("Не удалось назначить аудио", str(exc), parent=self)
            return

        # Overrides are now package-only. Never fall back to an absolute external
        # path in autoinformant_pro_settings.json.
        self.settings.get("overrides", {}).pop(stop_key(self.map_name, stop, idx), None)
        save_settings(self.settings)
        self.library.overrides = self.settings.get("overrides", {})
        self._rebuild_stop_rows()
        try:
            profile_root = self.pack_manager.active_profile_info(self.map_name or "", self.current_state)["root"]
            relative = stored.relative_to(profile_root).as_posix()
        except Exception:
            relative = stored.name
        action = "выбран из профиля" if stored.resolve() == source else "импортирован в профиль"
        self.log(
            f"Для «{stop.get('name')}» файл {action} «{relative}» "
            f"(источник: {source_label}); назначение сохранено в manifest.json активного .aipack."
        )

    # Compatibility alias for callers from older UI code.
    def assign_selected_audio(self):
        self.import_selected_audio_to_pack()

    def clear_selected_override(self):
        idx = self._selected_index()
        if idx is None or idx >= len(self.stops):
            return
        stop = self.stops[idx]
        cleared = self.pack_manager.clear_stop_override(self.map_name, stop, idx, self.current_state)
        key = stop_key(self.map_name, stop, idx)
        if key in self.settings.get("overrides", {}):
            del self.settings["overrides"][key]
            save_settings(self.settings)
            self.library.overrides = self.settings["overrides"]
            cleared = True
        if cleared:
            self._rebuild_stop_rows()
            self.log(f"Ручное назначение для «{stop.get('name')}» сброшено.")

    # ---------- Per-vehicle bank, schedule editor, map-specific doors, music ----------
    def choose_audio_folder_for_current_vehicle(self):
        key = self.library.vehicle_bank_key(self.current_state)
        if not key:
            messagebox.showinfo("Модель не определена", "Загрузи автобус в openOMSI: плагин должен передать модель транспорта.", parent=self)
            return
        existing = self.settings.setdefault("vehicle_audio_folders", {}).get(key)
        initial = existing or str(self.library._map_dir(self.map_name, self.current_state))
        if not Path(initial).is_dir():
            initial = str(self.library.root if self.library.root.is_dir() else APP_DIR)
        chosen = filedialog.askdirectory(title=f"Аудиотека для модели {key}", initialdir=initial, parent=self)
        if not chosen:
            return
        folder = Path(chosen).expanduser().resolve()
        if not folder.is_dir():
            messagebox.showerror("Папка не найдена", str(folder), parent=self)
            return
        self.settings.setdefault("vehicle_audio_folders", {})[key] = str(folder)
        self.library.vehicle_folders = self.settings["vehicle_audio_folders"]
        self.library._index_cache.clear()
        save_settings(self.settings)
        self._rebuild_stop_rows()
        self.log(f"Для модели транспорта «{key}» назначена аудиотека: {folder}")

    def reset_audio_folder_for_current_vehicle(self):
        key = self.library.vehicle_bank_key(self.current_state)
        if not key:
            messagebox.showinfo("Модель не определена", "Сейчас игра не передала модель транспорта.", parent=self)
            return
        folders = self.settings.setdefault("vehicle_audio_folders", {})
        keys = [saved for saved in folders if saved.casefold() == key.casefold()]
        if not keys:
            messagebox.showinfo("Без переназначения", f"Для модели «{key}» отдельная папка не задана.", parent=self)
            return
        for saved in keys:
            folders.pop(saved, None)
        self.library.vehicle_folders = folders
        self.library._index_cache.clear()
        save_settings(self.settings)
        self._rebuild_stop_rows()
        self.log(f"Для модели «{key}» отменено переназначение аудиотеки.")

    def toggle_doors_for_current_map(self):
        name = str(self.map_name or "").strip()
        if not name or name == "Неизвестная карта":
            messagebox.showinfo("Карта не загружена", "Сначала загрузи карту в openOMSI.", parent=self)
            return
        enabled = not bool(self.profile_setting("doors_enabled", False))
        if not self.pack_manager.set_setting(name, self.current_state, "doors_enabled", enabled):
            mapping = self.settings.setdefault("door_close_enabled_maps", {})
            existing_key = next((key for key in mapping if str(key).casefold() == name.casefold()), name)
            mapping[existing_key] = enabled
            save_settings(self.settings)
        if enabled:
            path = self.library.named_cue_file(name, self.current_state, "DOORS")
            if path:
                self.log(f"DOORS для карты «{name}» включён: {path.name}. Закрытие: Ctrl+Alt+D.")
                messagebox.showinfo("DOORS включён", f"Карта: {name}\nФайл: {path}\n\nПри открытых дверях нажми Ctrl+Alt+D: сначала прозвучит файл, затем плагин попробует закрыть двери.", parent=self)
            else:
                self.log(f"DOORS включён для «{name}», но файл DOORS.mp3/.wav в текущей аудиотеке не найден.")
                messagebox.showwarning("Нет DOORS", f"Настройка включена для карты «{name}», но файл DOORS.mp3 или DOORS.wav не найден в активной аудиотеке.", parent=self)
        else:
            self.log(f"DOORS-режим выключен для карты «{name}».")
            messagebox.showinfo("DOORS выключен", f"Особая последовательность закрытия дверей отключена для карты «{name}».", parent=self)

    def _handle_door_close_request(self):
        name = str(self.map_name or "").strip()
        enabled = bool(self.profile_setting("doors_enabled", False))
        if not self.pack_manager.map_profile(name) and not self.pack_manager.category_profile(name, self.current_state):
            mapping = self.settings.get("door_close_enabled_maps", {})
            enabled = any(str(key).casefold() == name.casefold() and val for key, val in mapping.items())
        if not enabled:
            self.log("Ctrl+Alt+D: последовательность DOORS не включена для текущей карты. Включи её в настройках.")
            return
        cue = self.library.named_cue_file(name, self.current_state, "DOORS")
        if not cue:
            self.log(f"DOORS: файл DOORS.mp3/.wav не найден для карты «{name}».")
            return
        if not self._bridge or not self._bridge.enabled:
            self.log("DOORS: мост с openOMSI не подключён.")
            return
        # The regular game door key cannot safely be delayed by a plugin. A dedicated
        # chord starts the cue first; Lua closes a currently-open door when it finishes.
        self.player.stop(clear_queue=True)
        self.player.enqueue(cue, f"DOORS перед закрытием · {name}", then_close_doors=True)
        self.log(f"DOORS: сначала играет {cue.name}; затем плагин попробует закрыть двери.")

    def open_announcement_schedule_editor(self):
        if not self.map_name or self.map_name == "Неизвестная карта":
            messagebox.showinfo("Карта не загружена", "Для настройки расписания загрузи карту в openOMSI.", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("Расписание анонсов")
        win.geometry("780x500")
        win.minsize(640, 380)
        self._place_dialog(win, self)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Карта: {self.map_name} · расписание проверяется по дате и времени игры", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(tree_frame, columns=("file", "condition"), show="headings", selectmode="browse")
        tree.heading("file", text="Файл анонса")
        tree.heading("condition", text="Когда разрешён")
        tree.column("file", width=360)
        tree.column("condition", width=340)
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        paths = self.library.announcement_files(self.map_name, self.current_state)
        schedules = self.current_announcement_schedules()
        path_by_iid = {}
        def desc(entry):
            if not isinstance(entry, dict) or not entry.get("enabled", True): return "Всегда"
            parts = []
            if entry.get("start_date") or entry.get("end_date"):
                parts.append(f"{entry.get('start_date') or '…'} — {entry.get('end_date') or '…'}" + (" ежегодно" if entry.get("annual") else ""))
            if entry.get("start_time") or entry.get("end_time"):
                parts.append(f"{entry.get('start_time') or '00:00'}–{entry.get('end_time') or '23:59'}")
            return "; ".join(parts) if parts else "Всегда"
        for idx, path in enumerate(paths):
            key = str(path.resolve())
            iid = f"a{idx}"
            path_by_iid[iid] = path
            tree.insert("", "end", iid=iid, values=(path.name, desc(schedules.get(key))))
        def edit_schedule(_event=None):
            selected = tree.selection()
            if not selected: return
            path = path_by_iid[selected[0]]
            key = str(path.resolve())
            prior = schedules.get(key, {}) if isinstance(schedules.get(key), dict) else {}
            dlg = tk.Toplevel(win)
            dlg.title(f"Расписание — {path.name}")
            dlg.geometry("560x390")
            self._place_dialog(dlg, win, modal=True)
            body = ttk.Frame(dlg, padding=16)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text=path.name, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
            enabled_v = tk.BooleanVar(value=bool(prior.get("enabled", True)))
            annual_v = tk.BooleanVar(value=bool(prior.get("annual", False)))
            ModernSwitch(body, "Использовать ограничения (иначе всегда разрешён)", enabled_v,
                         background=self._ui_colors["bg"], foreground=self._ui_colors["fg"],
                         accent=self._ui_colors["accent"]).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
            fields = {}
            for i, (label, field, default) in enumerate((
                ("Начальная дата", "start_date", ""), ("Конечная дата", "end_date", ""),
                ("Время с (игровое)", "start_time", ""), ("Время до (игровое)", "end_time", ""))):
                ttk.Label(body, text=label).grid(row=i+2, column=0, sticky="w", padx=(0, 12), pady=4)
                v = tk.StringVar(value=str(prior.get(field) or default))
                fields[field] = v
                ttk.Entry(body, textvariable=v, width=24).grid(row=i+2, column=1, sticky="ew", pady=4)
            ModernSwitch(body, "Повторять ежегодно (для даты вводи ММ-ДД, например 12-31)", annual_v,
                         background=self._ui_colors["bg"], foreground=self._ui_colors["fg"],
                         accent=self._ui_colors["accent"]).grid(row=6, column=0, columnspan=2, sticky="w", pady=6)
            ttk.Label(body, text="Даты YYYY-MM-DD; ежегодно — MM-DD. Время HH:MM. Пустое поле означает без ограничения.", wraplength=520).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 12))
            body.columnconfigure(1, weight=1)
            def save_entry():
                start_date, end_date = fields["start_date"].get().strip(), fields["end_date"].get().strip()
                start_time, end_time = fields["start_time"].get().strip(), fields["end_time"].get().strip()
                annual = annual_v.get()
                try:
                    for value in (start_date, end_date):
                        if not value: continue
                        if annual and re.fullmatch(r"\d{2}-\d{2}", value):
                            _datetime.datetime.strptime("2000-" + value, "%Y-%m-%d")
                        else:
                            _datetime.datetime.strptime(value, "%Y-%m-%d")
                    for value in (start_time, end_time):
                        if value: _datetime.datetime.strptime(value, "%H:%M")
                except ValueError:
                    messagebox.showerror("Неверный формат", "Используй YYYY-MM-DD (обычно), MM-DD (ежегодно) и HH:MM для времени.", parent=dlg)
                    return
                entry = None if not enabled_v.get() else {"enabled": True, "annual": annual, "start_date": start_date,
                                                            "end_date": end_date, "start_time": start_time, "end_time": end_time}
                if not self.pack_manager.save_schedule(self.map_name, self.current_state, path, entry):
                    if entry is None:
                        schedules.pop(key, None)
                    else:
                        schedules[key] = entry
                    self.settings["announcement_schedules"] = schedules
                    save_settings(self.settings)
                else:
                    schedules = self.current_announcement_schedules()
                tree.set(selected[0], "condition", desc(schedules.get(key)))
                dlg.destroy()
                self.log(f"Расписание анонса обновлено: {path.name} → {desc(schedules.get(key))}")
            buttons = ttk.Frame(body)
            buttons.grid(row=8, column=0, columnspan=2, sticky="e")
            ttk.Button(buttons, text="Сохранить", command=save_entry).pack(side="right")
            ttk.Button(buttons, text="Отмена", command=dlg.destroy).pack(side="right", padx=6)
        def clear_schedule():
            selected = tree.selection()
            if not selected: return
            path = path_by_iid[selected[0]]
            if not self.pack_manager.save_schedule(self.map_name, self.current_state, path, None):
                schedules.pop(str(path.resolve()), None)
                self.settings["announcement_schedules"] = schedules
                save_settings(self.settings)
            else:
                schedules = self.current_announcement_schedules()
            tree.set(selected[0], "condition", "Всегда")
            self.log(f"Расписание удалено: {path.name}")
        tree.bind("<Double-1>", edit_schedule)
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Изменить расписание…", command=edit_schedule).pack(side="left")
        ttk.Button(btns, text="Сбросить ограничения", command=clear_schedule).pack(side="left", padx=6)
        ttk.Button(btns, text="Закрыть", command=win.destroy).pack(side="right")
        if not paths:
            ttk.Label(frame, text="В текущей аудиотеке не найдены файлы анонсов. Поддерживаются аудиофайлы из папок «Анонсы».").pack(anchor="w", pady=8)

    def _music_log(self, message: str):
        self.log("Музыка: " + message)

    def _normalise_radio_station(self, station):
        if isinstance(station, dict):
            name = str(station.get("name") or station.get("url") or "Станция").strip()
            url = str(station.get("url") or "").strip()
            return {"name": name, "url": url} if url else None
        if isinstance(station, str) and station.strip().lower().startswith(("http://", "https://")):
            return {"name": station.strip(), "url": station.strip()}
        return None

    def open_music_player(self):
        """Navigate to the embedded music/radio page (no extra top-level window)."""
        self._show_page("music")

    def _save_music_playlist(self):
        self.settings["music_playlist"] = [str(p) for p in self.music_playlist if p.is_file()]
        save_settings(self.settings)

    def _play_selected_local_music(self):
        tree = self._music_local_tree
        selected = tree.selection() if tree else ()
        if selected:
            self.music_index = int(selected[0])
        elif self.music_index < 0 and self.music_playlist:
            self.music_index = 0
        if not (0 <= self.music_index < len(self.music_playlist)):
            messagebox.showinfo("Плейлист пуст", "Добавь файлы или просканируй папку с музыкой.", parent=self._music_window or self)
            return
        path = self.music_playlist[self.music_index]
        self._start_music_source(path, mode="local")

    def _start_music_source(self, source, mode="local"):
        if not self._bridge or not self._bridge.enabled:
            messagebox.showwarning("Нет соединения", "Сначала настрой путь к Lua-плагину AutoInformantPro и запусти openOMSI.", parent=self._music_window or self)
            return False
        self._stop_radio_process()
        self._music_source_generation += 1
        generation = self._music_source_generation
        self._music_finished_event.clear(); self._music_started_event.clear()
        self.music_mode = mode; self.music_active = True
        label = Path(source).name if mode == "local" else str(source)
        self._music_log(f"подготовка: {label}")
        def launch():
            try:
                # Decode/preprocessing can take a while for long audio files; do not freeze Tk.
                # Serialize with Stop and radio segments so stale starts cannot race after Stop.
                with self._music_worker_lock:
                    if generation != self._music_source_generation or not self.music_active:
                        return
                    self._bridge.start_music(source, (float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))))
                if generation == self._music_source_generation and self.music_active:
                    self.after(0, lambda: self._music_log(f"команда отправлена: {label}"))
            except Exception as exc:
                msg = str(exc)
                if generation == self._music_source_generation:
                    self.music_active = False; self.music_mode = "stopped"
                    self.after(0, lambda message=msg: self._music_log(f"ошибка запуска: {message}"))
                    self.after(0, lambda message=msg: messagebox.showerror("Не удалось запустить музыку", message, parent=self._music_window or self))
        threading.Thread(target=launch, daemon=True, name="AutoInformant-music-start").start()
        return True

    def _handle_music_event(self, state):
        event_type = str(state.get("type") or "")
        source = str(state.get("source") or "")
        event_id = str(state.get("id") or "")
        # Late stop/error notifications from a previous source must not turn off a
        # newly selected native station. openOMSI keeps the same ID when Shift+R
        # changes the station, so that event remains accepted.
        if source in {"native_radio", "openOMSI native radio"}:
            # Ignore events from a prior native source if the user has already switched
            # to a local track, or the command ID belongs to an older station.
            if self.music_mode != "radio" or not self._active_native_radio_id or (event_id and event_id != self._active_native_radio_id):
                self._music_log(f"устаревшее событие радио пропущено (id {event_id[:8] or '—'})")
                return
        if event_type == "music_started":
            self._music_started_event.set()
            if self.music_mode != "yandex":
                self._music_log(f"играет: {state.get('track') or 'аудио'}")
        elif event_type == "music_finished":
            self._music_finished_event.set()
            if self.music_active and self.music_mode == "local":
                self.after(150, self.next_music_track)
            elif self.music_active and self.music_mode == "yandex":
                marker = parse_segment_marker(state.get("track"))
                if marker:
                    event_generation, segment_index = marker
                    if (event_generation == self._yandex_final_segment_generation
                            and segment_index == self._yandex_final_segment_index):
                        self._yandex_song_finished_event.set()
                        self.after(120, self._yandex_on_track_finished)
        elif event_type == "music_stopped":
            self._music_finished_event.set()
            self._music_started_event.set()
            if state.get("source") == "native_radio" and self.music_mode == "radio":
                self.music_active = False
                self.music_mode = "stopped"
                self._active_native_radio_id = None
                self._music_log("игровое радио выключено в openOMSI")
            else:
                self._music_log("остановлено")
        elif event_type == "native_radio_station_changed":
            self.music_mode = "radio"
            self.music_active = True
            native_index = int(state.get("station_index") or 0)
            matched = False
            stations = [item for item in (self._normalise_radio_station(x) for x in self.settings.get("radio_stations", [])) if item]
            for idx, item in enumerate(stations):
                try:
                    runtime_index, _changed, loaded = self._native_radio_station_index(item)
                except Exception:
                    continue
                if loaded and runtime_index == native_index:
                    self.radio_station_index = idx
                    if self._music_radio_tree and self._music_radio_tree.winfo_exists():
                        self._music_radio_tree.selection_set(str(idx))
                        self._music_radio_tree.see(str(idx))
                    matched = True
                    self._music_log(f"радио переключено игрой: {item['name']}")
                    break
            if not matched:
                self._music_log(f"радио переключено штатной клавишей openOMSI: станция №{native_index}")
        elif event_type == "music_error":
            self._music_finished_event.set(); self._music_started_event.set()
            message = str(state.get("message") or "?")
            if state.get("source") == "native_radio" and self.music_active and self.music_mode == "radio":
                stations = self.settings.get("radio_stations", [])
                idx = self.radio_station_index
                if isinstance(stations, list) and 0 <= idx < len(stations):
                    station = dict(stations[idx])
                    self._active_native_radio_id = None
                    self._music_log(f"нативное радио не сработало ({message}); переключаюсь на резервный поток")
                    self.after(0, lambda item=station: self._start_radio_stream(item, force_ffmpeg=True))
                    return
            self.music_active = False; self.music_mode = "stopped"
            self._radio_stop_event.set()
            if getattr(self, "_yandex_progress", None) is not None:
                try: self._yandex_progress.stop()
                except Exception: pass
            self._music_log(f"ошибка: {message}")

    def next_music_track(self):
        if not self.music_playlist:
            return
        self.music_index = (self.music_index + 1) % len(self.music_playlist)
        if self._music_local_tree and self._music_local_tree.winfo_exists():
            self._music_local_tree.selection_set(str(self.music_index)); self._music_local_tree.see(str(self.music_index))
        self._play_selected_local_music()

    def previous_music_track(self):
        if not self.music_playlist: return
        self.music_index = (self.music_index - 1) % len(self.music_playlist)
        if self._music_local_tree and self._music_local_tree.winfo_exists():
            self._music_local_tree.selection_set(str(self.music_index)); self._music_local_tree.see(str(self.music_index))
        self._play_selected_local_music()

    def stop_music(self, *, send_to_game: bool = True, yandex_feedback: bool = True):
        if (yandex_feedback and getattr(self, "music_mode", "") == "yandex"
                and getattr(self, "yandex_current_track", None)
                and (getattr(self, "yandex_current_source", None) or getattr(self, "yandex_queue_source", "")) in {"wave", "favorite_wave"}
                and getattr(self, "yandex_track_started_at", 0.0)):
            try: self._yandex_send_feedback("skip")
            except Exception: pass
        self._music_source_generation += 1
        if getattr(self, "music_mode", "") == "yandex" and hasattr(self, "_yandex_song_finished_event"):
            self._yandex_song_finished_event.set()
        self.music_active = False; self.music_mode = "stopped"
        if hasattr(self, "yandex_current_source"):
            self.yandex_current_source = None
            self.yandex_current_track = None
        self._radio_stop_event.set()
        self._stop_radio_process()
        try:
            with self._music_worker_lock:
                if self._bridge and send_to_game:
                    self._bridge.stop_music()
        except Exception as exc: self._music_log(f"ошибка остановки: {exc}")
        if send_to_game:
            self._active_native_radio_id = None
        self._music_finished_event.set()
        self._music_started_event.set()
        if getattr(self, "_yandex_progress", None) is not None:
            try: self._yandex_progress.stop()
            except Exception: pass
        if getattr(self, "_yandex_progress_after", None):
            try: self.after_cancel(self._yandex_progress_after)
            except Exception: pass
            self._yandex_progress_after = None
        if getattr(self, "_yandex_now_label", None) is not None:
            try:
                if self._yandex_now_label.winfo_exists() and getattr(self, "music_mode", "") == "stopped":
                    self._yandex_now_label.configure(text="Воспроизведение остановлено")
            except Exception: pass
        self._music_log("стоп")

    def _stop_radio_process(self):
        self._radio_stop_event.set()
        proc = self._radio_stream_proc
        self._radio_stream_proc = None
        if proc and proc.poll() is None:
            try: proc.terminate()
            except Exception: pass
            try: proc.wait(timeout=1.0)
            except Exception:
                try: proc.kill()
                except Exception: pass
        self._radio_stream_generation += 1

    def _play_selected_radio(self):
        tree = self._music_radio_tree
        selected = tree.selection() if tree else ()
        stations = [station for station in (self._normalise_radio_station(x) for x in self.settings.get("radio_stations", [])) if station]
        if selected:
            self.radio_station_index = int(selected[0])
        elif self.radio_station_index < 0 and stations:
            self.radio_station_index = 0
        if not (0 <= self.radio_station_index < len(stations)):
            messagebox.showinfo("Нет станций", "Добавь прямой URL интернет-радио.", parent=self._music_window or self); return
        station = stations[self.radio_station_index]
        self._start_radio_stream(station)

    def next_radio_station(self):
        stations = [station for station in (self._normalise_radio_station(x) for x in self.settings.get("radio_stations", [])) if station]
        if not stations:
            return # openOMSI's own Shift+R remains active
        self.radio_station_index = (self.radio_station_index + 1) % len(stations)
        if self._music_radio_tree and self._music_radio_tree.winfo_exists():
            self._music_radio_tree.selection_set(str(self.radio_station_index)); self._music_radio_tree.see(str(self.radio_station_index))
        self._play_selected_radio()

    def _native_radio_station_index(self, station: dict[str, str]) -> tuple[int | None, bool, bool]:
        """Return station index in openOMSI's runtime list, including map-local rows first."""
        global_path = openomsi_radio_cfg_path()
        selected = _normal_url(station.get("url", ""))
        existed_before_sync = selected in {_normal_url(item["url"]) for item in parse_openomsi_radio_cfg(global_path)}
        changed, _status = sync_openomsi_radio_cfg(self.settings.get("radio_stations", []))
        loaded = True
        # If this URL was already in radio.cfg, appending/renaming some OTHER station
        # does not invalidate its index: new entries are always appended. Only a newly
        # introduced URL needs the game to reload radio.cfg before native playback.
        if not existed_before_sync and global_path.exists():
            try:
                import psutil
                cfg_mtime = global_path.stat().st_mtime
                for proc in psutil.process_iter(["name", "create_time"]):
                    try:
                        name = str(proc.info.get("name") or "").casefold()
                        if "openomsi" in name:
                            if float(proc.info.get("create_time") or 0) < cfg_mtime - 1.0:
                                loaded = False
                            break
                    except (psutil.Error, TypeError, ValueError):
                        continue
            except Exception:
                loaded = not changed
        urls: list[str] = []
        map_urls: list[str] = []
        map_path = str((self.current_state or {}).get("map_path") or "").strip()
        if map_path:
            path = Path(map_path)
            map_radio = path.parent / "radio.cfg" if path.name.casefold() == "global.cfg" else path / "radio.cfg"
            for item in parse_openomsi_radio_cfg(map_radio):
                key = _normal_url(item["url"])
                if key and key not in urls:
                    urls.append(key)
                    map_urls.append(key)
        for item in parse_openomsi_radio_cfg(global_path):
            key = _normal_url(item["url"])
            if key and key not in urls:
                urls.append(key)
        selected = _normal_url(station.get("url", ""))
        # Map-local radio.cfg is read when the map is loaded, regardless of the
        # timestamp of the global list. Its stations are safe to select now.
        if selected in map_urls:
            loaded = True
        try:
            return (urls.index(selected) + 1 if selected in urls else None), changed, loaded
        except ValueError:
            return None, changed, loaded

    def _resolve_ffmpeg(self):
        # Prefer the binary shipped by imageio-ffmpeg (installed with app dependencies).
        # Keep legacy custom/PATH paths as fallbacks for existing installations.
        try:
            import imageio_ffmpeg
            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled and Path(bundled).is_file():
                return bundled
        except Exception:
            pass
        legacy = str(self.settings.get("ffmpeg_path") or "").strip()
        if legacy and Path(legacy).is_file():
            return legacy
        return shutil.which("ffmpeg")

    def _start_radio_stream(self, station, *, force_ffmpeg: bool = False):
        # Prefer openOMSI's native radio: it streams without file-segment seams and uses
        # the game's own 3D, Doppler and outside-through-windows audio path. If Lua
        # rejects the native radio variables, the event handler retries using FFmpeg.
        if not force_ffmpeg:
            try:
                native_index, cfg_changed, loaded = self._native_radio_station_index(station)
                radio_state = self.current_state or {}
                supports_ext = bool(radio_state.get("radio_ext_available"))
                supports_simple = bool(radio_state.get("radio_simple_available"))
                # openOMSI native radio is gapless, but its API clamps gain to 0..1.
                # Above 100%, use the FFmpeg chunk path so the requested preamp is real.
                requested_gain = float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))
                can_use_native = loaded and native_index is not None and (supports_ext or (supports_simple and native_index == 1)) and requested_gain <= 1.0
                if can_use_native and self._bridge and self._bridge.enabled:
                    # Do not send a separate stop command here. The Lua start_native_radio
                    # handler replaces the old source itself; two back-to-back file commands
                    # could otherwise race and leave the radio switched off.
                    self.stop_music(send_to_game=False)
                    self._radio_retry_count = 0
                    self.music_mode, self.music_active = "radio", True
                    self._music_started_event.clear(); self._music_finished_event.clear()
                    self._active_native_radio_id = self._bridge.start_native_radio(
                        native_index, (float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))), station["name"]
                    )
                    self._music_log(f"нативное радио openOMSI: {station['name']} · станция №{native_index}")
                    return
                if cfg_changed and not loaded:
                    self._music_log("Список радио обновлён; openOMSI прочитает новую станцию после перезапуска игры. Пока использую резервный поток.")
            except Exception as native_exc:
                self._music_log(f"нативное радио недоступно, включаю резервный поток: {native_exc}")

        ffmpeg = self._resolve_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("Не удалось подготовить радио", "Автоматически встроенный FFmpeg недоступен. Закрой и снова запусти run_app.bat, чтобы приложение установило свои зависимости.", parent=self._music_window or self)
            return
        if not self._bridge or not self._bridge.enabled:
            messagebox.showwarning("Нет соединения", "Сначала настрой интеграцию с openOMSI.", parent=self._music_window or self); return
        self.stop_music()
        self._radio_stop_event = threading.Event()
        self._radio_stream_generation += 1
        self._radio_retry_count = 0
        generation = self._radio_stream_generation
        self.music_mode = "radio"; self.music_active = True
        self._radio_stream_thread = threading.Thread(target=self._radio_stream_worker, args=(station, ffmpeg, generation, self._radio_stop_event), daemon=True, name="AutoInformant-radio")
        self._radio_stream_thread.start()
        self._music_log(f"подключение к {station['name']}")

    def _radio_stream_worker(self, station, ffmpeg, generation, stop_event):
        temp_dir = Path(tempfile.mkdtemp(prefix="auto-informant-radio-"))
        self._radio_stream_dir = temp_dir
        segment_pattern = temp_dir / "stream_%06d.wav"
        logs_dir = APP_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_log_path = logs_dir / "radio_ffmpeg.log"
        command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning", "-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1", "-reconnect_delay_max", "5", "-user_agent", "AutoInformantPro/1.5", "-i", station["url"], "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1", "-f", "segment", "-segment_time", "8", "-reset_timestamps", "1", "-segment_format", "wav", str(segment_pattern)]
        proc = None
        ffmpeg_log_file = None
        current_payload = None
        next_payload = None
        preloaded_payload = None

        def is_current():
            return (not stop_event.is_set() and self.music_active and self.music_mode == "radio" and generation == self._radio_stream_generation)

        def wait_for_closed_segment(index: int, timeout: float = 45.0):
            # To safely prepare segment index+1 we need index+2 to exist; FFmpeg's
            # segment muxer publishes a WAV only after it is closed.
            needed = temp_dir / f"stream_{index:06d}.wav"
            deadline = time.monotonic() + timeout
            while is_current():
                if needed.is_file():
                    return
                if proc is not None and proc.poll() is not None:
                    try:
                        if ffmpeg_log_file: ffmpeg_log_file.flush()
                        details = ffmpeg_log_path.read_text(encoding="utf-8", errors="replace")[-1800:]
                    except OSError:
                        details = ""
                    raise RuntimeError(f"FFmpeg завершился (код {proc.returncode}). Подробности: {details.strip() or 'см. logs/radio_ffmpeg.log'}")
                if time.monotonic() >= deadline:
                    try:
                        if ffmpeg_log_file: ffmpeg_log_file.flush()
                        details = ffmpeg_log_path.read_text(encoding="utf-8", errors="replace")[-1800:]
                    except OSError:
                        details = ""
                    raise RuntimeError("Радиопоток не выдавал закрытые аудиосегменты 45 секунд. "
                                       f"Диагностика FFmpeg: {details.strip() or 'см. logs/radio_ffmpeg.log'}")
                time.sleep(0.04)
            raise RuntimeError("Поток остановлен.")

        try:
            ffmpeg_log_file = ffmpeg_log_path.open("a", encoding="utf-8", errors="replace")
            ffmpeg_log_file.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {station.get('name')} <{station.get('url')}>\n")
            ffmpeg_log_file.flush()
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ffmpeg_log_file, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            self._radio_stream_proc = proc
            # Warm up the first two complete eight-second chunks and queue the second
            # before playback starts. This absorbs normal decode/network jitter.
            wait_for_closed_segment(0, timeout=45.0)
            wait_for_closed_segment(1, timeout=45.0)
            wait_for_closed_segment(2, timeout=45.0)
            first_path = temp_dir / "stream_000000.wav"
            second_path = temp_dir / "stream_000001.wav"
            third_path = temp_dir / "stream_000002.wav"
            with self._music_worker_lock:
                if stop_event.is_set() or not self._bridge:
                    return
                current_payload = self._bridge.prepare_music(first_path, (float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))), ephemeral=True)
                next_payload = self._bridge.prepare_music(second_path, (float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))), ephemeral=True)
                preloaded_payload = self._bridge.prepare_music(third_path, (float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))), ephemeral=True)
            first_path.unlink(missing_ok=True)
            second_path.unlink(missing_ok=True)
            third_path.unlink(missing_ok=True)

            self._music_started_event.clear()
            self._music_finished_event.clear()
            with self._music_worker_lock:
                if stop_event.is_set() or not self._bridge:
                    return
                self._bridge.start_prepared_music(current_payload)
            if not self._music_started_event.wait(12.0):
                if not is_current():
                    return
                raise RuntimeError("openOMSI не подтвердил запуск первого сегмента интернет-радио.")
            self._radio_retry_count = 0
            self._music_log(f"поток играет: {station['name']}")

            current_index = 0
            while is_current():
                # Hand off audio within Lua without waiting for Python at the end of
                # every WAV segment. We only keep one future segment queued by design.
                self._music_started_event.clear()
                with self._music_worker_lock:
                    if not self._bridge or not is_current():
                        break
                    self._bridge.queue_prepared_music(next_payload)
                if not self._music_started_event.wait(10.0):
                    if not is_current():
                        break
                    raise RuntimeError("openOMSI не запустил следующий сегмент интернет-радио.")
                if not is_current():
                    break
                old_payload = current_payload
                current_payload = next_payload
                next_payload = preloaded_payload
                preloaded_payload = None
                if self._bridge:
                    self._bridge.cleanup_prepared_music(old_payload)
                current_index += 1

                # Keep TWO prepared segments ahead. Decode and DSP the next WAV while
                # the current/queued segments play, rather than racing the live boundary.
                preload_index = current_index + 2
                wait_for_closed_segment(preload_index, timeout=45.0)
                preload_path = temp_dir / f"stream_{preload_index:06d}.wav"
                with self._music_worker_lock:
                    if not self._bridge or not is_current():
                        break
                    preloaded_payload = self._bridge.prepare_music(preload_path, (float(self.settings.get("volume", 0.90)) * float(self.settings.get("music_volume", 0.65))), ephemeral=True)
                preload_path.unlink(missing_ok=True)
        except Exception as exc:
            if is_current():
                message = str(exc)
                self.after(0, lambda msg=message: self._music_log(f"поток потерян: {msg}; переподключаюсь автоматически"))
                try:
                    with self._music_worker_lock:
                        if self._bridge:
                            self._bridge.stop_music()
                except Exception:
                    pass
                # Recover quietly from temporary network/FFmpeg failures.
                delay = min(15.0, 1.0 + self._radio_retry_count * 2.0)
                self._radio_retry_count = min(7, self._radio_retry_count + 1)
                def retry_radio():
                    if (not stop_event.is_set() and self.music_active and self.music_mode == "radio"
                            and generation == self._radio_stream_generation):
                        self._radio_stream_thread = threading.Thread(
                            target=self._radio_stream_worker,
                            args=(station, ffmpeg, generation, stop_event),
                            daemon=True, name="AutoInformant-radio-retry")
                        self._radio_stream_thread.start()
                self.after(int(delay * 1000), retry_radio)
        else:
            self._radio_retry_count = 0
        finally:
            try:
                if self._bridge:
                    self._bridge.cleanup_prepared_music(current_payload)
                    self._bridge.cleanup_prepared_music(next_payload)
                    self._bridge.cleanup_prepared_music(preloaded_payload)
            except Exception:
                pass
            if proc and proc.poll() is None:
                try: proc.terminate(); proc.wait(timeout=1.0)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
            if ffmpeg_log_file:
                try: ffmpeg_log_file.close()
                except Exception: pass
            try:
                for path in temp_dir.glob("*"): path.unlink(missing_ok=True)
                temp_dir.rmdir()
            except Exception: pass
            if self._radio_stream_proc is proc: self._radio_stream_proc = None
            if self._radio_stream_dir == temp_dir: self._radio_stream_dir = None

    def _toggle_auto(self):
        self.settings["auto_enabled"] = bool(self.var_auto.get())
        save_settings(self.settings)
        if not self.settings["auto_enabled"]:
            self.player.stop(clear_queue=True)
            self.engine.was_in_zone = False
            self.engine.zone_index = None
            self.log("Автоматические объявления выключены.")
        else:
            self.log("Автоматические объявления включены.")

    def open_settings(self):
        self._show_page("settings")

    def _build_settings_editor(self, page):
        """Build the existing advanced settings editor inside the main workspace."""
        win = self
        frame = ttk.Frame(page, padding=10)
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, highlightthickness=0, background="#101117")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))
        def _wheel(event):
            try:
                if event.widget.winfo_toplevel() is not win:
                    return None
            except Exception:
                return None
            if getattr(event, "delta", 0):
                canvas.yview_scroll(int(-event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(3, "units")
            return "break"
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind_all(sequence, _wheel)
        self._settings_wheel_bindings = [(sequence, None) for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>")]
        def _cleanup_scroll_bindings(event):
            if event.widget is win:
                for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    try:
                        win.unbind_all(sequence)
                    except tk.TclError:
                        pass
        win.bind("<Destroy>", _cleanup_scroll_bindings, add="+")
        vars: dict[str, Any] = {}
        combo_maps: dict[str, dict[str, str]] = {}
        row = 0

        def section(title: str):
            nonlocal row
            heading = ttk.Frame(inner, style="Card.TFrame", padding=(10, 7))
            heading.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(16, 7))
            ttk.Label(heading, text=title.upper(), style="Section.TLabel").pack(anchor="w")
            row += 1

        def checkbox(key: str, label: str):
            nonlocal row
            default = DEFAULT_SETTINGS.get(key, False)
            try:
                current = self.profile_setting(key, self.settings.get(key, default)) if self.map_name else self.settings.get(key, default)
            except Exception:
                current = self.settings.get(key, default)
            var = tk.BooleanVar(value=bool(current))
            vars[key] = var
            ModernSwitch(inner, label, var, background=self._ui_colors["panel"],
                         foreground=self._ui_colors["fg"], accent=self._ui_colors["accent"]).grid(
                             row=row, column=0, columnspan=3, sticky="w", pady=3)
            row += 1

        def slider(key: str, label: str, minv: float, maxv: float, resolution: float = 1.0, suffix: str = ""):
            nonlocal row
            ttk.Label(inner, text=label, wraplength=300, justify="left").grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            try:
                resolved = self.profile_setting(key, self.settings.get(key, DEFAULT_SETTINGS.get(key, minv))) if self.map_name else self.settings.get(key, DEFAULT_SETTINGS.get(key, minv))
            except Exception:
                resolved = self.settings.get(key, DEFAULT_SETTINGS.get(key, minv))
            initial_value = float(resolved)
            if key in {"volume", "music_volume", "informant_volume"}:
                initial_value *= 100.0  # internal gain is stored as a ratio; slider is percent
            var = tk.DoubleVar(value=initial_value)
            vars[key] = var
            value_label = ttk.Label(inner, text=f"{var.get():g}{suffix}", width=9, anchor="e")
            value_label.grid(row=row, column=2, sticky="e")
            scale_canvas = tk.Canvas(inner, height=25, bg=self._ui_colors["bg"], bd=0,
                                     highlightthickness=0, cursor="hand2")
            scale_canvas.grid(row=row, column=1, sticky="ew", padx=(4, 8))
            def draw_scale(_event=None, c=scale_canvas, v=var, low=minv, high=maxv):
                width = max(80, c.winfo_width())
                height = max(22, c.winfo_height())
                left, right = 9, width - 9
                y = height / 2
                current = max(low, min(high, float(v.get())))
                ratio = (current - low) / max(0.000001, high - low)
                pos = left + (right - left) * ratio
                c.delete("all")
                c.create_line(left, y, right, y, fill="#3c424b", width=5, capstyle="round")
                if pos > left:
                    c.create_line(left, y, pos, y, fill=self._ui_colors["accent"], width=5, capstyle="round")
                c.create_oval(pos-6, y-6, pos+6, y+6, fill="#f4f5f6",
                              outline=self._ui_colors["accent"], width=2)
            def apply_scale_event(event, c=scale_canvas, v=var, low=minv, high=maxv,
                                  step=resolution, label=value_label, unit=suffix):
                width = max(80, c.winfo_width())
                left, right = 9, width - 9
                ratio = max(0.0, min(1.0, (float(event.x) - left) / max(1.0, right - left)))
                value = low + ratio * (high - low)
                if step and step > 0:
                    value = low + round((value - low) / step) * step
                value = max(low, min(high, value))
                # Avoid floating point tails for values that are specified in tenths/hundredths.
                decimals = max(0, min(5, len(str(step).split(".")[-1]) if "." in str(step) else 0))
                value = round(value, decimals)
                v.set(value)
                label.configure(text=f"{value:g}{unit}")
                draw_scale()
            scale_canvas.bind("<Configure>", draw_scale)
            scale_canvas.bind("<Button-1>", apply_scale_event)
            scale_canvas.bind("<B1-Motion>", apply_scale_event)
            self.after_idle(draw_scale)
            row += 1

        def entry(key: str, label: str, default: str = ""):
            nonlocal row
            ttk.Label(inner, text=label, wraplength=300, justify="left").grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            try:
                resolved = self.profile_setting(key, self.settings.get(key, default)) if self.map_name else self.settings.get(key, default)
            except Exception:
                resolved = self.settings.get(key, default)
            var = tk.StringVar(value=str(resolved))
            vars[key] = var
            ttk.Entry(inner, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
            row += 1

        def combo(key: str, label: str, choices: list[tuple[str, str]]):
            nonlocal row
            ttk.Label(inner, text=label, wraplength=300, justify="left").grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            try:
                current_value = str(self.profile_setting(key, self.settings.get(key, choices[0][0])) if self.map_name else self.settings.get(key, choices[0][0]))
            except Exception:
                current_value = str(self.settings.get(key, choices[0][0]))
            value_to_label = {value: label for value, label in choices}
            label_to_value = {label: value for value, label in choices}
            var = tk.StringVar(value=value_to_label.get(current_value, choices[0][1]))
            vars[key] = var
            combo_maps[key] = label_to_value
            widget = ttk.Combobox(inner, textvariable=var, state="readonly", values=[x[1] for x in choices])
            widget.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
            row += 1

        section("Источник и файлы")
        entry("audio_root", "Корневая папка аудиотеки", str(APP_DIR / "maps"))
        ttk.Button(inner, text="Выбрать…", command=lambda: self._choose_audio_root(vars.get("audio_root"))).grid(row=row - 1, column=2, sticky="e", padx=(4, 0))
        current_map = self.map_name
        if current_map and current_map != "Неизвестная карта":
            base = self.pack_manager.map_profile(current_map)
            category = self.pack_manager.category_profile(current_map, self.current_state)
            profile_line = f"Карта в игре: {current_map}\nОсновной пак: {base[1].get('name') if base else 'не установлен'}\nКатегориальный слой: {category[1].get('name') if category else 'не назначен для этого транспорта'}"
        else:
            profile_line = "Пакеты карт и категорий настраиваются во вкладке «Аудиопаки». Маршрут и остановки приложение продолжает получать из duty openOMSI."
        ttk.Label(inner, text=profile_line, foreground="#989dad", wraplength=760, justify="left").grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 7))
        row += 1
        ttk.Button(inner, text="Открыть менеджер аудиопаков…", command=lambda: self._show_page("packs")).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 7))
        row += 1
        entry("plugin_dir", "Путь к плагину AutoInformantPro", "")
        ttk.Button(inner, text="Обновить плагин…", command=lambda: self._settings_install_plugin(win)).grid(row=row - 1, column=2, sticky="e", padx=(4, 0))
        checkbox("auto_enabled", "Автоматически объявлять остановки")
        checkbox("auto_arrival", "Объявлять остановку при прибытии")
        checkbox("auto_departure", "Объявлять следующую остановку при отъезде")
        checkbox("play_next_prefix", "Перед названием играть файл NEXT / «Следующая» (если найден)")
        checkbox("play_terminus", "Играть TERMINUS после названия конечной — при объявлении следующей и при прибытии")
        checkbox("play_jingle", "Играть JINGLE.mp3 один раз перед каждой целой фразой")
        ttk.Button(inner, text="Расписание анонсов: даты и время…",
                   command=self.open_announcement_schedule_editor).grid(row=row, column=0, columnspan=3, sticky="w", pady=(5, 2))
        row += 1
        ttk.Button(inner, text="DOORS: настройка закрытия дверей для текущей карты…",
                   command=self.toggle_doors_for_current_map).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 4))
        row += 1

        section("GPS-подобное определение остановки")
        slider("arrival_radius", "Радиус прибытия", 5, 45, 1, " м")
        slider("max_arrival_speed", "Макс. скорость для GPS-прибытия", 0, 8, 0.5, " км/ч")
        slider("exit_speed", "Считать автобус отъехавшим при", 1, 15, 0.5, " км/ч")
        slider("average_speed_kmh", "Средняя скорость для оценки времени", 10, 65, 1, " км/ч")

        section("Анонсы между остановками")
        checkbox("anons_enabled", "Автоматически проигрывать анонсы")
        combo("anons_mode", "Частота анонсов", [("stops", "Каждые X остановок"), ("minutes", "Каждые X минут игрового времени")])
        slider("anons_interval", "X — интервал", 1, 20, 1, "")
        slider("anons_safety_seconds", "Оставлять запас перед остановкой", 0, 20, 1, " с")
        ttk.Label(inner, text="Автоанонс подбирает только файлы, которые по длительности помещаются в расчётный интервал. Если ETA неизвестно — не рискует начать длинный анонс.", wraplength=620, foreground="#989dad").grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        section("Громкость воспроизведения")
        # The UI uses percent, while the internal/API setting remains 0.0..1.0.
        slider("volume", "Общая громкость (информатор + музыка)", 0, 100, 1, "%")
        ttk.Label(inner, text="Снаружи проигрывается отдельная приглушённая версия, а не тот же самый громкий сигнал из салона.", foreground="#989dad").grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        section("Эффект маленького динамика (отдельно от громкости)")
        slider("speaker_effect", "Сила окраски/искажения динамика", 0, 100, 1, "%")
        ttk.Label(inner, text="0% — чистый исходник; 100% — выраженная окраска динамика с сохранением разборчивости речи и верхних частот. Изменение применяется к следующему воспроизведению.", wraplength=620, foreground="#989dad").grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        slider("informant_volume", "Громкость информатора отдельно (до 200%)", 0, 200, 1, "%")
        ttk.Label(inner, text="Выше 100% сигнал аккуратно усиливается в WAV с ограничителем пиков, а не обрезается ползунком игры.", foreground="#989dad", wraplength=620).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1
        slider("music_volume", "Громкость музыки / радио отдельно (до 200%)", 0, 200, 1, "%")
        ttk.Label(inner, text="Музыка играет отдельным пространственным источником; объявления могут звучать поверх неё. Общая громкость ограничивает оба источника.", foreground="#989dad", wraplength=620).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        section("Пространственная подача и звук снаружи")
        slider("exterior_volume_pct", "Информатор снаружи (относительно салона)", 0, 100, 1, "%")
        slider("music_exterior_volume_pct", "Музыка снаружи (относительно салона)", 0, 100, 1, "%")
        slider("interior_range", "Радиус информатора в салоне", 1, 50, 1, " м")
        slider("exterior_range", "Радиус внешнего приглушённого звука", 2, 100, 1, " м")
        combo("source_mode", "Позиция информатора", [("ceiling", "Потолочный динамик / PA"), ("bus", "Условный центр автобуса"), ("fixed", "Фиксированная точка карты")])
        slider("speaker_forward", "Информатор — вперёд / назад", -5, 5, 0.05, " м")
        slider("speaker_right", "Информатор — вправо", -3, 3, 0.05, " м")
        slider("speaker_up", "Информатор — вверх", -1, 5, 0.05, " м")

        section("Отдельная позиция музыкального динамика")
        ttk.Label(inner, text="Источники разделены: информатор по умолчанию звучит из потолочного динамика PA, а радио/музыка — из передней магнитолы. Координаты — относительно автобуса (вперёд/назад, вправо/влево, вверх/вниз). Для локализации используется моно-сигнал; в салоне и снаружи звучат отдельные варианты.", wraplength=620, foreground="#989dad").grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row += 1
        combo("music_source_mode", "Позиция музыки", [("ceiling", "Передняя магнитола / динамик (3D-точка)"), ("bus", "Условный центр автобуса"), ("fixed", "Фиксированная точка карты")])
        slider("music_range", "Радиус музыки в салоне", 1, 50, 1, " м")
        slider("music_speaker_forward", "Музыка — вперёд / назад", -5, 5, 0.05, " м")
        slider("music_speaker_right", "Музыка — вправо / влево", -3, 3, 0.05, " м")
        slider("music_speaker_up", "Музыка — высота динамика", -1, 5, 0.05, " м")
        entry("music_fixed_x", "Музыка — карта X (фиксированный режим)", "0")
        entry("music_fixed_y", "Музыка — карта Y (фиксированный режим)", "0")
        entry("music_fixed_z", "Музыка — карта Z (фиксированный режим)", "0")

        # Keep the settings editor inside the visible client area at 1366x768 and
        # smaller DPI-scaled workspaces. Long labels wrap instead of widening the page.
        inner.columnconfigure(0, weight=0, minsize=230)
        inner.columnconfigure(1, weight=1, minsize=180)
        inner.columnconfigure(2, weight=0, minsize=96)
        buttons = ttk.Frame(page, padding=(10, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Открыть аудиотеку", command=self.open_audio_root).pack(side="left")
        ttk.Button(buttons, text="Сохранить настройки", style="Accent.TButton",
                   command=lambda: self._save_settings_window(win, vars, combo_maps)).pack(side="right")
        ttk.Button(buttons, text="Назад", command=lambda: self._show_page("home")).pack(side="right", padx=(0, 8))

    def choose_audio_folder_for_current_map(self):
        """Select a direct audio bank for the currently loaded in-game map."""
        map_name = str(self.map_name or "").strip()
        if not map_name or map_name == "Неизвестная карта":
            messagebox.showinfo(
                "Сначала выбери карту",
                "Загрузи карту и активный рейс в openOMSI. После этого можно назначить отдельную папку озвучки именно для этой карты.",
                parent=self,
            )
            return
        existing = self.settings.setdefault("map_audio_folders", {}).get(map_name)
        initial = existing or str(self.library._map_dir(map_name, self.current_state))
        if not Path(initial).is_dir():
            initial = str(self.library.root if self.library.root.is_dir() else APP_DIR)
        chosen = filedialog.askdirectory(
            title=f"Папка аудиотеки для карты «{map_name}» (любой вариант озвучки)",
            initialdir=initial,
            parent=self,
        )
        if not chosen:
            return
        folder = Path(chosen).expanduser().resolve()
        if not folder.is_dir():
            messagebox.showerror("Папка не найдена", f"Папка недоступна:\n{folder}", parent=self)
            return
        self.settings.setdefault("map_audio_folders", {})[map_name] = str(folder)
        self.library.map_folders = self.settings["map_audio_folders"]
        self.library._index_cache.clear()
        save_settings(self.settings)
        self.refresh_audio_library()
        self.log(f"Для карты «{map_name}» выбрана отдельная аудиотека: {folder}")
        messagebox.showinfo(
            "Аудиотека назначена",
            f"Для карты «{map_name}» теперь используется:\n{folder}\n\nЭта папка может содержать другую озвучку тех же остановок. Имя папки не обязано совпадать с именем карты.",
            parent=self,
        )

    def reset_audio_folder_for_current_map(self):
        map_name = str(self.map_name or "").strip()
        if not map_name or map_name == "Неизвестная карта":
            messagebox.showinfo("Сначала выбери карту", "Нет активной карты openOMSI.", parent=self)
            return
        folders = self.settings.setdefault("map_audio_folders", {})
        keys = [key for key in folders if key.casefold() == map_name.casefold()]
        if not keys:
            messagebox.showinfo("Нет отдельного выбора", f"Для карты «{map_name}» отдельная аудиотека не задана.", parent=self)
            return
        for key in keys:
            folders.pop(key, None)
        self.library.map_folders = folders
        self.library._index_cache.clear()
        save_settings(self.settings)
        self.refresh_audio_library()
        self.log(f"Для карты «{map_name}» восстановлен стандартный поиск maps/<имя карты>.")

    def _choose_audio_root(self, var):
        path = filedialog.askdirectory(title="Выбери корневую папку аудио", initialdir=str(self.library.root), parent=self)
        if path and var:
            var.set(path)

    def _settings_install_plugin(self, settings_window):
        settings_window.grab_release()
        settings_window.destroy()
        self.install_plugin()

    def _save_settings_window(self, win, vars, combo_maps: dict[str, dict[str, str]] | None = None):
        try:
            previous_plugin_dir = str(self.settings.get("plugin_dir") or "")
            combo_maps = combo_maps or {}
            for key, var in vars.items():
                value = var.get()
                if key in combo_maps:
                    value = combo_maps[key].get(str(value), str(value))
                if key == "volume":
                    value = max(0.0, min(1.0, float(value) / 100.0))
                elif key in {"music_volume", "informant_volume"}:
                    # Source gains may exceed 100%; extra gain is applied before a limiter.
                    value = max(0.0, min(2.0, float(value) / 100.0))
                elif key in {"arrival_radius", "max_arrival_speed", "exit_speed", "average_speed_kmh", "anons_safety_seconds",
                             "exterior_volume_pct", "music_exterior_volume_pct", "interior_range", "exterior_range", "speaker_forward", "speaker_right", "speaker_up",
                             "music_range", "music_speaker_forward", "music_speaker_right", "music_speaker_up",
                             "music_fixed_x", "music_fixed_y", "music_fixed_z"}:
                    value = float(value)
                elif key == "speaker_effect" or key == "anons_interval":
                    value = int(round(float(value)))
                elif key in {"auto_enabled", "auto_arrival", "auto_departure", "play_next_prefix", "play_terminus", "anons_enabled", "play_jingle"}:
                    value = bool(value)
                elif key in {"audio_root", "plugin_dir", "anons_mode", "source_mode", "music_source_mode"}:
                    value = str(value).strip()
                pack_scoped = {"auto_arrival", "auto_departure", "arrival_radius", "max_arrival_speed", "exit_speed", "average_speed_kmh", "play_next_prefix", "play_terminus", "anons_enabled", "anons_mode", "anons_interval", "anons_safety_seconds", "play_jingle"}
                saved_to_pack = False
                if key in pack_scoped and self.map_name and self.map_name != "Неизвестная карта":
                    saved_to_pack = self.pack_manager.set_setting(self.map_name, self.current_state, key, value)
                if not saved_to_pack:
                    self.settings[key] = value
            self.settings["udp_port"] = 47821
            save_settings(self.settings)
            self.library.set_root(self.settings.get("audio_root") or APP_DIR)
            self.library.overrides = self.settings.get("overrides", {})
            self.library.map_folders = self.settings.get("map_audio_folders", {})
            self.library.vehicle_folders = self.settings.get("vehicle_audio_folders", {})
            self.library._index_cache.clear()
            self._rebuild_stop_rows()
            if previous_plugin_dir != str(self.settings.get("plugin_dir") or ""):
                self._configure_bridge(restart=True)
            elif self._bridge:
                self._bridge.update_config(self._bridge_config())
            else:
                self._configure_bridge(restart=True)
            self.var_auto.set(bool(self.settings.get("auto_enabled", True)))
            self.log(f"Настройки сохранены. Общая громкость: {self.settings.get('volume', 0.9) * 100:.0f}%; информатор: {self.settings.get('informant_volume', 1.0) * 100:.0f}%; музыка: {self.settings.get('music_volume', 0.65) * 100:.0f}%; эффект динамика: {self.settings.get('speaker_effect', 45)}%. Новые настройки DSP применяются к следующему воспроизведению; старый кэш не удалялся.")
            self._show_page("settings")
            self.log("Настройки сохранены.")
        except Exception as exc:
            traceback.print_exc()
            messagebox.showerror("Ошибка настроек", f"{exc}\n\n{traceback.format_exc()}", parent=self)

    def refresh_audio_library(self):
        self.library._index_cache.clear()
        self.library._duration_cache.clear()
        self._rebuild_stop_rows()
        missing = sum(1 for idx, stop in enumerate(self.stops) if self.is_announceable_stop(stop, idx) and not self.resolve_stop_audio(stop, idx))
        self.log(f"Аудиотека пересканирована. Остановок без найденного файла: {missing}.")

    def open_audio_root(self):
        root = Path(self.settings.get("audio_root") or APP_DIR)
        try:
            root.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(root))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(root)])
            else:
                subprocess.Popen(["xdg-open", str(root)])
        except Exception as exc:
            messagebox.showerror("Не удалось открыть", str(exc), parent=self)

    def _schedule_heartbeat(self):
        def tick():
            if self._bridge and self._bridge.connected:
                self.lbl_connection.configure(text="● openOMSI подключён", foreground="#4ade80")
            elif self.settings.get("plugin_dir"):
                self.lbl_connection.configure(text="● ждём openOMSI", foreground="#f6bd60")
            else:
                self.lbl_connection.configure(text="● установи плагин", foreground="#f6bd60")
            if self.current_state:
                sync = "подключён" if self._bridge and self._bridge.connected else "нет связи с плагином"
                route_status = self._status_message
                self.lbl_sync.configure(text=f"{sync} · {route_status}")
            self.after(800, tick)
        self.after(800, tick)

    def on_close(self):
        try:
            if self._yandex_login_cancel_event: self._yandex_login_cancel_event.set()
        except Exception: pass
        try:
            self.stop_music()
        except Exception:
            pass
        try:
            self.player.close()
        except Exception:
            pass
        try:
            if self._bridge:
                self._bridge.close()
        except Exception:
            pass
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self.destroy()


def main():
    app = AutoInformantApp()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("AutoInformant Pro — ошибка запуска", f"{exc}\n\n{traceback.format_exc()}")
            root.destroy()
        except Exception:
            pass

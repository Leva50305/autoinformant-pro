# -*- coding: utf-8 -*-
"""Core helpers for AutoInformant Pro. Kept GUI-independent for testability."""
from __future__ import annotations

import math
import re
import wave
from pathlib import Path
from typing import Any, Iterable

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
AUX_ALIASES = {
    "next": {"next", "далее", "следующая", "следующая остановка", "sled"},
    "prev": {"prev", "back", "назад", "предыдущая", "предыдущая остановка"},
    "signal": {"signal", "sign", "jingle", "джингл", "сигнал"},
    "terminus": {"terminus", "конечная", "конец", "final", "konechnaya"},
}


def normalize_text(value: Any) -> str:
    s = str(value or "").replace("\u00a0", " ").strip().casefold()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\u2010-\u2015]+", "-", s)
    s = re.sub(r"[^\w\- ]+", "", s, flags=re.UNICODE)
    return s.strip()


def folder_key(value: Any) -> str:
    """Normalize folder names across maps using spaces, underscores and hyphens."""
    return re.sub(r"\s+", " ", normalize_text(value).replace("_", " ").replace("-", " ")).strip()


def stem_aliases(path_or_name: str | Path) -> set[str]:
    p = Path(path_or_name)
    stem = p.stem if p.suffix else str(path_or_name)
    raw = stem.strip()
    candidates = {normalize_text(raw)}
    # Common archive/export prefixes: "01 - Stop", "001.Stop", "12_Stop".
    stripped = re.sub(r"^\s*\d{1,4}\s*[._\-–—)]+\s*", "", raw)
    if stripped != raw:
        candidates.add(normalize_text(stripped))
    stripped2 = re.sub(r"^\s*\[?\d{1,4}\]?\s+", "", raw)
    if stripped2 != raw:
        candidates.add(normalize_text(stripped2))
    return {x for x in candidates if x}


def safe_float(value: Any) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def duration_seconds(path: str | Path) -> float | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        if p.suffix.lower() == ".wav":
            with wave.open(str(p), "rb") as wf:
                return wf.getnframes() / max(1, wf.getframerate())
    except Exception:
        pass
    try:
        from mutagen import File as MutagenFile  # type: ignore
        audio = MutagenFile(str(p))
        length = getattr(getattr(audio, "info", None), "length", None)
        if length is not None and math.isfinite(float(length)) and float(length) >= 0:
            return float(length)
    except Exception:
        pass
    return None


def stop_key(map_name: str, stop: dict[str, Any], index: int) -> str:
    object_id = stop.get("id", stop.get("object_id"))
    if object_id not in (None, ""):
        return f"{map_name.casefold()}|id:{object_id}"
    return f"{map_name.casefold()}|name:{normalize_text(stop.get('name'))}|index:{index}"


def route_identity(state: dict[str, Any], map_name: str) -> str:
    return "|".join([
        map_name.casefold(), str(state.get("line") or ""), str(state.get("tour") or ""),
        str(state.get("trip") or ""), str(state.get("trip_name") or ""),
        str(state.get("terminus") or ""),
    ])


def normalize_stop_objects(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("duty_stop_objects")
    if isinstance(raw, list) and raw:
        stops: list[dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if isinstance(item, dict):
                st = dict(item)
                st["name"] = str(st.get("name") or "").strip()
                st["raw_index"] = int(st.get("raw_index", idx + 1) or idx + 1)
                if st["name"]:
                    stops.append(st)
            elif str(item or "").strip():
                stops.append({"name": str(item).strip(), "raw_index": idx + 1})
        return stops
    names = state.get("duty_stops")
    if isinstance(names, list):
        return [{"name": str(n).strip(), "raw_index": i + 1}
                for i, n in enumerate(names) if str(n or "").strip()]
    return []


def find_current_stop_index(state: dict[str, Any], stops: list[dict[str, Any]]) -> int | None:
    """Return zero-based index; prefer stop object ID over potentially repeated names."""
    if not stops:
        return None
    target_id = state.get("next_stop_id")
    if target_id not in (None, ""):
        for i, st in enumerate(stops):
            sid = st.get("id", st.get("object_id"))
            if sid not in (None, "") and str(sid) == str(target_id):
                return i
    try:
        n = int(state.get("next_stop_index") or 0)
        if n >= 1:
            for i, stop in enumerate(stops):
                if int(stop.get("raw_index", i + 1) or i + 1) == n:
                    return i
            if n <= len(stops):
                return n - 1
    except (ValueError, TypeError):
        pass
    target_name = normalize_text(state.get("next_stop_name"))
    if target_name:
        matches = [i for i, s in enumerate(stops) if normalize_text(s.get("name")) == target_name]
        if len(matches) == 1:
            return matches[0]
    return None


def is_announceable_stop(stop: dict[str, Any]) -> bool:
    # openOMSI API: `stops == false` means this trip passes the point without stopping.
    return (stop.get("stops") is not False and stop.get("closed") is not True
            and not bool(stop.get("pass_through", False)))


def estimated_time_to_next_stop(
    state: dict[str, Any], stops: list[dict[str, Any]],
    current_index: int | None, average_speed_kmh: float = 25.0,
    route_distance_factor: float = 1.55,
) -> float | None:
    """Conservative ETA in game/real seconds using schedule, nav distance, or stop coords."""
    if current_index is None or current_index >= len(stops):
        return None
    # Prefer timetable arrival/departure values when they form a plausible positive interval.
    previous_index = current_index - 1
    if previous_index >= 0:
        prev_dep = safe_float(stops[previous_index].get("departure"))
        next_arr = safe_float(stops[current_index].get("arrival"))
        if prev_dep is not None and next_arr is not None:
            delta = (next_arr - prev_dep) % 86400.0
            if 8.0 <= delta <= 3600.0:
                return delta
    # Navigation distance (nil when the navigator and route arrows are off).
    distance = safe_float(state.get("next_stop_distance_nav"))
    if distance is None or distance < 20.0:
        # The straight-line distance to the stop is also useful, but can be small immediately
        # after departure because state may still refer to the stop just served.
        d2 = safe_float(state.get("next_stop_distance"))
        if d2 is not None and d2 >= 20.0:
            distance = d2
    if distance is not None and 20.0 <= distance <= 6000.0:
        speed = max(average_speed_kmh * 0.75, safe_float(state.get("speed")) or 0.0, 12.0)
        # Slight safety margin: the road is generally longer than straight-line/nav distance.
        return (distance / (speed / 3.6)) * 1.15
    # Estimate from known map coordinates; direct distance is padded for the road path.
    if previous_index >= 0:
        x1, y1 = safe_float(stops[previous_index].get("x")), safe_float(stops[previous_index].get("y"))
        x2, y2 = safe_float(stops[current_index].get("x")), safe_float(stops[current_index].get("y"))
        if None not in (x1, y1, x2, y2):
            dist = math.hypot(x2 - x1, y2 - y1) * max(1.0, route_distance_factor)
            if 20.0 <= dist <= 10000.0:
                return dist / (max(10.0, average_speed_kmh) / 3.6)
    return None


class AudioLibrary:
    """Resolves stop and announcement audio without requiring route.txt."""
    def __init__(
        self,
        root: str | Path,
        overrides: dict[str, str] | None = None,
        map_folders: dict[str, str] | None = None,
        vehicle_folders: dict[str, str] | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.overrides = overrides if overrides is not None else {}
        self.map_folders = map_folders if map_folders is not None else {}
        self.vehicle_folders = vehicle_folders if vehicle_folders is not None else {}
        self.pack_manager = None
        self._index_cache: dict[str, tuple[float, float, list[Path]]] = {}
        self._duration_cache: dict[str, tuple[float, float | None]] = {}

    def set_root(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._index_cache.clear()

    def set_pack_manager(self, manager) -> None:
        """Attach the optional portable profile resolver without changing legacy lookup."""
        self.pack_manager = manager
        self._index_cache.clear()

    def set_map_folder(self, map_name: str, folder: str | Path) -> None:
        """Use an explicit voice/audio bank folder for one in-game map.

        The selected folder may have any name (e.g. ``Trolleybus Female``) and
        is used directly instead of requiring ``audio_root/<map_name>``.
        """
        name = str(map_name or "").strip()
        path = Path(folder).expanduser().resolve()
        if name and path.is_dir():
            self.map_folders[name] = str(path)
            self._index_cache.clear()

    def clear_map_folder(self, map_name: str) -> None:
        name = str(map_name or "").strip()
        to_remove = [key for key in self.map_folders if key.casefold() == name.casefold()
                     or folder_key(key) == folder_key(name)]
        for key in to_remove:
            self.map_folders.pop(key, None)
        self._index_cache.clear()

    @staticmethod
    def vehicle_bank_key(state: dict[str, Any] | None) -> str:
        state = state or {}
        manufacturer = str(state.get("vehicle_manufacturer") or "").strip()
        model = str(state.get("vehicle_model") or "").strip()
        if not model and not manufacturer:
            return ""
        return " | ".join(part for part in (manufacturer, model) if part)

    def _vehicle_dir(self, state: dict[str, Any] | None) -> Path | None:
        key = self.vehicle_bank_key(state)
        if not key:
            return None
        selected = self.vehicle_folders.get(key)
        if selected is None:
            wanted = folder_key(key)
            selected = next((value for name, value in self.vehicle_folders.items() if folder_key(name) == wanted), None)
        if selected:
            path = Path(selected).expanduser()
            if path.is_dir():
                return path.resolve()
        return None

    def _map_dir(self, map_name: str, state: dict[str, Any] | None = None) -> Path:
        # An assigned pack category can replace the entire map bank for this vehicle.
        # In that mode stop clips, cues and announcements all resolve from the category.
        if self.pack_manager is not None:
            packed = self.pack_manager.effective_audio_dir(map_name, state)
            if packed is not None and packed.is_dir():
                return packed.resolve()
        # Legacy per-vehicle audio banks have priority over map-specific banks. This allows
        # one vehicle model to retain its own voice pack across different maps.
        vehicle_dir = self._vehicle_dir(state)
        if vehicle_dir is not None:
            return vehicle_dir
        name = str(map_name or "").strip()
        # An explicit per-map voice-bank selection has priority. This lets users
        # swap male/female announcers or map variants without renaming directories.
        selected = None
        if name:
            selected = self.map_folders.get(name)
            if selected is None:
                for key, value in self.map_folders.items():
                    if folder_key(key) == folder_key(name):
                        selected = value
                        break
        if selected:
            explicit = Path(selected).expanduser()
            if explicit.is_dir():
                return explicit.resolve()
        candidate = self.root / map_name
        if candidate.is_dir():
            return candidate
        # Allow selecting the map's folder itself as the audio root.
        if folder_key(self.root.name) == folder_key(map_name):
            return self.root
        # Windows directories are case-insensitive in ordinary installs, but pack names
        # and the map's in-game title often differ by underscore/hyphen/space punctuation.
        # Match a sibling directory only after the exact path failed.
        if self.root.is_dir():
            try:
                children = [p for p in self.root.iterdir() if p.is_dir()]
                exact = [p for p in children if folder_key(p.name) == folder_key(map_name)]
                if exact:
                    return sorted(exact, key=lambda p: p.name.casefold())[0]
            except OSError:
                pass
        # Do not silently search another map when the expected folder is missing.
        return candidate

    def _all_files(self, base: Path, key: str) -> list[Path]:
        if not base.is_dir():
            return []
        try:
            stamp = base.stat().st_mtime
        except OSError:
            stamp = 0.0
        import time
        now = time.monotonic()
        cached = self._index_cache.get(key)
        if cached and cached[0] == stamp and now - cached[1] < 4.0:
            return cached[2]
        files: list[Path] = []
        try:
            for p in base.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                    continue
                # Stop audio search must never accidentally take a spot from an announcer bank.
                if any(normalize_text(part) in {"анонсы", "announcements", "anons"} for part in p.parts):
                    continue
                files.append(p)
        except (OSError, PermissionError):
            pass
        files.sort(key=lambda x: str(x).casefold())
        self._index_cache[key] = (stamp, now, files)
        return files

    def _route_hint_dirs(self, map_dir: Path, state: dict[str, Any]) -> list[Path]:
        hints = []
        line = normalize_text(state.get("line"))
        tour = normalize_text(state.get("tour"))
        trip = normalize_text(state.get("trip_name"))
        tour2 = tour.zfill(2) if tour.isdigit() else tour
        tour3 = tour.zfill(3) if tour.isdigit() else tour
        for label in (f"{line}-{tour}" if line and tour else "", f"{line}-{tour2}" if line and tour2 else "",
                      f"{line}-{tour3}" if line and tour3 else "", f"{line}{tour}" if line and tour else "",
                      f"{line}{tour2}" if line and tour2 else "", trip, line):
            if not label:
                continue
            exact = map_dir / label
            if exact.is_dir() and exact not in hints:
                hints.append(exact)
            try:
                for child in map_dir.iterdir():
                    if child.is_dir() and folder_key(child.name) == folder_key(label) and child not in hints:
                        hints.append(child)
            except OSError:
                pass
        # Older StopListMaker output stores shared audio at map/All/<...>.
        for candidate in (map_dir / "All", map_dir / "all"):
            if candidate.is_dir() and candidate not in hints:
                hints.append(candidate)
        return hints

    def _override_path(self, map_name: str, stop: dict[str, Any], index: int) -> Path | None:
        key = stop_key(map_name, stop, index)
        stored = self.overrides.get(key)
        if not stored:
            return None
        p = Path(stored).expanduser()
        if not p.is_absolute():
            p = self.root / p
        return p.resolve() if p.is_file() and p.suffix.lower() in AUDIO_EXTS else None

    def find_stop_audio(self, map_name: str, state: dict[str, Any], stop: dict[str, Any], index: int) -> Path | None:
        # Once a portable map/category profile is active, its relative override table
        # is authoritative. Do not leak absolute overrides from the old installation
        # into another package or into an exclusive vehicle category.
        has_pack_profile = False
        if self.pack_manager is not None:
            try:
                has_pack_profile = bool(self.pack_manager.category_profile(map_name, state) or self.pack_manager.map_profile(map_name))
            except Exception:
                has_pack_profile = False
        override = None if has_pack_profile else self._override_path(map_name, stop, index)
        if override:
            return override
        map_dir = self._map_dir(map_name, state)
        target = normalize_text(stop.get("name"))
        if not target:
            return None
        # Search preferred route paths before all routes, then other map folders.
        search_groups: list[list[Path]] = []
        hints = self._route_hint_dirs(map_dir, state)
        if hints:
            search_groups.append([p for p in hints if p.name.casefold() != "all"])
            search_groups.append([p for p in hints if p.name.casefold() == "all"])
        search_groups.append([map_dir])
        visited: set[str] = set()
        for group_num, folders in enumerate(search_groups):
            candidates: list[tuple[int, str, Path]] = []
            for folder in folders:
                fkey = f"{group_num}:{folder}"
                for p in self._all_files(folder, fkey):
                    full = str(p.resolve())
                    if full in visited:
                        continue
                    visited.add(full)
                    aliases = stem_aliases(p)
                    if target in aliases:
                        # A direct stop-name match always beats names with numbering prefixes.
                        exactness = 0 if normalize_text(p.stem) == target else 1
                        candidates.append((exactness, str(p).casefold(), p))
            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1]))
                return candidates[0][2]
        return None

    def announcement_files(self, map_name: str, state: dict[str, Any]) -> list[Path]:
        map_dir = self._map_dir(map_name, state)
        dirs: list[Path] = []
        for route_dir in self._route_hint_dirs(map_dir, state):
            candidate = route_dir / "Анонсы"
            if candidate.is_dir() and candidate not in dirs:
                dirs.append(candidate)
            candidate = route_dir / "Announcements"
            if candidate.is_dir() and candidate not in dirs:
                dirs.append(candidate)
        pack_active = False
        if self.pack_manager is not None:
            try:
                pack_active = self.pack_manager.has_profile(map_name, state)
            except Exception:
                pack_active = False
        candidates = [map_dir / "Анонсы", map_dir / "Announcements", map_dir / "All" / "Анонсы"]
        if not pack_active:
            candidates.extend((self.root / "Анонсы", self.root / "Announcements"))
        for candidate in candidates:
            if candidate.is_dir() and candidate not in dirs:
                dirs.append(candidate)
        # If the map has another naming convention, discover folders literally named as announcements.
        try:
            for child in map_dir.rglob("*"):
                if child.is_dir() and folder_key(child.name) in {"анонсы", "announcements", "announcement", "anons"}:
                    if child not in dirs:
                        dirs.append(child)
        except OSError:
            pass
        result: list[Path] = []
        seen: set[str] = set()
        for folder in dirs:
            try:
                for p in sorted(folder.iterdir(), key=lambda q: q.name.casefold()):
                    if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                        cue_aliases = (AUX_ALIASES["next"] | AUX_ALIASES["prev"] |
                                       AUX_ALIASES["signal"] | AUX_ALIASES["terminus"])
                        if stem_aliases(p) & {normalize_text(value) for value in cue_aliases}:
                            continue
                        full = str(p.resolve())
                        if full not in seen:
                            result.append(p)
                            seen.add(full)
            except OSError:
                continue
        return result

    def _map_only_dir(self, map_name: str) -> Path:
        """Resolve the base map pack folder without category replacement."""
        if self.pack_manager is not None:
            packed = self.pack_manager.map_audio_dir(map_name)
            if packed is not None and packed.is_dir():
                return packed.resolve()
        name = str(map_name or "").strip()
        selected = self.map_folders.get(name)
        if selected is None:
            selected = next((value for key, value in self.map_folders.items() if folder_key(key) == folder_key(name)), None)
        if selected:
            explicit = Path(selected).expanduser()
            if explicit.is_dir(): return explicit.resolve()
        candidate = self.root / name
        if candidate.is_dir(): return candidate.resolve()
        if folder_key(self.root.name) == folder_key(name) and self.root.is_dir(): return self.root
        try:
            matches = [p for p in self.root.iterdir() if p.is_dir() and folder_key(p.name) == folder_key(name)]
            if matches: return sorted(matches, key=lambda p: p.name.casefold())[0].resolve()
        except OSError:
            pass
        return candidate

    def named_cue_file(self, map_name: str, state: dict[str, Any] | None, cue_name: str) -> Path | None:
        """Find exact JINGLE/DOORS cues without crossing an active pack boundary.

        A map pack owns its own cues. An assigned category pack is an exclusive
        replacement for the base map pack on that vehicle. Legacy vehicle folders
        are consulted only when no portable pack profile matches the current state.
        """
        target = normalize_text(cue_name)
        pack_active = False
        if self.pack_manager is not None:
            try:
                pack_active = self.pack_manager.has_profile(map_name, state or {})
            except Exception:
                pack_active = False
        if pack_active:
            search_dirs = [self._map_dir(map_name, state or {})]
        else:
            map_dir = self._map_only_dir(map_name)
            vehicle_dir = self._vehicle_dir(state or {})
            search_dirs = [map_dir]
            if vehicle_dir is not None and vehicle_dir.resolve() != map_dir.resolve():
                search_dirs.append(vehicle_dir)
        for base in search_dirs:
            if not base.is_dir(): continue
            candidates: list[Path] = []
            for route_dir in self._route_hint_dirs(base, state or {}):
                if route_dir.is_dir():
                    candidates.extend((route_dir / f"{cue_name}.mp3", route_dir / f"{cue_name}.wav"))
            for folder in (base, base / "All", base / "all"):
                candidates.extend((folder / f"{cue_name}.mp3", folder / f"{cue_name}.wav"))
            for path in candidates:
                if path.is_file(): return path.resolve()
            try:
                files = [p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
                         and normalize_text(p.stem) == target
                         and not any(folder_key(part) in {"анонсы", "announcements", "anons"} for part in p.parts)]
                if files:
                    files.sort(key=lambda p: (len(p.relative_to(base).parts), str(p).casefold()))
                    return files[0].resolve()
            except OSError:
                pass
        return None

    def jingle_file(self, map_name: str, state: dict[str, Any] | None) -> Path | None:
        return self.named_cue_file(map_name, state, "JINGLE")

    def aux_file(self, map_name: str, state: dict[str, Any], aliases: set[str]) -> Path | None:
        """Find NEXT/TERMINUS/SIGNAL-style clips in common and legacy layouts.

        Unlike ordinary announcements, auxiliary cues are often stored directly beside
        route/stop audio files (rather than inside an ``Анонсы`` directory). Search
        the current route first, then map-wide folders, then nested fallback matches.
        """
        wanted = {normalize_text(value) for value in aliases if normalize_text(value)}
        if not wanted:
            return None
        map_dir = self._map_dir(map_name, state)
        route_hints = self._route_hint_dirs(map_dir, state) if map_dir.is_dir() else []
        dirs: list[Path] = []

        def add_dir(folder: Path | None) -> None:
            if folder is not None and folder.is_dir() and folder not in dirs:
                dirs.append(folder)

        def add_announcement_children(folder: Path | None) -> None:
            if folder is None or not folder.is_dir():
                return
            try:
                for child in folder.iterdir():
                    if child.is_dir() and normalize_text(child.name).replace("_", " ") in {
                        "анонсы", "announcements", "announcement", "anons"
                    }:
                        add_dir(child)
            except OSError:
                pass

        # Route-specific directories take priority over map-wide files.
        for route_dir in route_hints:
            if route_dir.name.casefold() in {"all", "все"}:
                continue
            add_dir(route_dir)
            add_dir(route_dir / "Анонсы")
            add_dir(route_dir / "Announcements")
            add_announcement_children(route_dir)

        # Map/common audio directories. A category bank is exclusive, including for cues.
        pack_active = False
        if self.pack_manager is not None:
            try:
                pack_active = self.pack_manager.has_profile(map_name, state)
            except Exception:
                pack_active = False
        folders = [
            map_dir / "Анонсы", map_dir / "Announcements", map_dir / "anons",
            map_dir / "All" / "Анонсы", map_dir / "All" / "Announcements",
            map_dir / "All", map_dir / "all", map_dir,
        ]
        if not pack_active:
            folders.extend((self.root / "Анонсы", self.root / "Announcements", self.root / "All" / "Анонсы",
                            self.root / "All" / "Announcements", self.root / "All"))
        for folder in folders:
            add_dir(folder)
        add_announcement_children(map_dir)
        all_dir = map_dir / "All"
        add_announcement_children(all_dir)
        for route_dir in route_hints:
            if route_dir.name.casefold() in {"all", "все"}:
                add_dir(route_dir / "Анонсы")
                add_dir(route_dir / "Announcements")
                add_dir(route_dir)
                add_announcement_children(route_dir)

        def matches(path: Path) -> bool:
            candidates = stem_aliases(path)
            expanded = set(candidates)
            expanded.update(value.replace("_", " ").replace("-", " ") for value in candidates)
            return bool(expanded & wanted)

        # First pass: only direct children in high-priority folders.
        visited: set[str] = set()
        for folder in dirs:
            try:
                files = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for path in files:
                if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
                    continue
                try:
                    key = str(path.resolve())
                except OSError:
                    key = str(path)
                if key in visited:
                    continue
                visited.add(key)
                if matches(path):
                    return path

        # Final fallback for map packs that put NEXT/TERMINUS several folders deep or
        # in a route folder whose name differs from the game's line/tour naming.
        # Keep this scoped to the selected map; never scan sibling maps.
        if map_dir.is_dir():
            fallback: list[Path] = []
            try:
                for path in map_dir.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
                        continue
                    if any(part.casefold() in {"cache", "__pycache__", ".git"} for part in path.parts):
                        continue
                    if matches(path):
                        fallback.append(path)
            except OSError:
                pass
            if fallback:
                # Prefer shorter/shallower path, then lexical order for deterministic results.
                fallback.sort(key=lambda path: (len(path.relative_to(map_dir).parts), str(path).casefold()))
                return fallback[0]
        return None

    def duration(self, path: str | Path) -> float | None:
        p = Path(path)
        try:
            stamp = p.stat().st_mtime
        except OSError:
            return None
        cache = self._duration_cache.get(str(p.resolve()))
        if cache and cache[0] == stamp:
            return cache[1]
        result = duration_seconds(p)
        self._duration_cache[str(p.resolve())] = (stamp, result)
        return result

    def choose_announcement(self, files: Iterable[Path], eta_seconds: float | None,
                            history: list[str], margin_seconds: float = 5.0,
                            rng=None) -> Path | None:
        import random
        rng = rng or random
        candidates = []
        excluded = set().union(*AUX_ALIASES.values())
        for p in files:
            if stem_aliases(p) & excluded:
                continue
            duration = self.duration(p)
            if duration is None:
                continue
            if eta_seconds is None or duration + margin_seconds > eta_seconds:
                continue
            candidates.append(p)
        if not candidates:
            return None
        key = lambda p: str(p.resolve())
        recent_count = min(3, max(1, len(candidates) - 1))
        recent = set(history[-recent_count:]) if history else set()
        fresh = [p for p in candidates if key(p) not in recent]
        if not fresh:
            fresh = [p for p in candidates if not history or key(p) != history[-1]]
        # With one eligible file, stay silent on consecutive cycles rather than repeat it.
        if not fresh:
            return None
        return rng.choice(fresh)


def _parse_hhmm(value: Any) -> int | None:
    try:
        parts = str(value).strip().split(":")
        if len(parts) != 2:
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute
    except (TypeError, ValueError):
        return None


def announcement_schedule_allows(path: str | Path, state: dict[str, Any] | None,
                                 schedules: dict[str, dict[str, Any]] | None) -> bool:
    """Return whether a scheduled announcement may play at the current in-game date/time.

    No schedule means always allowed. Dates are YYYY-MM-DD, or MM-DD when `annual` is true.
    Date and time ranges may cross New Year/midnight. Missing game time/date fails open, so a
    schedule never silently disables announcements on older plugin builds without telemetry.
    """
    state = state or {}
    schedules = schedules or {}
    resolved = str(Path(path).expanduser().resolve())
    entry = schedules.get(resolved)
    if entry is None:
        wanted = str(path)
        entry = next((v for k, v in schedules.items() if str(k) == wanted or Path(str(k)).name == Path(wanted).name), None)
    if not isinstance(entry, dict) or not entry.get("enabled", True):
        return True

    date_value = state.get("game_date")
    if isinstance(date_value, dict):
        try:
            year, month, day = int(date_value["year"]), int(date_value["month"]), int(date_value["day"])
            current_date = f"{year:04d}-{month:02d}-{day:02d}"
            current_md = f"{month:02d}-{day:02d}"
        except (KeyError, TypeError, ValueError):
            current_date = current_md = ""
    else:
        current_date = current_md = ""

    start_date = str(entry.get("start_date") or "").strip()
    end_date = str(entry.get("end_date") or "").strip()
    annual = bool(entry.get("annual", False))
    if (start_date or end_date) and current_date:
        def date_token(value: str) -> str:
            value = value.strip()
            if annual:
                if len(value) == 10 and value[4] == "-":
                    return value[5:]
                return value
            if len(value) == 5 and value[2] == "-":
                return ""
            return value
        now_token = current_md if annual else current_date
        start_token = date_token(start_date) if start_date else ""
        end_token = date_token(end_date) if end_date else ""
        if (start_date and not start_token) or (end_date and not end_token):
            return False
        # ISO / MM-DD lexicographical order is chronological within a year. Wrap ranges allow
        # festive periods like 12-20 .. 01-10.
        if start_token and end_token and start_token > end_token:
            if not (now_token >= start_token or now_token <= end_token):
                return False
        elif start_token and now_token < start_token:
            return False
        elif end_token and now_token > end_token:
            return False
    elif start_date or end_date:
        # Data is not supplied by the game. Fail open rather than suppressing all scheduled ads.
        pass

    clock = state.get("game_clock")
    if clock is None:
        clock = state.get("game_time")
    try:
        minute_now = int(float(clock) // 60)
    except (TypeError, ValueError):
        minute_now = None
    start_time = _parse_hhmm(entry.get("start_time"))
    end_time = _parse_hhmm(entry.get("end_time"))
    if minute_now is not None and (start_time is not None or end_time is not None):
        start_min = start_time if start_time is not None else 0
        end_min = end_time if end_time is not None else 1439
        if start_min <= end_min:
            if not (start_min <= minute_now <= end_min):
                return False
        elif not (minute_now >= start_min or minute_now <= end_min):
            return False
    return True

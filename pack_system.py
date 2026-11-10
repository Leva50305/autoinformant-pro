# -*- coding: utf-8 -*-
"""Portable .aipack library manager for AutoInformant Pro.

The package system stores configuration beside the audio assets it controls.
It deliberately does not inspect or parse legacy route.txt files: live routes and
stops continue to come from the openOMSI duty API.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from informant_core import AUDIO_EXTS, normalize_text, folder_key, stem_aliases, stop_key

PACK_FORMAT = "autoinformant-pack"
PACK_SCHEMA_VERSION = 1
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


def _safe_id(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._").lower()
    return (text[:64] or uuid.uuid4().hex[:12])


def _normal_vehicle_key(value: str) -> str:
    return folder_key(value).replace(" | ", " ")


def _safe_relative(value: str) -> Path:
    raw = str(value or "").replace("\\", "/")
    posix = PurePosixPath(raw)
    if posix.is_absolute() or any(part in {"..", ""} for part in posix.parts):
        raise ValueError(f"Недопустимый относительный путь в пакете: {value}")
    return Path(*posix.parts)


class PackManager:
    """Installed-pack registry and profile resolver.

    A pack can contain one map profile and any number of category profiles.
    A matching category marked ``replace_map_bank`` fully replaces the base map
    bank for that assigned vehicle (stops, cues and announcements all resolve
    from the category directory, without falling back to the map bank).
    """

    def __init__(self, app_dir: str | Path, settings: dict[str, Any], on_change=None):
        self.app_dir = Path(app_dir).resolve()
        self.root = self.app_dir / "packs"
        self.installed_dir = self.root / "installed"
        self.export_dir = self.root / "exports"
        self.settings = settings
        self.on_change = on_change
        self.installed_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self._packs: dict[str, dict[str, Any]] = {}
        self.refresh()

    def refresh(self) -> None:
        self._packs.clear()
        for directory in sorted(self.installed_dir.iterdir(), key=lambda p: p.name.casefold()):
            manifest_path = directory / "manifest.json"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                self._validate_manifest(manifest)
            except Exception:
                continue
            pack_id = str(manifest["id"])
            self._packs[pack_id] = {"dir": directory, "manifest": manifest}
        self.settings.setdefault("pack_enabled", {})
        self.settings.setdefault("pack_priorities", {})
        self.settings.setdefault("active_map_packs", {})
        self.settings.setdefault("active_category_profiles", {})

    def list_packs(self) -> list[dict[str, Any]]:
        result = []
        for pack_id, item in self._packs.items():
            manifest = item["manifest"]
            result.append({
                "id": pack_id,
                "name": str(manifest.get("name") or pack_id),
                "author": str(manifest.get("author") or ""),
                "description": str(manifest.get("description") or ""),
                "icon": self._icon_path(pack_id),
                "enabled": self.is_enabled(pack_id),
                "priority": self.priority(pack_id),
                "map_profiles": len(manifest.get("map_profiles", [])),
                "category_profiles": len(manifest.get("category_profiles", [])),
                "manifest": manifest,
                "dir": item["dir"],
            })
        return result

    def exported_files(self) -> list[Path]:
        try:
            return sorted((p for p in self.export_dir.glob("*.aipack") if p.is_file()), key=lambda p: p.name.casefold())
        except OSError:
            return []

    def is_enabled(self, pack_id: str) -> bool:
        return bool(self.settings.get("pack_enabled", {}).get(pack_id, True))

    def set_enabled(self, pack_id: str, enabled: bool) -> None:
        if pack_id not in self._packs:
            raise KeyError(pack_id)
        self.settings.setdefault("pack_enabled", {})[pack_id] = bool(enabled)
        self._changed()

    def priority(self, pack_id: str) -> int:
        try:
            return int(self.settings.get("pack_priorities", {}).get(pack_id, 0))
        except (TypeError, ValueError):
            return 0

    def set_priority(self, pack_id: str, priority: int) -> None:
        self.settings.setdefault("pack_priorities", {})[pack_id] = int(priority)
        self._changed()

    def _changed(self) -> None:
        self.refresh()
        if callable(self.on_change):
            self.on_change()

    def _manifest_path(self, pack_id: str) -> Path:
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        return item["dir"] / "manifest.json"

    def _save_manifest(self, pack_id: str) -> None:
        path = self._manifest_path(pack_id)
        manifest = self._packs[pack_id]["manifest"]
        self._validate_manifest(manifest)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        if callable(self.on_change):
            self.on_change()

    def _validate_manifest(self, manifest: Any) -> None:
        if not isinstance(manifest, dict):
            raise ValueError("Манифест пакета должен быть JSON-объектом")
        if manifest.get("format") != PACK_FORMAT:
            raise ValueError("Это не пакет AutoInformant Pro")
        if int(manifest.get("schema_version", 0)) != PACK_SCHEMA_VERSION:
            raise ValueError("Неподдерживаемая версия формата пакета")
        pack_id = str(manifest.get("id") or "").strip()
        if not pack_id or _safe_id(pack_id) != pack_id:
            raise ValueError("В пакете некорректный идентификатор")
        if not str(manifest.get("name") or "").strip():
            raise ValueError("У пакета не указано название")
        for field in ("map_profiles", "category_profiles"):
            profiles = manifest.get(field, [])
            if not isinstance(profiles, list):
                raise ValueError(f"Поле {field} должно быть списком")
            for index, profile in enumerate(profiles):
                if not isinstance(profile, dict):
                    raise ValueError(f"Профиль {field}[{index}] повреждён")
                profile_id = str(profile.get("id") or "").strip()
                if not profile_id:
                    raise ValueError(f"У профиля {field}[{index}] отсутствует id")
                _safe_relative(str(profile.get("asset_root") or "assets"))
                aliases = profile.get("map_aliases", [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                    raise ValueError("map_aliases должен быть списком строк")
                if field == "category_profiles":
                    assignments = profile.get("vehicle_assignments", [])
                    if not isinstance(assignments, list):
                        raise ValueError("vehicle_assignments должен быть списком")
                for key in ("stop_overrides", "announcement_schedules"):
                    value = profile.get(key, {})
                    if not isinstance(value, dict):
                        raise ValueError(f"{key} должен быть объектом")
                ignored = profile.get("ignored_stops", [])
                if not isinstance(ignored, list):
                    raise ValueError("ignored_stops должен быть списком")

    def import_pack(self, archive_path: str | Path, *, overwrite: bool = False) -> str:
        archive_path = Path(archive_path).expanduser().resolve()
        if not archive_path.is_file():
            raise FileNotFoundError(archive_path)
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            total = sum(max(0, info.file_size) for info in infos)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("Пакет слишком большой")
            for info in infos:
                name = info.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or any(part == ".." for part in path.parts):
                    raise ValueError(f"Пакет содержит небезопасный путь: {name}")
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
            except KeyError as exc:
                raise ValueError("В архиве отсутствует manifest.json") from exc
            self._validate_manifest(manifest)
            pack_id = str(manifest["id"])
            destination = self.installed_dir / pack_id
            if destination.exists() and not overwrite:
                raise FileExistsError(f"Пакет {manifest.get('name')} уже установлен")
            staging = self.installed_dir / f".{pack_id}.install-{uuid.uuid4().hex[:8]}"
            staging.mkdir(parents=True, exist_ok=False)
            try:
                for info in infos:
                    relative = _safe_relative(info.filename)
                    target = staging / relative
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                # Some older packs omit empty directories from their ZIP index.
                # Recreate each profile root so newly-created empty categories remain usable.
                for field in ("map_profiles", "category_profiles"):
                    for profile in manifest.get(field, []):
                        (staging / _safe_relative(str(profile.get("asset_root") or "assets"))).mkdir(
                            parents=True, exist_ok=True
                        )
                # Ensure extracted manifest is the validated version, not a second encoding.
                (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                backup = None
                if destination.exists():
                    backup = self.installed_dir / f".{pack_id}.backup-{uuid.uuid4().hex[:8]}"
                    os.replace(destination, backup)
                try:
                    os.replace(staging, destination)
                except Exception:
                    if backup and backup.exists():
                        os.replace(backup, destination)
                    raise
                if backup and backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        self.settings.setdefault("pack_enabled", {})[pack_id] = True
        self.settings.setdefault("pack_priorities", {}).setdefault(pack_id, 0)
        # The first installed pack for a map becomes its default. Later packs for
        # the same map coexist without silently replacing the user's active choice.
        active_maps = self.settings.setdefault("active_map_packs", {})
        for profile in manifest.get("map_profiles", []):
            for alias in profile.get("map_aliases", []):
                active_maps.setdefault(folder_key(alias), pack_id)
        self._changed()
        return pack_id

    def export_pack(self, pack_id: str, destination: str | Path) -> Path:
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        destination = Path(destination).expanduser().resolve()
        if destination.suffix.lower() != ".aipack":
            destination = destination.with_suffix(".aipack")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.write(item["dir"] / "manifest.json", "manifest.json")
                for path in sorted(item["dir"].rglob("*"), key=lambda p: str(p).casefold()):
                    if path.is_symlink():
                        raise ValueError(f"Символические ссылки не поддерживаются: {path}")
                    relative = path.relative_to(item["dir"]).as_posix()
                    if path.is_dir():
                        archive.write(path, relative.rstrip("/") + "/")
                    elif path.is_file() and path.name != "manifest.json":
                        archive.write(path, relative)
            with zipfile.ZipFile(temp, "r") as check:
                bad = check.testzip()
                if bad:
                    raise ValueError(f"Ошибка CRC в архиве: {bad}")
            os.replace(temp, destination)
        except Exception:
            try: temp.unlink(missing_ok=True)
            except OSError: pass
            raise
        return destination

    @staticmethod
    def _copy_tree_contents(source: Path, destination: Path, *, exclude_roots: set[str] | None = None) -> None:
        """Copy a directory's contents without changing names or following symlinks."""
        source = source.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        excluded = {str(value).replace("\\", "/").strip("/") for value in (exclude_roots or set())}
        for child in source.iterdir():
            relative = child.name
            if relative in excluded:
                continue
            if child.is_symlink():
                raise ValueError(f"Символические ссылки не поддерживаются: {child}")
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target, symlinks=False)
            elif child.is_file():
                shutil.copy2(child, target)

    @staticmethod
    def _assert_tree_has_no_symlinks(root: Path) -> int:
        count = 0
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in list(dirs) + list(files):
                candidate = current_path / name
                if candidate.is_symlink():
                    raise ValueError(f"В исходной папке обнаружена символическая ссылка: {candidate}")
                if candidate.is_file():
                    count += 1
        return count

    @staticmethod
    def _safe_folder_component(value: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
        value = value.strip(" .")
        if value in {"", ".", ".."}:
            raise ValueError("Не удалось определить безопасное имя папки")
        if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            value = "_" + value
        return value[:100]

    @staticmethod
    def _suggest_pack_id(value: str) -> str:
        candidate = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._").lower()
        return (candidate[:48] or f"audio-pack-{uuid.uuid4().hex[:8]}")

    @staticmethod
    def _looks_like_route_folder(name: str) -> bool:
        """Recognize legacy numeric duty/route directories, including seasonal variants."""
        return bool(re.fullmatch(r"\d+(?:[-_]\d+)?(?:\s*\([^/\\]*\))?", str(name or "").strip()))

    @classmethod
    def suggest_category_folders(cls, source_dir: str | Path) -> list[str]:
        """Return plausible direct-child category folders; route/All/announcements stay in base bank."""
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise NotADirectoryError(source)
        reserved = {
            "all", "анонсы", "анонс", "announcements", "announcement", "_autoinformantcustom",
            "__pycache__", "temp", "cache",
        }
        result = []
        for child in sorted(source.iterdir(), key=lambda p: p.name.casefold()):
            if not child.is_dir() or child.is_symlink():
                continue
            folded = child.name.casefold().strip()
            if folded in reserved or cls._looks_like_route_folder(child.name):
                continue
            result.append(child.name)
        return result

    @staticmethod
    def inspect_audio_folder(source_dir: str | Path) -> dict[str, Any]:
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise NotADirectoryError(source)
        files = PackManager._assert_tree_has_no_symlinks(source)
        total_bytes = sum(p.stat().st_size for p in source.rglob("*") if p.is_file() and not p.is_symlink())
        return {
            "path": source,
            "name": source.name,
            "file_count": files,
            "total_bytes": total_bytes,
            "directories": [p.name for p in sorted(source.iterdir(), key=lambda p: p.name.casefold()) if p.is_dir()],
            "is_package_directory": (source / "manifest.json").is_file(),
            "suggested_categories": PackManager.suggest_category_folders(source),
        }

    def _unique_pack_id(self, seed: str) -> str:
        base = self._suggest_pack_id(seed)
        candidate = base
        index = 2
        while candidate in self._packs or (self.export_dir / f"{candidate}.aipack").exists():
            candidate = f"{base[:44]}-{index}"
            index += 1
        return candidate

    def _validate_package_directory(self, root: Path) -> dict[str, Any]:
        root = root.expanduser().resolve()
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("В папке нет manifest.json: это не распакованный пакет AutoInformant Pro")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        self._validate_manifest(manifest)
        total_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())
        if total_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError(f"Распакованный пакет превышает ограничение {MAX_ARCHIVE_BYTES // (1024 * 1024)} МБ")
        for field in ("map_profiles", "category_profiles"):
            for profile in manifest.get(field, []):
                relative = _safe_relative(str(profile.get("asset_root") or "assets"))
                asset_path = (root / relative).resolve()
                if root not in asset_path.parents or not asset_path.is_dir():
                    raise ValueError(f"Не найдена папка ресурсов профиля «{profile.get('name') or profile.get('id')}»: {relative}")
        self._assert_tree_has_no_symlinks(root)
        return manifest

    def _write_directory_as_pack(self, package_dir: Path, destination: str | Path, *, overwrite: bool = False) -> Path:
        package_dir = package_dir.expanduser().resolve()
        self._validate_package_directory(package_dir)
        destination = Path(destination).expanduser().resolve()
        if destination.suffix.lower() != ".aipack":
            destination = destination.with_suffix(".aipack")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.is_dir():
            raise IsADirectoryError(destination)
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        # Snapshot the source tree before creating the temporary archive. This also
        # allows saving the resulting .aipack beside files inside the selected folder
        # without accidentally embedding the archive in itself.
        package_entries = sorted(list(package_dir.rglob("*")),
                                 key=lambda p: p.relative_to(package_dir).as_posix().casefold())
        package_entries = [path for path in package_entries if path.resolve() != destination]
        temp = destination.with_suffix(destination.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for path in package_entries:
                    if path.is_symlink():
                        raise ValueError(f"Символические ссылки не поддерживаются: {path}")
                    relative = path.relative_to(package_dir).as_posix()
                    _safe_relative(relative)
                    if path.is_dir():
                        archive.write(path, relative.rstrip("/") + "/")
                    elif path.is_file():
                        archive.write(path, relative)
            with zipfile.ZipFile(temp, "r") as check:
                bad = check.testzip()
                if bad:
                    raise ValueError(f"Ошибка CRC в архиве: {bad}")
                if "manifest.json" not in check.namelist():
                    raise ValueError("Не удалось записать manifest.json")
            os.replace(temp, destination)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        return destination

    def create_pack_from_package_folder(self, source_dir: str | Path, destination: str | Path,
                                        *, overwrite: bool = False) -> Path:
        """Repack an already-unpacked AutoInformant package folder into .aipack."""
        return self._write_directory_as_pack(Path(source_dir), destination, overwrite=overwrite)

    def create_pack_from_audio_folder(
        self,
        source_dir: str | Path,
        destination: str | Path,
        *,
        name: str,
        author: str = "",
        description: str = "",
        map_alias: str | None = None,
        category_folders: list[str] | tuple[str, ...] = (),
        category_assignments: dict[str, list[str] | tuple[str, ...] | str] | None = None,
        pack_id: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Build a portable map pack from a legacy audio folder without altering its source."""
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise NotADirectoryError(source)
        if (source / "manifest.json").is_file():
            raise ValueError("Выбрана папка готового пакета. Для неё используй упаковку распакованного .aipack.")
        if not str(name or "").strip():
            raise ValueError("Укажи название аудиопака")
        alias = str(map_alias or source.name).strip()
        if not alias:
            raise ValueError("Укажи название карты (map alias)")
        file_count = self._assert_tree_has_no_symlinks(source)
        if file_count <= 0:
            raise ValueError("В выбранной аудиопапке нет файлов")
        source_bytes = sum(path.stat().st_size for path in source.rglob("*") if path.is_file() and not path.is_symlink())
        if source_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError(f"Аудиотека превышает ограничение {MAX_ARCHIVE_BYTES // (1024 * 1024)} МБ")

        category_names: list[str] = []
        for folder_name in category_folders or []:
            folder_name = str(folder_name)
            child = source / folder_name
            if child.parent != source or not child.is_dir() or child.is_symlink():
                raise ValueError(f"Категория должна быть существующей подпапкой первого уровня: {folder_name}")
            if folder_name in category_names:
                raise ValueError(f"Категория «{folder_name}» выбрана дважды")
            category_names.append(folder_name)

        map_component = self._safe_folder_component(source.name)
        used_components = {map_component.casefold()}
        category_components: dict[str, str] = {}
        for folder_name in category_names:
            component = self._safe_folder_component(folder_name)
            if component.casefold() in used_components:
                raise ValueError(f"Конфликт имён папок категории: {folder_name}")
            used_components.add(component.casefold())
            category_components[folder_name] = component

        archive_destination = Path(destination).expanduser().resolve()
        if archive_destination.suffix.lower() != ".aipack":
            archive_destination = archive_destination.with_suffix(".aipack")
        archive_destination.parent.mkdir(parents=True, exist_ok=True)
        if archive_destination.exists() and not overwrite:
            raise FileExistsError(archive_destination)

        new_id = _safe_id(pack_id or self._unique_pack_id(alias))
        if pack_id is None and new_id in self._packs:
            new_id = self._unique_pack_id(alias)
        default_settings = {
            "auto_arrival": True, "auto_departure": True,
            "arrival_radius": 18.0, "max_arrival_speed": 2.5, "exit_speed": 4.0,
            "play_next_prefix": True, "play_terminus": True,
            "anons_enabled": True, "anons_mode": "minutes", "anons_interval": 2,
            "average_speed_kmh": 25.0, "anons_safety_seconds": 5.0,
            "play_jingle": True, "doors_enabled": False,
        }
        manifest = {
            "format": PACK_FORMAT,
            "schema_version": PACK_SCHEMA_VERSION,
            "id": new_id,
            "name": str(name).strip(),
            "author": str(author or "").strip(),
            "description": str(description or "").strip(),
            "icon": "pack_icon.png",
            "map_profiles": [{
                "id": "map-main",
                "map_aliases": [alias],
                "asset_root": f"assets/maps/{map_component}",
                "settings": dict(default_settings),
                "stop_overrides": {},
                "ignored_stops": [],
                "announcement_schedules": {},
            }],
            "category_profiles": [],
        }

        assignments_by_folder = category_assignments or {}
        for index, folder_name in enumerate(category_names, start=1):
            raw_assignments = assignments_by_folder.get(folder_name, [])
            if isinstance(raw_assignments, str):
                assignments = [part.strip() for part in re.split(r"[;\n]+", raw_assignments) if part.strip()]
            else:
                assignments = [str(part).strip() for part in (raw_assignments or []) if str(part).strip()]
            manifest["category_profiles"].append({
                "id": f"category-{index}-{uuid.uuid4().hex[:8]}",
                "name": folder_name,
                "map_aliases": [alias],
                "asset_root": f"assets/categories/{map_component}/{category_components[folder_name]}",
                "replace_map_bank": True,
                "vehicle_assignments": assignments,
                "settings": dict(default_settings),
                "stop_overrides": {},
                "ignored_stops": [],
                "announcement_schedules": {},
            })

        staging = self.export_dir / f".build-{uuid.uuid4().hex[:10]}"
        try:
            map_root = staging / "assets" / "maps" / map_component
            excluded = set(category_names)
            self._copy_tree_contents(source, map_root, exclude_roots=excluded)
            for folder_name in category_names:
                category_root = staging / "assets" / "categories" / map_component / category_components[folder_name]
                shutil.copytree(source / folder_name, category_root, symlinks=False)
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            fallback_icon = self.app_dir / "pack_icon_default.png"
            if fallback_icon.is_file():
                shutil.copy2(fallback_icon, staging / "pack_icon.png")
            self._validate_manifest(manifest)
            result = self._write_directory_as_pack(staging, archive_destination, overwrite=overwrite)
            return result
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def export_pack_to_folder(self, pack_id: str, destination: str | Path, *,
                              mode: str = "package", map_profile_id: str | None = None,
                              include_categories: bool = True, overwrite: bool = False) -> Path:
        """Export an installed pack either as its full editable folder or a legacy audio tree.

        ``package`` retains manifest, icon, profile settings and every asset.
        ``audio`` restores the classic map audio folder, with categories as named
        child directories; pack-only metadata is intentionally not flattened.
        """
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        destination = Path(destination).expanduser().resolve()
        installed_root = item["dir"].resolve()
        if destination == installed_root or installed_root in destination.parents or destination in installed_root.parents:
            raise ValueError("Нельзя экспортировать пакет внутрь его установленной папки или заменять её родительскую папку")
        if destination.exists() and not destination.is_dir():
            raise NotADirectoryError(f"По пути уже существует файл: {destination}")
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + f".export-{uuid.uuid4().hex[:8]}")
        backup = destination.with_name(destination.name + f".backup-{uuid.uuid4().hex[:8]}")
        try:
            if mode in {"package", "full", "package_folder"}:
                shutil.copytree(item["dir"], staging, symlinks=False)
                self._assert_tree_has_no_symlinks(staging)
            elif mode in {"audio", "legacy", "audio_folder"}:
                manifest = item["manifest"]
                map_profiles = manifest.get("map_profiles", [])
                if map_profile_id:
                    base = next((p for p in map_profiles if p.get("id") == map_profile_id), None)
                else:
                    base = map_profiles[0] if len(map_profiles) == 1 else None
                if not base:
                    raise ValueError("В паке несколько профилей карт. Укажи, какой из них экспортировать.")
                base_root = (item["dir"] / _safe_relative(str(base.get("asset_root") or "assets"))).resolve()
                pack_root = item["dir"].resolve()
                if pack_root not in base_root.parents or not base_root.is_dir():
                    raise ValueError("Папка основной аудиотеки профиля не найдена или некорректна")
                staging.mkdir(parents=True, exist_ok=False)
                categories = []
                excluded = set()
                if include_categories:
                    for category in manifest.get("category_profiles", []):
                        base_aliases = list(base.get("map_aliases", []))
                        category_aliases = list(category.get("map_aliases", []))
                        if category_aliases and base_aliases and not any(
                            self._map_matches(category, alias) for alias in base_aliases
                        ):
                            continue
                        cat_root = (item["dir"] / _safe_relative(str(category.get("asset_root") or "assets"))).resolve()
                        if pack_root not in cat_root.parents or not cat_root.is_dir():
                            raise ValueError(f"Папка категории «{category.get('name') or category.get('id')}» не найдена")
                        try:
                            rel_to_base = cat_root.relative_to(base_root)
                            # Category assets may be stored under the map root in older schema-v1 packs.
                            excluded.add(rel_to_base.parts[0])
                        except ValueError:
                            pass
                        categories.append((str(category.get("name") or category.get("id") or "Category"), cat_root))
                self._copy_tree_contents(base_root, staging, exclude_roots=excluded)
                used = {p.name.casefold() for p in staging.iterdir()}
                for category_name, cat_root in categories:
                    component = self._safe_folder_component(category_name)
                    if component.casefold() in used:
                        raise ValueError(f"Нельзя экспортировать категорию «{category_name}»: такая папка уже есть в основной аудиотеке")
                    shutil.copytree(cat_root, staging / component, symlinks=False)
                    used.add(component.casefold())
            else:
                raise ValueError("Неизвестный режим экспорта папки")

            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except Exception:
                if backup.exists():
                    os.replace(backup, destination)
                raise
            shutil.rmtree(backup, ignore_errors=True)
            return destination
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise

    def remove_pack(self, pack_id: str) -> None:
        item = self._packs.get(pack_id)
        if not item:
            return
        shutil.rmtree(item["dir"])
        self.settings.get("pack_enabled", {}).pop(pack_id, None)
        self.settings.get("pack_priorities", {}).pop(pack_id, None)
        active = self.settings.get("active_map_packs", {})
        for key in list(active):
            if active.get(key) == pack_id:
                active.pop(key, None)
        active_categories = self.settings.get("active_category_profiles", {})
        prefix = pack_id + ":"
        for key in list(active_categories):
            if str(active_categories.get(key) or "").startswith(prefix):
                active_categories.pop(key, None)
        self._changed()

    def edit_metadata(self, pack_id: str, *, name: str, author: str, description: str) -> None:
        manifest = self._packs[pack_id]["manifest"]
        if not name.strip():
            raise ValueError("Название пакета не может быть пустым")
        manifest["name"] = name.strip()
        manifest["author"] = author.strip()
        manifest["description"] = description.strip()
        self._save_manifest(pack_id)
        self.refresh()

    @staticmethod
    def _category_folder_name(name: str) -> str:
        """Return a Windows-safe, readable folder component (Unicode is preserved)."""
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "").strip())
        value = value.strip(" .")
        return (value[:72] or "Category")

    def create_category(self, pack_id: str, *, name: str, map_profile_id: str | None = None) -> str:
        """Create an isolated replacement audio bank inside an installed map pack."""
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        name = str(name or "").strip()
        if not name:
            raise ValueError("Укажи название категории")
        manifest = item["manifest"]
        map_profiles = manifest.get("map_profiles", [])
        if not map_profiles:
            raise ValueError("В паке нет профиля карты, к которому можно привязать категорию")
        base = next((p for p in map_profiles if p.get("id") == map_profile_id), None) if map_profile_id else map_profiles[0]
        if not base:
            raise ValueError("Выбранный профиль карты больше не существует")
        if any(str(p.get("name") or "").casefold() == name.casefold()
               for p in manifest.get("category_profiles", [])):
            raise ValueError("Категория с таким названием уже существует")

        category_id = f"category-{uuid.uuid4().hex[:10]}"
        folder_name = self._category_folder_name(name)
        asset_root = f"assets/categories/{folder_name}"
        asset_path = (item["dir"] / _safe_relative(asset_root)).resolve()
        pack_root = item["dir"].resolve()
        if asset_path != pack_root and pack_root not in asset_path.parents:
            raise ValueError("Путь категории выходит за пределы пакета")
        asset_path.mkdir(parents=True, exist_ok=False)
        profile = {
            "id": category_id,
            "name": name,
            "map_aliases": list(base.get("map_aliases", [])),
            "asset_root": asset_root,
            # Category banks are deliberately isolated: for the assigned vehicle
            # they replace the map bank instead of mixing bus and trolley cues.
            "replace_map_bank": True,
            "vehicle_assignments": [],
            "settings": dict(base.get("settings", {})),
            "stop_overrides": {},
            "ignored_stops": [],
            "announcement_schedules": {},
        }
        manifest.setdefault("category_profiles", []).append(profile)
        try:
            self._save_manifest(pack_id)
        except Exception:
            manifest["category_profiles"].remove(profile)
            shutil.rmtree(asset_path, ignore_errors=True)
            raise
        return category_id

    def update_category(self, pack_id: str, category_id: str, *, name: str,
                        map_profile_id: str | None = None) -> None:
        """Rename a category and optionally point it at another map profile."""
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        name = str(name or "").strip()
        if not name:
            raise ValueError("Название категории не может быть пустым")
        manifest = item["manifest"]
        profile = next((p for p in manifest.get("category_profiles", []) if p.get("id") == category_id), None)
        if not profile:
            raise KeyError(category_id)
        if any(p is not profile and str(p.get("name") or "").casefold() == name.casefold()
               for p in manifest.get("category_profiles", [])):
            raise ValueError("Категория с таким названием уже существует")
        profile["name"] = name
        if map_profile_id is not None:
            base = next((p for p in manifest.get("map_profiles", []) if p.get("id") == map_profile_id), None)
            if not base:
                raise ValueError("Выбранный профиль карты больше не существует")
            profile["map_aliases"] = list(base.get("map_aliases", []))
        profile["replace_map_bank"] = True
        self._save_manifest(pack_id)

    def delete_category(self, pack_id: str, category_id: str) -> None:
        """Remove a category and its contained assets, without touching other banks."""
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        manifest = item["manifest"]
        profile = next((p for p in manifest.get("category_profiles", []) if p.get("id") == category_id), None)
        if not profile:
            return
        root = (item["dir"] / _safe_relative(str(profile.get("asset_root") or "assets"))).resolve()
        pack_root = item["dir"].resolve()
        if root == pack_root or pack_root not in root.parents:
            raise ValueError("Нельзя удалить корневую папку пакета")
        manifest["category_profiles"].remove(profile)
        try:
            self._save_manifest(pack_id)
        except Exception:
            manifest["category_profiles"].append(profile)
            raise
        shutil.rmtree(root, ignore_errors=True)
        active = self.settings.setdefault("active_category_profiles", {})
        selected_value = f"{pack_id}:{category_id}"
        for key in list(active):
            if active.get(key) == selected_value:
                active.pop(key, None)
        self._changed()

    def add_vehicle_assignment(self, pack_id: str, category_id: str, assignment: str) -> None:
        """Add a manually entered manufacturer/model assignment."""
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        assignment = " ".join(str(assignment or "").split())
        if not assignment:
            raise ValueError("Укажи производителя и/или модель транспорта")
        profile = next((p for p in item["manifest"].get("category_profiles", []) if p.get("id") == category_id), None)
        if not profile:
            raise KeyError(category_id)
        assignments = profile.setdefault("vehicle_assignments", [])
        if any(_normal_vehicle_key(str(value)) == _normal_vehicle_key(assignment) for value in assignments):
            raise ValueError("Это назначение уже есть в категории")
        assignments.append(assignment)
        self._save_manifest(pack_id)

    def import_category_audio(self, pack_id: str, category_id: str, source_dir: str | Path) -> Path:
        """Replace a category's asset folder with a copy of a user's audio folder.

        Copying keeps the package portable and leaves the user's source folder intact.
        File names (including Cyrillic names) are copied without decoding/renaming.
        """
        item = self._packs.get(pack_id)
        if not item:
            raise KeyError(pack_id)
        profile = next((p for p in item["manifest"].get("category_profiles", []) if p.get("id") == category_id), None)
        if not profile:
            raise KeyError(category_id)
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise NotADirectoryError(source)
        destination = (item["dir"] / _safe_relative(str(profile.get("asset_root") or "assets"))).resolve()
        pack_root = item["dir"].resolve()
        if destination == pack_root or pack_root not in destination.parents:
            raise ValueError("Папка категории выходит за пределы пакета")
        # Do not copy a folder into itself or copy the whole package into a
        # category (which would recursively include the staging folder).
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError("Выбери внешнюю папку аудиотеки, не папку внутри самого пакета")
        for child in source.rglob("*"):
            if child.is_symlink():
                raise ValueError(f"В исходной аудиопапке обнаружена символическая ссылка: {child}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + f".import-{uuid.uuid4().hex[:8]}")
        backup = destination.with_name(destination.name + f".backup-{uuid.uuid4().hex[:8]}")
        try:
            shutil.copytree(source, staging, symlinks=False,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "Thumbs.db"))
            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except Exception:
                if backup.exists():
                    os.replace(backup, destination)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._save_manifest(pack_id)
        return destination

    def set_icon(self, pack_id: str, source: str | Path) -> None:
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        suffix = source.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Иконка должна быть PNG, JPG или WEBP")
        target_name = "pack_icon" + suffix
        target = self._packs[pack_id]["dir"] / target_name
        shutil.copy2(source, target)
        self._packs[pack_id]["manifest"]["icon"] = target_name
        self._save_manifest(pack_id)

    def _icon_path(self, pack_id: str) -> Path:
        item = self._packs[pack_id]
        icon_name = str(item["manifest"].get("icon") or "pack_icon.png")
        try:
            path = item["dir"] / _safe_relative(icon_name)
            return path if path.is_file() else self.app_dir / "pack_icon_default.png"
        except Exception:
            return self.app_dir / "pack_icon_default.png"

    def _profile_path(self, pack_id: str, profile: dict[str, Any]) -> Path | None:
        item = self._packs.get(pack_id)
        if not item:
            return None
        try:
            path = item["dir"] / _safe_relative(str(profile.get("asset_root") or "assets"))
            return path.resolve()
        except Exception:
            return None

    @staticmethod
    def _map_matches(profile: dict[str, Any], map_name: str) -> bool:
        aliases = profile.get("map_aliases", [])
        if not aliases:
            return True
        wanted = folder_key(map_name)
        return any(folder_key(alias) == wanted for alias in aliases)

    def _map_candidates(self, map_name: str) -> list[tuple[str, dict[str, Any], Path]]:
        candidates = []
        for pack_id, item in self._packs.items():
            if not self.is_enabled(pack_id):
                continue
            for profile in item["manifest"].get("map_profiles", []):
                if self._map_matches(profile, map_name):
                    path = self._profile_path(pack_id, profile)
                    if path and path.is_dir():
                        candidates.append((pack_id, profile, path))
        candidates.sort(key=lambda item: (-self.priority(item[0]), item[0].casefold()))
        return candidates

    def map_profile(self, map_name: str) -> tuple[str, dict[str, Any], Path] | None:
        candidates = self._map_candidates(map_name)
        if not candidates:
            return None
        active = self.settings.get("active_map_packs", {}).get(folder_key(map_name))
        if active:
            found = next((item for item in candidates if item[0] == active), None)
            if found:
                return found
        return candidates[0]

    def category_profile(self, map_name: str, state: dict[str, Any] | None) -> tuple[str, dict[str, Any], Path] | None:
        state = state or {}
        vehicle_key = " | ".join(part for part in (
            str(state.get("vehicle_manufacturer") or "").strip(),
            str(state.get("vehicle_model") or "").strip(),
        ) if part)
        if not vehicle_key:
            return None
        candidates = []
        for pack_id, item in self._packs.items():
            if not self.is_enabled(pack_id):
                continue
            for profile in item["manifest"].get("category_profiles", []):
                if not self._map_matches(profile, map_name):
                    continue
                assignments = profile.get("vehicle_assignments", [])
                if any(_normal_vehicle_key(str(value)) == _normal_vehicle_key(vehicle_key) for value in assignments):
                    path = self._profile_path(pack_id, profile)
                    if path and path.is_dir():
                        candidates.append((pack_id, profile, path))
        candidates.sort(key=lambda item: (-self.priority(item[0]), item[0].casefold()))
        if not candidates:
            return None
        selection_key = self._category_selection_key(map_name, state)
        active = self.settings.get("active_category_profiles", {}).get(selection_key)
        if active:
            selected = next((item for item in candidates
                             if f"{item[0]}:{item[1].get('id')}" == str(active)), None)
            if selected:
                return selected
        return candidates[0]

    @staticmethod
    def _category_selection_key(map_name: str, state: dict[str, Any] | None) -> str:
        state = state or {}
        vehicle_key = " | ".join(part for part in (
            str(state.get("vehicle_manufacturer") or "").strip(),
            str(state.get("vehicle_model") or "").strip(),
        ) if part)
        return f"{folder_key(map_name)}|{_normal_vehicle_key(vehicle_key)}"

    def has_profile(self, map_name: str, state: dict[str, Any] | None = None) -> bool:
        return bool(self.map_profile(map_name) or self.category_profile(map_name, state))

    def effective_profiles(self, map_name: str, state: dict[str, Any] | None = None):
        return self.map_profile(map_name) if not state else (self.map_profile(map_name), self.category_profile(map_name, state))

    def map_audio_dir(self, map_name: str) -> Path | None:
        profile = self.map_profile(map_name)
        return profile[2] if profile else None

    def category_audio_dir(self, map_name: str, state: dict[str, Any] | None) -> Path | None:
        profile = self.category_profile(map_name, state)
        if not profile or not bool(profile[1].get("replace_map_bank", True)):
            return None
        return profile[2]

    def effective_audio_dir(self, map_name: str, state: dict[str, Any] | None) -> Path | None:
        category = self.category_audio_dir(map_name, state)
        return category or self.map_audio_dir(map_name)

    def _active_profile_for_edit(self, map_name: str, state: dict[str, Any] | None):
        category = self.category_profile(map_name, state)
        if category and bool(category[1].get("replace_map_bank", True)):
            return category
        return self.map_profile(map_name)

    def active_profile_info(self, map_name: str, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Describe the exact profile whose assets and overrides are currently effective.

        Vehicle categories marked ``replace_map_bank`` take precedence over the map
        profile, matching the same rule used by stop lookup and all profile edits.
        """
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return None
        pack_id, manifest_profile, root = profile
        pack = self._packs.get(pack_id)
        if not pack:
            return None
        root = root.resolve()
        pack_root = Path(pack["dir"]).resolve()
        try:
            root.relative_to(pack_root)
        except ValueError:
            return None
        return {
            "pack_id": pack_id,
            "pack_name": str(pack["manifest"].get("name") or pack_id),
            "profile_id": str(manifest_profile.get("id") or ""),
            "profile_name": str(manifest_profile.get("name") or "Основной профиль карты"),
            "root": root,
        }

    def list_active_profile_audio_files(self, map_name: str, state: dict[str, Any] | None = None) -> list[Path]:
        """List playable audio assets that belong to the effective profile only."""
        info = self.active_profile_info(map_name, state)
        if not info:
            return []
        root = info["root"]
        result: list[Path] = []
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
                    continue
                resolved = path.resolve()
                resolved.relative_to(root)
                result.append(resolved)
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda p: p.relative_to(root).as_posix().casefold())

    def get_setting(self, map_name: str, state: dict[str, Any] | None, key: str, default: Any = None) -> Any:
        category = self.category_profile(map_name, state)
        if category and bool(category[1].get("replace_map_bank", True)):
            settings = category[1].get("settings", {})
            if key in settings:
                return settings[key]
        base = self.map_profile(map_name)
        if base:
            settings = base[1].get("settings", {})
            if key in settings:
                return settings[key]
        return default

    def set_setting(self, map_name: str, state: dict[str, Any] | None, key: str, value: Any) -> bool:
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return False
        pack_id, item, _path = profile
        item.setdefault("settings", {})[key] = value
        self._save_manifest(pack_id)
        return True

    def current_schedules(self, map_name: str, state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        category = self.category_profile(map_name, state)
        if category and bool(category[1].get("replace_map_bank", True)):
            profile = category
        else:
            profile = self.map_profile(map_name)
        if not profile:
            return result
        _pack_id, item, root = profile
        for relative, entry in item.get("announcement_schedules", {}).items():
            try:
                absolute = (root / _safe_relative(relative)).resolve()
                result[str(absolute)] = dict(entry) if isinstance(entry, dict) else {}
            except Exception:
                continue
        return result

    def save_schedule(self, map_name: str, state: dict[str, Any] | None, audio_path: str | Path,
                      entry: dict[str, Any] | None) -> bool:
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return False
        pack_id, item, root = profile
        path = Path(audio_path).expanduser().resolve()
        try:
            relative = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            return False
        schedules = item.setdefault("announcement_schedules", {})
        if entry is None:
            schedules.pop(relative, None)
        else:
            schedules[relative] = dict(entry)
        self._save_manifest(pack_id)
        return True

    @staticmethod
    def _stop_match_key(stop: dict[str, Any], index: int) -> tuple[str, str]:
        stop_id = stop.get("id", stop.get("object_id"))
        if stop_id not in (None, ""):
            return "id", str(stop_id)
        name = normalize_text(stop.get("name"))
        return "name", name or f"index:{index}"

    def is_stop_ignored(self, map_name: str, stop: dict[str, Any], index: int,
                        state: dict[str, Any] | None = None) -> bool:
        # Ignore rules belong to the effective audio profile. An exclusive vehicle
        # category must not inherit the base map's ignore list, otherwise a stop
        # cannot reliably be unignored while that category is active.
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return False
        _pack_id, item, _root = profile
        key_type, key_value = self._stop_match_key(stop, index)
        name_key = normalize_text(stop.get("name"))
        for entry in item.get("ignored_stops", []):
            if not isinstance(entry, dict):
                continue
            if key_type == "id" and str(entry.get("id") or "") == key_value:
                return True
            if key_type == "name" and normalize_text(entry.get("name_key") or entry.get("name")) == name_key:
                return True
        return False

    def set_stop_ignored(self, map_name: str, stop: dict[str, Any], index: int,
                         state: dict[str, Any] | None, ignored: bool) -> bool:
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return False
        pack_id, item, _root = profile
        key_type, key_value = self._stop_match_key(stop, index)
        entries = item.setdefault("ignored_stops", [])
        def matches(existing):
            if key_type == "id":
                return str(existing.get("id") or "") == key_value
            return normalize_text(existing.get("name_key") or existing.get("name")) == key_value
        entries[:] = [existing for existing in entries if not isinstance(existing, dict) or not matches(existing)]
        if ignored:
            entry = {"name": str(stop.get("name") or ""), "name_key": normalize_text(stop.get("name"))}
            if key_type == "id": entry["id"] = key_value
            else: entry["fallback_index"] = int(index)
            entries.append(entry)
        self._save_manifest(pack_id)
        return True

    def stop_override_path(self, map_name: str, stop: dict[str, Any], index: int,
                           state: dict[str, Any] | None = None) -> Path | None:
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return None
        _pack_id, item, root = profile
        key_type, key_value = self._stop_match_key(stop, index)
        key = f"{key_type}:{key_value}"
        relative = item.get("stop_overrides", {}).get(key)
        if not relative:
            return None
        try:
            path = (root / _safe_relative(relative)).resolve()
            path.relative_to(root.resolve())
            return path if path.is_file() else None
        except Exception:
            return None

    def set_stop_override(self, map_name: str, stop: dict[str, Any], index: int,
                          state: dict[str, Any] | None, source: str | Path) -> Path | None:
        """Assign a stop audio file without ever storing an external absolute path.

        Files already inside the effective profile are referenced in place. Files
        selected from elsewhere are copied into ``_AutoInformantCustom`` under that
        profile and then recorded as a package-relative path in manifest.json.
        """
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return None
        pack_id, item, root = profile
        root = root.resolve()
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in AUDIO_EXTS:
            raise ValueError(f"Неподдерживаемый аудиоформат: {source.suffix or 'без расширения'}")

        # Use an existing asset directly when it belongs to this exact effective
        # profile. An asset from another profile, even in the same .aipack, is copied
        # into the active profile so category isolation remains intact.
        target: Path
        try:
            relative = source.relative_to(root).as_posix()
            _safe_relative(relative)
            target = source
            imported_copy = False
        except ValueError:
            store = root / "_AutoInformantCustom"
            store.mkdir(parents=True, exist_ok=True)
            safe_stem = self._safe_folder_component(source.stem)[:80]
            safe_name = f"{safe_stem}{source.suffix}"
            target = store / f"{uuid.uuid4().hex[:10]}_{safe_name}"
            temporary = target.with_name(target.name + ".tmp")
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            relative = target.relative_to(root).as_posix()
            imported_copy = True

        key_type, key_value = self._stop_match_key(stop, index)
        key = f"{key_type}:{key_value}"
        overrides = item.setdefault("stop_overrides", {})
        previous = overrides.get(key)
        overrides[key] = relative
        try:
            self._save_manifest(pack_id)
        except Exception:
            # Do not leave an unreferenced imported file when manifest persistence
            # fails. Existing package assets are never removed here.
            if imported_copy:
                target.unlink(missing_ok=True)
            if previous is None:
                overrides.pop(key, None)
            else:
                overrides[key] = previous
            raise
        return target

    def clear_stop_override(self, map_name: str, stop: dict[str, Any], index: int,
                            state: dict[str, Any] | None) -> bool:
        profile = self._active_profile_for_edit(map_name, state)
        if not profile:
            return False
        pack_id, item, _root = profile
        key_type, key_value = self._stop_match_key(stop, index)
        existed = item.setdefault("stop_overrides", {}).pop(f"{key_type}:{key_value}", None) is not None
        if existed:
            self._save_manifest(pack_id)
        return existed

    def set_active_pack_for_map(self, map_name: str, pack_id: str) -> None:
        map_profile = self._packs.get(pack_id)
        if not map_profile:
            raise KeyError(pack_id)
        profiles = map_profile["manifest"].get("map_profiles", [])
        if not any(self._map_matches(profile, map_name) for profile in profiles):
            raise ValueError("Выбранный пак не предназначен для этой карты")
        self.settings.setdefault("active_map_packs", {})[folder_key(map_name)] = pack_id
        self._changed()

    def assign_current_vehicle_to_category(self, pack_id: str, category_id: str,
                                           manufacturer: str, model: str, *, activate: bool = True) -> None:
        if not manufacturer.strip() and not model.strip():
            raise ValueError("Модель транспорта не определена")
        manifest = self._packs[pack_id]["manifest"]
        profile = next((p for p in manifest.get("category_profiles", []) if p.get("id") == category_id), None)
        if not profile:
            raise KeyError(category_id)
        key = " | ".join(part for part in (manufacturer.strip(), model.strip()) if part)
        assignments = profile.setdefault("vehicle_assignments", [])
        changed = not any(_normal_vehicle_key(str(value)) == _normal_vehicle_key(key) for value in assignments)
        if changed:
            assignments.append(key)
            self._save_manifest(pack_id)
        if activate:
            map_aliases = profile.get("map_aliases", [])
            map_name = map_aliases[0] if map_aliases else ""
            selection_key = self._category_selection_key(map_name, {
                "vehicle_manufacturer": manufacturer, "vehicle_model": model,
            })
            self.settings.setdefault("active_category_profiles", {})[selection_key] = f"{pack_id}:{category_id}"
            self._changed()

    def set_active_category_for_vehicle(self, pack_id: str, category_id: str,
                                        map_name: str, manufacturer: str, model: str) -> None:
        if not manufacturer.strip() and not model.strip():
            raise ValueError("Модель транспорта не определена")
        manifest = self._packs.get(pack_id, {}).get("manifest")
        if not manifest:
            raise KeyError(pack_id)
        profile = next((p for p in manifest.get("category_profiles", []) if p.get("id") == category_id), None)
        if not profile or not self._map_matches(profile, map_name):
            raise ValueError("Категория не предназначена для этой карты")
        vehicle_key = " | ".join(part for part in (manufacturer.strip(), model.strip()) if part)
        if not any(_normal_vehicle_key(str(value)) == _normal_vehicle_key(vehicle_key)
                   for value in profile.get("vehicle_assignments", [])):
            self.assign_current_vehicle_to_category(pack_id, category_id, manufacturer, model, activate=False)
        selection_key = self._category_selection_key(map_name, {
            "vehicle_manufacturer": manufacturer, "vehicle_model": model,
        })
        self.settings.setdefault("active_category_profiles", {})[selection_key] = f"{pack_id}:{category_id}"
        self._changed()

    def remove_vehicle_assignment(self, pack_id: str, category_id: str, assignment: str) -> None:
        manifest = self._packs[pack_id]["manifest"]
        profile = next((p for p in manifest.get("category_profiles", []) if p.get("id") == category_id), None)
        if not profile:
            return
        target = _normal_vehicle_key(assignment)
        profile["vehicle_assignments"] = [v for v in profile.get("vehicle_assignments", [])
                                           if _normal_vehicle_key(str(v)) != target]
        self._save_manifest(pack_id)

    def pack_for_profile(self, pack_id: str) -> dict[str, Any] | None:
        item = self._packs.get(pack_id)
        return item["manifest"] if item else None

    def pack_directory(self, pack_id: str) -> Path | None:
        item = self._packs.get(pack_id)
        return item["dir"] if item else None

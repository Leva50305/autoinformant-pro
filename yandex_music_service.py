# -*- coding: utf-8 -*-
"""Yandex Music adapter for AutoInformant Pro 2.0.

Uses the community yandex-music-api client. Authentication tokens are stored
only in the current Windows user's DPAPI-protected local data directory.
No audio is shared with other players/users and no full tracks are persisted.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable


class YandexMusicError(RuntimeError):
    pass


class TokenStoreError(YandexMusicError):
    pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class WindowsDpapiTokenStore:
    """Small DPAPI-backed token vault. The token file is unreadable to other users."""

    ENTROPY = b"AutoInformantPro|YandexMusic|2.0"
    DESCRIPTION = "AutoInformant Pro Yandex Music token"

    def __init__(self, path: Path | str):
        self.path = Path(path)

    @staticmethod
    def default_path() -> Path:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "AutoInformantPro" / "user_data" / "yandex_music.dpapi"

    @staticmethod
    def available() -> bool:
        return os.name == "nt"

    @staticmethod
    def _blob(data: bytes):
        if not data:
            return _DATA_BLOB(0, None), None
        buffer = ctypes.create_string_buffer(data)
        blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        return blob, buffer  # retain the backing buffer while DPAPI consumes the pointer

    def _protect(self, raw: bytes) -> bytes:
        if os.name != "nt":
            raise TokenStoreError("Защищённое хранение токена доступно в Windows через DPAPI.")
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        data_in, keep_in = self._blob(raw)
        entropy, keep_entropy = self._blob(self.ENTROPY)
        data_out = _DATA_BLOB()
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptProtectData(ctypes.byref(data_in), self.DESCRIPTION,
                                        ctypes.byref(entropy), None, None, 0x1,
                                        ctypes.byref(data_out)):
            raise TokenStoreError(f"Windows не смог защитить токен (ошибка {ctypes.get_last_error()}).")
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            kernel32.LocalFree(data_out.pbData)

    def _unprotect(self, encrypted: bytes) -> bytes:
        if os.name != "nt":
            raise TokenStoreError("Расшифровка токена доступна только в Windows через DPAPI.")
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        data_in, keep_in = self._blob(encrypted)
        entropy, keep_entropy = self._blob(self.ENTROPY)
        data_out = _DATA_BLOB()
        description = wintypes.LPWSTR()
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DATA_BLOB),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptUnprotectData(ctypes.byref(data_in), ctypes.byref(description),
                                          ctypes.byref(entropy), None, None, 0x1,
                                          ctypes.byref(data_out)):
            raise TokenStoreError("Не удалось расшифровать сохранённую авторизацию. Войди в Яндекс заново.")
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            kernel32.LocalFree(data_out.pbData)
            if description:
                kernel32.LocalFree(description)

    def save(self, token_payload: dict[str, Any]) -> None:
        if not isinstance(token_payload, dict) or not token_payload.get("access_token"):
            raise TokenStoreError("Яндекс не вернул access token — авторизацию нельзя сохранить.")
        raw = json.dumps(token_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = self._protect(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_bytes(encrypted)
        os.replace(temp, self.path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
        except TokenStoreError:
            raise
        except Exception as exc:
            raise TokenStoreError(f"Не удалось прочитать защищённую авторизацию: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise TokenStoreError("Файл авторизации повреждён. Войди в Яндекс заново.")
        return payload

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise TokenStoreError(f"Не удалось удалить локальную авторизацию: {exc}") from exc


def _serialized(method):
    """Serialize requests through the shared requests.Session owned by Client."""
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    wrapped.__name__ = method.__name__
    wrapped.__doc__ = method.__doc__
    return wrapped


def make_segment_marker(generation: int, segment_index: int) -> str:
    return f"__AIP_YANDEX_SEGMENT__:{int(generation)}:{int(segment_index)}"


def parse_segment_marker(value: Any) -> tuple[int, int] | None:
    import re
    match = re.fullmatch(r"__AIP_YANDEX_SEGMENT__:(\d+):(\d+)", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


class YandexMusicService:
    """Thin, defensive wrapper around MarshalX/yandex-music-api."""

    def __init__(self, token_store: WindowsDpapiTokenStore | None = None):
        self.token_store = token_store or WindowsDpapiTokenStore(WindowsDpapiTokenStore.default_path())
        self.client = None
        self.token_payload: dict[str, Any] | None = None
        self._lock = threading.RLock()
        self.wave_session_id: str | None = None
        self.wave_batch_id: str | None = None
        self.wave_diversity = "discover"
        # Rotor's `queue` parameter means tracks already delivered to this client,
        # not merely tracks whose playback-start feedback has been sent.
        self.wave_queue_ids: list[str] = []

    @staticmethod
    def library_available() -> bool:
        try:
            import yandex_music  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _client_class():
        try:
            from yandex_music import Client
            return Client
        except Exception as exc:
            raise YandexMusicError(
                "Не установлена библиотека Яндекс Музыки. Закрой программу и запусти run_app.bat, "
                "чтобы установить зависимости."
            ) from exc

    def load_saved_token(self) -> dict[str, Any] | None:
        return self.token_store.load()

    def connect(self, token: str | dict[str, Any]) -> dict[str, Any]:
        Client = self._client_class()
        payload = token if isinstance(token, dict) else {"access_token": str(token)}
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise YandexMusicError("В сохранённой авторизации нет access token.")
        with self._lock:
            client = Client(access_token)
            client.init()
            self.client = client
            self.token_payload = dict(payload)
            return self.profile()

    def device_auth(self, on_code: Callable[[Any], None], cancel_event: threading.Event | None = None) -> dict[str, Any]:
        Client = self._client_class()
        cancel_event = cancel_event or threading.Event()
        client = Client()
        # The pinned 3.2.2 client supports cooperative cancellation. The login
        # window can therefore be closed without leaving a polling worker behind.
        token = client.device_auth(on_code=on_code, should_cancel=cancel_event.is_set)
        if cancel_event.is_set() or token is None:
            raise YandexMusicError("Авторизация отменена.")
        payload = {
            "access_token": getattr(token, "access_token", None),
            "refresh_token": getattr(token, "refresh_token", None),
            "expires_in": getattr(token, "expires_in", None),
            "token_type": getattr(token, "token_type", None),
            "saved_at": int(time.time()),
        }
        if not payload["access_token"]:
            raise YandexMusicError("Яндекс не вернул токен авторизации.")
        client.init()
        with self._lock:
            self.client = client
            self.token_payload = payload
        self.token_store.save(payload)
        return self.profile()

    def disconnect(self) -> None:
        with self._lock:
            self.client = None
            self.token_payload = None
            self.wave_session_id = None
            self.wave_batch_id = None
            self.wave_queue_ids = []
        self.token_store.delete()

    @_serialized
    def profile(self) -> dict[str, Any]:
        self._require_client()
        me = getattr(self.client, "me", None)
        account = getattr(me, "account", None) if me else None
        subscription = getattr(me, "subscription", None) if me else None
        plus = getattr(subscription, "had_any_subscription", None) if subscription else None
        login = getattr(account, "login", None) or "Аккаунт Яндекса"
        display = getattr(account, "display_name", None) or getattr(account, "full_name", None) or login
        uid = getattr(account, "uid", None)
        return {"login": str(login), "display_name": str(display), "uid": uid,
                "has_subscription": bool(plus) if plus is not None else None}

    def _require_client(self):
        if self.client is None:
            raise YandexMusicError("Сначала войди в аккаунт Яндекса.")
        return self.client

    @staticmethod
    def _get(obj: Any, key: str, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @classmethod
    def normalize_track(cls, track: Any, batch_id: str | None = None) -> dict[str, Any] | None:
        if track is None:
            return None
        raw_id = cls._get(track, "id")
        if raw_id is None:
            return None
        albums = cls._get(track, "albums", []) or []
        album = albums[0] if albums else None
        album_id = cls._get(album, "id") if album else cls._get(track, "album_id")
        key = f"{raw_id}:{album_id}" if album_id else str(raw_id)
        artists = cls._get(track, "artists", []) or []
        artist_names = [str(cls._get(a, "name", "")).strip() for a in artists]
        artist_names = [a for a in artist_names if a]
        album_title = str(cls._get(album, "title", "") or "") if album else ""
        cover = cls._get(track, "cover_uri") or cls._get(album, "cover_uri")
        duration = cls._get(track, "duration_ms") or cls._get(track, "duration") or 0
        return {
            "id": str(raw_id), "track_key": key, "album_id": str(album_id) if album_id else "",
            "title": str(cls._get(track, "title", "Без названия") or "Без названия"),
            "artist": ", ".join(artist_names) or str(cls._get(track, "artist", "Исполнитель неизвестен")),
            "album": album_title, "duration_ms": int(duration or 0),
            "cover_uri": str(cover or ""), "batch_id": batch_id,
            "raw": track,
        }

    @staticmethod
    def _cover_url(uri: str, size: str = "200x200") -> str:
        value = str(uri or "").strip()
        if not value:
            return ""
        value = value.replace("%%", size)
        if value.startswith("//"):
            value = "https:" + value
        elif not value.startswith(("http://", "https://")):
            value = "https://" + value.lstrip("/")
        return value

    @_serialized
    def search(self, query: str, kind: str = "all") -> list[dict[str, Any]]:
        client = self._require_client()
        query = str(query or "").strip()
        if not query:
            return []
        result = client.search(query, type_=kind if kind in {"all", "track", "artist", "album", "playlist", "user"} else "all")
        if kind == "all":
            kind = "track"
        bucket = self._get(result, f"{kind}s", None)
        if kind == "track" and bucket is None:
            bucket = self._get(result, "tracks", None)
        items = self._get(bucket, "results", []) or []
        normalized: list[dict[str, Any]] = []
        if kind == "track":
            for item in items:
                row = self.normalize_track(item)
                if row: normalized.append(row)
        elif kind == "artist":
            for item in items:
                normalized.append({"kind": "artist", "id": str(self._get(item, "id", "")),
                                   "title": str(self._get(item, "name", "Исполнитель")),
                                   "artist": "Исполнитель", "album": "", "duration_ms": 0,
                                   "cover_uri": str(self._get(item, "cover", "") or "")})
        elif kind == "album":
            for item in items:
                artists = self._get(item, "artists", []) or []
                normalized.append({"kind": "album", "id": str(self._get(item, "id", "")),
                                   "title": str(self._get(item, "title", "Альбом")),
                                   "artist": ", ".join(str(self._get(a, "name", "")) for a in artists),
                                   "album": "Альбом", "duration_ms": 0,
                                   "cover_uri": str(self._get(item, "cover_uri", "") or "")})
        elif kind == "playlist":
            for item in items:
                owner = self._get(item, "owner", None)
                normalized.append({"kind": "playlist", "id": str(self._get(item, "kind", self._get(item, "id", ""))),
                                   "owner_id": str(self._get(owner, "uid", "") or ""),
                                   "title": str(self._get(item, "title", "Плейлист")),
                                   "artist": str(self._get(owner, "login", "Яндекс Музыка") or "Яндекс Музыка"),
                                   "album": "Плейлист", "duration_ms": 0,
                                   "cover_uri": str(self._get(item, "cover_uri", "") or "")})
        return normalized

    @_serialized
    def liked_tracks(self) -> list[dict[str, Any]]:
        client = self._require_client()
        response = client.users_likes_tracks()
        items = self._get(response, "tracks", response) or []
        result = []
        for short in items:
            try:
                track = short.fetch_track() if hasattr(short, "fetch_track") else short
                if isinstance(track, list): track = track[0] if track else None
                row = self.normalize_track(track)
                if row: row["liked"] = True; result.append(row)
            except Exception:
                continue
        return result

    @_serialized
    def liked_albums(self) -> list[dict[str, Any]]:
        client = self._require_client()
        result = []
        for like in client.users_likes_albums(rich=True) or []:
            album = self._get(like, "album", None)
            if album is None:
                continue
            artists = self._get(album, "artists", []) or []
            cover = self._get(album, "cover", None)
            cover_uri = self._get(cover, "uri", "") if cover else self._get(album, "cover_uri", "")
            result.append({
                "kind": "album", "id": str(self._get(album, "id", "")),
                "title": str(self._get(album, "title", "Альбом")),
                "artist": ", ".join(str(self._get(a, "name", "")) for a in artists if self._get(a, "name", "")),
                "album": "Альбом", "duration_ms": 0, "cover_uri": str(cover_uri or ""),
                "liked": True,
            })
        return result

    @_serialized
    def liked_artists(self) -> list[dict[str, Any]]:
        client = self._require_client()
        result = []
        for like in client.users_likes_artists() or []:
            artist = self._get(like, "artist", None)
            if artist is None:
                continue
            cover = self._get(artist, "cover", None)
            cover_uri = self._get(cover, "uri", "") if cover else self._get(artist, "cover_uri", "")
            result.append({
                "kind": "artist", "id": str(self._get(artist, "id", "")),
                "title": str(self._get(artist, "name", "Исполнитель")),
                "artist": "Исполнитель", "album": "Исполнитель", "duration_ms": 0,
                "cover_uri": str(cover_uri or ""), "liked": True,
            })
        return result

    @_serialized
    def playlists(self) -> list[dict[str, Any]]:
        client = self._require_client()
        result = client.users_playlists_list()
        playlists = result if isinstance(result, list) else self._get(result, "playlists", [])
        rows = []
        for item in playlists or []:
            owner = self._get(item, "owner", None)
            rows.append({"kind": "playlist", "id": str(self._get(item, "kind", self._get(item, "id", ""))),
                         "owner_id": str(self._get(owner, "uid", "") or ""),
                         "title": str(self._get(item, "title", "Плейлист")),
                         "artist": str(self._get(owner, "login", "Моя коллекция") or "Моя коллекция"),
                         "album": "Плейлист", "duration_ms": 0,
                         "cover_uri": str(self._get(item, "cover_uri", "") or "")})
        return rows

    @_serialized
    def playlist_tracks(self, playlist_kind: str, owner_id: str | int | None = None) -> list[dict[str, Any]]:
        client = self._require_client()
        playlist = client.users_playlists(playlist_kind, user_id=owner_id) if owner_id else client.users_playlists(playlist_kind)
        items = self._get(playlist, "tracks", None)
        if items is None and hasattr(playlist, "fetch_tracks"):
            items = playlist.fetch_tracks()
        rows = []
        for short in items or []:
            track = self._get(short, "track", None) or short
            if hasattr(track, "fetch_track"):
                try: track = track.fetch_track()
                except Exception: pass
            row = self.normalize_track(track)
            if row: rows.append(row)
        return rows

    @_serialized
    def album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        client = self._require_client()
        album = client.albums_with_tracks(album_id)
        volumes = self._get(album, "volumes", []) or []
        rows = []
        for volume in volumes:
            for track in volume or []:
                row = self.normalize_track(track)
                if row: rows.append(row)
        if not rows:
            for track in self._get(album, "tracks", []) or []:
                row = self.normalize_track(track)
                if row: rows.append(row)
        return rows

    @_serialized
    def artist_tracks(self, artist_id: str) -> list[dict[str, Any]]:
        client = self._require_client()
        response = client.artists_tracks(artist_id, page=0, page_size=50)
        tracks = self._get(response, "tracks", []) or []
        return [row for row in (self.normalize_track(t) for t in tracks) if row]

    @_serialized
    def set_liked(self, entity: dict[str, Any], liked: bool) -> bool:
        client = self._require_client()
        kind = str(entity.get("kind") or "track")
        entity_id = str(entity.get("id") or "").strip()
        if not entity_id:
            raise YandexMusicError("У выбранного объекта отсутствует ID.")
        if kind == "album":
            method = client.users_likes_albums_add if liked else client.users_likes_albums_remove
        elif kind == "artist":
            method = client.users_likes_artists_add if liked else client.users_likes_artists_remove
        elif kind == "track":
            # Likes endpoint expects the numeric track ID; album-qualified IDs
            # are only needed when requesting a specific recording.
            method = client.users_likes_tracks_add if liked else client.users_likes_tracks_remove
        else:
            raise YandexMusicError("Лайк этого типа объекта пока не поддерживается.")
        return bool(method(entity_id))

    @_serialized
    def stream_url(self, track: dict[str, Any]) -> tuple[str, bool, str]:
        """Return (direct_url, is_preview, codec); never requests encrypted raw delivery."""
        client = self._require_client()
        key = str(track.get("track_key") or track.get("id") or "")
        if not key:
            raise YandexMusicError("У выбранного трека отсутствует ID.")
        infos = client.tracks_download_info(key, get_direct_links=False)
        if not infos:
            raise YandexMusicError("Яндекс не предоставил варианты потокового воспроизведения для этого трека.")
        def rank(info):
            codec = str(self._get(info, "codec", "")).lower()
            preview = bool(self._get(info, "preview", False))
            bitrate = int(self._get(info, "bitrate_in_kbps", 0) or 0)
            allowed = 0 if codec in {"mp3", "aac", "m4a", "alac"} else 1
            return (preview, allowed, -bitrate)
        candidates = sorted(infos, key=rank)
        last_error = None
        for info in candidates:
            codec = str(self._get(info, "codec", "")).lower()
            if codec in {"encraw", "raw", "flac", "wav"}:
                continue
            try:
                # The signed URL is generated immediately before FFmpeg opens it.
                url = info.get_direct_link(timeout=15) if hasattr(info, "get_direct_link") else ""
                if url:
                    return str(url), bool(self._get(info, "preview", False)), codec
            except Exception as exc:
                last_error = exc
        if last_error:
            raise YandexMusicError(f"Не удалось получить прямую ссылку Яндекса: {last_error}") from last_error
        raise YandexMusicError("Для трека не найден поддерживаемый MP3/AAC-поток. Зашифрованный raw-формат не используется.")

    @staticmethod
    def _sequence_tracks(response: Any) -> tuple[list[Any], str | None]:
        sequences = YandexMusicService._get(response, "sequence", []) or []
        tracks = []
        batch_id = YandexMusicService._get(response, "batch_id", None)
        for sequence in sequences:
            if str(YandexMusicService._get(sequence, "type", "track")).lower() not in {"track", ""}:
                continue
            track = YandexMusicService._get(sequence, "track", None)
            if track is not None:
                tracks.append(track)
        return tracks, str(batch_id) if batch_id is not None else None

    @staticmethod
    def _rotor_track_id(track: Any) -> str:
        raw_id = YandexMusicService._get(track, "id", None)
        if raw_id is None:
            return ""
        albums = YandexMusicService._get(track, "albums", []) or []
        album = albums[0] if albums else None
        album_id = YandexMusicService._get(album, "id", None) if album else YandexMusicService._get(track, "album_id", None)
        return f"{raw_id}:{album_id}" if album_id else str(raw_id)

    def _remember_rotor_tracks(self, tracks: list[Any]) -> None:
        known = set(self.wave_queue_ids)
        for track in tracks:
            key = self._rotor_track_id(track)
            if key and key not in known:
                self.wave_queue_ids.append(key)
                known.add(key)

    @_serialized
    def wave_start(self, diversity: str = "discover", *, exclude_queue: list[str] | None = None) -> list[dict[str, Any]]:
        client = self._require_client()
        diversity = "favorite" if diversity == "favorite" else "discover"
        seeds = ["user:onyourwave", f"settingDiversity:{diversity}"]
        excluded = list(dict.fromkeys(str(value) for value in (exclude_queue or []) if str(value)))
        session_kwargs = {
            "seeds": seeds,
            "include_tracks_in_response": True,
            "include_wave_model": True,
            "interactive": True,
            "allow_explicit": True,
        }
        if excluded:
            session_kwargs["queue"] = list(excluded)
        session = client.rotor_session_new(**session_kwargs)
        if session is None:
            raise YandexMusicError("Яндекс не создал сессию «Моей волны».")
        session_id = self._get(session, "radio_session_id") or self._get(session, "id")
        if not session_id:
            raise YandexMusicError("Яндекс создал волну без идентификатора сессии.")
        self.wave_session_id = str(session_id)
        self.wave_diversity = diversity
        tracks, batch_id = self._sequence_tracks(session)
        self.wave_batch_id = batch_id
        self.wave_queue_ids = excluded
        self._remember_rotor_tracks(tracks)
        try:
            client.rotor_session_feedback_radio_started(self.wave_session_id, batch_id=batch_id)
        except Exception:
            pass
        return [row for row in (self.normalize_track(t, batch_id) for t in tracks) if row]

    @_serialized
    def wave_next(self) -> list[dict[str, Any]]:
        client = self._require_client()
        if not self.wave_session_id:
            return self.wave_start(self.wave_diversity)
        response = client.rotor_session_tracks(self.wave_session_id, queue=list(self.wave_queue_ids))
        if response is None or bool(self._get(response, "unknown_session", False)):
            # Renew an expired Rotor session without forgetting tracks already
            # delivered to the player; the new session can continue the same wave.
            return self.wave_start(self.wave_diversity, exclude_queue=list(self.wave_queue_ids))
        tracks, batch_id = self._sequence_tracks(response)
        self.wave_batch_id = batch_id or self.wave_batch_id
        self._remember_rotor_tracks(tracks)
        rows = [row for row in (self.normalize_track(t, self.wave_batch_id) for t in tracks) if row]
        if rows:
            return rows
        # An empty batch is not treated as the end of a personal radio session.
        # Re-create it with the delivered-track exclusion list to avoid looping
        # back to the first five songs.
        return self.wave_start(self.wave_diversity, exclude_queue=list(self.wave_queue_ids))

    @_serialized
    def wave_track_started(self, track: dict[str, Any]) -> None:
        if not self.wave_session_id:
            return
        track_id = str(track.get("track_key") or track.get("id"))
        self._require_client().rotor_session_feedback_track_started(
            self.wave_session_id, track_id, batch_id=track.get("batch_id") or self.wave_batch_id)

    @_serialized
    def wave_track_finished(self, track: dict[str, Any], played_seconds: float) -> None:
        if not self.wave_session_id:
            return
        track_id = str(track.get("track_key") or track.get("id"))
        self._require_client().rotor_session_feedback_track_finished(
            self.wave_session_id, track_id, max(0.0, float(played_seconds)),
            batch_id=track.get("batch_id") or self.wave_batch_id)

    @_serialized
    def wave_track_skipped(self, track: dict[str, Any], played_seconds: float) -> None:
        if not self.wave_session_id:
            return
        track_id = str(track.get("track_key") or track.get("id"))
        self._require_client().rotor_session_feedback_skip(
            self.wave_session_id, track_id, max(0.0, float(played_seconds)),
            batch_id=track.get("batch_id") or self.wave_batch_id)

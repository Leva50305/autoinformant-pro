# -*- coding: utf-8 -*-
"""Offline tests for the Yandex Music adapter. No real account or CDN is used."""
from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from yandex_music_service import (
    YandexMusicService, YandexMusicError, TokenStoreError,
    make_segment_marker, parse_segment_marker, WindowsDpapiTokenStore,
)


class FakeTokenStore:
    def __init__(self):
        self.payload = None

    def save(self, payload):
        self.payload = dict(payload)

    def load(self):
        return dict(self.payload) if self.payload else None

    def delete(self):
        self.payload = None


class FakeDownloadInfo:
    def __init__(self, codec="mp3", bitrate=192, preview=False, direct="https://cdn.example/audio"):
        self.codec = codec
        self.bitrate_in_kbps = bitrate
        self.preview = preview
        self.direct = direct
        self.calls = []

    def get_direct_link(self, timeout=15):
        self.calls.append(timeout)
        return self.direct


class FakeClient:
    def __init__(self, token=None):
        self.token = token
        self.me = None
        self.calls = []
        self.download_infos = []
        self.search_result = None
        self.rotor_response = None
        self.next_rotor_response = None

    def init(self):
        self.me = SimpleNamespace(
            account=SimpleNamespace(login="test-user", display_name="Test User", uid=123),
            subscription=SimpleNamespace(had_any_subscription=True),
        )
        return self

    def device_auth(self, on_code=None, should_cancel=None, timeout=None, poll_interval=None):
        self.calls.append(("device_auth", timeout, poll_interval, should_cancel is not None))
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled")
        if on_code:
            on_code(SimpleNamespace(verification_url="https://example.test/device", user_code="ABCD-EFGH", expires_in=600))
        return SimpleNamespace(access_token="access-test", refresh_token="refresh-test", expires_in=3600, token_type="bearer")

    def search(self, query, type_="all"):
        self.calls.append(("search", query, type_))
        return self.search_result

    def tracks_download_info(self, track_id, get_direct_links=False):
        self.calls.append(("download_info", track_id, get_direct_links))
        return self.download_infos

    def rotor_session_new(self, **kwargs):
        self.calls.append(("rotor_new", kwargs))
        return self.rotor_response

    def rotor_session_feedback_radio_started(self, *args, **kwargs):
        self.calls.append(("radio_started", args, kwargs)); return True

    def rotor_session_tracks(self, session_id, queue=None):
        self.calls.append(("rotor_next", session_id, list(queue or [])))
        return self.next_rotor_response

    def rotor_session_feedback_track_started(self, *args, **kwargs):
        self.calls.append(("track_started", args, kwargs)); return True

    def rotor_session_feedback_track_finished(self, *args, **kwargs):
        self.calls.append(("track_finished", args, kwargs)); return True

    def rotor_session_feedback_skip(self, *args, **kwargs):
        self.calls.append(("track_skip", args, kwargs)); return True

    def users_likes_tracks(self):
        return SimpleNamespace(tracks=[])

    def users_likes_tracks_add(self, track_id):
        self.calls.append(("like", track_id)); return True

    def users_likes_tracks_remove(self, track_id):
        self.calls.append(("unlike", track_id)); return True

    def users_likes_albums(self, rich=False):
        return [SimpleNamespace(album=SimpleNamespace(
            id=201, title="Liked Album", artists=[SimpleNamespace(name="Album Artist")],
            cover=SimpleNamespace(uri="https://img.test/album"))) ]

    def users_likes_artists(self):
        return [SimpleNamespace(artist=SimpleNamespace(
            id=301, name="Liked Artist", cover=SimpleNamespace(uri="https://img.test/artist"))) ]

    def users_likes_albums_add(self, album_id):
        self.calls.append(("like_album", album_id)); return True

    def users_likes_albums_remove(self, album_id):
        self.calls.append(("unlike_album", album_id)); return True

    def users_likes_artists_add(self, artist_id):
        self.calls.append(("like_artist", artist_id)); return True

    def users_likes_artists_remove(self, artist_id):
        self.calls.append(("unlike_artist", artist_id)); return True

    def users_playlists_list(self):
        return [SimpleNamespace(kind=42, title="Test playlist", owner=SimpleNamespace(uid=123, login="test-user"), cover_uri="")]

    def users_playlists(self, kind, user_id=None):
        return SimpleNamespace(tracks=[])

    def albums_with_tracks(self, album_id):
        return SimpleNamespace(volumes=[])

    def artists_tracks(self, artist_id, page=0, page_size=50):
        return SimpleNamespace(tracks=[])


def fake_track(track_id=100, album_id=200, title="Song"):
    return SimpleNamespace(
        id=track_id, title=title, duration_ms=184000,
        albums=[SimpleNamespace(id=album_id, title="Album", cover_uri="https://img.test/cover")],
        artists=[SimpleNamespace(name="Artist")], cover_uri=None,
    )


class TestYandexMarkers(unittest.TestCase):
    def test_marker_round_trip(self):
        marker = make_segment_marker(17, 4)
        self.assertEqual(marker, "__AIP_YANDEX_SEGMENT__:17:4")
        self.assertEqual(parse_segment_marker(marker), (17, 4))

    def test_invalid_marker_is_ignored(self):
        for marker in (None, "song", "__AIP_YANDEX_SEGMENT__:x:1", "__AIP_YANDEX_SEGMENT__:1:2:3"):
            self.assertIsNone(parse_segment_marker(marker))


class TestYandexService(unittest.TestCase):
    def setUp(self):
        self.store = FakeTokenStore()
        self.service = YandexMusicService(self.store)
        self.client = FakeClient("token")
        self.service.client = self.client

    def test_normalize_track_preserves_track_and_album_ids(self):
        row = self.service.normalize_track(fake_track())
        self.assertEqual(row["id"], "100")
        self.assertEqual(row["album_id"], "200")
        self.assertEqual(row["track_key"], "100:200")
        self.assertEqual(row["artist"], "Artist")
        self.assertEqual(row["duration_ms"], 184000)

    def test_search_tracks_normalizes_results(self):
        self.client.search_result = SimpleNamespace(tracks=SimpleNamespace(results=[fake_track()]))
        rows = self.service.search("test song", "track")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Song")
        self.assertEqual(self.client.calls[-1], ("search", "test song", "track"))

    def test_search_empty_query_does_not_call_api(self):
        self.assertEqual(self.service.search("  ", "track"), [])
        self.assertEqual(self.client.calls, [])

    def test_stream_url_prefers_full_mp3_over_preview_and_returns_temporary_url(self):
        preview = FakeDownloadInfo("mp3", 320, True, "https://cdn/preview")
        full_low = FakeDownloadInfo("mp3", 128, False, "https://cdn/full-low")
        full_high = FakeDownloadInfo("aac", 256, False, "https://cdn/full-high")
        self.client.download_infos = [preview, full_low, full_high]
        url, is_preview, codec = self.service.stream_url({"id": "100", "track_key": "100:200"})
        self.assertEqual(url, "https://cdn/full-high")
        self.assertFalse(is_preview)
        self.assertEqual(codec, "aac")
        self.assertEqual(full_high.calls, [15])
        self.assertEqual(self.client.calls[0], ("download_info", "100:200", False))

    def test_stream_url_refuses_encrypted_raw_when_no_supported_format(self):
        encrypted = FakeDownloadInfo("encraw", 0, False, "")
        self.client.download_infos = [encrypted]
        with self.assertRaisesRegex(YandexMusicError, "Зашифрованный raw"):
            self.service.stream_url({"id": "100"})
        self.assertEqual(encrypted.calls, [])

    def test_wave_favorite_uses_real_rotor_session_seed(self):
        self.client.rotor_response = SimpleNamespace(
            radio_session_id="session-1", batch_id="batch-1",
            sequence=[SimpleNamespace(type="track", track=fake_track()),
                      SimpleNamespace(type="artist", track=None)],
        )
        rows = self.service.wave_start("favorite")
        args = self.client.calls[0][1]
        self.assertEqual(args["seeds"], ["user:onyourwave", "settingDiversity:favorite"])
        self.assertEqual(self.service.wave_session_id, "session-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch_id"], "batch-1")
        self.assertTrue(any(c[0] == "radio_started" for c in self.client.calls))

    def test_rotor_queue_tracks_every_delivered_track_not_only_started_tracks(self):
        first_batch = [fake_track(track_id=i, album_id=200 + i, title=f"Song {i}") for i in range(100, 105)]
        second_batch = [fake_track(track_id=i, album_id=200 + i, title=f"Song {i}") for i in range(105, 110)]
        third_batch = [fake_track(track_id=i, album_id=200 + i, title=f"Song {i}") for i in range(110, 115)]
        self.client.rotor_response = SimpleNamespace(
            radio_session_id="session-queue", batch_id="batch-1",
            sequence=[SimpleNamespace(type="track", track=item) for item in first_batch],
        )
        self.client.next_rotor_response = SimpleNamespace(
            batch_id="batch-2", sequence=[SimpleNamespace(type="track", track=item) for item in second_batch],
        )
        rows1 = self.service.wave_start("discover")
        self.assertEqual(len(rows1), 5)
        self.assertEqual(self.service.wave_queue_ids, [f"{i}:{200 + i}" for i in range(100, 105)])

        rows2 = self.service.wave_next()
        self.assertEqual(len(rows2), 5)
        request1 = next(call for call in self.client.calls if call[0] == "rotor_next")
        self.assertEqual(request1[2], [f"{i}:{200 + i}" for i in range(100, 105)])
        self.assertEqual(self.service.wave_queue_ids, [f"{i}:{200 + i}" for i in range(100, 110)])

        self.client.next_rotor_response = SimpleNamespace(
            batch_id="batch-3", sequence=[SimpleNamespace(type="track", track=item) for item in third_batch],
        )
        rows3 = self.service.wave_next()
        self.assertEqual(len(rows3), 5)
        requests = [call for call in self.client.calls if call[0] == "rotor_next"]
        self.assertEqual(requests[1][2], [f"{i}:{200 + i}" for i in range(100, 110)])
        self.assertEqual(len(self.service.wave_queue_ids), 15)
        self.assertEqual(len(set(self.service.wave_queue_ids)), 15)

    def test_empty_rotor_batch_restarts_session_without_repeating_delivered_tracks(self):
        first_batch = [fake_track(track_id=i, album_id=200 + i, title=f"Song {i}") for i in range(100, 105)]
        replacement_batch = [fake_track(track_id=i, album_id=200 + i, title=f"Song {i}") for i in range(105, 108)]
        self.client.rotor_response = SimpleNamespace(
            radio_session_id="session-old", batch_id="batch-old",
            sequence=[SimpleNamespace(type="track", track=item) for item in first_batch],
        )
        self.service.wave_start("discover")
        self.client.next_rotor_response = SimpleNamespace(batch_id="batch-empty", sequence=[])
        self.client.rotor_response = SimpleNamespace(
            radio_session_id="session-new", batch_id="batch-new",
            sequence=[SimpleNamespace(type="track", track=item) for item in replacement_batch],
        )

        rows = self.service.wave_next()
        self.assertEqual([row["id"] for row in rows], ["105", "106", "107"])
        renew = [call for call in self.client.calls if call[0] == "rotor_new"][-1]
        self.assertEqual(renew[1]["queue"], [f"{i}:{200 + i}" for i in range(100, 105)])
        self.assertEqual(self.service.wave_session_id, "session-new")
        self.assertEqual(len(self.service.wave_queue_ids), 8)

    def test_rotor_received_track_ids_are_deduplicated_and_can_be_queued_before_play(self):
        repeated = [fake_track(track_id=100, album_id=200), fake_track(track_id=100, album_id=200)]
        self.service.wave_queue_ids = []
        self.service._remember_rotor_tracks(repeated)
        self.assertEqual(self.service.wave_queue_ids, ["100:200"])

    def test_wave_feedback_methods_use_session_batch(self):
        self.service.wave_session_id = "session-1"
        self.service.wave_batch_id = "batch-1"
        row = {"id": "100", "track_key": "100:200"}
        self.service.wave_track_started(row)
        self.service.wave_track_finished(row, 33.5)
        self.service.wave_track_skipped(row, 4.0)
        names = [call[0] for call in self.client.calls]
        self.assertEqual(names, ["track_started", "track_finished", "track_skip"])
        self.assertEqual(self.client.calls[1][1][1], "100:200")
        self.assertEqual(self.client.calls[1][1][2], 33.5)

    def test_device_flow_saves_token_and_initializes_profile(self):
        token_store = FakeTokenStore()
        service = YandexMusicService(token_store)
        fake = FakeClient()
        with patch.object(service, "_client_class", return_value=lambda: fake):
            seen = []
            profile = service.device_auth(seen.append, threading.Event())
        self.assertEqual(profile["login"], "test-user")
        self.assertEqual(token_store.payload["access_token"], "access-test")
        self.assertEqual(seen[0].user_code, "ABCD-EFGH")
        self.assertTrue(any(call[0] == "device_auth" and call[3] for call in fake.calls))

    def test_device_flow_can_be_cancelled_without_saving_a_token(self):
        token_store = FakeTokenStore()
        service = YandexMusicService(token_store)
        fake = FakeClient()
        with patch.object(service, "_client_class", return_value=lambda: fake):
            cancelled = threading.Event(); cancelled.set()
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                service.device_auth(lambda _code: None, cancelled)
        self.assertIsNone(token_store.payload)

    def test_liked_flag_methods_use_track_numeric_id(self):
        row = {"id": "100", "track_key": "100:200"}
        self.assertTrue(self.service.set_liked(row, True))
        self.assertEqual(self.client.calls[-1], ("like", "100"))
        self.assertTrue(self.service.set_liked(row, False))
        self.assertEqual(self.client.calls[-1], ("unlike", "100"))

    def test_liked_albums_normalize_like_album_objects(self):
        rows = self.service.liked_albums()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "album")
        self.assertEqual(rows[0]["id"], "201")
        self.assertEqual(rows[0]["title"], "Liked Album")
        self.assertEqual(rows[0]["artist"], "Album Artist")
        self.assertTrue(rows[0]["liked"])

    def test_liked_artists_normalize_like_artist_objects(self):
        rows = self.service.liked_artists()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "artist")
        self.assertEqual(rows[0]["id"], "301")
        self.assertEqual(rows[0]["title"], "Liked Artist")
        self.assertTrue(rows[0]["liked"])

    def test_liked_album_and_artist_actions_use_matching_endpoints(self):
        self.assertTrue(self.service.set_liked({"kind": "album", "id": "201"}, True))
        self.assertEqual(self.client.calls[-1], ("like_album", "201"))
        self.assertTrue(self.service.set_liked({"kind": "album", "id": "201"}, False))
        self.assertEqual(self.client.calls[-1], ("unlike_album", "201"))
        self.assertTrue(self.service.set_liked({"kind": "artist", "id": "301"}, True))
        self.assertEqual(self.client.calls[-1], ("like_artist", "301"))
        self.assertTrue(self.service.set_liked({"kind": "artist", "id": "301"}, False))
        self.assertEqual(self.client.calls[-1], ("unlike_artist", "301"))

    @unittest.skipIf(os.name == "nt", "проверяет поведение DPAPI вне Windows")
    def test_packaging_app_does_not_call_dpapi_outside_windows(self):
        store = WindowsDpapiTokenStore("/tmp/not-used.dpapi")
        with self.assertRaises(TokenStoreError):
            store._protect(b"secret")


if __name__ == "__main__":
    unittest.main()

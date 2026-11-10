from __future__ import annotations
import math
import json
import shutil
import struct
import tempfile
import unittest
import unittest.mock
import wave
from pathlib import Path

from informant_core import (
    AudioLibrary, AUX_ALIASES, estimated_time_to_next_stop, find_current_stop_index,
    is_announceable_stop, normalize_text, normalize_stop_objects,
)
from openomsi_bridge import OpenOMSIBridge
from AutoInformant import AutoAnnouncementEngine, DEFAULT_SETTINGS, load_settings, should_preserve_terminal_phrase
from pack_system import PackManager
import AutoInformant as app_module


def make_wav(path: Path, seconds: float = 1.0, sample_rate: int = 8000) -> None:
    n = int(seconds * sample_rate)
    samples = bytearray(n * 2)
    for i in range(n):
        v = int(7000 * math.sin(2 * math.pi * 440 * i / sample_rate))
        struct.pack_into("<h", samples, i * 2, v)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples)


class FakeApp:
    def __init__(self, root: Path):
        self.settings = {
            "auto_enabled": True, "auto_arrival": True, "auto_departure": True,
            "play_next_prefix": True, "play_terminus": False,
            "anons_enabled": True, "anons_mode": "stops", "anons_interval": 1,
            "average_speed_kmh": 25, "anons_safety_seconds": 1,
        }
        self.library = AudioLibrary(root, {})
        self.map_name = "MapA"
        self.current_state = {}
        self.items = []
        self.logs = []

    def resolve_stop_audio(self, stop, index):
        return self.library.find_stop_audio(self.map_name, self.current_state, stop, index)

    def enqueue_audio(self, path, label):
        if path:
            self.items.append((Path(path), label))

    def log(self, message):
        self.logs.append(message)


class TestInformantCore(unittest.TestCase):
    def test_normalize_text(self):
        self.assertEqual(normalize_text("  Ёлочная  площадь! "), "ёлочная площадь")

    def test_current_stop_prefers_id(self):
        stops = [{"id": "A", "name": "Same"}, {"id": "B", "name": "Same"}]
        self.assertEqual(find_current_stop_index({"next_stop_id": "B", "next_stop_index": 1}, stops), 1)

    def test_normalize_stop_objects_and_pass_through(self):
        stops = normalize_stop_objects({"duty_stop_objects": [
            {"id": 2, "name": "Station", "stops": True},
            {"id": 3, "name": "Pass point", "stops": False},
        ]})
        self.assertEqual(len(stops), 2)
        self.assertTrue(is_announceable_stop(stops[0]))
        self.assertFalse(is_announceable_stop(stops[1]))

    def test_audio_library_match_and_map_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "MapA" / "Line01" / "01 - Центральная площадь.wav"
            wrong_map = root / "MapB" / "All" / "Центральная площадь.wav"
            make_wav(good)
            make_wav(wrong_map)
            lib = AudioLibrary(root)
            hit = lib.find_stop_audio("MapA", {"line": "Line01"}, {"name": "Центральная площадь"}, 0)
            self.assertEqual(hit, good)
            miss = lib.find_stop_audio("MapC", {}, {"name": "Центральная площадь"}, 0)
            self.assertIsNone(miss)

    def test_announcement_fits_eta_and_does_not_repeat_single_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            anon = root / "MapA" / "Анонсы" / "museum.wav"
            make_wav(anon, seconds=1)
            lib = AudioLibrary(root)
            files = lib.announcement_files("MapA", {})
            picked = lib.choose_announcement(files, eta_seconds=10, history=[], margin_seconds=1)
            self.assertEqual(picked, anon)
            self.assertIsNone(lib.choose_announcement(files, eta_seconds=10, history=[str(anon.resolve())], margin_seconds=1))
            self.assertIsNone(lib.choose_announcement(files, eta_seconds=1, history=[], margin_seconds=1))

    def test_aux_cues_find_route_root_and_map_root_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route_dir = root / "MapA" / "Line01"
            next_clip = route_dir / "NEXT.wav"
            terminus_clip = route_dir / "TERMINUS.wav"
            map_signal = root / "MapA" / "SIGNAL.wav"
            museum = route_dir / "Анонсы" / "museum.wav"
            make_wav(next_clip)
            make_wav(terminus_clip)
            make_wav(map_signal)
            make_wav(museum, seconds=2)
            lib = AudioLibrary(root)
            state = {"line": "Line01", "tour": "1"}
            self.assertEqual(lib.aux_file("MapA", state, AUX_ALIASES["next"]), next_clip)
            self.assertEqual(lib.aux_file("MapA", state, AUX_ALIASES["terminus"]), terminus_clip)
            self.assertEqual(lib.aux_file("MapA", state, AUX_ALIASES["signal"]), map_signal)
            files = lib.announcement_files("MapA", state)
            self.assertEqual([p.name for p in files], ["museum.wav"])

    def test_next_prefix_is_queued_when_file_is_in_route_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route_dir = root / "MapA" / "Line01"
            make_wav(route_dir / "Stop A.wav")
            make_wav(route_dir / "Stop B.wav")
            next_clip = route_dir / "NEXT.wav"
            make_wav(next_clip)
            fake = FakeApp(root)
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [
                {"id": "a", "name": "Stop A", "stops": True},
                {"id": "b", "name": "Stop B", "stops": True},
            ]
            first = {"line": "Line01", "tour": "1", "trip": "1", "trip_name": "test",
                     "map_path": "x", "at_stop": True, "next_stop_id": "a", "next_stop_index": 1,
                     "next_stop_name": "Stop A", "speed": 0, "next_stop_distance": 0, "game_clock": 100}
            fake.current_state = first
            engine.reset_route("MapA|Line01", first)
            engine.update(first, stops, "MapA")
            departed = dict(first, at_stop=False, next_stop_id="b", next_stop_index=2,
                            next_stop_name="Stop B", speed=12, next_stop_distance=700, game_clock=110)
            fake.current_state = departed
            engine.update(departed, stops, "MapA")
            labels = [label for _path, label in fake.items]
            self.assertEqual(labels, ["Прибытие: Stop A", "Следующая остановка — сигнал", "Следующая остановка: Stop B"])
            self.assertEqual(fake.items[1][0], next_clip)

    def test_aux_fallback_finds_cue_in_route_folder_even_when_route_label_differs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cue = root / "MapA" / "Route Folder Name Generated By User" / "TERMINUS.wav"
            make_wav(cue)
            lib = AudioLibrary(root)
            self.assertEqual(lib.aux_file("MapA", {"line": "X99", "tour": "8"}, AUX_ALIASES["terminus"]), cue)

    def test_aux_cues_match_map_folder_with_underscore_vs_spaces(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            map_dir = root / "Tver_2022"
            next_clip = map_dir / "NEXT.wav"
            make_wav(next_clip)
            lib = AudioLibrary(root)
            self.assertEqual(lib.aux_file("Tver 2022", {}, AUX_ALIASES["next"]), next_clip)

    def test_aux_cues_find_in_global_audio_root_announcements(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "MapA").mkdir()
            cue = root / "Анонсы" / "NEXT.wav"
            make_wav(cue)
            lib = AudioLibrary(root)
            self.assertEqual(lib.aux_file("MapA", {}, AUX_ALIASES["next"]), cue)

    def test_aux_cues_support_numbered_filename_prefixes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            next_clip = root / "MapA" / "Анонсы" / "01 - NEXT.wav"
            terminus_clip = root / "MapA" / "Анонсы" / "02_TERMINUS.wav"
            make_wav(next_clip)
            make_wav(terminus_clip)
            lib = AudioLibrary(root)
            self.assertEqual(lib.aux_file("MapA", {}, AUX_ALIASES["next"]), next_clip)
            self.assertEqual(lib.aux_file("MapA", {}, AUX_ALIASES["terminus"]), terminus_clip)

    def test_manual_per_map_audio_bank_can_have_any_folder_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "maps"
            wrong_default = root / "Tver_2026" / "All" / "Центральная.wav"
            make_wav(wrong_default)
            selected_bank = Path(td) / "Female Trolleybus Voice"
            stop_file = selected_bank / "All" / "Центральная.wav"
            terminus = selected_bank / "All" / "TERMINUS.wav"
            make_wav(stop_file)
            make_wav(terminus)
            lib = AudioLibrary(root, {}, {"Tver_2026": str(selected_bank)})
            resolved = lib.find_stop_audio("Tver_2026", {}, {"name": "Центральная"}, 0)
            self.assertEqual(resolved, stop_file)
            cue = lib.aux_file("Tver_2026", {}, AUX_ALIASES["terminus"])
            self.assertEqual(cue, terminus)

    def test_terminal_index_ignores_trailing_pass_through_points(self):
        stops = [
            {"name": "First", "stops": True},
            {"name": "Terminal", "stops": True},
            {"name": "Road end marker", "stops": False},
        ]
        self.assertEqual(AutoAnnouncementEngine._final_announceable_index(stops, {"terminus": "Terminal"}), 1)
        self.assertEqual(AutoAnnouncementEngine._final_announceable_index(stops, {}), 1)

    def test_terminus_queued_after_final_stop_name_on_departure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route_dir = root / "MapA" / "Line01"
            make_wav(route_dir / "Stop A.wav")
            make_wav(route_dir / "Stop B.wav")
            make_wav(route_dir / "NEXT.wav")
            terminus = route_dir / "TERMINUS.wav"
            make_wav(terminus)
            fake = FakeApp(root)
            fake.settings["play_terminus"] = True
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [
                {"id": "a", "name": "Stop A", "stops": True, "departure": 100, "x": 0, "y": 0},
                {"id": "b", "name": "Stop B", "stops": True, "arrival": 220, "x": 800, "y": 0},
            ]
            state = {"line": "Line01", "tour": "1", "terminus": "Stop B", "game_clock": 100,
                     "speed": 15, "next_stop_distance": 500, "next_stop_distance_nav": 500}
            engine._departure(state, stops, "MapA", 0, 1)
            labels = [label for _path, label in fake.items]
            self.assertEqual(labels[:3], ["Следующая остановка — сигнал", "Следующая остановка: Stop B", "Конечная (следующая остановка)"])
            self.assertEqual(fake.items[2][0], terminus)

    def test_schedule_eta(self):
        stops = [
            {"name": "A", "departure": 1000, "x": 0, "y": 0},
            {"name": "B", "arrival": 1120, "x": 800, "y": 0},
        ]
        eta = estimated_time_to_next_stop({"speed": 0}, stops, 1)
        self.assertEqual(eta, 120)

    def test_auto_engine_arrival_then_departure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            map_dir = root / "MapA"
            make_wav(map_dir / "Stop A.wav")
            make_wav(map_dir / "Stop B.wav")
            make_wav(map_dir / "Анонсы" / "NEXT.wav")
            make_wav(map_dir / "Анонсы" / "museum.wav", seconds=3)
            fake = FakeApp(root)
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [
                {"id": "a", "name": "Stop A", "stops": True, "departure": 100, "x": 0, "y": 0},
                {"id": "b", "name": "Stop B", "stops": True, "arrival": 220, "x": 800, "y": 0},
            ]
            first = {"line": "1", "tour": "1", "trip": "1", "trip_name": "test", "map_path": "x", 
                     "at_stop": True, "next_stop_id": "a", "next_stop_index": 1,
                     "next_stop_name": "Stop A", "speed": 0, "next_stop_distance": 0, "game_clock": 100}
            fake.current_state = first
            engine.reset_route("MapA|1", first)
            engine.update(first, stops, "MapA")
            self.assertEqual([x[1] for x in fake.items], ["Прибытие: Stop A"])
            departed = dict(first, at_stop=False, next_stop_id="b", next_stop_index=2,
                            next_stop_name="Stop B", speed=12, next_stop_distance=700, game_clock=110)
            fake.current_state = departed
            engine.update(departed, stops, "MapA")
            labels = [x[1] for x in fake.items]
            self.assertIn("Следующая остановка: Stop B", labels)
            self.assertTrue(any(label.startswith("Анонс:") for label in labels))

    def test_pcm_wav_3d_preparation_survives_missing_pygame_at_full_speaker_effect(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "sound.wav"
            make_wav(source, seconds=0.25, sample_rate=8000)
            bridge = OpenOMSIBridge({"enabled": True, "plugin_dir": str(root / "plugin"), "speaker_effect": 100})
            with mock.patch.dict("sys.modules", {"pygame": None}):
                inside, duration = bridge._prepare_wav(source)
                outside, outside_duration = bridge._prepare_exterior_wav(source)
            self.assertTrue(inside.is_file())
            self.assertTrue(outside.is_file())
            self.assertAlmostEqual(duration, 0.25, places=2)
            self.assertAlmostEqual(outside_duration, 0.25, places=2)
            # Live conversion must not fall back to the old Python per-sample DSP loop;
            # the filters are executed by the bundled FFmpeg process instead.
            with mock.patch.object(bridge, "_small_speaker_pcm", side_effect=AssertionError("slow Python DSP called")):
                processed, _ = bridge._prepare_wav(source, ephemeral=True)
                self.assertTrue(processed.is_file())
                bridge.cleanup_prepared_music({"_cleanup_files": [str(processed)]})
            with wave.open(str(outside), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)

    def test_live_speaker_dsp_uses_native_ffmpeg_filters(self):
        from openomsi_bridge import OpenOMSIBridge
        filters = OpenOMSIBridge._speaker_ffmpeg_filters(1.0)
        self.assertIn("highpass=f=", filters)
        self.assertIn("acompressor=", filters)
        self.assertIn("asoftclip=", filters)
        self.assertIn("acrusher=bits=12", filters)
        self.assertEqual(filters.count("lowpass=f="), 1)
        self.assertIn("makeup=", filters)
        self.assertIn("lowpass=f=", filters)
        self.assertIn("highpass=f=85,lowpass=f=1450", OpenOMSIBridge._exterior_ffmpeg_filters())

    def test_head_turn_panning_compensation_is_cab_only(self):
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        position = lua.split("local function ceiling_position(options)", 1)[1].split("local function stop_active", 1)[0]
        self.assertIn("omsi.camera.in_cab()", position)
        self.assertIn("omsi.camera.orientation()", position)
        self.assertIn("camera.look_yaw", position)
        self.assertIn("camera_yaw - heading", position)
        self.assertIn("math.rad(head_delta * 0.60)", position)
        self.assertIn("adjusted_right = right * c - forward * sn", position)
        self.assertIn("Outside views keep world-fixed sources", position)

    def test_source_gains_allow_above_100_percent_and_keep_master_separate(self):
        self.assertEqual(DEFAULT_SETTINGS["version"], 12)
        self.assertEqual(DEFAULT_SETTINGS["informant_volume"], 1.0)
        self.assertEqual(DEFAULT_SETTINGS["music_volume"], 0.65)
        source = (Path(__file__).parent / "AutoInformant.py").read_text(encoding="utf-8")
        self.assertIn('slider("informant_volume", "Громкость информатора отдельно (до 200%)", 0, 200', source)
        self.assertIn('slider("music_volume", "Громкость музыки / радио отдельно (до 200%)", 0, 200', source)
        bridge_source = (Path(__file__).parent / "openomsi_bridge.py").read_text(encoding="utf-8")
        self.assertIn('volume={extra_gain:.3f},alimiter=limit=0.97', bridge_source)

    def test_speaker_dsp_changes_intensity(self):
        sr = 8000
        raw = bytearray()
        for i in range(sr):
            v = int(12000 * math.sin(2 * math.pi * 1300 * i / sr) + 2000 * math.sin(2 * math.pi * 3000 * i / sr))
            raw += struct.pack("<h", max(-32768, min(32767, v)))
        clean = OpenOMSIBridge._small_speaker_pcm(bytes(raw), sr, 1, 0.01)
        worn = OpenOMSIBridge._small_speaker_pcm(bytes(raw), sr, 1, 1.0)
        self.assertEqual(len(clean), len(raw))
        self.assertEqual(len(worn), len(raw))
        self.assertNotEqual(clean, worn)
        self.assertGreater(sum(abs(a-b) for a,b in zip(clean, worn)), 10000)

    def test_live_output_levels_use_settings_mailbox_not_playback_mailbox(self):
        with tempfile.TemporaryDirectory() as td:
            plugin_dir = Path(td) / "plugin"
            bridge = OpenOMSIBridge({"enabled": True, "plugin_dir": str(plugin_dir), "udp_port": 47821})
            bridge._started = True
            bridge.set_output_levels(0.37, 0.0)
            payload = json.loads(bridge.settings_file.read_text(encoding="utf-8"))
            self.assertAlmostEqual(payload["informant_output_level"], 0.37)
            self.assertAlmostEqual(payload["music_output_level"], 0.0)
            self.assertFalse(bridge.command_file.exists())
            bridge.set_output_levels(2.0, -1.0)
            payload = json.loads(bridge.settings_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["informant_output_level"], 1.0)
            self.assertEqual(payload["music_output_level"], 0.0)

    def test_lua_applies_quick_output_faders_from_independent_settings_file(self):
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        apply_settings = lua.split("local function apply_settings(value)", 1)[1].split("local function process_settings_file", 1)[0]
        self.assertIn("informant_output_level = clamp(tonumber(value.informant_output_level)", apply_settings)
        self.assertIn("music_output_level = clamp(tonumber(value.music_output_level)", apply_settings)
        self.assertIn("apply_native_radio_output_level()", apply_settings)
        self.assertIn("local function apply_native_radio_output_level()", lua)
        self.assertIn("version = 2", lua)

    def test_bridge_assembles_chunked_duty_stops(self):
        seen = []
        bridge = OpenOMSIBridge({"enabled": False, "plugin_dir": "", "udp_port": 47821}, on_state=seen.append)
        bridge._handle_game_state_event({
            "type": "game_state", "state_seq": 7, "stops_chunked": True,
            "duty_stop_count": 3, "duty_stop_chunks": 2, "line": "A1",
        })
        self.assertEqual(seen, [])
        bridge._handle_duty_stop_chunk({
            "type": "duty_stop_chunk", "state_seq": 7, "chunk_index": 2,
            "chunk_count": 2, "stops": [{"raw_index": 3, "name": "C", "id": "c"}],
        })
        self.assertEqual(seen, [])
        bridge._handle_duty_stop_chunk({
            "type": "duty_stop_chunk", "state_seq": 7, "chunk_index": 1,
            "chunk_count": 2, "stops": [
                {"raw_index": 1, "name": "A", "id": "a"},
                {"raw_index": 2, "name": "B", "id": "b"},
            ],
        })
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["duty_stops"], ["A", "B", "C"])
        self.assertEqual(len(seen[0]["duty_stop_objects"]), 3)
        self.assertFalse(seen[0]["stops_chunked"])

    def test_terminus_is_queued_on_final_stop_with_aux_file_in_route_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route_dir = root / "MapA" / "Line01"
            make_wav(route_dir / "Stop A.wav")
            terminus = route_dir / "TERMINUS.wav"
            make_wav(terminus)
            fake = FakeApp(root)
            fake.settings["play_terminus"] = True
            fake.current_state = {"line": "Line01", "tour": "1"}
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [{"id": "a", "name": "Stop A", "stops": True}]
            state = {"line": "Line01", "tour": "1", "trip": "1", "trip_name": "test",
                     "map_path": "x", "at_stop": True, "next_stop_id": "a", "next_stop_index": 1,
                     "next_stop_name": "Stop A", "speed": 0, "next_stop_distance": 0, "game_clock": 100}
            engine.reset_route("MapA|Line01", state)
            engine.update(state, stops, "MapA")
            self.assertEqual([label for _path, label in fake.items], ["Прибытие: Stop A", "Конечная"])
            self.assertEqual(fake.items[-1][0], terminus)

    def test_old_settings_migration_enables_terminus_and_separates_volume_values(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text('{"version": 1, "play_terminus": false, "volume": 0.77, "speaker_effect": 100}', encoding="utf-8")
            previous = app_module.SETTINGS_PATH
            app_module.SETTINGS_PATH = path
            try:
                loaded = load_settings()
            finally:
                app_module.SETTINGS_PATH = previous
            self.assertEqual(loaded["version"], 12)
            self.assertTrue(loaded["play_terminus"])
            self.assertAlmostEqual(loaded["volume"], 0.77)
            self.assertEqual(loaded["speaker_effect"], 100)
            self.assertNotEqual(loaded["volume"], loaded["speaker_effect"])

    def test_music_spatial_payload_uses_independent_speaker_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugin = root / "plugin"
            (plugin / "audio" / "cache").mkdir(parents=True)
            bridge = OpenOMSIBridge({
                "enabled": True,
                "plugin_dir": str(plugin),
                "source_mode": "ceiling",
                "range": 5.0,
                "speaker_forward": -1.20,
                "speaker_right": 0.00,
                "speaker_up": 2.35,
                "exterior_volume": 0.08,
                "music_source_mode": "ceiling",
                "music_range": 10.0,
                "music_speaker_forward": 1.10,
                "music_speaker_right": 0.25,
                "music_speaker_up": 1.55,
                "music_exterior_volume": 0.12,
            })
            main_wav = plugin / "audio" / "cache" / "main.wav"
            outside_wav = plugin / "audio" / "cache" / "outside.wav"
            announcement = bridge._spatial_payload(main_wav, outside_wav, 0.9)
            music = bridge._spatial_payload(main_wav, outside_wav, 0.55, music=True)
            self.assertEqual((announcement["forward"], announcement["right"], announcement["up"]), (-1.20, 0.0, 2.35))
            self.assertEqual(announcement["range"], 5.0)
            self.assertEqual(announcement["exterior_volume"], 0.08)
            self.assertEqual((music["forward"], music["right"], music["up"]), (1.10, 0.25, 1.55))
            self.assertEqual(music["range"], 10.0)
            self.assertEqual(music["exterior_volume"], 0.12)

    def test_spatial_payload_defaults_put_pa_on_ceiling_and_music_at_front(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = Path(td) / "plugin"
            (plugin / "audio" / "cache").mkdir(parents=True)
            bridge = OpenOMSIBridge({"enabled": True, "plugin_dir": str(plugin)})
            wav = plugin / "audio" / "cache" / "a.wav"
            pa = bridge._spatial_payload(wav, None, 0.9)
            music = bridge._spatial_payload(wav, None, 0.55, music=True)
            self.assertEqual((pa["forward"], pa["right"], pa["up"]), (-1.65, 0.35, 2.25))
            self.assertEqual((music["forward"], music["right"], music["up"]), (2.10, 0.70, 1.25))
            self.assertEqual(pa["range"], 7.0)
            self.assertEqual(music["range"], 9.0)
            self.assertEqual(pa["exterior_volume"], 0.25)
            self.assertEqual(music["exterior_volume"], 0.30)
            self.assertEqual(pa["exterior_range"], 45.0)
            self.assertEqual(music["exterior_range"], 45.0)

    def test_v5_settings_migrate_only_reversed_defaults_and_exterior_volume(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({
                "version": 5,
                "speaker_forward": 0.25, "speaker_right": 1.10, "speaker_up": 2.55,
                "music_speaker_forward": -1.20, "music_speaker_right": 0.0, "music_speaker_up": 2.35,
                "exterior_volume_pct": 0.8,
                "interior_range": 5.0,
                "music_range": 10.0,
                "music_volume": 0.61,
            }), encoding="utf-8")
            previous = app_module.SETTINGS_PATH
            app_module.SETTINGS_PATH = path
            try:
                loaded = load_settings()
            finally:
                app_module.SETTINGS_PATH = previous
            self.assertEqual(loaded["version"], 12)
            self.assertEqual((loaded["speaker_forward"], loaded["speaker_right"], loaded["speaker_up"]), (-1.65, 0.35, 2.25))
            self.assertEqual((loaded["music_speaker_forward"], loaded["music_speaker_right"], loaded["music_speaker_up"]), (2.10, 0.70, 1.25))
            self.assertEqual(loaded["exterior_volume_pct"], 25.0)
            self.assertEqual(loaded["music_exterior_volume_pct"], 30.0)
            self.assertEqual(loaded["interior_range"], 7.0)
            self.assertEqual(loaded["music_range"], 9.0)
            self.assertAlmostEqual(loaded["music_volume"] * loaded["volume"], 0.61, places=6)

    def test_stereo_pcm_is_downmixed_to_mono_for_positional_audio(self):
        raw = struct.pack("<hhhh", 10000, 20000, -10000, 20000)
        mono = OpenOMSIBridge._downmix_pcm16_mono(raw, 2)
        self.assertEqual(struct.unpack("<hh", mono), (15000, 5000))

    def test_temporary_stream_wav_edge_fades_suppress_boundary_clicks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chunk.wav"
            sample_rate = 44100
            frames = sample_rate // 5  # 200 ms
            raw = struct.pack("<" + "h" * frames, *([12000] * frames))
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(raw)

            OpenOMSIBridge._fade_wav_edges(path, fade_ms=7.0)
            with wave.open(str(path), "rb") as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, sample_rate))
                samples = struct.unpack("<" + "h" * wav.getnframes(), wav.readframes(wav.getnframes()))
            self.assertEqual(samples[0], 0)
            self.assertEqual(samples[-1], 0)
            self.assertEqual(samples[frames // 2], 12000)
            self.assertGreater(samples[10], 0)
            self.assertGreater(samples[-11], 0)

    def test_pcm_to_mono16_converts_24bit_stereo_pcm(self):
        # Two stereo frames with equal left/right channels: +0.5 and -0.5 in signed 24-bit.
        raw = bytes.fromhex("000040 000040 0000c0 0000c0")
        mono = OpenOMSIBridge._pcm_to_mono16(raw, 3, 2)
        self.assertEqual(struct.unpack("<hh", mono), (16384, -16384))

    def test_settings_recover_station_list_and_developer_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "radio.cfg"
            cfg.write_text("Record = https://radio.example/live.mp3\nvolume = 0.6\n", encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({
                "version": 8, "audio_root": "/mnt/data/aip13/maps", "plugin_dir": "",
                "radio_stations": [],
            }), encoding="utf-8")
            old_path = app_module.SETTINGS_PATH
            old_radio = app_module.openomsi_radio_cfg_path
            app_module.SETTINGS_PATH = settings
            app_module.openomsi_radio_cfg_path = lambda: cfg
            try:
                loaded = load_settings()
            finally:
                app_module.SETTINGS_PATH = old_path
                app_module.openomsi_radio_cfg_path = old_radio
            self.assertEqual(loaded["version"], 12)
            self.assertNotIn("/mnt/data/aip13/maps", loaded["audio_root"].replace("\\", "/"))
            self.assertEqual(loaded["radio_stations"], [{"name": "Record", "url": "https://radio.example/live.mp3"}])

    def test_openomsi_radio_cfg_preserves_existing_and_appends_without_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "radio.cfg"
            cfg.write_text("Existing = https://one.example/live.mp3\nvolume = 0.75\n", encoding="utf-8")
            changed, _ = app_module.sync_openomsi_radio_cfg([
                {"name": "Existing renamed in app", "url": "https://one.example/live.mp3"},
                {"name": "New Radio", "url": "https://two.example/stream"},
            ], cfg)
            self.assertTrue(changed)
            parsed = app_module.parse_openomsi_radio_cfg(cfg)
            self.assertEqual([x["url"] for x in parsed], ["https://one.example/live.mp3", "https://two.example/stream"])
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("volume = 0.75", text)
            self.assertEqual(text.count("https://two.example/stream"), 1)
            changed, _ = app_module.sync_openomsi_radio_cfg([
                {"name": "Updated Station", "url": "https://one.example/live.mp3"},
                {"name": "New Radio", "url": "https://two.example/stream"},
            ], cfg)
            self.assertTrue(changed)
            self.assertIn("Updated Station = https://one.example/live.mp3", cfg.read_text(encoding="utf-8"))

    def test_fast_stop_skip_announces_passed_stop_then_next_stop_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route = root / "MapA" / "Line01"
            make_wav(route / "Stop A.wav")
            make_wav(route / "Stop B.wav")
            make_wav(route / "NEXT.wav")
            fake = FakeApp(root)
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [
                {"id": "a", "name": "Stop A", "stops": True},
                {"id": "b", "name": "Stop B", "stops": True},
                {"id": "c", "name": "Stop C", "stops": True},
            ]
            start = {"line": "Line01", "tour": "1", "trip": "1", "trip_name": "test",
                     "map_path": "x", "at_stop": False, "next_stop_id": "a", "next_stop_index": 1,
                     "next_stop_name": "Stop A", "speed": 20, "next_stop_distance": 500, "game_clock": 100}
            fake.current_state = start
            engine.reset_route("MapA|Line01", start)
            engine.update(start, stops, "MapA")
            passed = dict(start, next_stop_id="b", next_stop_index=2, next_stop_name="Stop B", game_clock=115)
            fake.current_state = passed
            engine.update(passed, stops, "MapA")
            expected = ["Прибытие: Stop A", "Следующая остановка — сигнал", "Следующая остановка: Stop B"]
            self.assertEqual([label for _p, label in fake.items], expected)
            engine.update(passed, stops, "MapA")
            self.assertEqual([label for _p, label in fake.items], expected)

    def test_native_radio_rejection_has_ffmpeg_fallback_path(self):
        source = (Path(__file__).parent / "AutoInformant.py").read_text(encoding="utf-8")
        self.assertIn("def _start_radio_stream(self, station, *, force_ffmpeg: bool = False)", source)
        self.assertIn('state.get("source") == "native_radio"', source)
        self.assertIn("force_ffmpeg=True", source)
        self.assertIn("переключаюсь на резервный поток", source)

    def test_music_position_defaults_are_separate_from_informant(self):
        self.assertNotEqual(DEFAULT_SETTINGS["music_speaker_forward"], DEFAULT_SETTINGS["speaker_forward"])
        self.assertNotEqual(DEFAULT_SETTINGS["music_speaker_up"], DEFAULT_SETTINGS["speaker_up"])
        self.assertEqual(DEFAULT_SETTINGS["music_source_mode"], "ceiling")
        self.assertEqual(DEFAULT_SETTINGS["speaker_forward"], -1.65)
        self.assertEqual(DEFAULT_SETTINGS["music_speaker_forward"], 2.10)
        self.assertEqual(DEFAULT_SETTINGS["exterior_volume_pct"], 25.0)
        self.assertEqual(DEFAULT_SETTINGS["music_exterior_volume_pct"], 30.0)

    def test_lua_periodic_settings_refresh_cannot_consume_music_queue(self):
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        settings_handler = lua.split("local function process_settings_file()", 1)[1].split("local function process_command_file()", 1)[0]
        initializer = lua.split("local function initialize_file_ids()", 1)[1].split("initialize_file_ids()", 1)[0]
        self.assertNotIn("last_music_command_id = music.id", settings_handler)
        self.assertIn("last_music_command_id = music.id", initializer)

    def test_radio_stations_can_be_edited_and_dialogs_are_centered(self):
        source = (Path(__file__).parent / "AutoInformant.py").read_text(encoding="utf-8")
        self.assertIn("def station_dialog(index=None):", source)
        self.assertIn("def edit_station(_event=None):", source)
        self.assertIn('text="Изменить…", command=edit_station', source)
        self.assertIn("def _place_dialog(self, win, parent=None, *, modal=False):", source)
        self.assertIn('def _build_settings_editor(self, page):', source)
        self.assertIn('self._show_page("settings")', source)

    def test_lua_has_doppler_and_exterior_hard_cutoff(self):
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        self.assertIn("local function doppler_pitch(source, options, now, history_key)", lua)
        self.assertIn("omsi.camera.position()", lua)
        self.assertIn("outside_too_far", lua)
        self.assertIn("return 1.0", lua)
        self.assertIn("native mixer already applies Doppler", lua)
        self.assertIn("camera_yaw - heading", lua)
        self.assertIn("pitch = pitch", lua)
        self.assertIn("active_current_volume = 0.00000001", lua)
        self.assertIn("music_current_volume = 0.00000001", lua)

    def test_plugin_contains_live_duty_telemetry(self):
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        self.assertIn("local BRIDGE_PORT = 47821", lua)
        self.assertIn("duty_stop_objects = stop_objects", lua)
        self.assertIn("send_game_state(event, stop_objects, names)", lua)
        self.assertIn("duty_stop_chunk", lua)
        self.assertIn("next_stop_distance_nav", lua)
        self.assertIn("omsi.duty.stops", lua)
        self.assertIn("omsi.camera.in_cab", lua)
        self.assertIn("omsi.camera.get", lua)
        self.assertIn("omsi.camera.view", lua)

    def test_vehicle_audio_bank_overrides_map_bank_and_reset_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mapbank = root / "MapA"; vehiclebank = root / "custom_vehicle_voice"
            make_wav(mapbank / "Stop One.wav")
            make_wav(vehiclebank / "Stop One.wav")
            state = {"vehicle_manufacturer": "Example", "vehicle_model": "Model 12"}
            lib = AudioLibrary(root, map_folders={"MapA": str(mapbank)}, vehicle_folders={"Example | Model 12": str(vehiclebank)})
            self.assertEqual(lib.find_stop_audio("MapA", state, {"name": "Stop One"}, 0), vehiclebank / "Stop One.wav")
            lib.vehicle_folders.clear(); lib._index_cache.clear()
            self.assertEqual(lib.find_stop_audio("MapA", state, {"name": "Stop One"}, 0), mapbank / "Stop One.wav")

    def test_jingle_exact_match_is_separate_from_each_audio_clip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_wav(root / "MapA" / "Stop A.wav")
            make_wav(root / "MapA" / "NEXT.wav")
            make_wav(root / "MapA" / "Stop B.wav")
            map_jingle = root / "MapA" / "JINGLE.wav"
            make_wav(map_jingle)
            vehicle_root = root / "vehicle bank"
            make_wav(vehicle_root / "JINGLE.wav")
            lib = AudioLibrary(root, vehicle_folders={"Maker | Bus": str(vehicle_root)})
            # Map's JINGLE must win even when a per-vehicle bank is assigned.
            self.assertEqual(lib.jingle_file("MapA", {"vehicle_manufacturer": "Maker", "vehicle_model": "Bus"}), map_jingle)
            self.assertIsNone(lib.named_cue_file("MapA", {}, "JINGLE_EXTRA"))

    def test_jingle_queued_once_before_each_complete_arrival_or_departure_phrase(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_wav(root / "MapA" / "JINGLE.wav")
            make_wav(root / "MapA" / "Stop A.wav")
            make_wav(root / "MapA" / "NEXT.wav")
            make_wav(root / "MapA" / "Stop B.wav")
            fake = FakeApp(root)
            fake.settings["play_jingle"] = True
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [{"id": "a", "name": "Stop A", "stops": True}, {"id": "b", "name": "Stop B", "stops": True}]
            first = {"line": "Line01", "tour": "1", "trip": "1", "map_path": "x", "at_stop": True,
                     "next_stop_id": "a", "next_stop_index": 1, "next_stop_name": "Stop A", "speed": 0,
                     "next_stop_distance": 0, "game_clock": 100}
            fake.current_state = first; engine.reset_route("MapA|Line01", first); engine.update(first, stops, "MapA")
            departed = dict(first, at_stop=False, next_stop_id="b", next_stop_index=2, next_stop_name="Stop B",
                            speed=12, next_stop_distance=700, game_clock=110)
            fake.current_state = departed; engine.update(departed, stops, "MapA")
            names = [path.name for path, _label in fake.items]
            self.assertEqual(names, ["JINGLE.wav", "Stop A.wav", "JINGLE.wav", "NEXT.wav", "Stop B.wav"])

    def test_announcement_schedule_game_date_and_time(self):
        from informant_core import announcement_schedule_allows
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "NewYear.wav"; make_wav(path)
            schedule = {str(path.resolve()): {"enabled": True, "annual": True,
                        "start_date": "12-20", "end_date": "01-10", "start_time": "22:00", "end_time": "05:00"}}
            self.assertTrue(announcement_schedule_allows(path, {"game_date": {"year": 2026, "month": 12, "day": 31}, "game_clock": 23 * 3600}, schedule))
            self.assertTrue(announcement_schedule_allows(path, {"game_date": {"year": 2027, "month": 1, "day": 5}, "game_clock": 2 * 3600}, schedule))
            self.assertFalse(announcement_schedule_allows(path, {"game_date": {"year": 2026, "month": 5, "day": 1}, "game_clock": 12 * 3600}, schedule))

    def test_next_stop_announcement_is_deduped_by_target_not_only_origin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("Stop A", "Stop B", "Stop C"):
                make_wav(root / "MapA" / f"{name}.wav")
            fake = FakeApp(root)
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [
                {"id": "a", "name": "Stop A", "stops": True},
                {"id": "b", "name": "Stop B", "stops": True},
                {"id": "c", "name": "Stop C", "stops": True},
            ]
            fake.current_state = {"line": "1", "tour": "1", "trip": "1", "game_clock": 100}
            engine._departure(fake.current_state, stops, "MapA", 0, 1)
            # A stale stop index could invent a second origin for the same target.
            engine._departure(fake.current_state, stops, "MapA", 2, 1)
            labels = [label for _path, label in fake.items if label.startswith("Следующая остановка:")]
            self.assertEqual(labels, ["Следующая остановка: Stop B"])

    def test_braking_after_departure_does_not_replay_next(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("Stop A", "Stop B", "Stop C"):
                make_wav(root / "MapA" / f"{name}.wav")
            make_wav(root / "MapA" / "NEXT.wav")
            fake = FakeApp(root)
            engine = AutoAnnouncementEngine(fake)  # type: ignore[arg-type]
            stops = [
                {"id": "a", "name": "Stop A", "stops": True},
                {"id": "b", "name": "Stop B", "stops": True},
                {"id": "c", "name": "Stop C", "stops": True},
            ]
            at_a = {"line": "1", "tour": "1", "trip": "1", "map_path": "x", "at_stop": True,
                    "next_stop_id": "a", "next_stop_index": 1, "next_stop_name": "Stop A",
                    "speed": 0, "next_stop_distance": 0, "game_clock": 100}
            fake.current_state = at_a
            engine.reset_route("MapA|1", at_a)
            engine.update(at_a, stops, "MapA")
            departed = dict(at_a, at_stop=False, speed=12, next_stop_distance=100, game_clock=110)
            fake.current_state = departed
            engine.update(departed, stops, "MapA")
            # Bad/stale at_stop=true while the bus has stopped in traffic, still on A.
            queue_brake = dict(at_a, at_stop=True, speed=0, next_stop_distance=0, game_clock=111)
            for _ in range(5):
                fake.current_state = queue_brake
                engine.update(queue_brake, stops, "MapA")
            next_labels = [label for _path, label in fake.items if label.startswith("Следующая остановка:")]
            self.assertEqual(next_labels, ["Следующая остановка: Stop B"])

    def test_native_radio_switch_avoids_stop_command_race_and_updates_ui_event(self):
        app = (Path(__file__).parent / "AutoInformant.py").read_text(encoding="utf-8")
        bridge = (Path(__file__).parent / "openomsi_bridge.py").read_text(encoding="utf-8")
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        self.assertIn('"native_radio_station_changed"}', app)
        self.assertIn('self.stop_music(send_to_game=False)', app)
        self.assertIn('self._active_native_radio_id = self._bridge.start_native_radio(', app)
        self.assertIn('def start_native_radio(self, station_index: int, volume: float = 0.55, station_name: str = "Интернет-радио") -> str:', bridge)
        # Shift+R belongs exclusively to openOMSI; the companion bridge forwards only Q.
        self.assertIn('str(event.get("key", "")).upper() == "Q"', bridge)
        self.assertNotIn('"SHIFT_R"', bridge)
        self.assertIn('type = "native_radio_station_changed"', lua)
        self.assertIn('local function stop_music(reason, report, preserve_native_radio_state)', lua)
        self.assertIn('stop_music("replaced", false, true)', lua)

    def test_native_radio_ignores_transient_zero_during_shift_r(self):
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        self.assertIn("local native_radio_zero_since = nil", lua)
        self.assertIn("omsi.time() - native_radio_zero_since < 0.8", lua)
        self.assertIn("radio switched off in game", lua)

    def test_max_speaker_effect_preserves_midrange_intelligibility(self):
        sample_rate = 12000
        raw = bytearray()
        for i in range(sample_rate):
            sample = int(12000 * math.sin(2 * math.pi * 3200 * i / sample_rate))
            raw += struct.pack("<h", sample)
        clean = OpenOMSIBridge._small_speaker_pcm(bytes(raw), sample_rate, 1, 0.0)
        worn = OpenOMSIBridge._small_speaker_pcm(bytes(raw), sample_rate, 1, 1.0)
        def rms(buf):
            vals = struct.unpack("<" + "h" * (len(buf) // 2), buf)
            return math.sqrt(sum(v * v for v in vals) / max(1, len(vals)))
        self.assertGreater(rms(worn), rms(clean) * 0.50)
        self.assertNotEqual(worn, clean)
        source = (Path(__file__).parent / "openomsi_bridge.py").read_text(encoding="utf-8")
        self.assertIn("post_lp_state", source)
        self.assertIn("quant_bits = max(9, min(15, int(round(15 - 6 * intensity))))", source)
        filters = OpenOMSIBridge._speaker_ffmpeg_filters(1.0)
        lowpass = float(filters.split("lowpass=f=", 1)[1].split(",", 1)[0])
        self.assertGreaterEqual(lowpass, 5400.0)

    def test_music_and_doors_interfaces_are_present_in_plugin(self):
        lua = (Path(__file__).parent / "plugin" / "AutoInformantPro" / "main.lua").read_text(encoding="utf-8")
        self.assertIn('action == "play_music"', lua)
        self.assertIn('then_close_doors', lua)
        self.assertIn('door_close_request', lua)
        app = (Path(__file__).parent / "AutoInformant.py").read_text(encoding="utf-8")
        self.assertIn('def open_music_player', app)
        self.assertIn('def open_announcement_schedule_editor', app)
        self.assertNotIn('text="Собрать EXE"', app)



class TestSettingsMigrationBackup(unittest.TestCase):
    def test_first_v12_save_keeps_one_time_pre_v19_backup(self):
        with tempfile.TemporaryDirectory() as td:
            settings_path = Path(td) / "autoinformant_pro_settings.json"
            original = {"version": 11, "audio_root": "C:/old/maps", "music_playlist": ["C:/music/a.mp3"]}
            settings_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            current = dict(original)
            current["version"] = 12
            current.pop("mute_captured_app", None)
            with unittest.mock.patch.object(app_module, "SETTINGS_PATH", settings_path):
                app_module.save_settings(current)
                app_module.save_settings(current)
            backup_path = settings_path.with_name("autoinformant_pro_settings.pre-v1.9.json")
            self.assertTrue(backup_path.is_file())
            self.assertEqual(json.loads(backup_path.read_text(encoding="utf-8")), original)
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["version"], 12)


class TestPortableAudioPacks(unittest.TestCase):
    @staticmethod
    def write_pack(path: Path, pack_id: str, name: str, *, map_assets: dict[str, bytes] | None = None,
                   category_assets: dict[str, bytes] | None = None, assignments=None):
        import zipfile
        manifest = {
            "format": "autoinformant-pack", "schema_version": 1,
            "id": pack_id, "name": name, "author": "Test", "description": "test pack",
            "icon": "pack_icon.png",
            "map_profiles": [{"id": "map", "map_aliases": ["MapA"], "asset_root": "assets/map",
                              "settings": {"play_jingle": True}, "stop_overrides": {},
                              "ignored_stops": [], "announcement_schedules": {}}],
            "category_profiles": [],
        }
        entries = {"manifest.json": json.dumps(manifest, ensure_ascii=False).encode("utf-8")}
        if map_assets:
            entries.update({f"assets/map/{name}": payload for name, payload in map_assets.items()})
        if category_assets is not None:
            manifest["category_profiles"] = [{
                "id": "trolley", "name": "Троллейбусы", "map_aliases": ["MapA"],
                "asset_root": "assets/map/Trolley", "replace_map_bank": True,
                "vehicle_assignments": list(assignments or []), "settings": {"play_jingle": False, "doors_enabled": False},
                "stop_overrides": {}, "ignored_stops": [], "announcement_schedules": {},
            }]
            entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            entries.update({f"assets/map/Trolley/{name}": payload for name, payload in category_assets.items()})
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel, payload in entries.items():
                zf.writestr(rel, payload)
        return path

    def test_two_map_packs_coexist_and_active_one_is_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_dir = root / "app"
            settings = {}
            pm = PackManager(app_dir, settings)
            one = self.write_pack(root / "one.aipack", "map-one", "Map One",
                                  map_assets={"Stop.wav": b"one"})
            two = self.write_pack(root / "two.aipack", "map-two", "Map Two",
                                  map_assets={"Stop.wav": b"two"})
            pm.import_pack(one)
            pm.import_pack(two)
            self.assertEqual(len(pm.list_packs()), 2)
            self.assertEqual(pm.map_profile("MapA")[0], "map-one")
            pm.set_active_pack_for_map("MapA", "map-two")
            self.assertEqual(pm.map_profile("MapA")[0], "map-two")

    def test_assigned_vehicle_category_fully_replaces_base_audio_bank(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_dir = root / "app"
            package = self.write_pack(
                root / "test.aipack", "map-with-category", "Map with trolley category",
                map_assets={"Stop One.wav": b"bus", "JINGLE.wav": b"bus-jingle",
                            "NEXT.wav": b"bus-next", "TERMINUS.wav": b"bus-terminus",
                            "Анонсы/Bus.wav": b"bus-anon"},
                category_assets={"Stop One.wav": b"trolley"},
            )
            settings = {}
            pm = PackManager(app_dir, settings)
            pm.import_pack(package)
            vehicle = {"vehicle_manufacturer": "ZiU", "vehicle_model": "682G g00"}
            pm.set_active_category_for_vehicle("map-with-category", "trolley", "MapA", "ZiU", "682G g00")
            library = AudioLibrary(root / "empty-maps", {}, vehicle_folders={
                "ZiU | 682G g00": str(root / "legacy-vehicle-bank")
            })
            library.set_pack_manager(pm)
            stop = library.find_stop_audio("MapA", vehicle, {"name": "Stop One"}, 0)
            self.assertEqual(stop.read_bytes(), b"trolley")
            # A missing category cue/announcement must not leak from the map or legacy bank.
            self.assertIsNone(library.jingle_file("MapA", vehicle))
            self.assertIsNone(library.aux_file("MapA", vehicle, AUX_ALIASES["next"]))
            self.assertEqual(library.announcement_files("MapA", vehicle), [])
            # An unassigned model continues to use the map package.
            bus_state = {"vehicle_manufacturer": "Other", "vehicle_model": "Bus"}
            bus_stop = library.find_stop_audio("MapA", bus_state, {"name": "Stop One"}, 0)
            self.assertEqual(bus_stop.read_bytes(), b"bus")
            self.assertEqual(library.jingle_file("MapA", bus_state).read_bytes(), b"bus-jingle")

    def test_ignored_stop_is_saved_inside_package_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "ignore.aipack", "ignore-pack", "Ignore test",
                                      map_assets={"Stop.wav": b"audio"})
            settings = {}
            pm = PackManager(root / "app", settings)
            pm.import_pack(archive)
            stop = {"id": "terminal-parking", "name": "Автовокзал-стоянка", "stops": True}
            self.assertTrue(pm.set_stop_ignored("MapA", stop, 3, {}, True))
            self.assertTrue(pm.is_stop_ignored("MapA", stop, 3, {}))
            pm2 = PackManager(root / "app", settings)
            self.assertTrue(pm2.is_stop_ignored("MapA", stop, 3, {}))
            self.assertFalse(pm2.is_stop_ignored("MapA", {"id": "different", "name": "Автовокзал"}, 2, {}))

    def test_exclusive_category_has_its_own_stop_ignore_layer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_dir = root / "app"
            archive = self.write_pack(root / "category-ignore.aipack", "category-ignore", "Category ignore",
                                      map_assets={"Stop.wav": b"base"},
                                      category_assets={"Stop.wav": b"category"}, assignments=["ZiU | 682G"])
            settings = {}
            pm = PackManager(app_dir, settings)
            pm.import_pack(archive)
            stop = {"id": "terminal", "name": "Автовокзал-стоянка", "stops": True}
            self.assertTrue(pm.set_stop_ignored("MapA", stop, 3, {}, True))
            state = {"vehicle_manufacturer": "ZiU", "vehicle_model": "682G"}
            # The category is exclusive, so the base profile's ignore does not bleed into it.
            self.assertFalse(pm.is_stop_ignored("MapA", stop, 3, state))
            self.assertTrue(pm.set_stop_ignored("MapA", stop, 3, state, True))
            self.assertTrue(pm.is_stop_ignored("MapA", stop, 3, state))
            self.assertTrue(pm.set_stop_ignored("MapA", stop, 3, state, False))
            self.assertFalse(pm.is_stop_ignored("MapA", stop, 3, state))
            # A vehicle with no matching category returns to the untouched base profile.
            bus_state = {"vehicle_manufacturer": "Other", "vehicle_model": "Bus"}
            self.assertTrue(pm.is_stop_ignored("MapA", stop, 3, bus_state))

    def test_category_selection_can_choose_between_two_installed_category_packs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_dir = root / "app"
            p1 = self.write_pack(root / "c1.aipack", "cat-one", "Category one",
                                 map_assets={"Stop.wav": b"base1"},
                                 category_assets={"Stop.wav": b"one"}, assignments=["ZiU | 682G"])
            p2 = self.write_pack(root / "c2.aipack", "cat-two", "Category two",
                                 map_assets={"Stop.wav": b"base2"},
                                 category_assets={"Stop.wav": b"two"}, assignments=["ZiU | 682G"])
            settings = {}
            pm = PackManager(app_dir, settings)
            pm.import_pack(p1); pm.import_pack(p2)
            state = {"vehicle_manufacturer": "ZiU", "vehicle_model": "682G"}
            self.assertEqual(pm.category_profile("MapA", state)[0], "cat-one")
            pm.set_active_category_for_vehicle("cat-two", "trolley", "MapA", "ZiU", "682G")
            self.assertEqual(pm.category_profile("MapA", state)[0], "cat-two")

    def test_external_stop_audio_is_imported_into_aipack_manifest_and_survives_reload(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "custom.aipack", "custom-pack", "Custom pack",
                                      map_assets={"Default Stop.wav": b"default"})
            settings = {}
            pm = PackManager(root / "app", settings)
            pm.import_pack(archive)

            external = root / "outside" / "Моя остановка.wav"
            external.parent.mkdir(parents=True)
            external.write_bytes(b"external-audio-bytes")
            stop = {"id": "stop-42", "name": "Площадь Ленина"}
            stored = pm.set_stop_override("MapA", stop, 4, {}, external)

            info = pm.active_profile_info("MapA", {})
            self.assertIsNotNone(info)
            self.assertTrue(stored.is_file())
            self.assertEqual(stored.read_bytes(), b"external-audio-bytes")
            self.assertEqual(stored.parent.name, "_AutoInformantCustom")
            self.assertTrue(stored.is_relative_to(info["root"]))

            manifest = pm.pack_for_profile("custom-pack")
            relative = manifest["map_profiles"][0]["stop_overrides"]["id:stop-42"]
            self.assertTrue(relative.startswith("_AutoInformantCustom/"))
            self.assertNotIn(str(external), json.dumps(manifest, ensure_ascii=False))
            self.assertEqual(pm.stop_override_path("MapA", stop, 4, {}), stored)

            # Reopening the installed package must still resolve the stop override.
            pm_reloaded = PackManager(root / "app", settings)
            reloaded = pm_reloaded.stop_override_path("MapA", stop, 4, {})
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.read_bytes(), b"external-audio-bytes")

            exported = pm_reloaded.export_pack("custom-pack", root / "exports" / "custom.aipack")
            with zipfile.ZipFile(exported) as zf:
                asset_root = "assets/map"
                self.assertIn(f"{asset_root}/{relative}", zf.namelist())
                exported_manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertEqual(exported_manifest["map_profiles"][0]["stop_overrides"]["id:stop-42"], relative)

    def test_stop_audio_selected_from_active_pack_is_referenced_without_duplicate_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "inside.aipack", "inside-pack", "Inside pack",
                                      map_assets={"Default Stop.wav": b"default"})
            pm = PackManager(root / "app", {})
            pm.import_pack(archive)
            profile_root = pm.active_profile_info("MapA", {})["root"]
            selected = profile_root / "Voice Banks" / "Центральная.wav"
            selected.parent.mkdir(parents=True)
            selected.write_bytes(b"already-in-pack")

            listed = pm.list_active_profile_audio_files("MapA", {})
            self.assertIn(selected, listed)
            stop = {"name": "Центральная"}
            stored = pm.set_stop_override("MapA", stop, 0, {}, selected)
            self.assertEqual(stored, selected)
            self.assertEqual(stored.read_bytes(), b"already-in-pack")
            manifest = pm.pack_for_profile("inside-pack")
            relative = manifest["map_profiles"][0]["stop_overrides"]["name:центральная"]
            self.assertEqual(relative, "Voice Banks/Центральная.wav")
            self.assertFalse((profile_root / "_AutoInformantCustom").exists())

    def test_external_override_uses_effective_vehicle_category_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(
                root / "vehicle.aipack", "vehicle-pack", "Vehicle pack",
                map_assets={"Stop.wav": b"map-bank"},
                category_assets={"Stop.wav": b"category-bank"},
                assignments=["ZiU | 682G"],
            )
            pm = PackManager(root / "app", {})
            pm.import_pack(archive)
            state = {"vehicle_manufacturer": "ZiU", "vehicle_model": "682G"}
            info = pm.active_profile_info("MapA", state)
            self.assertEqual(info["profile_id"], "trolley")

            # Selecting a resource that belongs to the base map profile must copy
            # it into the active category instead of creating a cross-profile link.
            source = pm.map_audio_dir("MapA") / "Stop.wav"
            stop = {"id": "shared-stop", "name": "Остановка"}
            stored = pm.set_stop_override("MapA", stop, 0, state, source)
            self.assertTrue(stored.is_relative_to(info["root"]))
            self.assertEqual(stored.parent.name, "_AutoInformantCustom")
            self.assertNotEqual(stored, source)
            self.assertEqual(stored.read_bytes(), b"map-bank")
            self.assertEqual(pm.stop_override_path("MapA", stop, 0, state).read_bytes(), b"map-bank")
            self.assertIsNone(pm.stop_override_path("MapA", stop, 0, {}))

    def test_create_rename_delete_category_and_import_unicode_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            package = self.write_pack(root / "editable.aipack", "editable-pack", "Editable pack",
                                      map_assets={"Base Stop.wav": b"base-bank"})
            settings = {}
            pm = PackManager(root / "app", settings)
            pm.import_pack(package)

            category_id = pm.create_category("editable-pack", name="Трамваи", map_profile_id="map")
            manifest = pm.pack_for_profile("editable-pack")
            category = next(item for item in manifest["category_profiles"] if item["id"] == category_id)
            self.assertEqual(category["name"], "Трамваи")
            self.assertTrue(category["replace_map_bank"])
            self.assertTrue((pm.pack_directory("editable-pack") / category["asset_root"]).is_dir())

            pm.add_vehicle_assignment("editable-pack", category_id, "ТМ | 1")
            with self.assertRaises(ValueError):
                pm.add_vehicle_assignment("editable-pack", category_id, "тм | 1")
            pm.update_category("editable-pack", category_id, name="Городские трамваи", map_profile_id="map")
            self.assertEqual(category["name"], "Городские трамваи")

            source = root / "my-tram-audio"
            original = source / "All" / "Ёлочная площадь.mp3"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"cyrillic-audio-name")
            imported = pm.import_category_audio("editable-pack", category_id, source)
            self.assertEqual((imported / "All" / "Ёлочная площадь.mp3").read_bytes(), b"cyrillic-audio-name")
            self.assertEqual(original.read_bytes(), b"cyrillic-audio-name")

            pm.set_active_category_for_vehicle("editable-pack", category_id, "MapA", "ТМ", "1")
            state = {"vehicle_manufacturer": "ТМ", "vehicle_model": "1"}
            self.assertEqual(pm.category_profile("MapA", state)[1]["id"], category_id)
            base_dir = pm.map_audio_dir("MapA")
            self.assertTrue((base_dir / "Base Stop.wav").is_file())

            pm.delete_category("editable-pack", category_id)
            self.assertEqual(pm.pack_for_profile("editable-pack")["category_profiles"], [])
            self.assertFalse(imported.exists())
            self.assertTrue((base_dir / "Base Stop.wav").is_file())
            self.assertFalse(settings.get("active_category_profiles"))

    def test_create_audio_pack_from_legacy_folder_preserves_routes_and_separates_category(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "Tver_2026"
            fixtures = {
                "NEXT.mp3": b"next",
                "All/Остановка Ёлочная.mp3": b"base-stop",
                "001-01/route-stop.mp3": b"route-stop",
                "Анонсы/Детский сад.mp3": b"announcement",
                "Троллейбусы/All/Остановка Троллейбуса.mp3": b"trolley-stop",
            }
            for relative, payload in fixtures.items():
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            (source / ".stoplistmaker.json").write_text('{"keep": true}', encoding="utf-8")

            app_dir = root / "app"
            app_dir.mkdir()
            shutil.copy2(Path(__file__).with_name("pack_icon_default.png"), app_dir / "pack_icon_default.png")
            pm = PackManager(app_dir, {})
            output = pm.create_pack_from_audio_folder(
                source, root / "exports" / "tver.aipack", name="Информатор Твери",
                author="Test", description="folder conversion", map_alias="Tver_2026",
                category_folders=["Троллейбусы"],
                category_assignments={"Троллейбусы": ["ZiU | 682G g00"]},
            )
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                names = set(archive.namelist())
                base_root = "assets/maps/Tver_2026/"
                category_root = "assets/categories/Tver_2026/Троллейбусы/"
                self.assertIn(base_root + "NEXT.mp3", names)
                self.assertIn(base_root + "All/Остановка Ёлочная.mp3", names)
                self.assertIn(base_root + "001-01/route-stop.mp3", names)
                self.assertIn(base_root + "Анонсы/Детский сад.mp3", names)
                self.assertIn(base_root + ".stoplistmaker.json", names)
                self.assertIn(category_root + "All/Остановка Троллейбуса.mp3", names)
                self.assertNotIn(base_root + "Троллейбусы/All/Остановка Троллейбуса.mp3", names)
                self.assertEqual(manifest["map_profiles"][0]["map_aliases"], ["Tver_2026"])
                self.assertEqual(manifest["category_profiles"][0]["vehicle_assignments"], ["ZiU | 682G g00"])
                self.assertEqual(archive.read(category_root + "All/Остановка Троллейбуса.mp3"), b"trolley-stop")
            # Source remains untouched, including original Cyrillic names.
            self.assertEqual((source / "All/Остановка Ёлочная.mp3").read_bytes(), b"base-stop")
            self.assertTrue((source / "Троллейбусы/All/Остановка Троллейбуса.mp3").is_file())

    def test_folder_category_suggestions_do_not_classify_route_and_announcement_folders(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Chistogorskaya oblast"
            for name in ("146 (#U043b#U0435#U0442#U043d#U0438#U0439)", "146 (летний)",
                         "001-01", "10", "All", "Анонсы", "Троллейбусы"):
                (root / name).mkdir(parents=True)
            self.assertEqual(PackManager.suggest_category_folders(root), ["Троллейбусы"])

    def test_full_folder_export_and_repack_preserve_package_files(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "original.aipack", "roundtrip", "Round trip",
                                      map_assets={"All/Ёлочная.mp3": b"base", "001-01/NEXT.mp3": b"next"},
                                      category_assets={"All/Троллейбус.mp3": b"trolley"},
                                      assignments=["ZiU | 682G"])
            pm = PackManager(root / "app", {})
            pm.import_pack(archive)
            package_folder = pm.export_pack_to_folder("roundtrip", root / "package-folder", mode="package")
            self.assertTrue((package_folder / "manifest.json").is_file())
            self.assertTrue((package_folder / "assets/map/All/Ёлочная.mp3").is_file())
            repacked = pm.create_pack_from_package_folder(package_folder, root / "repacked.aipack")
            with zipfile.ZipFile(archive) as first, zipfile.ZipFile(repacked) as second:
                self.assertIsNone(second.testzip())
                first_files = {name for name in first.namelist() if not name.endswith("/")}
                second_files = {name for name in second.namelist() if not name.endswith("/")}
                self.assertEqual(first_files, second_files)
                for name in first_files:
                    if name == "manifest.json":
                        self.assertEqual(json.loads(first.read(name)), json.loads(second.read(name)))
                    else:
                        self.assertEqual(first.read(name), second.read(name), name)
                self.assertIn("assets/map/", second.namelist())

    def test_repack_can_be_saved_inside_selected_full_package_folder_without_self_embedding(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "source.aipack", "inside-folder", "Inside folder",
                                      map_assets={"All/Stop.mp3": b"stop"})
            pm = PackManager(root / "app", {})
            pm.import_pack(archive)
            package_folder = pm.export_pack_to_folder("inside-folder", root / "unpacked", mode="package")
            output = package_folder / "copy.aipack"
            repacked = pm.create_pack_from_package_folder(package_folder, output)
            with zipfile.ZipFile(repacked) as zf:
                self.assertIsNone(zf.testzip())
                self.assertNotIn("copy.aipack", zf.namelist())
                self.assertIn("assets/map/All/Stop.mp3", zf.namelist())
            self.assertTrue(output.is_file())

    def test_plain_audio_folder_export_restores_base_and_category_siblings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "nested-category.aipack", "nested-category", "Nested category",
                                      map_assets={"All/Base.mp3": b"base", "001-01/Route.mp3": b"route",
                                                  "Trolley/All/Category.mp3": b"trolley"},
                                      category_assets={"All/Category.mp3": b"trolley"},
                                      assignments=["ZiU | 682G"])
            pm = PackManager(root / "app", {})
            pm.import_pack(archive)
            manifest = pm.pack_for_profile("nested-category")
            # Simulate the older valid schema-v1 layout where category assets were nested under map root.
            category = manifest["category_profiles"][0]
            category["asset_root"] = "assets/map/Trolley"
            (pm.pack_directory("nested-category") / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            output = pm.export_pack_to_folder("nested-category", root / "legacy-audio", mode="audio")
            self.assertTrue((output / "All/Base.mp3").is_file())
            self.assertTrue((output / "001-01/Route.mp3").is_file())
            self.assertTrue((output / "Троллейбусы/All/Category.mp3").is_file())
            self.assertFalse((output / "Trolley").exists())
            self.assertFalse((output / "manifest.json").exists())

    def test_created_folder_pack_imports_and_audio_library_resolves_base_and_category(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "MapPack"
            (source / "All").mkdir(parents=True)
            (source / "Троллейбусы" / "All").mkdir(parents=True)
            (source / "All" / "Остановка.mp3").write_bytes(b"base")
            (source / "Троллейбусы" / "All" / "Остановка.mp3").write_bytes(b"trolley")
            pm = PackManager(root / "app", {})
            archive = pm.create_pack_from_audio_folder(
                source, root / "MapPack.aipack", name="MapPack", map_alias="MapPack",
                category_folders=["Троллейбусы"],
                category_assignments={"Троллейбусы": ["ZiU | 682G"]},
            )
            pack_id = pm.import_pack(archive)
            library = AudioLibrary(root / "legacy-maps", {})
            library.set_pack_manager(pm)
            bus_state = {"vehicle_manufacturer": "MAN", "vehicle_model": "Lion's City"}
            trolley_state = {"vehicle_manufacturer": "ZiU", "vehicle_model": "682G"}
            self.assertEqual(library.find_stop_audio("MapPack", bus_state, {"name": "Остановка"}, 0).read_bytes(), b"base")
            self.assertEqual(library.find_stop_audio("MapPack", trolley_state, {"name": "Остановка"}, 0).read_bytes(), b"trolley")
            self.assertEqual(pm.pack_for_profile(pack_id)["category_profiles"][0]["name"], "Троллейбусы")

    def test_current_separate_category_layout_exports_as_sibling_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "separate.aipack", "separate-category", "Separate category",
                                      map_assets={"All/Base.mp3": b"base", "Анонсы/One.mp3": b"anon"},
                                      category_assets={"All/Trolley.mp3": b"trolley"},
                                      assignments=["ZiU | 682G"])
            pm = PackManager(root / "app", {})
            pm.import_pack(archive)
            pack_root = pm.pack_directory("separate-category")
            manifest = pm.pack_for_profile("separate-category")
            cat = manifest["category_profiles"][0]
            old_cat_root = pack_root / cat["asset_root"]
            new_cat_root = pack_root / "assets/categories/MapA/Троллейбусы"
            new_cat_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_cat_root), str(new_cat_root))
            cat["asset_root"] = "assets/categories/MapA/Троллейбусы"
            (pack_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            output = pm.export_pack_to_folder("separate-category", root / "classic", mode="audio")
            self.assertEqual((output / "All/Base.mp3").read_bytes(), b"base")
            self.assertEqual((output / "Анонсы/One.mp3").read_bytes(), b"anon")
            self.assertEqual((output / "Троллейбусы/All/Trolley.mp3").read_bytes(), b"trolley")

    def test_pack_archive_preserves_empty_route_and_category_directories(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "EmptyFoldersMap"
            (source / "001-01").mkdir(parents=True)
            (source / "All").mkdir(parents=True)
            (source / "Троллейбусы").mkdir(parents=True)
            (source / "NEXT.mp3").write_bytes(b"next")
            pm = PackManager(root / "app", {})
            archive = pm.create_pack_from_audio_folder(
                source, root / "empty-folders.aipack", name="Empty folders", map_alias="EmptyFoldersMap",
                category_folders=["Троллейбусы"],
            )
            with zipfile.ZipFile(archive) as zf:
                names = set(zf.namelist())
                self.assertIn("assets/maps/EmptyFoldersMap/001-01/", names)
                self.assertIn("assets/maps/EmptyFoldersMap/All/", names)
                self.assertIn("assets/categories/EmptyFoldersMap/Троллейбусы/", names)
            pm.import_pack(archive)
            base = pm.map_audio_dir("EmptyFoldersMap")
            category = pm.pack_directory(pm.list_packs()[0]["id"]) / "assets/categories/EmptyFoldersMap/Троллейбусы"
            self.assertTrue((base / "001-01").is_dir())
            self.assertTrue((base / "All").is_dir())
            self.assertTrue(category.is_dir())

    def test_folder_pack_creation_rejects_symlinks_and_does_not_overwrite_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "Map"
            source.mkdir()
            (source / "stop.mp3").write_bytes(b"stop")
            outside = root / "outside.mp3"
            outside.write_bytes(b"outside")
            try:
                (source / "link.mp3").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            pm = PackManager(root / "app", {})
            with self.assertRaises(ValueError):
                pm.create_pack_from_audio_folder(source, root / "pack.aipack", name="Pack")
            (source / "link.mp3").unlink()
            (root / "pack.aipack").write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                pm.create_pack_from_audio_folder(source, root / "pack.aipack", name="Pack")
            self.assertEqual((root / "pack.aipack").read_bytes(), b"keep")

    def test_category_audio_import_rejects_pack_parent_and_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.write_pack(root / "safe.aipack", "safe-pack", "Safe pack", map_assets={"base.wav": b"base"})
            pm = PackManager(root / "app", {})
            pm.import_pack(archive)
            category_id = pm.create_category("safe-pack", name="Электробусы")
            pack_dir = pm.pack_directory("safe-pack")
            with self.assertRaises(ValueError):
                pm.import_category_audio("safe-pack", category_id, pack_dir)

            source = root / "symlink-source"
            source.mkdir()
            outside = root / "outside.bin"
            outside.write_bytes(b"do not import")
            try:
                (source / "link.bin").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")
            with self.assertRaises(ValueError):
                pm.import_category_audio("safe-pack", category_id, source)

    def test_archive_path_traversal_is_rejected_before_extraction(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {"format": "autoinformant-pack", "schema_version": 1, "id": "unsafe",
                        "name": "Unsafe", "map_profiles": [{"id": "map", "map_aliases": ["MapA"],
                        "asset_root": "assets/map"}], "category_profiles": []}
            archive = root / "unsafe.aipack"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("manifest.json", json.dumps(manifest))
                zf.writestr("../outside.txt", b"nope")
            pm = PackManager(root / "app", {})
            with self.assertRaises(ValueError):
                pm.import_pack(archive)
            self.assertFalse((root / "outside.txt").exists())

    def test_legacy_route_txt_is_not_a_source_for_live_route_or_stops(self):
        state = {"duty_stop_objects": [{"id": "game-1", "name": "Остановка из openOMSI", "stops": True}],
                 "duty_stops": ["wrong fallback"]}
        stops = normalize_stop_objects(state)
        self.assertEqual([stop["name"] for stop in stops], ["Остановка из openOMSI"])
        source = (Path(__file__).parent / "informant_core.py").read_text(encoding="utf-8")
        self.assertNotIn("read_text", source[source.find("def normalize_stop_objects"):source.find("def find_current_stop_index")])
        self.assertNotIn("route.txt", source[source.find("def normalize_stop_objects"):source.find("def find_current_stop_index")])


class TestTerminalRollover(unittest.TestCase):
    class Engine:
        terminal_phrase_queued = True

    class Player:
        is_busy = True

    def test_same_map_and_vehicle_preserves_queued_terminal_phrase(self):
        self.assertTrue(should_preserve_terminal_phrase(
            self.Engine(), self.Player(), "Tver_2026", "tver 2026", "ZiU | 682G", "ZiU | 682G"))

    class EngineWithoutTerminalFlag:
        terminal_phrase_queued = False

    def test_terminal_handover_marker_survives_engine_route_reset_until_player_finishes(self):
        self.assertTrue(should_preserve_terminal_phrase(
            self.EngineWithoutTerminalFlag(), self.Player(), "MapA", "MapA",
            "ZiU | 682G", "ZiU | 682G", terminal_pending=True,
        ))

    def test_terminal_phrase_is_not_preserved_for_other_map_vehicle_or_idle_player(self):
        self.assertFalse(should_preserve_terminal_phrase(
            self.Engine(), self.Player(), "Tver_2026", "Chuguev", "ZiU | 682G", "ZiU | 682G"))
        self.assertFalse(should_preserve_terminal_phrase(
            self.Engine(), self.Player(), "Tver_2026", "Tver_2026", "ZiU | 682G", "LiAZ | 5292"))
        idle = type("IdlePlayer", (), {"is_busy": False})()
        self.assertFalse(should_preserve_terminal_phrase(
            self.Engine(), idle, "Tver_2026", "Tver_2026", "ZiU | 682G", "ZiU | 682G"))

if __name__ == "__main__":
    unittest.main(verbosity=2)

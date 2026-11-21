"""Current-start health checks using fake identities and temporary files only."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bilingual import native_health, native_overlay
from bilingual.game_session import GameSession, _canonical


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.current = None
        self.calls = 0
    def process_identity(self):
        self.calls += 1
        return self.current


class NativeHealthTests(unittest.TestCase):
    wall = 1_700_000_000
    game_id = "health-fixture"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bilingual-health-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "game"
        self.folder.mkdir()
        self.exe = self.folder / "Game.exe"
        self.exe.write_bytes(b"fake process image; never executed")
        self.project = self.root / "workbench"
        data = self.project / "data"
        data.mkdir(parents=True)
        database = data / "corpus.sqlite3"
        database.write_bytes(b"database path fixture; never queried")
        self.service = SimpleNamespace(project=self.project, data_dir=data, database=database)
        self.marker = data / "native_profiles" / (self.game_id + ".json")
        self.marker.parent.mkdir()
        self.marker_value = {"version": 2, "backend": "reshade", "phase": "enabled",
                             "directory": str(self.folder), "config_hashes": {},
                             "health_protocol": 1, "addon_version": "0.5.3"}
        self.write_marker()
        self.addon = self.folder / "BilingualPanel.addon64"
        self.addon.write_bytes(b"fake current addon")
        built = self.project / "native/build/bin/Release/BilingualPanel.addon64"
        built.parent.mkdir(parents=True)
        built.write_bytes(self.addon.read_bytes())
        (self.folder / "dxgi.dll").write_bytes(b"fake runtime")
        (self.folder / "ReShade.ini").write_text("[INPUT]\nInputProcessing=2\n")
        self.write_config(database)
        state = self.folder / ".bilingual-workbench/native-panel/state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"version": 1, "profile_id": self.game_id + ":native-overlay",
                                    "phase": "enabled", "files": [{"target": name} for name in ("dxgi.dll", "BilingualPanel.addon64")]}))
        self.clock = Clock()
        self.session = FakeSession()
        for mocked in (
            patch("bilingual.native_health.GameSession", return_value=self.session),
            patch("bilingual.native_health.time.time", side_effect=lambda: self.wall + self.clock.value),
            patch("bilingual.native_overlay._paths", return_value=({}, self.exe, self.folder, self.marker)),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.monitor = native_health.NativeHealthMonitor(self.service, self.game_id, clock=self.clock)

    def write_marker(self):
        self.marker.write_text(json.dumps(self.marker_value), encoding="utf-8")

    def write_config(self, database):
        (self.folder / "BilingualPanel.ini").write_text(
            f"[Panel]\nGameId={self.game_id}\nDatabase={database}\nOpacity=0.78\n", encoding="utf-16")

    def identity(self, pid=41, start=None):
        start = self.clock.value if start is None else start
        return {"pid": pid, "exe_path": str(self.exe),
                "process_start_filetime": str(native_health._FILETIME_EPOCH + int((self.wall + start) * 10_000_000))}

    def status(self, identity=None, **changes):
        identity = identity or self.session.current
        value = {"schema_version": 1, "addon_version": "0.5.3", "pid": identity["pid"],
                 "process_start_filetime": identity["process_start_filetime"], "game_id": self.game_id,
                 "updated_at_ms": int((self.wall + self.clock.value) * 1000), "corpus_count": 31,
                 "panel_drawn": False, "error_code": "", "error_detail": ""}
        value.update({flag: True for flag, _ in native_health._FLAGS})
        value.update(changes)
        (self.folder / f"BilingualPanel.status.{identity['pid']}.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_waiting_partial_ready_and_actual_display_are_distinct(self):
        self.assertEqual(self.monitor.poll()["state"], "waiting")
        self.session.current = self.identity()
        self.assertEqual(self.monitor.poll()["state"], "starting")
        self.status(overlay_ready=False, input_ready=False)
        partial = self.monitor.poll()
        self.assertEqual(partial["state"], "starting")
        self.assertFalse(next(s for s in partial["steps"] if s["label"] == "绘制回调")["state"] == "ready")
        self.status()
        ready = self.monitor.poll()
        self.assertEqual(ready["title"], "面板已加载，按 F8 打开")
        self.assertFalse(ready["panel_drawn"])
        self.status(panel_drawn=True)
        self.assertTrue(self.monitor.poll()["panel_drawn"])
        self.assertEqual(self.session.calls, 5)

    def test_old_status_and_reused_pid_cannot_establish_readiness(self):
        self.session.current = self.identity()
        self.status(self.identity(start=-100))
        self.assertEqual(self.monitor.poll()["state"], "starting")
        self.status()
        self.assertEqual(self.monitor.poll()["state"], "ready")
        self.clock.value = 10
        self.session.current = self.identity(pid=41)  # Same PID, new creation time.
        self.assertEqual(self.monitor.poll()["state"], "starting")
        self.status(game_id="another-game")
        self.assertNotEqual(self.monitor.poll()["state"], "ready")

    def test_timeout_can_recover_with_late_success_and_restart(self):
        self.session.current = self.identity()
        self.monitor.poll()
        self.clock.value = 91
        failed = self.monitor.poll()
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["terminal"])
        self.assertIn("不能据此确定", failed["detail"])
        self.status()
        self.assertEqual(self.monitor.poll()["state"], "ready")
        self.session.current = None
        self.assertEqual(self.monitor.poll()["state"], "exited")
        self.clock.value = 100
        self.session.current = self.identity(pid=42)
        restarted = self.monitor.poll()
        self.assertEqual(restarted["state"], "starting")
        self.assertLess(restarted["elapsed"], 1)

    def test_error_precedes_ready_and_empty_game_id_is_restricted(self):
        self.session.current = self.identity()
        self.status(error_code="config_error", error_detail="Database missing", game_id="")
        result = self.monitor.poll()
        self.assertEqual(result["state"], "failed")
        self.assertIn("Database missing", result["detail"])
        self.status(game_id="")
        self.assertNotEqual(self.monitor.poll()["state"], "ready")
        self.status(error_code="empty_corpus", corpus_count=0)
        self.assertEqual(self.monitor.poll()["state"], "failed")
        self.status()
        self.assertEqual(self.monitor.poll()["state"], "ready")

    def test_heartbeat_expiry_and_partial_timeout_identify_the_stage(self):
        self.session.current = self.identity()
        self.status()
        self.assertEqual(self.monitor.poll()["state"], "ready")
        self.clock.value = 16
        stale = self.monitor.poll()
        self.assertEqual(stale["state"], "starting")
        self.assertIn("15 秒", stale["detail"])
        self.clock.value = 91
        self.status(runtime_ready=False, overlay_ready=False, input_ready=False)
        result = self.monitor.poll()
        self.assertEqual(result["state"], "failed")
        self.assertIn("图形挂钩", result["title"])

    def test_final_initialization_error_survives_stopped_writer(self):
        self.session.current = self.identity()
        self.status(game_id="", error_code="register_failed", error_detail="registration rejected")
        self.monitor.poll()
        self.clock.value = 120
        result = self.monitor.poll()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["title"], "插件注册失败")
        self.assertIn("registration rejected", result["detail"])
        self.session.current = self.identity()  # Reused PID in a different start.
        self.assertEqual(self.monitor.poll()["state"], "starting")

    def test_input_waits_until_timeout_and_appearance_failure_keeps_loaded_context(self):
        self.session.current = self.identity()
        self.status(input_ready=False, error_code="input_not_ready", error_detail="window unavailable")
        self.assertEqual(self.monitor.poll()["state"], "starting")
        self.clock.value = 91
        self.status(input_ready=False, error_code="input_not_ready")
        self.assertEqual(self.monitor.poll()["state"], "failed")
        self.status(error_code="config_write_error", error_detail="access denied", panel_drawn=True)
        saved = self.monitor.poll()
        self.assertEqual(saved["title"], "已加载，外观保存失败")
        self.assertTrue(saved["panel_drawn"])
        self.status(error_code="db_error", error_detail="database locked", corpus_ready=False)
        database = self.monitor.poll()
        self.assertEqual(database["title"], "本机文本库读取失败")
        self.assertIn("更新本地文本库", database["actions"][0])

    def test_old_plugin_is_reported_before_game_starts_and_hashes_are_cached(self):
        self.marker_value.pop("health_protocol")
        self.marker_value["addon_version"] = "0.5.2"
        self.write_marker()
        self.assertEqual(self.monitor.poll()["state"], "needs_update")
        self.marker_value.update(health_protocol=1, addon_version="0.5.3")
        self.write_marker()
        with patch("bilingual.native_overlay.digest", wraps=native_overlay.digest) as digest:
            self.assertEqual(self.monitor.poll()["state"], "waiting")
            self.monitor.poll()
            self.monitor.poll()
            self.assertEqual(digest.call_count, 2)
            self.assertNotIn(self.exe, [call.args[0] for call in digest.call_args_list])
        self.addon.write_bytes(b"old locally installed addon")
        self.assertEqual(self.monitor.poll()["state"], "needs_update")

    def test_missing_database_and_config_are_real_failures_not_running_blockers(self):
        self.session.current = self.identity()
        self.write_config(self.root / "missing.sqlite3")
        result = self.monitor.poll()
        self.assertEqual(result["state"], "failed")
        self.assertIn("数据库路径不存在", result["detail"])
        self.write_config(self.service.database)
        self.status()
        self.assertEqual(self.monitor.poll()["state"], "ready")
        (self.folder / "BilingualPanel.ini").unlink()
        self.assertEqual(self.monitor.poll()["state"], "failed")

    def test_other_executable_and_old_logs_cannot_make_success(self):
        self.session.current = dict(self.identity(), exe_path=str(self.root / "different.exe"))
        self.status(self.identity())
        (self.folder / "ReShade.log").write_text("addon registered; corpus opened; ready")
        self.assertEqual(self.monitor.poll()["state"], "waiting")


class ProcessIdentityTests(unittest.TestCase):
    def test_cached_pid_is_revalidated_and_creation_time_changes(self):
        expected = _canonical(Path("game.exe"))
        identity = {"pid": 41, "exe_path": expected, "process_start_filetime": "100"}
        class Api:
            scans = 0
            def pids(self):
                self.scans += 1
                return [40, 41]
            def identity(self, pid, wanted):
                return dict(identity) if pid == 41 and identity["exe_path"] == wanted else None
        session = object.__new__(GameSession)
        session._lock = threading.RLock()
        session._api = Api()
        session._preferred_pid = None
        session.exe_path = expected
        self.assertEqual(session.process_identity()["process_start_filetime"], "100")
        identity["process_start_filetime"] = "200"
        self.assertEqual(session.process_identity()["process_start_filetime"], "200")
        self.assertEqual(session._api.scans, 1)
        identity["exe_path"] = _canonical(Path("other.exe"))
        self.assertIsNone(session.process_identity())
        self.assertEqual(session._api.scans, 2)
        self.assertIsNone(session.process_identity(True))


if __name__ == "__main__":
    unittest.main()

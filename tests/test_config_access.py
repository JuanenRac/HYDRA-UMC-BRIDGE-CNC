# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - tests/test_config_access.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The configuration file lives in a directory of the service, and the account of the service is proved able to read it.

The service account cannot enter the shared /etc/hydra-umc, so the unit names its own configuration directory. These tests run with temporary folders
and no root: the helpers, the two flags of the service and the text of the unit.
"""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hydra_umc_bridge_cnc import config_access
from hydra_umc_bridge_cnc.config_access import check_access, diagnose_access, ensure_directory, migrate_legacy_config
from hydra_umc_bridge_cnc.service import CONFIG_ENVIRONMENT, DEFAULT_CONFIG_PATH, main

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy/hydra-umc-bridge-cnc.service"
POSIX_USER = hasattr(os, "geteuid") and os.geteuid() != 0 and os.name != "nt"


class PathsTests(unittest.TestCase):
    def test_the_default_file_is_in_the_directory_of_the_service_and_the_earlier_place_is_kept_for_the_migration(self):
        self.assertEqual(DEFAULT_CONFIG_PATH, "/etc/hydra-umc-bridge-cnc/bridge-cnc.toml")
        self.assertEqual(config_access.LEGACY_CONFIG_PATH, "/etc/hydra-umc/bridge-cnc.toml")
        self.assertEqual(config_access.DEFAULT_CONFIG_PATH, DEFAULT_CONFIG_PATH)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.legacy = self.base / "old" / "bridge-cnc.toml"
        self.legacy.parent.mkdir()
        self.target = self.base / "new" / "bridge-cnc.toml"

    def test_a_file_of_the_earlier_place_is_copied_once_and_the_old_one_stays(self):
        self.legacy.write_text("a = 1\n", encoding="utf-8")
        self.assertEqual(migrate_legacy_config(str(self.legacy), str(self.target)), "migrated")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "a = 1\n")
        self.assertTrue(self.legacy.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)

    def test_a_file_that_is_already_there_is_never_overwritten(self):
        self.legacy.write_text("old = 1\n", encoding="utf-8")
        self.target.parent.mkdir()
        self.target.write_text("new = 1\n", encoding="utf-8")
        self.assertEqual(migrate_legacy_config(str(self.legacy), str(self.target)), "kept")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new = 1\n")

    def test_nothing_to_migrate_creates_nothing(self):
        self.assertEqual(migrate_legacy_config(str(self.legacy), str(self.target)), "none")
        self.assertFalse(self.target.parent.exists())

    def test_a_group_that_does_not_exist_is_not_a_group(self):
        self.assertFalse(config_access.group_exists("hydra-no-such-group-xyz"))


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_the_directory_of_the_service_is_made_and_set_to_0750(self):
        directory = self.base / "hydra-umc-bridge-cnc"
        self.assertEqual(ensure_directory(str(directory)), "ready")
        self.assertTrue(directory.is_dir())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)

    def test_a_directory_that_systemd_made_with_another_mode_is_set_to_0750(self):
        directory = self.base / "hydra-umc-bridge-cnc"
        directory.mkdir()
        if os.name != "nt":
            directory.chmod(0o700)
        ensure_directory(str(directory))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)

    def test_the_shared_directory_is_never_changed(self):
        shared = self.base / "hydra-umc"
        shared.mkdir()
        before = shared.stat().st_mode
        with self.assertRaises(ValueError):
            ensure_directory(str(shared))
        self.assertEqual(shared.stat().st_mode, before)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_a_readable_file_passes(self):
        path = self.base / "bridge-cnc.toml"
        path.write_text("a = 1\n", encoding="utf-8")
        code, message = check_access(str(path))
        self.assertEqual(code, 0)
        self.assertIn("readable", message)

    def test_a_missing_file_is_refused_and_says_so(self):
        code, message = check_access(str(self.base / "no" / "bridge-cnc.toml"))
        self.assertEqual(code, 2)
        self.assertIn("does not exist", message)

    @unittest.skipUnless(POSIX_USER, "needs a POSIX account that is not root")
    def test_a_directory_the_account_cannot_enter_is_named(self):
        locked = self.base / "locked"
        locked.mkdir()
        (locked / "bridge-cnc.toml").write_text("a = 1\n", encoding="utf-8")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        code, message = check_access(str(locked / "bridge-cnc.toml"))
        self.assertEqual(code, 2)
        self.assertIn("cannot be entered", message)
        self.assertIn(str(locked), diagnose_access(str(locked / "bridge-cnc.toml")))

    @unittest.skipUnless(POSIX_USER, "needs a POSIX account that is not root")
    def test_a_file_the_account_cannot_read_is_named(self):
        path = self.base / "bridge-cnc.toml"
        path.write_text("a = 1\n", encoding="utf-8")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        code, message = check_access(str(path))
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", message)


class FlagsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config = self.base / "new" / "bridge-cnc.toml"
        self.legacy = self.base / "old" / "bridge-cnc.toml"
        self.legacy.parent.mkdir()

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.config), *args])
        return code, out.getvalue(), err.getvalue()

    def test_prepare_config_copies_the_earlier_file_once_and_exits_zero(self):
        self.legacy.write_text("a = 1\n", encoding="utf-8")
        code, out, _ = self.run_main("--prepare-config", "--legacy-config", str(self.legacy))
        self.assertEqual(code, 0)
        self.assertIn("migrated", out)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "a = 1\n")
        code, out, _ = self.run_main("--prepare-config", "--legacy-config", str(self.legacy))
        self.assertEqual((code, "kept" in out), (0, True))

    def test_prepare_config_without_any_file_still_exits_zero_and_the_start_is_refused_later(self):
        code, out, _ = self.run_main("--prepare-config", "--legacy-config", str(self.legacy))
        self.assertEqual((code, "none" in out), (0, True))
        code, _, err = self.run_main("--check")
        self.assertEqual(code, 2)
        self.assertIn("configuration error", err)

    def test_prepare_config_only_changes_a_directory_that_is_the_one_of_the_service(self):
        elsewhere = self.base / "elsewhere" / "bridge-cnc.toml"
        elsewhere.parent.mkdir()
        if os.name != "nt":
            elsewhere.parent.chmod(0o755)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(elsewhere), "--prepare-config", "--legacy-config", str(self.legacy)])
        self.assertEqual(code, 0)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(elsewhere.parent.stat().st_mode), 0o755)

    def test_prepare_config_sets_the_directory_of_the_service_to_0750(self):
        config = self.base / "hydra-umc-bridge-cnc" / "bridge-cnc.toml"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(config), "--prepare-config", "--legacy-config", str(self.legacy)])
        self.assertEqual(code, 0)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), 0o750)

    def test_check_access_exits_zero_when_readable_and_two_with_the_reason_when_not(self):
        self.config.parent.mkdir()
        self.config.write_text("a = 1\n", encoding="utf-8")
        code, out, _ = self.run_main("--check-access")
        self.assertEqual((code, "readable" in out), (0, True))
        self.config.unlink()
        code, _, err = self.run_main("--check-access")
        self.assertEqual(code, 2)
        self.assertIn("cannot read the configuration file", err)

    def test_check_access_does_not_need_a_valid_file(self):
        self.config.parent.mkdir()
        self.config.write_text("this is not toml = = =\n", encoding="utf-8")
        code, _, _ = self.run_main("--check-access")
        self.assertEqual(code, 0)

    def test_the_default_comes_from_the_environment_or_the_directory_of_the_service(self):
        with mock.patch.dict(os.environ, {CONFIG_ENVIRONMENT: str(self.config)}):
            self.config.parent.mkdir()
            self.config.write_text("x = 1\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(["--check-access"]), 0)


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.text = UNIT.read_text(encoding="utf-8")

    def test_the_unit_asks_systemd_for_a_directory_of_its_own_with_mode_0750(self):
        self.assertIn("ConfigurationDirectory=hydra-umc-bridge-cnc\n", self.text)
        self.assertIn("ConfigurationDirectoryMode=0750\n", self.text)

    def test_the_unit_reads_the_file_from_that_directory_and_not_from_the_shared_one(self):
        self.assertIn("=/etc/hydra-umc-bridge-cnc/bridge-cnc.toml", self.text)
        self.assertNotIn("CONFIG=/etc/hydra-umc/", self.text)
        self.assertNotRegex(self.text, r"(?m)^ExecStart.*\s/etc/hydra-umc/")

    def test_the_migration_runs_as_root_and_the_access_check_runs_as_the_service_account_before_the_start(self):
        lines = [line for line in self.text.splitlines() if line.startswith("Exec")]
        prepare = next(i for i, line in enumerate(lines) if "--prepare-config" in line)
        access = next(i for i, line in enumerate(lines) if "--check-access" in line)
        start = next(i for i, line in enumerate(lines) if line.startswith("ExecStart="))
        self.assertTrue(lines[prepare].startswith("ExecStartPre=+"))
        self.assertTrue(lines[access].startswith("ExecStartPre=/"))
        self.assertLess(prepare, access)
        self.assertLess(access, start)

    def test_the_shared_directory_is_not_named_as_a_configuration_directory(self):
        self.assertNotIn("ConfigurationDirectory=hydra-umc\n", self.text)
        self.assertNotIn("ConfigurationDirectory=/etc/hydra-umc", self.text)


if __name__ == "__main__":
    unittest.main()

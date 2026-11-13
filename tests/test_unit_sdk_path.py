# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - tests/test_unit_sdk_path.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The unit of the service finds the SDK it imports.

The service runs from /opt/hydra-umc/<component>/src with a PYTHONPATH of its own. When its code imports `hydra_umc_sdk` the unit must also list the folder the installer
of HYDRA-UMC-OS puts the package in, or the service starts and dies with ModuleNotFoundError. Evidence: the unit file and the source are read as text.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDK_FOLDER = "/opt/hydra-umc/sdk-python"
UNITS = sorted(ROOT.glob("deploy/*.service"))


def imports_the_sdk() -> list[str]:
    found = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            if any(name == "hydra_umc_sdk" or name.startswith("hydra_umc_sdk.") for name in names):
                found.append(str(path.relative_to(ROOT)))
                break
    return found


class UnitSdkPathTests(unittest.TestCase):
    def test_there_is_a_unit(self):
        self.assertTrue(UNITS)

    def test_the_unit_lists_the_sdk_folder_after_its_own_source(self):
        for unit in UNITS:
            paths = re.findall(r"^Environment=PYTHONPATH=(.+)$", unit.read_text(encoding="utf-8"), re.M)
            self.assertEqual(len(paths), 1, unit.name)
            parts = paths[0].strip().split(":")
            self.assertTrue(parts[0].endswith("/src"), unit.name)
            self.assertEqual(parts[1:], [SDK_FOLDER], unit.name)

    def test_a_service_whose_code_imports_the_sdk_cannot_have_a_unit_without_it(self):
        users = imports_the_sdk()
        self.assertTrue(users, "the check has nothing to guard if no file imports the SDK any more: remove the test and the folder from the unit")
        for unit in UNITS:
            self.assertIn(SDK_FOLDER, unit.read_text(encoding="utf-8"), f"{unit.name}: {', '.join(users)} import hydra_umc_sdk")

    def test_the_scan_sees_an_import_of_the_sdk(self):
        tree = ast.parse("from hydra_umc_sdk.mcu_errors import X\nimport os")
        names = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertEqual(names, ["hydra_umc_sdk.mcu_errors"])


if __name__ == "__main__":
    unittest.main()

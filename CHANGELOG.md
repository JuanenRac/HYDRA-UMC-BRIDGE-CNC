<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Change history
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Changelog

## [0.0.4] - 2026-08-30

- Added `docs/BRIDGE_GUIDE.md`, defining evidence scope, controller
  compatibility, script conventions and the CNC hardware acceptance gate.
- Removed the duplicated terminal BUILD & RUN section from all seven README files.
- Added an offline CLI for inspecting saved controller-evidence JSON, including
  supplied GRBL state evidence, without opening a serial, HAL or network link.
- Added CLI contract coverage; the full suite now has nine tests.
- Synchronized package metadata, ecosystem manifest and all seven README files.

## [0.0.3] - 2026-08-30

- Added read-only normalization of mapping evidence and supplied GRBL status
  lines; the module opens no serial, HAL or network connection.
- Made missing or non-Boolean E-STOP and door signals fail closed before an
  observed CNC state can be trusted.
- Added four deterministic evidence-boundary tests; the suite now has eight
  tests. Package metadata, manifest and all seven README files are synchronized.

## [0.0.2] - 2026-08-30

- Made an unexpected non-text controller state fail safe as `OFFLINE` instead
  of raising while evaluating the CNC cell boundary.
- Synchronized the English README and all six translated README files with
  the current version.
- Successful incremental build: synchronized package metadata and
  `hydra-umc.project.json`.

## [0.0.1]

- Added fail-safe CNC cell snapshot and SDK safety-gate tests.
- Added non-mutating build-test scripts and CI SDK checkout.
- Standardized README (all 7 languages) and project banner to match the
  rest of the ecosystem's established-project structure.
- Promoted to `established`: manifest, docs, build-test/CI, real local
  verification and no private-doc references all confirmed - no
  functional gap found in this bridge's own small, SDK-delegated core.

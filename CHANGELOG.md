<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Change history
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Changelog

## [0.0.5] - Real GRBL Alarm/Jog/Home/Hold/Door states

- Added a read-only MTConnect execution normalizer for saved controller
  evidence. Known execution states map into the SDK gate; unknown values and
  missing independent safeguards remain fail-safe/offline.
- **`cell.py`** - `CncSnapshot.machine_state()` now recognizes GRBL v1.1's
  full real status-report vocabulary, researched against
  [github.com/gnea/grbl/wiki/Grbl-v1.1-Interface](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface):
  `Jog`/`Home` (real active-motion states, previously swallowed by the
  `OFFLINE` fallback) now map to `RUNNING`; `Alarm` (GRBL's own most
  safety-critical report - limit trip, lost position, unresolved E-STOP,
  also previously indistinguishable from "not reporting") now maps to
  `FAULT`; `Door` (GRBL's own real safety-interlock state) now maps to
  `SAFE_STOP` as a defensive second signal alongside this bridge's
  existing `door_closed` input.
- `Hold` (and MTConnect's `FEED_HOLD`/`INTERRUPTED`, which already
  normalized to the same `"HOLD"` token) now correctly maps to `HOLDING`
  instead of `RUNNING` - a paused program is a real, distinct condition
  from an actively running one, matching the same real
  `print_stats.state=paused` -> `HOLDING` fix already made in the sibling
  PRINTER3D bridge. This does not change any dispatch decision
  (`evaluate_job()` only permits productive work on `IDLE` either way),
  only the accuracy of the reported state.
- 6 new/updated regression tests - 16/16 tests passing.

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

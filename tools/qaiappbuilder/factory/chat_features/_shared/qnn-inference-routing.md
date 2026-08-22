# QNN Inference Routing — Per-Platform Policy

> **Single source of truth. Shared by `app-builder` / `model-builder` / `model-hub`.**
> Edit only here; do NOT copy the body into individual skills.

---

## 1. Terminology (verbatim from QAIRT / QNN official docs)

| Term | Definition | Doc anchor |
|------|------------|------------|
| **Real HTP execution** | `.bin`/`.dlc` running on a physical Hexagon NPU. Two forms only: (a) `HOST_OS` in {`windows-arm64`, `linux-aarch64`} executing on its **local** HTP; (b) any host pushing to an ARM64 device via `adb_runner.py`. Nothing else qualifies. | — |
| **CPU backend** (`libQnnCpu.so` / `QnnCpu.dll`) | *"Backend for Snapdragon™ CPU hardware core(s)"* — a real CPU backend, NOT an emulator. On x86_64 hosts it runs on the host CPU. Advertises **`fp32` and `int8`** compute configurations only; FP16 tensors are accepted only by type-conversion ops (Cast/Convert), not as a native compute precision. | `general/backend.html`, `OpDef/SupportedOps.html` |
| **HTP functional simulator** (x86_64 build of `libQnnHtp.so` / `QnnHtp.dll`) | *"x86 Linux host backend serves as a functional simulator for the hardware accelerator and supports graph preparation on x86_64 hosts."* Runs the full HTP graph on x86 CPU; intended for **functional validation only** (graph executes, pipeline / pre-post correct) — numerical equivalence with real HTP is **not guaranteed** (fixed-point rounding, saturation, and non-linear approximations may differ), and latency is **not** device performance. Used only on x64 hosts when the user opts in to local execution — see `x64-host-notes.md`. | `general/backend.html` |
| **Hexagon QEMU emulation** (`libQnnHtpQemu.so`) | Beta quality, HTP v68 only, quantized 8-bit only, requires pre-installed **Hexagon SDK v5.0.0.0**. **Not used by this project.** | `general/htp/htp_auto_qemu.html` |

> Never call `libQnnCpu.so` a "reference implementation" — Qualcomm reserves that phrase for the SampleApp source tree, not the CPU backend.

---

## 2. Per-platform routing (default + user override)

| `HOST_OS` | Physical HTP? | Default path | Override path (user opts in) |
|-----------|:-:|--------------|------------------------------|
| `windows-arm64` | Yes | **Local HTP** (`qai_runner.py`) | ADB to another ARM64 device |
| `linux-aarch64` | Yes | **Local HTP** (`qai_runner.py`) | ADB to another ARM64 device |
| `windows-x64` | No | **ADB to an ARM64 device** (`adb_runner.py`) — default | Local execution (QnnCpu or QnnHtp simulator) — user opts in; see `x64-host-notes.md` |
| `linux-x64` | No | **ADB to an ARM64 device** (`adb_runner.py`) — default | Local execution (QnnCpu or QnnHtp simulator) — user opts in; see `x64-host-notes.md` |

Both x64 platforms default to ADB (Path B). Local execution is opt-in and requires the user to select a backend (QnnCpu / QnnHtp simulator) via the interactive `question` tool — full rules and compatibility matrix in `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md`.

**Authoritative numbers reminder:** For real performance/accuracy figures, always route via ADB to a real device. Qualcomm's own benchmarking framework requires a physical device (`general/benchmarking.html:201-206`). The local x64 path is for functional validation of the inference pipeline, not for reporting hardware behaviour.

---

## 3. User override triggers (disambiguate default vs opt-in)

Ambiguous requests use the default. Only these keyword classes flip the path:

| To force | Trigger phrases (English) | Trigger phrases (中文) |
|----------|---------------------------|-----------------------|
| **ADB / on-device** | `real device`, `on device`, `on hardware`, `ADB`, `push to board`, `HTP performance`, `NPU performance` | 真机、真实设备、推到设备、HTP 性能、NPU 性能 |
| **Local x64 execution** (from x64 default of ADB) | `local`, `on host`, `no device` | 本机、不推设备、不用设备 |

Requests without any trigger keyword follow the default in §2. On x64 hosts, when the user's intent to run locally is detected (by phrasing, not keyword match), open `x64-host-notes.md` and use the `question` tool per its §2.

---

## 4. `.bin` producer/consumer binding (critical)

A generated `.bin` is a QNN context binary for the HTP backend. It can be loaded by any HTP backend library build:

- `aarch64-windows-msvc/QnnHtp.dll` — real NPU on ARM64 Windows hosts.
- `aarch64-oe-linux-gcc11.2/libQnnHtp.so` — real NPU on linux-aarch64.
- `x86_64-windows-msvc/QnnHtp.dll` / `x86_64-linux-clang/libQnnHtp.so` — HTP functional simulator on x64 hosts.

The `.bin` is **NOT** loadable by `QnnCpu.dll`. HTP `.bin` files may also carry a `htp_soc_id` field that ties them to a specific SoC (e.g. X Elite vs X2 Elite); when present, that lock applies to **real-device execution only** — the x86 simulator can typically load any SoC's `.bin` regardless.

A `.dlc` has **no such lock** — the backend is chosen at load time. Prefer `.dlc` for cross-device workflows or when the backend is not yet decided.

---

## 5. MANDATORY closing statement (x64 local execution runs)

When a run used the x64 local execution path (any x64 OS, any backend), a mandatory closing statement MUST be included in the final summary and `REPORT.md`. Full template + language rule → `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` §4.

---

## 6. Blocking condition (scope of reporting)

Any x64 local execution run must not be reported as real HTP performance. Full definition of the `B11` blocking condition (trigger, action, options offered to user) → `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` §5.

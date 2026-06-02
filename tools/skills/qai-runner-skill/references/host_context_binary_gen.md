# Host Context Binary Generation

Generate hardware-specific HTP context binaries on the host (x86 Linux or x86 Windows)
and deploy to the target ARM device for inference.

> **Key concept**: The host generates a context binary compiled for the **target SoC**.
> The `.bin` can then run on the target device even if host and target architectures differ.
> E.g., x86 Linux host → generates context binary → copies to ARM Linux target → runs inference.

---

## Prerequisites

1. Source QAIRT environment: `source $QAIRT_ENV_SETUP`
2. Do NOT require generating a new target `.so` / `.dll` for context-binary flow.
   Use existing context-binary artifacts directly when available; avoid adding cross-compilation steps unless explicitly requested.
3. **Know your target `soc_id` and `dsp_arch`** — required before generation (see [SOC/DSP Check Method](#socdsp-check-method) below)

> ⚠️ **`soc_id` and `dsp_arch` are mandatory.**
> Generating without them produces a generic binary that may crash or produce wrong results on the target.
> Always confirm the correct pair before generating. See [SOC/DSP Check Method](#socdsp-check-method).

---

## Step 1 — Get Target soc_id and dsp_arch

### Option A — Read target hardware identity (Linux host)

```bash
ssh <user>@<host> 'cat /proc/device-tree/model; echo; tr -d "\0" </proc/device-tree/compatible'
```
- Use the `compatible` string (e.g. `qcs9075`, `sa8775p`) to look up the QAIRT target table. Do **not** use `/sys/devices/soc0/soc_id` — that is the Linux kernel ID, not the QAIRT `soc_id`.

### Option B — Read target hardware identity from Windows Host (Remote Target)

> **Key point**: `soc_id` and `dsp_arch` describe the **target Qualcomm SoC**, not the Windows x86 host. You must query the target device from Windows, then map the result to the QAIRT table.

* **Sub-option B1 — SSH from Windows PowerShell** (target is a Linux/QNX device on the network):
  ```powershell
  ssh <user>@<target-ip> 'cat /proc/device-tree/model; echo; tr -d "\000" </proc/device-tree/compatible; echo'
  ```
  Example output:
  ```
  Qualcomm Technologies, Inc. QCS9075
  ...
  qcom,qcs9075 qcom,sa8775p
  ```
  Use the tokens (e.g. `qcs9075`, `sa8775p`) to look up the QAIRT table — same mapping as Linux.

* **Sub-option B2 — ADB from Windows** (target is an Android or embedded device connected via USB/network):
  ```powershell
  # Read device tree compatible string
adb shell "cat /proc/device-tree/model; echo; tr -d '\000' </proc/device-tree/compatible; echo"

  # Alternative: Android properties (may reveal platform name)
adb shell getprop ro.board.platform
adb shell getprop ro.soc.model
adb shell getprop ro.hardware
```
  Map the platform name or compatible token to the QAIRT documentation table.
  > ⚠️ Android properties (`ro.board.platform`, etc.) give a marketing/platform name, not the QAIRT `soc_id` directly. Always cross-reference with the QAIRT docs table.

### Option C — Local Windows on Snapdragon (WoS Target)

For Windows on Snapdragon devices, use `aipc_qairt_devinfo.ps1` to automatically detect the local QAIRT SoC ID and DSP/HTP architecture.

**1. Initialize the QAIRT environment first:**
```powershell
. "$env:QAIRT_SDK_ROOT\bin\envsetup.ps1"
```
*If `QAIRT_SDK_ROOT` is not yet set, dot-source the installed SDK setup script directly:*
```powershell
. "C:\Qualcomm\AIStack\QAIRT\<version>\bin\envsetup.ps1"
```

**2. Run the detection script from the skill scripts directory:**
* Human-readable output:
  ```powershell
  & "<path_to_skills>/aipc-toolkit/scripts/aipc_qairt_devinfo.ps1"
  ```
* JSON output for automation:
  ```powershell
  & "<path_to_skills>/aipc-toolkit/scripts/aipc_qairt_devinfo.ps1" -Json
  ```
* Override SDK root if needed:
  ```powershell
  & "<path_to_skills>/aipc-toolkit/scripts/aipc_qairt_devinfo.ps1" -SdkRoot "C:\Qualcomm\AIStack\QAIRT\<version>"
  ```

**Key Output Fields:**
- `SocModel`: QAIRT enum name from `QnnTypes.h` (e.g., `QNN_SOC_MODEL_JUDE` / `SM8650`)
- `SocId`: QAIRT numeric SoC ID (e.g., `100`, used in `.conf` config files)
- `DspCoreVersion`: DSP core string reported by `qnn-platform-validator`
- `DspArch`: Hexagon library folder/arch name (e.g., `hexagon-v73`, or simply mapped to `"v73"` for the config file)
- `DriverFamily`: Windows Qualcomm NPU/FastRPC driver family (e.g., `SC8380XP`)
- `SocConfidence`: Confidence of the inferred `SocModel` / `SocId` match


### Map to QAIRT soc_id / dsp_arch

Look up in QAIRT docs:
- `docs/QAIRT-Docs/QNN/general/overview.html`
- `docs/QAIRT-Docs/QNN/general/htp/htp_auto_single_nsp.html`

**Verified mappings in this project:**

| compatible token | QAIRT soc_id | dsp_arch | Status |
|-----------------|:------------:|:--------:|--------|
| `qcs9075`, `sa8775p` | **77** | **v73** | ✅ Preferred for this project |
| `qcs9075`, `sa8775p` | 52 | v73 | Also valid (legacy/default example) |

> ⚠️ Wrong `soc_id`/`dsp_arch` causes a **signal 11 crash** at runtime. Always validate on device after generation.

### General soc_id source guidance

- Do not force `soc_id` from Linux sysfs values alone (for example `/sys/devices/soc0/soc_id`) without QAIRT mapping validation.
- Linux kernel/platform IDs are not guaranteed to map 1:1 to QAIRT context config identifiers.
- Do not run speculative `dsp_arch` sweeps across unrelated versions (for example `v69`) unless QAIRT mapping for the current target token explicitly lists them.
- For a given target token, keep `dsp_arch` fixed to the mapped value and troubleshoot with VTCM / `soc_id` / `htp_arch` knobs first.
- If explicit `soc_id` config repeatedly fails (parse/compose/runtime), switch to architecture-based device config (for example `htp_arch`) and validate on target.
- If explicit `soc_id` settings repeatedly fail (parse/compose/runtime), try removing `soc_id` and using architecture-based device config (for example `htp_arch`) with target-side validation.

General VTCM guidance:
- Use `vtcm_mb=0` as the default starting setting.
- `vtcm_mb=0` may still fail on some targets; treat it as a starting point, not a guaranteed-working value.
- **Host generation always succeeds for all `vtcm_mb` values** — the host cannot
  detect whether a given VTCM size is supported by the target SoC at generation
  time. The failure only surfaces at **runtime on the target device**.
- If context load fails, sweep a small range and **validate each on the target device**:
  - `vtcm_mb=0,1,2,3,4,8`
- If your preferred `vtcm_mb` value fails, choose the **maximum passing `vtcm_mb`** from the sweep results and record both:
  - preferred value (failed) and error signature
  - selected fallback value (max passing) used for final deployment
- **Do NOT stop at the first passing value** — a lower value passing does not mean
  higher values also fail. Always complete the full sweep and pick the maximum
  passing value (higher VTCM = more on-chip memory = better HTP performance).
- Common runtime indicators of a bad VTCM setting:
  - `Request feature vtcm size with value <N> unsupported`
  - `Failed to register context to device and backend`
  - `Failed to create context from binary with err 0x138d`
- soc_id setting may be optional. test remove it.
- search web to get possible solution.



### Recommended: use aipc_qairt_devinfo.ps1 (Windows) or hardware identity lookup (Linux)

On **Windows on Snapdragon**, `scripts/aipc_qairt_devinfo.ps1` automates detection:
- Reads CPU/WMI, scans driver INF files, matches against QAIRT QnnTypes.h
- Returns soc_id (numeric), dsp_arch (string), driver family, and confidence score

On **Linux ARM**, use `cat /proc/device-tree/compatible` and look up the QAIRT mapping table (see Step 1 Option A).

---

## Step 2 — Prepare Config Files

Context binary generation requires two config files that specify the target SoC.

### 2a. Graph/device config (`.conf`)

```json
{
  "graphs": [
    {
      "graph_names": ["<GRAPH_NAME>"],
      "vtcm_mb": 8,
      "O": 3
    }
  ],
  "devices": [
    {
      "soc_id": 52,
      "dsp_arch": "v73",
      "cores": [
        {
          "perf_profile": "burst",
          "rpc_control_latency": 50
        }
      ]
    }
  ]
}
```

| Field | Required | Description |
|-------|:--------:|-------------|
| `graph_names` | ✅ | Must match the graph name used by your converted QNN artifact/context source |
| `soc_id` | ✅ | Target SoC ID from QAIRT table — must be confirmed |
| `dsp_arch` | ✅ | Target DSP architecture from QAIRT table — must be confirmed |
| `O` | optional | Optimization level: `1`=fast compile, `3`=best runtime perf |
| `vtcm_mb` | optional | VTCM budget in MB; `8` is a good default |
| `perf_profile` | optional | `burst` for latency-focused inference |
| `rpc_control_latency` | optional | RPC dispatch latency hint in microseconds |

### 2b. Backend extension wrapper JSON

```json
{
  "backend_extensions": {
    "shared_library_path": "/abs/path/to/libQnnHtpNetRunExtensions.so",
    "config_file_path": "/abs/path/to/soc52_v73.conf"
  }
}
```

- `shared_library_path`: `$QAIRT_SDK_ROOT/lib/<host_arch>/libQnnHtpNetRunExtensions.so` (Linux) or `QnnHtpNetRunExtensions.dll` (Windows)
- `config_file_path`: absolute path to the `.conf` from step 2a

---

## Step 3 — Generate Context Binary

### `--binary_file` rules (verified)

- Takes a **stem without `.bin`** — the tool appends `.bin` automatically
- Output goes to `--output_dir` (default `./output/`), **not** the current directory
- Do **not** pass an absolute path to `--binary_file` — it double-appends `.bin`

### Linux x86 host ✅ verified (exit 0, ~4MB output)

```bash
# Step 2a: create .conf  (replace graph name, soc_id, dsp_arch for your target)
cat > /tmp/soc52_v73.conf << 'CONF'
{"graphs":[{"graph_names":["<GRAPH_NAME>"],"vtcm_mb":8,"O":3}],"devices":[{"soc_id":52,"dsp_arch":"v73","cores":[{"perf_profile":"burst","rpc_control_latency":50}]}]}
CONF

# Step 2b: create backend extension wrapper JSON
cat > /tmp/soc52_v73.json << JSONEOF
{"backend_extensions":{"shared_library_path":"$QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtpNetRunExtensions.so","config_file_path":"/tmp/soc52_v73.conf"}}
JSONEOF

# Step 3: generate
mkdir -p /abs/output/dir
$QAIRT_SDK_ROOT/bin/x86_64-linux-clang/qnn-context-binary-generator \
  --backend     $QAIRT_SDK_ROOT/lib/x86_64-linux-clang/libQnnHtp.so \
  --model       /abs/path/to/<EXISTING_MODEL_LIBRARY> \
  --binary_file <OUTPUT_BASENAME> \
  --output_dir  /abs/output/dir \
  --config_file /tmp/soc52_v73.json
# output: /abs/output/dir/<OUTPUT_BASENAME>.bin
```

### Windows host (x86_64 or ARM64)

> ⚠️ **ARM64 Windows**: Despite being ARM64 hardware, the context binary generator runs under
> x86_64 emulation. The model library (`--model`) must be compiled for `windows-x86_64`, not
> `windows-aarch64`. The QNN HTP backend (`QnnHtp.dll` from `x86_64-windows-msvc`) handles
> the arch bridge during generation. The resulting `.bin` is SoC-specific and platform-independent.
> If the QAIRT venv Python is x86_64 emulated, `qnn-model-lib-generator` must use `-t windows-x86_64`.

```powershell
# Step 2a: create .conf  (replace graph name, soc_id, dsp_arch for your target)
'{"graphs":[{"graph_names":["<GRAPH_NAME>"],"vtcm_mb":8,"O":3}],"devices":[{"soc_id":52,"dsp_arch":"v73","cores":[{"perf_profile":"burst","rpc_control_latency":50}]}]}' `
  | Set-Content C:\tmp\soc52_v73.conf

# Step 2b: create backend extension wrapper JSON
$json = @{
    backend_extensions = @{
        shared_library_path = "$env:QAIRT_SDK_ROOT\lib\x86_64-windows-msvc\QnnHtpNetRunExtensions.dll"
        config_file_path = "C:\tmp\soc52_v73.conf"
    }
}
$json | ConvertTo-Json -Compress | Set-Content C:\tmp\soc52_v73.json

# Step 3: generate
& "$env:QAIRT_SDK_ROOT\bin\x86_64-windows-msvc\qnn-context-binary-generator.exe" `
  --backend     "$env:QAIRT_SDK_ROOT\lib\x86_64-windows-msvc\QnnHtp.dll" `
  --model       "C:\abs\path\to\<EXISTING_MODEL_LIBRARY>" `
  --binary_file <OUTPUT_BASENAME> `
  --output_dir  "C:\abs\output\dir" `
  --config_file "C:\tmp\soc52_v73.json"
# output: C:\abs\output\dir\<OUTPUT_BASENAME>.bin
```

---

## Step 4 — Deploy and Validate on Target

```bash
# Copy to remote
scp /abs/output/dir/<OUTPUT_BASENAME>.bin <user>@<host>:<workdir>/

# Run inference on remote — confirm generated .bin is selected
ssh <user>@<host> "cd <workdir> && source <qairt_setup> && python3 aipc infer_yolov8n.py --onnx yolov8n.onnx ..."
```

**Validation checklist:**
- [ ] Remote log shows selected context binary `.bin` path for runtime=HTP
- [ ] Exit code 0, output produced
- [ ] Cosine similarity vs ONNX baseline ≥ 0.95
- [ ] Latency reasonable (collect avg/min/max over 5 runs)

---

## Config Templates

Replace `<GRAPH_NAME>` with the graph name used by your converted QNN artifact/context source.
Add `soc_id` and `dsp_arch` from Step 1 into the `devices` block.

**Baseline — O=1, vtcm=2:**
```json
{"graphs":[{"graph_names":["<GRAPH_NAME>"],"vtcm_mb":2,"O":1}],"devices":[{"soc_id":<SOC_ID>,"dsp_arch":"<DSP_ARCH>","cores":[{"perf_profile":"burst","rpc_control_latency":50}]}]}
```

**Performance — O=3, vtcm=8:**
```json
{"graphs":[{"graph_names":["<GRAPH_NAME>"],"vtcm_mb":8,"O":3}],"devices":[{"soc_id":<SOC_ID>,"dsp_arch":"<DSP_ARCH>","cores":[{"perf_profile":"burst","rpc_control_latency":50}]}]}
```

---

## Additional CLI Options

| Option | Effect |
|--------|--------|
| `--profiling_level detailed --profiling_option optrace` | Enable HTP optrace profiling (not for throughput) |
| `--enable_intermediate_outputs` | Expose intermediate tensors — larger binary, slower runtime, debug only |
| `--input_output_tensor_mem_type memhandle` | Platform-dependent IO memory mode |

---

## See Also

- `scripts/aipc_qairt_devinfo.ps1` — automated soc_id/dsp_arch discovery on Windows on Snapdragon
- `scripts/aipc_dev_gen_contextbin.py` — wrapper script (no `--config_file` support)
- `scripts/aipc_dev_gen_contextbin_x86.py` — x86-host-aware wrapper (no `--config_file` support)

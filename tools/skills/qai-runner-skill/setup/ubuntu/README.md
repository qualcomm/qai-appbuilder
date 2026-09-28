# Use `QAIRT_SETUP_SOP_linux.md` With Codex

Use this prompt when QAIRT is not fully set up yet (or `aienv.sh` is missing):

```text
Set up QAIRT for Ubuntu 24.04 by following /home/ubuntu/Test/QAIRT_SETUP_SOP_linux.md exactly.

Ask me for any required inputs first, then run end-to-end:
- install/bootstrap QAIRT per SOP
- create or fix aienv.sh
- verify QAIRT env is sourceable
- run `${QAIRT_SDK_ROOT}/bin/check-linux-dependency.sh --dry-run`, install
  missing system packages with the documented `sudo` command, then rerun it
- verify the Linux dependency check finishes with `Done!!` before conversion
- run required validator checks
- show the SDK default/tested Python package versions with
  check-python-dependency --dry-run
- install only the model-framework packages needed for the target workflow,
  then rerun package/import validation
- report final paths and exported variables
```

After QAIRT setup completes, run the `qaiappbuilder_linux_install_sop.md` flow.

## QGenie 设置（Codex provider）

QGenie 用于 Codex provider 调用，与 QAIRT、`qai-appbuilder` 和 Ubuntu Linux 依赖检查相互独立。若 Codex 通过 provider-auth 子进程取得 token，必须确保该子进程能找到 QGenie CLI 配置和认证文件；仅在外层 shell 执行一次认证命令并不能证明 headless Codex 可用。

在 Codex 的运行时配置中指定 QGenie provider 和认证命令：

```toml
model_provider = "qgenie"

[model_providers.qgenie]
name = "QGenie"
base_url = "https://qgenie-api.qualcomm.com/v1"
wire_api = "responses"
supports_websockets = true

[model_providers.qgenie.auth]
command = "qgenie"
args = ["codex-auth-token"]
refresh_interval_ms = 300000
timeout_ms = 10000
```

在 clean-room 或 `env -i` 环境中，给 QGenie wrapper 固定有效的 `HOME` 和 `XDG_CONFIG_HOME`：

```bash
export HOME="/path/to/qgenie-home"
export XDG_CONFIG_HOME="$HOME/.config"
exec /path/to/qgenie-real "$@"
```

将 `$HOME/.config/qgenie-cli/` 下的最小必要配置和认证文件以只读方式提供给隔离环境，或显式传入已有的 `QGENIE_API_KEY`。不要把 API key 写入 prompt、日志或仓库。用与 provider 子进程等价的环境验证：

```bash
env -i PATH="$PATH" HOME="$HOME" XDG_CONFIG_HOME="$XDG_CONFIG_HOME" \
  qgenie codex-auth-token >/dev/null
echo "qgenie_auth_probe=$?"
```

若出现 `Config file not found and QGENIE_API_KEY not set`，这是 QGenie provider auth 环境配置问题，不是 `libc++-dev` 或其他 QAIRT Linux 依赖缺失；应先修正配置继承，再启动 headless Codex。

## Filled Values for `QAIRT_SETUP_SOP_linux.md` (Last Time)

Use this ready prompt to repeat the same QAIRT setup inputs:

```text
Set up QAIRT for Ubuntu 24.04 by following /home/ubuntu/Test/QAIRT_SETUP_SOP_linux.md exactly.

Use these values:
1) QAIRT version: 2.50.0.260828
2) target: local
3) install path: /home/ubuntu/Test/qairt

Then run the full SOP and report:
- QAIRT_SDK_ROOT
- venv path
- aienv.sh path
- QAIRT_DEVICE_BIN
- Linux dependency check result and any missing apt packages
- check-python-dependency --dry-run result
- selected framework package versions
- validation result summary
```


### test case
1. X86 linux
2. arm linux ,rb8
3. please note  /home/ubuntu/Test need to change for your environment



# Codex Usage: Run `qaiappbuilder_linux_install_sop.md`

Use this prompt with Codex when you want the same workflow as last time:

```text
Install qaiappbuilder on Ubuntu 24.04 by following /home/ubuntu/Test/qaiappbuilder_linux_install_sop.md exactly.

Before running install/download commands, ask me these 4 items and wait:
1) target device (local or remote host/IP)
2) aienv.sh path
3) Python venv path
4) absolute install path

Then execute end-to-end:
- precheck and source env
- detect SoC/model and DSP_ARCH using qnn-platform-validator
- set PRODUCT_SOC/DSP_ARCH and export ADSP_LIBRARY_PATH + LD_LIBRARY_PATH
- clone/pull qai-appbuilder with recursive submodules
- build wheel (python3.12 setup.py bdist_wheel)
- pip install generated wheel
- run verification commands from SOP
- update aienv.sh exports with detected values
- report final results (version, location, validator stub match)
```

### test case
1. arm linux ,rb8
2. please note  /home/ubuntu/Test need to change for your environment

## Filled Test Values

Use this when you want to test immediately with known values:

```text
Install qaiappbuilder on Ubuntu 24.04 by following /home/ubuntu/Test/qaiappbuilder_linux_install_sop.md exactly.

Use these values:
1) target device: local
2) aienv.sh path: /home/ubuntu/aienv.sh
3) Python venv path: /home/ubuntu/Test/qairt/2.50.0.260828/qairt/2.50.0.260828/pyqairt
4) absolute install path: /home/ubuntu/Test/qaiappbuilder
```

Then execute end-to-end and report:
- installed wheel name
- `pip show` Name/Version/Location
- validator DSP core version and loaded stub

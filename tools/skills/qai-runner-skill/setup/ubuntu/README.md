# Use `QAIRT_SETUP_SOP.md` With Codex

Use this prompt when QAIRT is not fully set up yet (or `aienv.sh` is missing):

```text
Set up QAIRT by following /home/ubuntu/Test/QAIRT_SETUP_SOP.md exactly.

Ask me for any required inputs first, then run end-to-end:
- install/bootstrap QAIRT per SOP
- create or fix aienv.sh
- verify QAIRT env is sourceable
- run required validator checks
- report final paths and exported variables
```

After QAIRT setup completes, run the `qaiappbuilder_install_sop.md` flow.

## Filled Values for `QAIRT_SETUP_SOP.md` (Last Time)

Use this ready prompt to repeat the same QAIRT setup inputs:

```text
Set up QAIRT by following /home/ubuntu/Test/QAIRT_SETUP_SOP.md exactly.

Use these values:
1) QAIRT version: 2.45.0.260326
2) target: local
3) install path: /home/ubuntu/Test/qairt

Then run the full SOP and report:
- QAIRT_SDK_ROOT
- venv path
- aienv.sh path
- QAIRT_DEVICE_BIN
- validation result summary
```


### test case
1. X86 linux
2. arm linux ,rb8
3. please note  /home/ubuntu/Test need to change for your environment



# Codex Usage: Run `qaiappbuilder_install_sop.md`

Use this prompt with Codex when you want the same workflow as last time:

```text
Install qaiappbuilder by following /home/ubuntu/Test/qaiappbuilder_install_sop.md exactly.

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
- build wheel (python3 setup.py bdist_wheel)
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
Install qaiappbuilder by following /home/ubuntu/Test/qaiappbuilder_install_sop.md exactly.

Use these values:
1) target device: local
2) aienv.sh path: /home/ubuntu/aienv.sh
3) Python venv path: /home/ubuntu/Test/qairt/pyqairt
4) absolute install path: /home/ubuntu/Test/qaiappbuilder
```

Then execute end-to-end and report:
- installed wheel name
- `pip show` Name/Version/Location
- validator DSP core version and loaded stub




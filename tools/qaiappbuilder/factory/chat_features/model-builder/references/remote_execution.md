# Remote Device Execution

When `RETMOE_DEVICE_INFO` is configured in `plan.md`, context binary generation,
inference, and validation are executed on the remote target device via SSH rather
than locally.

## `RETMOE_DEVICE_INFO` File Format

Points to a file (text or YAML) containing:

| Field | Description | Example |
|-------|-------------|---------|
| **(a) SSH information** | Host, user, port, key path | `user@192.168.1.100:22`, key: `~/.ssh/id_rsa` |
| **(b) Working folder** | Target directory for execution | `/home/user/qai_models/inception_v3` |
| **(c) Setup script path** | QAIRT env setup script on target | `/opt/qairt/bin/envsetup.sh` |

## SSH Execution Pattern

All remote operations follow this pattern:

```bash
# 1. SSH to the target device
ssh <user>@<host> -p <port> -i <key_path>

# 2. Change to the working folder
cd <working_folder>

# 3. Source the QAIRT environment
source <setup_script_path>

# 4. Execute the operation (context binary / inference / validation)
python <script> <args>
```

## Batch Mode + Remote Device Rules

- If `RETMOE_DEVICE_INFO` is set AND `MODE = batch`, the agent MUST execute remote
  deploy + inference + log collection before finishing. Do NOT stop at local artifact
  generation.
- If the remote target device is unavailable → **Blocking Condition B5**: stop and ask user.
- Use **absolute paths** on the target device, not host-relative paths.
- After remote execution, collect and report logs/results back to the host workspace.

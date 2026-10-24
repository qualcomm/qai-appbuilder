// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — hand-maintained, UTF-8 (no BOM).
// Namespace: builderEnv — model-conversion environment (x64 Python 3.10 venv + QAIRT SDK)
// installer card on the Downloads page and the chat-page banner.
// The key structure MUST be identical in zh-CN / zh-TW (enforced by tsc + parity test).
// =============================================================================

const builderEnv = {
  title: "Model conversion environment",
  ready: "The model conversion environment is installed and ready.",
  missing:
    "The model conversion environment (Python 3.10 + QAIRT SDK) is not installed. It is only needed to convert models; everything else works without it.",
  missingParts: "Missing",
  sizeHint:
    "About 4–5 GB will be downloaded (QAIRT SDK ≈ 2 GB, Python packages ≈ 2.5 GB). It is resumable — you can cancel and continue later.",
  unavailable: "This build cannot install it automatically.",
  installing: "Installing the model conversion environment",
  keepUsing: "You can keep using the app while this runs.",
  failed: "Installation failed",
  failedAt: "Failed at",
  cancelled: "Installation was cancelled.",
  resumeHint: "Downloaded files are kept — retrying continues where it stopped.",
  install: "Install",
  retry: "Retry",
  cancel: "Cancel",
  eta: "ETA",
  options: "Network options",
  mirror: "Download source",
  mirrorAuto: "Automatic (recommended)",
  mirrorOfficial: "Official sources",
  mirrorCn: "China mirrors",
  proxy: "Proxy (optional)",
  proxyInvalid: "The proxy must start with http://, https:// or socks5://",
  log: "Installer output",
  step: {
    python310: "Python 3.10",
    venv: "Virtual environment",
    deps: "Python packages",
    sdk_download: "Downloading QAIRT SDK",
    sdk_extract: "Unpacking QAIRT SDK",
    config: "Writing configuration",
    verify: "Verifying",
  },
  part: {
    config: "configuration file",
    sdk: "QAIRT SDK",
    venv: "Python 3.10 environment",
  },
  error: {
    network: "Network error while downloading. Check the connection, the mirror or the proxy, then retry.",
    disk_space: "Not enough free disk space.",
    uv_missing: "The uv tool is missing. Run setup_env.bat first.",
    python_install: "Python 3.10 could not be installed.",
    venv: "The Python 3.10 environment could not be created.",
    deps: "Installing the Python packages failed.",
    extract: "The QAIRT SDK could not be unpacked.",
    permission: "No permission to write the SDK folder. Run as administrator.",
    verify: "The installed environment did not pass verification.",
    already_running: "Another installation is already running.",
    cancelled: "Cancelled.",
    spawn_failed: "The installer could not be started.",
    unsupported_os: "This can only be installed on Windows.",
    internal: "Unexpected installer error.",
  },
  banner: {
    missing: "Model conversion needs an extra environment that is not installed yet.",
    installing: "Installing the model conversion environment… {percent}%",
    failed: "Installing the model conversion environment failed.",
    cancelled: "The model conversion environment installation was cancelled.",
    goInstall: "Go to Downloads",
    viewProgress: "View progress",
  },
};

export default builderEnv;

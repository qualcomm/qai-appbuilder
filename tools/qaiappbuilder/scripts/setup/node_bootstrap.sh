#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
# Shared Linux Node.js/pnpm bootstrap. Source this file after exporting REPO_ROOT.

if [[ -z "${REPO_ROOT:-}" ]]; then
  echo "[node-bootstrap] ERROR: REPO_ROOT must be set before sourcing node_bootstrap.sh" >&2
  return 1 2>/dev/null || exit 1
fi
export REPO_ROOT

export NVM_DIR="$REPO_ROOT/envs/nvm"
export NPM_CONFIG_PREFIX="$REPO_ROOT/envs/npm-global"
export NPM_CONFIG_CACHE="$REPO_ROOT/envs/npm-cache"
export NPM_CONFIG_USERCONFIG="$REPO_ROOT/envs/npmrc"
export COREPACK_HOME="$REPO_ROOT/envs/corepack"
export PNPM_HOME="$REPO_ROOT/envs/pnpm"
export npm_config_store_dir="$REPO_ROOT/data/pnpm-store"
export npm_config_virtual_store_dir="$REPO_ROOT/data/pnpm-virtual-store"
export PATH="$PNPM_HOME/bin:$PNPM_HOME:$NPM_CONFIG_PREFIX/bin:$PATH"
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export COREPACK_DEFAULT_TO_LATEST=0

# Recent Node releases can route their built-in fetches through the standard
# proxy environment. Leave TLS policy untouched; corporate CA configuration,
# when needed, remains the caller's responsibility.
if [[ -n "${HTTPS_PROXY:-}${https_proxy:-}${HTTP_PROXY:-}${http_proxy:-}" ]]; then
  export NODE_USE_ENV_PROXY=1
fi

mkdir -p \
  "$NVM_DIR" \
  "$NPM_CONFIG_PREFIX/bin" \
  "$NPM_CONFIG_PREFIX/lib" \
  "$NPM_CONFIG_CACHE" \
  "$COREPACK_HOME" \
  "$PNPM_HOME/bin" \
  "$npm_config_store_dir" \
  "$npm_config_virtual_store_dir"
touch "$NPM_CONFIG_USERCONFIG"
chmod 700 \
  "$NVM_DIR" \
  "$NPM_CONFIG_PREFIX" \
  "$NPM_CONFIG_PREFIX/bin" \
  "$NPM_CONFIG_PREFIX/lib" \
  "$NPM_CONFIG_CACHE" \
  "$COREPACK_HOME" \
  "$PNPM_HOME" \
  "$PNPM_HOME/bin" \
  "$npm_config_store_dir" \
  "$npm_config_virtual_store_dir"
chmod 600 "$NPM_CONFIG_USERCONFIG"

# The nvm installer rejects /dev/null as a profile. Give it an inert,
# project-local profile so any line it appends cannot modify a HOME profile.
_nvm_profile="$NVM_DIR/install-profile"
touch "$_nvm_profile"
chmod 600 "$_nvm_profile"

# nvm refuses both installation and Node selection when npm's prefix is set.
# Keep it unset for the complete nvm operation, then restore the local prefix.
_npm_config_prefix="$NPM_CONFIG_PREFIX"
unset NPM_CONFIG_PREFIX
if [[ ! -s "$NVM_DIR/nvm.sh" ]]; then
  echo "[node-bootstrap] Installing nvm into $NVM_DIR..."
  # Download to a file and verify its SHA256 before executing anything —
  # never pipe curl straight into bash (a compromised/MITM'd server, or a
  # retagged release, could inject arbitrary code with no verification step
  # in between). The checksum below is pinned to nvm v0.39.7's install.sh
  # specifically; bumping _nvm_install_version requires recomputing it (e.g.
  # `curl -fsSL <url> | sha256sum`) and reviewing the diff before trusting
  # the new value.
  _nvm_install_version="v0.39.7"
  _nvm_installer_sha256="8e45fa547f428e9196a5613efad3bfa4d4608b74ca870f930090598f5af5f643"
  _nvm_installer="$NVM_DIR/install.sh"
  curl -fsSL \
    "https://raw.githubusercontent.com/nvm-sh/nvm/${_nvm_install_version}/install.sh" \
    -o "$_nvm_installer"
  _nvm_installer_actual_sha256="$(sha256sum "$_nvm_installer" | awk '{print $1}')"
  if [[ "$_nvm_installer_actual_sha256" != "$_nvm_installer_sha256" ]]; then
    echo "[node-bootstrap] ERROR: nvm install.sh checksum mismatch for ${_nvm_install_version}." >&2
    echo "  expected: $_nvm_installer_sha256" >&2
    echo "  got:      $_nvm_installer_actual_sha256" >&2
    echo "  Refusing to execute an unverified script." >&2
    rm -f "$_nvm_installer"
    return 1 2>/dev/null || exit 1
  fi
  PROFILE="$_nvm_profile" bash "$_nvm_installer"
  rm -f "$_nvm_installer"
  unset _nvm_install_version _nvm_installer_sha256 _nvm_installer _nvm_installer_actual_sha256
fi

# shellcheck source=/dev/null
source "$NVM_DIR/nvm.sh" --no-use
if [[ "$(nvm version 22)" == "N/A" ]]; then
  nvm install 22
fi
nvm use 22
export NPM_CONFIG_PREFIX="$_npm_config_prefix"
unset _npm_config_prefix _nvm_profile
export PATH="$PNPM_HOME/bin:$PNPM_HOME:$NPM_CONFIG_PREFIX/bin:$PATH"

_node_major="$(node --version)"
_node_major="${_node_major#v}"
_node_major="${_node_major%%.*}"
if [[ ! "$_node_major" =~ ^[0-9]+$ ]] || (( _node_major < 22 )); then
  echo "[node-bootstrap] ERROR: Node.js >= 22 is required; found $(node --version 2>/dev/null || echo unavailable)" >&2
  return 1 2>/dev/null || exit 1
fi

# Keep Corepack's shims and downloaded package manager entirely project-local.
# Some corporate proxies prevent Corepack's package-manager download even when
# Node proxy support is enabled. In that case, discard its shims and let npm
# install the same pinned pnpm version into the project-local npm prefix.
if corepack enable --install-directory "$PNPM_HOME/bin" \
    && corepack prepare pnpm@11.9.0 --activate; then
  :
else
  echo "[node-bootstrap] Corepack could not prepare pnpm; falling back to project-local npm install." >&2
  rm -f "$PNPM_HOME/bin/pnpm" "$PNPM_HOME/bin/pnpx"
  npm install --global \
    --prefix "$NPM_CONFIG_PREFIX" \
    --cache "$NPM_CONFIG_CACHE" \
    --userconfig "$NPM_CONFIG_USERCONFIG" \
    pnpm@11.9.0
fi

_pnpm_version="$(pnpm --version)"
if [[ "$_pnpm_version" != "11.9.0" ]]; then
  echo "[node-bootstrap] ERROR: pnpm 11.9.0 is required; found ${_pnpm_version:-unavailable}" >&2
  return 1 2>/dev/null || exit 1
fi

_repo_real="$(realpath "$REPO_ROOT")"
_node_path="$(type -P node)"
_pnpm_path="$(type -P pnpm)"
_node_real="$(realpath "$_node_path")"
_pnpm_real="$(realpath "$_pnpm_path")"
case "$_node_path:$_node_real:$_pnpm_path:$_pnpm_real" in
  "$_repo_real"/*:"$_repo_real"/*:"$_repo_real"/*:"$_repo_real"/*) ;;
  *)
    echo "[node-bootstrap] ERROR: Node tooling escaped REPO_ROOT:" >&2
    echo "  node: $_node_path -> $_node_real" >&2
    echo "  pnpm: $_pnpm_path -> $_pnpm_real" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

unset _node_major _pnpm_version _repo_real _node_path _pnpm_path _node_real _pnpm_real

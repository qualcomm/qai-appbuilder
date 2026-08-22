#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#
# preflight_network.sh — is THIS network able to run QAIAppBuilder?
#
# Run this on the target machine, on the target Wi-Fi, BEFORE the event.
# Needs nothing but ``curl``: no venv, no install, no repo state. Safe to
# run repeatedly; it only issues read-only requests with a bogus token.
#
#   bash scripts/preflight_network.sh
#
# On Windows use Git Bash (``curl.exe`` is also fine standalone; see the
# PowerShell equivalents printed at the end of a failing run).
#
# Why this script exists
# ----------------------
# The app depends on exactly two remote endpoints, and one of them answers on
# a NON-STANDARD PORT (8012). Guest / venue / hotel Wi-Fi very often permits
# 80 and 443 only, and the way it refuses everything else is the trap: the TCP
# handshake SUCCEEDS and the connection is reset only once a request has been
# sent. So "the port is open" and even "curl connected" are both worthless as
# signals — the check has to prove a COMPLETE HTTP RESPONSE came back.
#
# That is what bit a Radxa board on a lab SSID: TCP connect fine, then
# ``Recv failure: Connection reset by peer``. Worse, the broker had already
# verified the login and MINTED (and charged quota for) a token — the response
# just never made it home, so the app looked signed in with no usable
# credential and every chat request 401'd.
#
# Captive portals are the second trap: they answer 200 with their own HTML for
# any URL, so a 200 does not prove you reached us. Check 3 is the antidote —
# it asserts a **401** from our own endpoint, which no portal will ever forge.
# ---------------------------------------------------------------------
set -uo pipefail

# Endpoints must match the deployment config:
#   QAI Service  → factory/user_config.toml [forge.qai_service] base_url
#   Okta issuer  → AuthSettings.issuer (src/qai/platform/config/settings.py)
SERVICE_ORIGIN="${QAI_PREFLIGHT_SERVICE:-http://qai-service.qualcomm.com:8012}"
OKTA_ISSUER="${QAI_PREFLIGHT_ISSUER:-https://account.qualcomm.com/oauth2/ausvbhs40oLZ6EsJ6697}"
TIMEOUT="${QAI_PREFLIGHT_TIMEOUT:-10}"

PASS=0
FAIL=0
LOCAL_IP=""
TLS_INTERCEPTED=0

c_ok=""; c_bad=""; c_warn=""; c_off=""
if [[ -t 1 ]]; then c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_warn=$'\033[33m'; c_off=$'\033[0m'; fi

say_pass() { PASS=$((PASS + 1)); printf '  %sPASS%s  %s\n' "$c_ok" "$c_off" "$1"; }
say_fail() { FAIL=$((FAIL + 1)); printf '  %sFAIL%s  %s\n' "$c_bad" "$c_off" "$1"; }
say_warn() { printf '  %sWARN%s  %s\n' "$c_warn" "$c_off" "$1"; }

# probe <label> <expected_http_code> <curl args...>
#
# Reports PASS only when curl exits 0 AND the status line matches. ``%{http_code}``
# is 000 unless a response actually arrived, which is precisely the
# connected-but-reset case we must catch.
probe() {
  local label="$1" expect="$2"; shift 2
  local err out rc code msg
  err="$(mktemp)"
  out="$(curl -sS -m "$TIMEOUT" -o /dev/null -w '%{http_code}|%{local_ip}' "$@" 2>"$err")"
  rc=$?
  msg="$(tr -d '\r' <"$err" | tail -1)"
  rm -f "$err"
  code="${out%%|*}"
  [[ -z "$LOCAL_IP" ]] && LOCAL_IP="${out##*|}"

  if [[ $rc -ne 0 ]]; then
    say_fail "$label — curl($rc): ${msg:-no detail}"
    case $rc in
      6)  echo "         → DNS 解析失败：这个网络的 DNS 拿不到该主机" ;;
      7)  echo "         → 连不上：端口被丢弃 / 无路由 / 没有服务在监听" ;;
      28) echo "         → 超时 ${TIMEOUT}s：被静默丢包（防火墙 DROP 而非 REJECT）" ;;
      35|60)
          echo "         → TLS 失败：可能是会场做了 HTTPS 中间人解密"
          TLS_INTERCEPTED=1 ;;
      56) echo "         → 连接建立后被重置(RST)：这就是"端口看似开放实则被拦"的典型形态" ;;
    esac
    return 1
  fi
  if [[ "$code" != "$expect" ]]; then
    say_fail "$label — 收到 HTTP $code，期望 $expect"
    [[ "$code" == "200" && "$expect" == "401" ]] && \
      echo "         → 200 而非 401，强烈提示被 captive portal / 透明代理劫持"
    return 1
  fi
  say_pass "$label — HTTP $code"
  return 0
}

# resolve4 <host> — 打印该主机的 IPv4 地址，逗号分隔；无结果则返回非 0。
#
# nslookup 的输出以「本机用的 DNS 服务器」块开头，其中同样有一行 ``Address:``。
# 直接 grep Address 会把 DNS 服务器的地址误当成解析结果（Windows 上实测如此）。
# 因此先用 sed 删掉首个空行之前的所有内容，把服务器块整段丢弃，再取答案区的
# ``Address:`` / ``Addresses:``（后者是多地址时的写法，后续地址为缩进续行）。
resolve4() {
  local h="$1" ips
  if command -v getent >/dev/null 2>&1; then
    ips="$(getent ahostsv4 "$h" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd, -)"
    [[ -n "$ips" ]] && { printf '%s\n' "$ips"; return 0; }
  fi
  if command -v nslookup >/dev/null 2>&1; then
    ips="$(nslookup "$h" 2>/dev/null | tr -d '\r' | sed '1,/^$/d' \
           | awk -F':[[:space:]]*' '
               /^Address(es)?:/ { print $2; inaddr = 1; next }
               inaddr && /^[[:space:]]+[0-9.]+$/ { gsub(/[[:space:]]/, ""); print; next }
               { inaddr = 0 }' \
           | grep -E '^[0-9]+(\.[0-9]+){3}$' | sort -u | paste -sd, -)"
    [[ -n "$ips" ]] && { printf '%s\n' "$ips"; return 0; }
  fi
  return 1
}

svc_host="${SERVICE_ORIGIN#*://}"; svc_host="${svc_host%%/*}"
okta_host="${OKTA_ISSUER#*://}"; okta_host="${okta_host%%/*}"
# 端口从 URL 推导（而非写死 8012），这样换部署地址后失败提示依然准确。
svc_port="${svc_host##*:}"
[[ "$svc_port" == "$svc_host" ]] && svc_port=80

echo
echo "QAIAppBuilder 网络预检"
echo "  QAI Service : $SERVICE_ORIGIN"
echo "  Okta issuer : $OKTA_ISSUER"
echo "  超时        : ${TIMEOUT}s"
echo

# ── 1. DNS ───────────────────────────────────────────────────────────────────
# 这一项的用途是拿到「这个网络把域名解析成了什么」，以便和一台已知可用的机器
# 逐字对比 —— 解析到不同地址本身就是一条重要线索。真正的连通性由 2/3/4 判定。
echo "[1/4] DNS 解析"
for h in "${svc_host%%:*}" "$okta_host"; do
  if ips="$(resolve4 "$h")"; then
    say_pass "$h → $ips"
  else
    say_fail "$h 无法解析"
  fi
done
echo

# ── 2. QAI Service 根路径：证明 8012 端口能收到完整响应 ──────────────────────
echo "[2/4] QAI Service 端口 8012 可达性（关键项）"
probe "GET $SERVICE_ORIGIN/" 200 "$SERVICE_ORIGIN/"
echo

# ── 3. exchange 端点：401 是无法被劫持伪造的权威证据 ─────────────────────────
# 用约 1.8KB 的假 token，让请求体尺寸接近真实 id_token —— 顺带排除只在大包上
# 出问题的 MTU / 分片故障。
echo "[3/4] exchange 端点（权威项：必须是 401）"
FAKE_TOKEN="e$(printf '%01800d' 0 | tr '0' 'A')"
probe "POST $SERVICE_ORIGIN/api/auth/exchange" 401 \
  -X POST -H 'Content-Type: application/json' \
  --data-binary "{\"id_token\":\"$FAKE_TOKEN\"}" \
  "$SERVICE_ORIGIN/api/auth/exchange"
echo

# ── 4. Okta：SSO 登录必须能到 443 ────────────────────────────────────────────
# 注意 auth.ssl_verify 默认 False（公司 CA 场景），所以 app 能容忍 TLS 中间人，
# 而 curl 默认不能。证书失败时用 -k 复验：-k 通过即 app 无碍。
echo "[4/4] Okta 登录端点 (443)"
if ! probe "GET $OKTA_ISSUER/v1/keys" 200 "$OKTA_ISSUER/v1/keys"; then
  if [[ $TLS_INTERCEPTED -eq 1 ]]; then
    if probe "GET .../v1/keys （忽略证书 -k 复验）" 200 -k "$OKTA_ISSUER/v1/keys"; then
      say_warn "会场对 HTTPS 做了解密，但 app 的 auth.ssl_verify 默认为 false，登录不受影响"
      FAIL=$((FAIL - 1))   # -k 通过即视为可用
    fi
  fi
fi
echo

# ── 结论 ─────────────────────────────────────────────────────────────────────
echo "──────────────────────────────────────────────────────────────"
if [[ $FAIL -eq 0 ]]; then
  printf '%s可以部署%s：%d 项全部通过。\n' "$c_ok" "$c_off" "$PASS"
  echo "（仅代表网络就绪；真实登录仍需浏览器完成一次 Okta 交互。）"
  exit 0
fi

printf '%s不可部署%s：%d 项失败。\n' "$c_bad" "$c_off" "$FAIL"
cat <<EOF

本机出口地址：${LOCAL_IP:-未知}
$(command -v ip >/dev/null && ip -br addr 2>/dev/null | sed 's/^/  /')

向会场网络方提需求时，请具体说明：
  放通 ${LOCAL_IP:-本机} → ${svc_host} 的 TCP 出站
  ${svc_host%%:*} 用的是非标端口 ${svc_port}，多数访客网络只放 80/443
  以及 ${okta_host}:443（SSO 登录）

临时绕行（本次已验证有效）：改用手机热点。

若只能用会场网络且 8012 无法放通，则需要在可达网络侧架一个反向代理并把
factory/user_config.toml 的两处 base_url 指过去（迁移器会在下次启动时
同步 data/ 下的运行时副本）。
EOF
exit 1

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Hexagon/HTP version + target-SoC tables shared by every model-builder script.

Single source of truth. Never re-implement a second copy of these tables --
a duplicated SDK-directory mapping is exactly what let ``aarch64-linux-gcc``
(a directory that exists in NO QAIRT SDK) survive in four separate files.

AUTHORITATIVE SDK SOURCES -- grep these when a platform is missing below.
Paths are relative to ``<qairt_sdk_root>/`` (``qairt_sdk_root`` from
``data/config/qairt_env.json``); they are HTML, so strip tags first:
``re.sub(r"<[^>]*>", " ", raw)`` + ``html.unescape`` (a long regex over raw
HTML gives false negatives).

  1. ``docs/QAIRT-Docs/QNN/general/overview.html``
     section "Supported Snapdragon devices" -- THE authoritative table.
     Columns: Snapdragon Device/Chip | Supported Toolchains | SOC Model |
     Hexagon Arch | LPAI Arch.  The "SOC Model" number is what ``soc_model`` /
     ``soc_id`` in an HTP config takes.  NOTE there are TWO tables with that
     same heading: consumer/mobile/IoT first, then a 4-column automotive one.
  2. ``docs/QAIRT-Docs/QNN/general/htp/htp_backend.html``
     section "QNN HTP TARGET CONFIG TABLE" -- Target Name | dsp_arch | soc_id.
     Automotive subset only, but states dsp_arch and soc_id side by side.
  3. ``include/QNN/QnnTypes.h`` enum ``Qnn_SocModel_t`` -- the only
     machine-readable list, but marked ``@deprecated`` ("will no longer be
     updated") and it STOPS AT 87, so it must NOT be used as an upper bound:
     overview.html already lists 88/90/96/105.  Prefer source 1.

WHY THIS MODULE EXISTS: hardcoding a wrong soc_model or dsp_arch does not
fail the build.  ``qnn-context-binary-generator`` returns rc=0 and emits a
``.bin`` that only fails when the target device loads it -- as a bare
``err:14``, with nothing pointing at the mismatch.  So: values come from the
SDK table or from the caller, never from a guess.
"""
from __future__ import annotations

# Hexagon versions this SDK generation can build HTP context binaries for.
# Verified against QAIRT 2.48.40 by listing lib/hexagon-v*/unsigned/ -- every
# one of these ships libQnnHtpV<N>Skel.so.
#
# v66 is deliberately ABSENT: lib/hexagon-v66/unsigned/ ships
# libQnnDspV66Skel.so, not an HTP skel. V66 parts run on the legacy DSP
# backend (QnnDsp), a different backend library from QnnHtp -- so v66 is not
# a valid --htp_version even though SOC_TARGETS records V66 parts.
#
# Availability is NOT uniform across host arches; the Stub (host-side proxy)
# is what limits a given host:
#   lib/aarch64-oe-linux-gcc11.2  -> v68 v69 v73 v75 v79 v81   (all)
#   lib/aarch64-windows-msvc      -> v68 v73 v81               (subset!)
#   lib/arm64x-windows-msvc       -> v73 v81
#   lib/x86_64-linux-clang        -> none (cross-compile host: no Stub->Skel
#                                   chain is needed, libHtpPrepare.so does
#                                   the offline graph prepare)
HTP_VERSIONS: tuple[str, ...] = ("v68", "v69", "v73", "v75", "v79", "v81")

# Hexagon versions that ship a DSP catalog (.cat) in QAIRT 2.48.40.
# Only these two: lib/hexagon-v{73,81}/unsigned/libqnnhtpv{73,81}.cat.
# v68/v69/v75/v79 ship the Skel but NO .cat -- so a missing .cat is normal
# for them, NOT a damaged SDK. See _copy_and_verify_htp_runtime_files.
HTP_VERSIONS_WITH_CAT: frozenset[str] = frozenset({"v73", "v81"})

# Default --htp_version for every CLI in this package. Kept here (not repeated
# as a literal in each argparse call) for the same reason as the table above:
# the v81->SM8750 defect survived precisely because a value was copied into
# several scripts and only some copies got fixed.
# v73 is chosen for backwards compatibility, NOT because it is newest -- it is
# what these scripts have always defaulted to (X Elite / SC8380XP class).
DEFAULT_HTP_VERSION: str = "v73"
assert DEFAULT_HTP_VERSION in HTP_VERSIONS, (
    "DEFAULT_HTP_VERSION must be one of HTP_VERSIONS"
)

# ---------------------------------------------------------------------------
# Target SoC table: name -> (SOC Model number, Hexagon/dsp_arch or "")
#
# Transcribed from overview.html § Supported Snapdragon devices (both tables),
# QAIRT 2.48.40, and verified entry-by-entry against it.  This is a
# CONVENIENCE SUBSET of the commonly targeted parts, not the whole table --
# when a platform is absent, read source 1 above rather than extrapolating.
# A name here is directly usable as the qairt-converter / qairt-quantizer
# ``--target_soc_model`` value; the number is what goes into an HTP config's
# ``soc_model``/``soc_id``.
#
# An EMPTY dsp_arch means "the SDK states the SOC Model number but no Hexagon
# arch" -- the caller must then pass --dsp_arch explicitly. Do not fill these
# in by inference from a sibling part number.
#
# Beware two traps this table encodes deliberately:
#   * SM8750 (Snapdragon 8 Elite) is v79, NOT v81.  Only SM8850 (8 Elite
#     Gen 5) and SC8480XP (X2 Elite) are the v81 mobile/compute parts.
#   * SOC Model numbers are NOT unique: 52 covers SA8650/SA8775/SA8255,
#     67 covers SA7255/SA8610/SA8620, 21 covers SM8250/QRB5165.
# ---------------------------------------------------------------------------
SOC_TARGETS: dict[str, tuple[int, str]] = {
    # --- Compute / Windows-on-Snapdragon ---
    "SC8480XP": (88, "v81"),   # Snapdragon X2 Elite Extreme
    "SC8380XP": (60, "v73"),   # Snapdragon X Elite (8cx Gen 4)
    "SC8280X":  (37, "v68"),   # Snapdragon 8cx Gen 3
    "SC7280X":  (44, "v68"),   # Snapdragon 7c Gen 2
    # --- Mobile flagships ---
    "SM8850":   (87, "v81"),   # SD 8 Elite Gen 5
    "SM8750":   (69, "v79"),   # SD 8 Elite      <- v79, not v81
    "SM8650":   (57, "v75"),   # SD 8 Gen 3
    "SM8550":   (43, "v73"),   # SD 8 Gen 2
    "SM8475":   (42, "v69"),   # SD 8+ Gen 1
    "SM8450":   (36, "v69"),   # SD 8 Gen 1
    "SM8350":   (30, "v68"),   # SD 888 / 888+
    "SM8250":   (21, "v66"),   # SD 865
    "SM8635":   (68, "v73"),   # SD 8s Gen 3
    "SM7675":   (70, "v73"),   # SD 7+ Gen 3
    # --- IoT / compute modules ---
    "QCS8550":  (66, "v73"),
    "QCS9100":  (77, "v73"),
    "QCM6690":  (78, "v73"),
    "QCM6490":  (93, "v68"),
    "QCS8625":  (90, "v75"),
    "QRB5165":  (21, "v66"),
    # --- Wearable / XR ---
    "SW6100":   (96, "v81"),   # SD W6 Gen 1
    "SAR2130P": (46, "v73"),   # AR2 Gen 1
    "SXR2230P": (53, "v69"),   # XR2 Gen 2
    # --- Automotive (second overview.html table + htp_backend.html) ---
    "SA8797":   (72, "v81"),
    "SA7255":   (67, "v75"),
    "SA8610":   (67, "v75"),
    "SA8620":   (67, "v75"),
    "SA8650":   (52, "v73"),
    "SA8775":   (52, "v73"),
    "SA8255":   (52, "v73"),
    "SA8295":   (39, "v68"),
    "SA8540":   (62, "v68"),
    # --- Networking / IPQ ---
    # SOC Model numbers from include/QNN/QnnTypes.h (Qnn_SocModel_t); these
    # parts appear in NO overview.html device table, so their Hexagon arch is
    # NOT documented in this SDK -- left empty on purpose rather than guessed.
    "IPQ9574":  (79, ""),
    "IPQ5404":  (80, ""),
    "IPQ5424":  (81, ""),
    # IPQ9650 "Juhu": absent from every QAIRT 2.48.40 doc and header. The
    # pairing below is what the platform owner reported for their own part, so
    # it is recorded as PROVENANCE_CUSTOMER (see below) rather than omitted --
    # a caller naming their platform must not have to hand-carry raw numbers.
    "IPQ9650":  (117, "v81"),
}

# Where each row's numbers come from. Rows NOT listed here are SDK-documented
# (overview.html for named devices, Qnn_SocModel_t for the IPQ ids) and are
# cross-checked against those files by the test suite.
#
# A CUSTOMER row is authoritative for that platform -- the platform owner knows
# their own silicon -- but it cannot be verified against this SDK, so the SDK
# cross-checks skip it BY THIS MARKER, never by a hardcoded name. Adding the
# next such part means adding it here, not editing a test.
PROVENANCE_CUSTOMER: frozenset[str] = frozenset({"IPQ9650"})

# Representative SoC per Hexagon version, for --soc_optimized (which needs ONE
# name for --target_soc_model). Compute parts win where one exists, because
# that is this workflow's primary target; otherwise the mobile flagship.
#
# Keys must stay within HTP_VERSIONS (no v66: DSP backend, not HTP).
#
# This choice bakes SoC-specific graph optimisation into the DLC. If you need a
# different part of the same Hexagon version, pass --soc_model / --dsp_arch for
# the exact target instead of relying on this default.
_HTP_VERSION_REPRESENTATIVE: dict[str, str] = {
    "v68": "SC8280X",
    "v69": "SM8450",
    "v73": "SC8380XP",
    "v75": "SM8650",
    "v79": "SM8750",
    "v81": "SC8480XP",
}


def normalize_htp_version(value: str) -> str:
    """Return a canonical ``vNN`` tag, or "" when unrecognised.

    Accepts "v81", "V81", "81" -- users and configs write all three.
    """
    if not value:
        return ""
    tag = str(value).strip().lower()
    if not tag.startswith("v"):
        tag = "v" + tag
    return tag if tag in HTP_VERSIONS else ""


def resolve_target_platform(name: str) -> tuple[int, str, str]:
    """Resolve a platform NAME to ``(soc_model, dsp_arch, error)``.

    ``error`` is non-empty when the name cannot be turned into a usable target;
    the caller must then fail rather than proceed, because a missing HTP target
    yields a generic .bin that only fails once it reaches the device (rc=0 here,
    err:14 there).  Shared by every CLI so the two entry points cannot drift.
    """
    entry = soc_target(name)
    if entry is None:
        return 0, "", (
            f"Unknown --target_platform {name!r}.\n"
            f"        Known platforms: {', '.join(sorted(SOC_TARGETS))}\n"
            f"        For a platform not listed, pass --soc_model and "
            f"--dsp_arch explicitly."
        )
    soc_id, arch = entry
    if not arch:
        # An id with no arch is an SDK-documented part whose Hexagon version the
        # SDK does not state. Inferring it from a sibling part number is the
        # exact guess that produces a wrong-SoC binary.
        return soc_id, "", (
            f"{name} has a documented soc_model ({soc_id}) but no documented "
            f"dsp_arch; pass --dsp_arch explicitly."
        )
    return soc_id, arch, ""


def htp_version_to_target_soc_model(htp_version: str) -> str:
    """Map an HTP version to a ``--target_soc_model`` name for the converter.

    Returns "" when unknown -- the caller must then skip --target_soc_model
    rather than substitute a guess.
    """
    return _HTP_VERSION_REPRESENTATIVE.get(normalize_htp_version(htp_version), "")


def soc_target(name: str) -> tuple[int, str] | None:
    """Look up ``(soc_model, dsp_arch)`` by SoC name; None when not in the table.

    None means "not in our convenience subset" -- NOT "unsupported". Read
    overview.html § Supported Snapdragon devices for the full list.
    """
    if not name:
        return None
    return SOC_TARGETS.get(str(name).strip().upper())


def socs_for_htp_version(htp_version: str) -> list[str]:
    """SoC names in the table that use this Hexagon version (sorted)."""
    tag = normalize_htp_version(htp_version)
    if not tag:
        return []
    return sorted(n for n, (_id, arch) in SOC_TARGETS.items() if arch == tag)


def hexagon_runtime_files(htp_version: str) -> list[tuple[str, bool]]:
    """Device-side Hexagon files for a version, as ``(sdk_rel_path, required)``.

    The Skel is required (every version ships one). The ``.cat`` is required
    only for versions that actually ship one -- see HTP_VERSIONS_WITH_CAT.
    """
    tag = normalize_htp_version(htp_version)
    if not tag:
        return []
    num = tag[1:]
    return [
        (f"lib/hexagon-{tag}/unsigned/libqnnhtp{tag}.cat",
         tag in HTP_VERSIONS_WITH_CAT),
        (f"lib/hexagon-{tag}/unsigned/libQnnHtpV{num}Skel.so", True),
    ]


def host_stub_name(htp_version: str, is_windows: bool) -> str:
    """Host-side Stub library name for a version ("" when version unknown)."""
    tag = normalize_htp_version(htp_version)
    if not tag:
        return ""
    num = tag[1:]
    return f"QnnHtpV{num}Stub.dll" if is_windows else f"libQnnHtpV{num}Stub.so"

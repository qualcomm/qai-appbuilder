# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation of the ``install`` pipeline orchestrator (PR-064)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qai.platform.logging import get_logger

from .._common.modes import Mode
from .._common.report import InitReport, InitReportEntry

_LOGGER = get_logger("qai.init.install")

# Canonical stage order. The CLI's ``--skip`` flag accepts any of these
# names. Order matters for ``apply`` — each stage may consume the
# previous stage's outputs.
STAGE_NAMES: tuple[str, ...] = (
    "compile_factory",
    "data_dir",
    "seed_defaults",
    "secret_bootstrap",
    "edition_secrets",
)


@dataclass
class StageResult:
    """Captured outcome of one pipeline stage."""

    name: str
    exit_code: int
    report_summary: str
    error_count: int


@dataclass
class RunResult:
    mode: Mode
    stages: list[StageResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if all(s.exit_code == 0 for s in self.stages) else 1

    def render_summary(self) -> str:
        """Return a multi-stage table + overall result."""
        # Defer the data_root / factory_root summary header to the
        # caller (the CLI prints them); here we only render the stage
        # table because the orchestrator does not own those paths in
        # the dataclass.
        lines: list[str] = []
        ok_overall = self.exit_code == 0
        lines.append(f"# install ({self.mode})")
        lines.append(f"  stages        = {len(self.stages)}")
        lines.append(f"  overall       = {'ok' if ok_overall else 'FAIL'}")
        lines.append("")
        # Pad stage names to a common width for legibility.
        width = max((len(s.name) for s in self.stages), default=0)
        for idx, stage in enumerate(self.stages, start=1):
            lines.append(
                f"  [{idx}] {stage.name.ljust(width)}  "
                f"exit={stage.exit_code}  errors={stage.error_count}"
            )
        if not self.stages:
            lines.append("  (no stages executed)")
        return "\n".join(lines) + "\n"


def run(
    *,
    mode: Mode,
    factory_source: Path | None,
    factory_source_data: Path | None,
    factory_root: Path,
    data_root: Path,
    sql_migrations_dir: Path,
    secret_backend: Any = None,
    timestamp: str | None = None,
    skip_stages: tuple[str, ...] = (),
    is_internal: bool | None = None,
) -> RunResult:
    """Run the four-stage install pipeline.

    Args:
        mode: ``dry-run`` / ``apply`` / ``verify``.
        factory_source: legacy ``config/`` dir for PR-060. When
            ``None`` (or non-existent), the PR-060 stage is skipped
            with a note — production installs ship a pre-built
            ``factory/`` bundle.
        factory_source_data: legacy ``data/`` dir for PR-060. Same
            skip rule as ``factory_source``.
        factory_root: target / source of the ``factory/`` bundle
            (PR-060 writes here; PR-061..63 read from here).
        data_root: target ``data/`` directory.
        sql_migrations_dir: location of the ``NNN_*.sql`` migration
            files (consumed by PR-061).
        secret_backend: opaque backend handle passed through to
            PR-063. ``None`` lets PR-063 build the default backend.
        timestamp: optional UTC timestamp string for deterministic
            backup directory names in PR-060 apply mode.
        skip_stages: names of stages to skip entirely (e.g.
            ``("compile_factory",)`` for production installs that
            already ship the bundle).
        is_internal: explicit edition gate for the ``edition_secrets``
            stage. ``None`` lets that stage resolve via
            ``Settings.is_internal`` (degrades to internal for the dev
            source tree). The CLI resolves this from
            ``<data_root.parent>/build_info.json`` and passes it
            explicitly so the gate is correct on external artifacts
            without relying on the edition package being physically
            absent (defence layer 1, not just layer 2).

    Returns:
        :class:`RunResult` with one :class:`StageResult` per stage
        that ran.
    """
    invalid = [s for s in skip_stages if s not in STAGE_NAMES]
    if invalid:
        raise ValueError(
            f"unknown stage name(s) in skip_stages: {invalid}; "
            f"valid names: {list(STAGE_NAMES)}"
        )

    skip = set(skip_stages)
    result = RunResult(mode=mode)

    # Stage 1: compile_factory -----------------------------------------------
    auto_skip_compile = (
        factory_source is None
        or not factory_source.exists()
        or factory_source_data is None
        or not factory_source_data.exists()
    )
    if "compile_factory" in skip:
        _push_skipped(
            result,
            name="compile_factory",
            mode=mode,
            note="skipped via --skip",
        )
    elif auto_skip_compile:
        _push_skipped(
            result,
            name="compile_factory",
            mode=mode,
            note=(
                "no source config; assuming factory_root is pre-built"
            ),
        )
    else:
        ok = _run_stage_compile_factory(
            mode=mode,
            source_config_dir=factory_source,  # type: ignore[arg-type]
            source_data_dir=factory_source_data,  # type: ignore[arg-type]
            dest_root=factory_root,
            timestamp=timestamp,
            result=result,
        )
        if mode == "apply" and not ok:
            return result

    # Stage 2: data_dir -----------------------------------------------------
    if "data_dir" in skip:
        _push_skipped(
            result,
            name="data_dir",
            mode=mode,
            note="skipped via --skip",
        )
    else:
        ok = _run_stage_data_dir(
            mode=mode,
            data_root=data_root,
            factory_root=factory_root,
            sql_migrations_dir=sql_migrations_dir,
            result=result,
        )
        if mode == "apply" and not ok:
            return result

    # Stage 3: seed_defaults ---------------------------------------------------
    if "seed_defaults" in skip:
        _push_skipped(
            result,
            name="seed_defaults",
            mode=mode,
            note="skipped via --skip",
        )
    else:
        ok = _run_stage_seed_defaults(
            mode=mode,
            data_root=data_root,
            factory_root=factory_root,
            result=result,
        )
        if mode == "apply" and not ok:
            return result

    # Stage 4: secret_bootstrap ------------------------------------------------
    if "secret_bootstrap" in skip:
        _push_skipped(
            result,
            name="secret_bootstrap",
            mode=mode,
            note="skipped via --skip",
        )
    else:
        ok = _run_stage_secret_bootstrap(
            mode=mode,
            data_root=data_root,
            factory_root=factory_root,
            secret_backend=secret_backend,
            result=result,
        )
        if mode == "apply" and not ok:
            return result

    # Stage 5: edition_secrets (internal-only) ---------------------------------
    # Provisions internal-edition factory cloud-provider API keys (e.g.
    # the default provider) into the SecretStore. No-op on external editions (gated
    # behind ``settings.is_internal`` inside the stage) and when no edition
    # factory keys are declared. Runs AFTER secret_bootstrap so the namespace
    # placeholder (if any) is already registered and this stage can overwrite
    # the empty placeholder with the real factory value.
    if "edition_secrets" in skip:
        _push_skipped(
            result,
            name="edition_secrets",
            mode=mode,
            note="skipped via --skip",
        )
    else:
        ok = _run_stage_edition_secrets(
            mode=mode,
            data_root=data_root,
            factory_root=factory_root,
            secret_backend=secret_backend,
            is_internal=is_internal,
            result=result,
        )
        if mode == "apply" and not ok:
            return result

    return result


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------


def _run_stage_compile_factory(
    *,
    mode: Mode,
    source_config_dir: Path,
    source_data_dir: Path,
    dest_root: Path,
    timestamp: str | None,
    result: RunResult,
) -> bool:
    # Lazy import: keeps the dependency graph clean at module-load time
    # and matches the "import at first use" pattern used by the other
    # init runners.
    from tools.build.factory_compiler import run as _run

    _LOGGER.info(
        "install.stage.start",
        stage="compile_factory",
        mode=mode,
    )
    sub = _run(
        mode=mode,
        source_config_dir=source_config_dir,
        source_data_dir=source_data_dir,
        dest_root=dest_root,
        timestamp=timestamp,
    )
    summary = sub.report.render_summary()
    error_count = len(sub.report.errors)
    result.stages.append(StageResult(
        name="compile_factory",
        exit_code=sub.exit_code,
        report_summary=summary,
        error_count=error_count,
    ))
    _LOGGER.info(
        "install.stage.end",
        stage="compile_factory",
        exit_code=sub.exit_code,
        errors=error_count,
    )
    return sub.exit_code == 0


def _run_stage_data_dir(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path,
    sql_migrations_dir: Path,
    result: RunResult,
) -> bool:
    from tools.init.data_dir import run as _run

    _LOGGER.info("install.stage.start", stage="data_dir", mode=mode)
    sub = _run(
        mode=mode,
        data_root=data_root,
        factory_root=factory_root,
        sql_migrations_dir=sql_migrations_dir,
    )
    summary = sub.report.render_summary()
    error_count = len(sub.report.errors)
    result.stages.append(StageResult(
        name="data_dir",
        exit_code=sub.exit_code,
        report_summary=summary,
        error_count=error_count,
    ))
    _LOGGER.info(
        "install.stage.end",
        stage="data_dir",
        exit_code=sub.exit_code,
        errors=error_count,
    )
    return sub.exit_code == 0


def _run_stage_seed_defaults(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path,
    result: RunResult,
) -> bool:
    from tools.init.seed_defaults import run as _run

    _LOGGER.info("install.stage.start", stage="seed_defaults", mode=mode)
    sub = _run(
        mode=mode,
        data_root=data_root,
        factory_root=factory_root,
    )
    summary = sub.report.render_summary()
    error_count = len(sub.report.errors)
    result.stages.append(StageResult(
        name="seed_defaults",
        exit_code=sub.exit_code,
        report_summary=summary,
        error_count=error_count,
    ))
    _LOGGER.info(
        "install.stage.end",
        stage="seed_defaults",
        exit_code=sub.exit_code,
        errors=error_count,
    )
    return sub.exit_code == 0


def _run_stage_secret_bootstrap(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path,
    secret_backend: Any,
    result: RunResult,
) -> bool:
    # Runtime import + clear ImportError surfacing if PR-063 isn't on
    # the module path yet (defensive — current tree has it landed but
    # the orchestrator was specced to tolerate the parallel sub-agent
    # gap window).
    try:
        from tools.init.secret_bootstrap import run as _run
    except ImportError as exc:  # pragma: no cover — requires missing PR-063
        report = InitReport(
            initialiser="secret_bootstrap",
            mode=mode,
            data_root=str(data_root),
            factory_root=str(factory_root),
        )
        report.add_error(
            "tools.init.secret_bootstrap not importable; "
            f"PR-063 must be landed for install to complete: {exc}"
        )
        result.stages.append(StageResult(
            name="secret_bootstrap",
            exit_code=1,
            report_summary=report.render_summary(),
            error_count=len(report.errors),
        ))
        return False

    _LOGGER.info(
        "install.stage.start",
        stage="secret_bootstrap",
        mode=mode,
    )
    sub = _run(
        mode=mode,
        data_root=data_root,
        factory_root=factory_root,
        secret_backend=secret_backend,
    )
    summary = sub.report.render_summary()
    error_count = len(sub.report.errors)
    result.stages.append(StageResult(
        name="secret_bootstrap",
        exit_code=sub.exit_code,
        report_summary=summary,
        error_count=error_count,
    ))
    _LOGGER.info(
        "install.stage.end",
        stage="secret_bootstrap",
        exit_code=sub.exit_code,
        errors=error_count,
    )
    return sub.exit_code == 0


def _run_stage_edition_secrets(
    *,
    mode: Mode,
    data_root: Path,
    factory_root: Path,
    secret_backend: Any,
    result: RunResult,
    is_internal: bool | None = None,
) -> bool:
    # Runtime import keeps the dependency graph lazy and tolerates an
    # external artifact that physically excluded the edition stage (the
    # ImportError path below records a clean skip, not a failure).
    try:
        from tools.init.edition_secrets import run as _run
    except ImportError as exc:  # pragma: no cover — only if pkg excluded
        report = InitReport(
            initialiser="edition_secrets",
            mode=mode,
            data_root=str(data_root),
            factory_root=str(factory_root),
        )
        report.add(InitReportEntry(
            initialiser="edition_secrets.skipped",
            location="skipped_empty",
            target="edition_secrets",
            note=f"edition_secrets package not present (external?): {exc}",
        ))
        result.stages.append(StageResult(
            name="edition_secrets",
            exit_code=0,
            report_summary=report.render_summary(),
            error_count=0,
        ))
        return True

    _LOGGER.info(
        "install.stage.start",
        stage="edition_secrets",
        mode=mode,
    )
    sub = _run(
        mode=mode,
        data_root=data_root,
        factory_root=factory_root,
        secret_backend=secret_backend,
        is_internal=is_internal,
    )
    summary = sub.report.render_summary()
    error_count = len(sub.report.errors)
    result.stages.append(StageResult(
        name="edition_secrets",
        exit_code=sub.exit_code,
        report_summary=summary,
        error_count=error_count,
    ))
    _LOGGER.info(
        "install.stage.end",
        stage="edition_secrets",
        exit_code=sub.exit_code,
        errors=error_count,
    )
    return sub.exit_code == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _push_skipped(
    result: RunResult,
    *,
    name: str,
    mode: Mode,
    note: str,
) -> None:
    """Record a stage that was skipped without invoking its runner."""
    report = InitReport(
        initialiser=name,
        mode=mode,
    )
    report.add(InitReportEntry(
        initialiser=f"{name}.skipped",
        location="skipped_existing",
        target=name,
        note=note,
    ))
    result.stages.append(StageResult(
        name=name,
        exit_code=0,
        report_summary=report.render_summary(),
        error_count=0,
    ))
    _LOGGER.info(
        "install.stage.skipped",
        stage=name,
        note=note,
    )


__all__ = ["RunResult", "STAGE_NAMES", "StageResult", "run"]

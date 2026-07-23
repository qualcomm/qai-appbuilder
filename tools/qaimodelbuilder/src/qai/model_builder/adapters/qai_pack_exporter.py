# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Concrete :class:`PackExporterPort` adapter.

Reproduces the 11-step pipeline from
``features/model-builder/scripts/qai_pack_export.py:export_pack``,
delegating to internal helpers:

1. validate workspace probed by :class:`WosAiWorkspaceReader`;
2. parse plan / report (already done by the workspace reader);
3. taxonomy classification (-> :class:`TaxonomyClassifierPort`);
4. locate context binaries via :func:`._pack_layout.find_context_binary`;
5. ``app_pack/`` directory layout (incremental);
6. extract live I/O contract via :func:`._io_contract_probe.extract_and_smoke_test_contract`;
7. write ``manifest.json`` via :func:`._manifest_builder.build_manifest_dict`;
8. render ``runner.py`` via :func:`._runner_templates.render_runner`;
9. write ``requirements.txt`` (minimal numpy entry);
10. copy example outputs + write ``examples/LICENSES.md``;
11. collect assets (labels / vocab) via :func:`._assets_collector.collect_assets`;
12. write ``provenance/`` (source plan + report copies + accuracy_summary.json
    + import_meta.json);
13. write ``_candidate.json`` (top-level structural marker).

The whole pipeline returns a :class:`PackExportResult`. Hard infra
failures (missing default ``.bin``, unavailable ``qai_appbuilder``,
smoke-test crash) raise from the domain error hierarchy; everything
else folds into ``result.errors`` / ``result.checks``.
"""

from __future__ import annotations

import json
import py_compile
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qai.model_builder.application.ports import (
    PackExporterPort,
    TaxonomyClassifierPort,
)
from qai.model_builder.domain import (
    AccuracySummary,
    ClassifyResult,
    ExportPackCommand,
    InvalidPrecisionError,
    IoKind,
    MissingContextBinError,
    ModelWorkspace,
    PackExportResult,
    Precision,
    Variant,
    legacy_for,
)
from qai.model_builder.domain.value_objects import (
    MIN_CONTEXT_BIN_SIZE,
)

from ._assets_collector import collect_assets
from ._io_contract_probe import extract_and_smoke_test_contract_subprocess
from ._manifest_builder import (
    build_accuracy_summary,
    build_accuracy_summary_dict,
    build_candidate_dict,
    build_import_meta_dict,
    build_manifest_dict,
)
from ._naming import (
    infer_display_name,
    infer_display_name_no_precision,
    infer_pack_id,
    infer_pack_id_no_precision,
)
from ._pack_layout import (
    clean_for_re_export,
    clear_generating,
    collect_example_images,
    find_context_binary,
    link_or_copy,
    mark_generating,
    sha256_file,
)
from ._runner_templates import render_runner
from .taxonomy_classifier import io_kinds_for_classification

__all__ = ["QaiPackExporter"]


_LICENSES_TEXT = (
    "# Example Licenses\n\n"
    "This directory contains example outputs generated during model validation.\n\n"
    "## License Information\n\n"
    "- Example outputs are generated from test inputs and are provided for\n"
    "  demonstration purposes only.\n"
    "- If your test inputs contain copyrighted material, ensure appropriate\n"
    "  licensing before distribution.\n"
    "- Model weights and architecture may be subject to their own licenses.\n"
    "  Refer to the original model repository for details.\n"
)


@dataclass(slots=True)
class _ExportMetadata:
    """Resolved taxonomy / naming / IO metadata (Step 2 output)."""

    classify_result: ClassifyResult
    legacy_category: str
    pack_id: str
    display_name: str
    input_kind: str
    output_kind: str


@dataclass(slots=True)
class _PackLayout:
    """The ``app_pack/`` directory tree created in Step 4."""

    pack_dir: Path
    examples_dir: Path
    provenance_dir: Path
    weights_dir: Path
    assets_dir: Path


@dataclass(slots=True)
class QaiPackExporter:
    """Materialise an ``app_pack/`` directory from a probed workspace."""

    classifier: TaxonomyClassifierPort
    qai_appbuilder_shared_dir: Path | None = None
    """Optional path containing ``qnn_helper.py`` + ``io_validator.py``.

    DI sets this to whatever local path bundles the App Builder shared
    runner helpers; when ``None`` the I/O contract probe falls back to
    importing them from ``sys.path``."""

    skip_smoke_test: bool = False
    """When ``True``, the exporter writes a placeholder I/O contract
    (``validated_at_export=False``) instead of loading the ``.bin``
    via ``qai_appbuilder``. Off by default because the legacy script
    treats unvalidated Packs as broken; DI may flip it on for hosts
    that do not have the runtime installed (a hard
    :class:`MissingQaiAppBuilderError` would otherwise abort export)."""

    require_qai_appbuilder: bool = True
    """When ``True`` (default), :class:`MissingQaiAppBuilderError`
    propagates. When ``False``, the exporter logs the failure and
    falls back to the placeholder contract — same as
    :attr:`skip_smoke_test`. Both knobs together let DI choose the
    right behaviour per deployment."""

    smoke_test_interpreter_argv: tuple[str, ...] | None = None
    """Launch prefix for the one-shot smoke-test child process.

    Typically ``(str(python_exe),)`` where ``python_exe`` is the ARM64 venv
    interpreter resolved via ``select_runner_interpreter`` so the child can
    load ``qai_appbuilder`` + the QNN runtime. When ``None`` the probe defaults
    to ``(sys.executable,)`` (dev / tests). DI wires the production value."""

    smoke_test_env: dict[str, str] | None = None
    """Full environment for the smoke-test child process.

    DI merges the QAIRT SDK env (``QAIRT_ROOT`` etc.) + ``PATH`` extras onto
    ``os.environ`` here (mirroring the sticky-worker spawn) so the child finds
    the QNN DLLs. When ``None`` the probe copies the current ``os.environ``."""

    async def export(
        self,
        *,
        workspace: ModelWorkspace,
        command: ExportPackCommand,
    ) -> PackExportResult:
        log: list[str] = []
        errors: list[str] = []

        log.append(f"[INFO] === QAI Pack Export ===")
        log.append(f"[INFO] Workdir: {workspace.workdir}")

        # In-flight marker: the route is synchronous, so this on-disk
        # sentinel is the only durable record that generation is under way.
        # Written BEFORE any long-running step (smoke test etc.) and cleared
        # in the ``finally`` below so a mid-generation Import-panel reopen
        # sees "生成中..." instead of the initial button. ``mark_generating``
        # also drops any stale ``_candidate.json`` so the status probe can't
        # report this fresh run as already finished before it is.
        pack_dir_early = workspace.workdir / "app_pack"
        mark_generating(pack_dir_early)
        try:
            # Step 1: precision selection.
            precisions, default_precision = self._resolve_precisions(
                workspace=workspace,
                command=command,
                log=log,
            )
            log.append(
                f"[INFO]   PRECISIONS = {[p.plan_key for p in precisions]} "
                f"(default = {default_precision.plan_key})"
            )
            # Step 2: taxonomy + display metadata.
            meta = self._resolve_metadata(
                workspace=workspace,
                command=command,
                precisions=precisions,
                default_precision=default_precision,
                log=log,
            )
            # Step 3: locate context binaries.
            per_precision = self._locate_context_binaries(
                workspace=workspace,
                precisions=precisions,
                default_precision=default_precision,
                log=log,
                errors=errors,
            )
            default_info = per_precision[default_precision.plan_key]
            default_bin: Path = default_info["bin"]
            # Step 4: app_pack/ layout (incremental).
            layout = self._setup_pack_layout(
                workspace=workspace,
                per_precision=per_precision,
                log=log,
            )
            # Step 5: I/O contract extraction (smoke test).
            io_contract = await self._extract_io_contract(
                default_bin=default_bin,
                log=log,
                errors=errors,
            )
            # Step 6: build variants[].
            variants = self._build_variants(
                per_precision=per_precision,
                default_precision=default_precision,
                pack_id=meta.pack_id,
                latencies_ms=workspace.latencies_ms,
            )
            # Step 7: manifest.json.
            manifest_path = self._write_manifest(
                workspace=workspace,
                meta=meta,
                default_precision=default_precision,
                default_bin=default_bin,
                default_info=default_info,
                io_contract=io_contract,
                variants=variants,
                pack_dir=layout.pack_dir,
                log=log,
            )
            # Step 8: runner.py.
            runner_path, runner_compiles = self._write_runner(
                workspace=workspace,
                meta=meta,
                default_precision=default_precision,
                default_bin=default_bin,
                pack_dir=layout.pack_dir,
                log=log,
                errors=errors,
            )
            # Step 8.5: SKILL.md skeleton — gives the App Builder chat Agent a
            # pack-specific summary + a pointer to runner.py's ``_resolve_weights``
            # for weight-path logic (P4 dual-root awareness).
            # Without this, the runtime ``FilesystemSkillFileLoader`` finds
            # nothing under either anchor and the LLM only sees the generic
            # top-level SKILL, missing all pack-specific I/O guidance.
            self._write_skill(
                meta=meta,
                default_precision=default_precision,
                variants=variants,
                pack_dir=layout.pack_dir,
                log=log,
                errors=errors,
            )
            # Step 9: requirements.txt.
            requirements_path = self._write_requirements(
                pack_id=meta.pack_id,
                pack_dir=layout.pack_dir,
                log=log,
            )
            # Step 10: example outputs + LICENSES.md.
            self._write_examples(
                workspace=workspace,
                examples_dir=layout.examples_dir,
                log=log,
            )
            # Step 11: assets (labels / vocab).
            self._collect_assets(
                workspace=workspace,
                classify_result=meta.classify_result,
                assets_dir=layout.assets_dir,
                log=log,
            )
            # Step 12: provenance.
            self._write_provenance(
                workspace=workspace,
                meta=meta,
                default_precision=default_precision,
                provenance_dir=layout.provenance_dir,
                log=log,
            )
            # Step 13: _candidate.json.
            checks, all_pass, failed_checks, candidate_json_path = (
                self._write_candidate(
                    workspace=workspace,
                    meta=meta,
                    default_precision=default_precision,
                    default_bin=default_bin,
                    default_info=default_info,
                    runner_compiles=runner_compiles,
                    manifest_path=manifest_path,
                    variants=variants,
                    pack_dir=layout.pack_dir,
                    log=log,
                )
            )
            self._log_completion(
                pack_dir=layout.pack_dir,
                all_pass=all_pass,
                failed_checks=failed_checks,
                log=log,
            )
        finally:
            # Clear the in-flight marker whether the export succeeded, hit a
            # soft-failure, or raised — the transient "生成中..." state must
            # never outlive the request. On success ``_candidate.json`` is the
            # durable "generated" signal the status probe reports instead.
            clear_generating(pack_dir_early)
        return PackExportResult(
            success=all_pass,
            pack_id=meta.pack_id,
            display_name=meta.display_name,
            pack_path=layout.pack_dir,
            candidate_json_path=candidate_json_path,
            manifest_path=manifest_path,
            runner_path=runner_path,
            requirements_path=requirements_path,
            examples_dir=layout.examples_dir,
            provenance_dir=layout.provenance_dir,
            weights_dir=layout.weights_dir,
            variants=tuple(variants),
            checks=checks,
            failed_checks=failed_checks,
            log_lines=tuple(log),
            errors=tuple(errors),
        )

    # ------------------------------------------------------------------
    # Step helpers (called in order by ``export``)
    # ------------------------------------------------------------------

    def _resolve_metadata(
        self,
        *,
        workspace: ModelWorkspace,
        command: ExportPackCommand,
        precisions: tuple[Precision, ...],
        default_precision: Precision,
        log: list[str],
    ) -> _ExportMetadata:
        """Step 2: taxonomy classification + display / id / IO metadata."""
        classify_result = self.classifier.classify(
            model_name=workspace.model_name,
            infer_manifest=workspace.inference_manifest,  # type: ignore[arg-type]
        )
        legacy_category = (
            command.category_override
            or _legacy_category_for(classify_result)
        )
        pack_id = (
            command.pack_id_override
            or infer_pack_id_no_precision(workspace.model_name)
        )

        if command.display_name_override:
            display_name = command.display_name_override
        elif len(precisions) >= 2:
            display_name = infer_display_name_no_precision(workspace.model_name)
        else:
            display_name = infer_display_name(
                workspace.model_name,
                default_precision.label,
            )

        # Input / output kinds: explicit override wins, then taxonomy IO,
        # then the legacy category-based fallback table.
        input_kind, output_kind = self._resolve_io_kinds(
            command=command,
            classify_result=classify_result,
            legacy_category=legacy_category,
        )
        log.append(f"[INFO] Auto-inferred metadata:")
        log.append(f"[INFO]   Pack ID:      {pack_id}")
        log.append(f"[INFO]   Category:     {legacy_category}")
        log.append(f"[INFO]   Display Name: {display_name}")
        log.append(f"[INFO]   Input Kind:   {input_kind}")
        log.append(f"[INFO]   Output Kind:  {output_kind}")

        return _ExportMetadata(
            classify_result=classify_result,
            legacy_category=legacy_category,
            pack_id=pack_id,
            display_name=display_name,
            input_kind=input_kind,
            output_kind=output_kind,
        )

    def _locate_context_binaries(
        self,
        *,
        workspace: ModelWorkspace,
        precisions: tuple[Precision, ...],
        default_precision: Precision,
        log: list[str],
        errors: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Step 3: stat + sha each ``<model>_<label>.bin`` per precision."""
        per_precision: dict[str, dict[str, Any]] = {}
        missing_precisions: list[str] = []
        for prec in precisions:
            bin_path = find_context_binary(
                workspace.output_dir,
                model_name=workspace.model_name,
                precision=prec,
            )
            if bin_path is None:
                log.append(
                    f"[WARN]   precision={prec.plan_key}: expected "
                    f"{workspace.model_name}_{prec.label}.bin not found "
                    f"in {workspace.output_dir}"
                )
                missing_precisions.append(prec.plan_key)
                continue
            try:
                size = bin_path.stat().st_size
                mtime_iso = datetime.fromtimestamp(
                    bin_path.stat().st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError as exc:
                log.append(
                    f"[WARN]   precision={prec.plan_key}: could not stat "
                    f"{bin_path}: {exc}"
                )
                missing_precisions.append(prec.plan_key)
                continue
            sha = sha256_file(bin_path)
            per_precision[prec.plan_key] = {
                "precision": prec,
                "bin": bin_path,
                "size": size,
                "sha": sha,
                "mtime_iso": mtime_iso,
            }
            log.append(
                f"[INFO]   precision={prec.plan_key}: {bin_path.name} "
                f"({size:,} bytes, sha={sha[:12]}...)"
            )

        if default_precision.plan_key not in per_precision:
            raise MissingContextBinError(
                f"default precision {default_precision.plan_key!r} has "
                "no usable .bin (missing or too small) under "
                f"{workspace.output_dir}"
            )

        if missing_precisions:
            log.append(
                f"[WARN]   {len(missing_precisions)} precision(s) "
                f"skipped: {missing_precisions}"
            )
            errors.append(
                f"missing precisions skipped: {missing_precisions}"
            )

        return per_precision

    def _setup_pack_layout(
        self,
        *,
        workspace: ModelWorkspace,
        per_precision: dict[str, dict[str, Any]],
        log: list[str],
    ) -> _PackLayout:
        """Step 4: (re)create ``app_pack/`` dirs and stage weights."""
        pack_dir = workspace.workdir / "app_pack"
        if pack_dir.exists():
            log.append(
                f"[INFO] Cleaning stale top-level files in app_pack/ "
                f"(keeping examples/provenance/weights/assets) ..."
            )
            clean_for_re_export(pack_dir)
        else:
            log.append(f"[INFO] Creating pack directory: {pack_dir}")
        pack_dir.mkdir(parents=True, exist_ok=True)
        examples_dir = pack_dir / "examples"
        provenance_dir = pack_dir / "provenance"
        weights_dir = pack_dir / "weights"
        assets_dir = pack_dir / "assets"
        for d in (examples_dir, provenance_dir, weights_dir, assets_dir):
            d.mkdir(exist_ok=True)

        for prec_key, info in per_precision.items():
            bin_src: Path = info["bin"]
            dst = weights_dir / bin_src.name
            try:
                link_or_copy(bin_src, dst)
            except (OSError, shutil.Error) as exc:
                log.append(
                    f"[WARN]   Could not stage weights for {prec_key}: {exc}"
                )

        return _PackLayout(
            pack_dir=pack_dir,
            examples_dir=examples_dir,
            provenance_dir=provenance_dir,
            weights_dir=weights_dir,
            assets_dir=assets_dir,
        )

    async def _extract_io_contract(
        self,
        *,
        default_bin: Path,
        log: list[str],
        errors: list[str],
    ) -> dict[str, Any] | None:
        """Step 5: smoke-test + extract the live I/O contract.

        Returns the validated contract on success, or ``None`` when the
        contract could not be validated (smoke test skipped, or the probe
        failed in non-strict mode). ``None`` makes ``_manifest_builder`` emit
        ``io_contract: null`` so the runtime falls back to live-only
        enforcement (the documented legacy-Pack path) instead of rejecting an
        empty-lists placeholder with ``CONTRACT_MISMATCH``. We never fabricate
        a contract we did not actually validate (State-Truth-First).

        The QNN native load + zero-tensor inference runs in a one-shot child
        process (:func:`extract_and_smoke_test_contract_subprocess`) so native
        ``printf`` output and any native crash stay isolated from this
        long-lived service process. The success value and the raised domain
        exceptions are identical to the in-process path, so the failure
        semantics below (strict raise / non-strict degrade to ``None``) are
        unchanged.
        """
        if self.skip_smoke_test:
            log.append(
                "[INFO]   Skipping I/O contract extraction "
                "(skip_smoke_test=True); manifest.io_contract = null "
                "(runtime uses live-only shape enforcement)"
            )
            return None
        try:
            io_contract = await extract_and_smoke_test_contract_subprocess(
                default_bin,
                shared_dir=self.qai_appbuilder_shared_dir,
                interpreter_argv=self.smoke_test_interpreter_argv,
                env=self.smoke_test_env,
            )
            log.append(
                f"[INFO] Extracted I/O contract from "
                f"{default_bin.name} via qai_appbuilder (out-of-process); "
                "smoke test passed (zero-tensor inference returned without error)"
            )
            return io_contract
        except Exception as exc:  # noqa: BLE001 — domain errors below
            if self.require_qai_appbuilder:
                raise
            log.append(
                f"[WARN]   I/O contract probe failed (running in "
                f"non-strict mode): {exc}; manifest.io_contract = null "
                "(runtime uses live-only shape enforcement instead of "
                "rejecting an un-validated placeholder)"
            )
            errors.append(f"io_contract_probe_failed: {exc}")
            return None

    def _write_manifest(
        self,
        *,
        workspace: ModelWorkspace,
        meta: _ExportMetadata,
        default_precision: Precision,
        default_bin: Path,
        default_info: dict[str, Any],
        io_contract: dict[str, Any] | None,
        variants: tuple[Variant, ...],
        pack_dir: Path,
        log: list[str],
    ) -> Path:
        """Step 7: render + write ``manifest.json``."""
        log.append("[INFO] Generating manifest.json ...")
        manifest_dict = build_manifest_dict(
            pack_id=meta.pack_id,
            display_name=meta.display_name,
            classify_result=meta.classify_result,
            legacy_category=meta.legacy_category,
            model_name=workspace.model_name,
            default_precision=default_precision,
            input_kind=meta.input_kind,
            output_kind=meta.output_kind,
            default_context_bin_name=default_bin.name,
            default_context_bin_sha256=default_info["sha"],
            default_context_bin_size=default_info["size"],
            plan_config=workspace.plan_config,
            cosine_similarities=workspace.cosine_similarities,
            latencies_ms=workspace.latencies_ms,
            validation_passed=workspace.validation_passed,
            inference_manifest=dict(workspace.inference_manifest),
            io_contract=io_contract,
            variants=list(variants),
            workdir=workspace.workdir,
        )
        manifest_path = pack_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return manifest_path

    def _write_runner(
        self,
        *,
        workspace: ModelWorkspace,
        meta: _ExportMetadata,
        default_precision: Precision,
        default_bin: Path,
        pack_dir: Path,
        log: list[str],
        errors: list[str],
    ) -> tuple[Path, bool]:
        """Step 8: render ``runner.py`` and py_compile-validate it."""
        log.append("[INFO] Generating runner.py ...")
        input_shape = self._extract_input_shape(workspace)
        runner_text = render_runner(
            pack_id=meta.pack_id,
            model_name=workspace.model_name,
            input_kind=meta.input_kind,
            output_kind=meta.output_kind,
            category=meta.legacy_category,
            weights_filename=default_bin.name,
            input_shape=input_shape,
            precision=default_precision.plan_key,
            infer_manifest=dict(workspace.inference_manifest),
        )
        runner_path = pack_dir / "runner.py"
        runner_path.write_text(runner_text, encoding="utf-8")

        runner_compiles = True
        try:
            py_compile.compile(str(runner_path), doraise=True)
            log.append("[INFO]   runner.py: py_compile OK")
        except py_compile.PyCompileError as exc:
            log.append(f"[ERROR]   runner.py: py_compile FAILED - {exc}")
            errors.append(f"runner_compile_failed: {exc}")
            runner_compiles = False
        return runner_path, runner_compiles

    def _write_skill(
        self,
        *,
        meta: _ExportMetadata,
        default_precision: str,
        variants: tuple[Variant, ...],
        pack_dir: Path,
        log: list[str],
        errors: list[str],
    ) -> Path | None:
        """Step 8.5: write a pack-specific ``SKILL.md`` skeleton.

        The App Builder chat Agent reads this to understand what a pack does
        without having to parse the full ``runner.py``. Contents:

        * one-line summary from ``manifest.display_name`` + I/O kinds;
        * a "P4 imported pack" tag (this exporter only produces user-imported
          packs — a hint the LLM needs when writing ``app.yaml`` and picking
          the correct ``pack_dir`` / ``model_dir`` layout);
        * pointer to ``runner.py._resolve_weights`` as the authoritative
          weight-path resolver (P4 double-anchor safe: runner logic handles
          both built-in ``APP_BUILDER_MODEL_ROOT`` and user
          ``APP_BUILDER_USER_MODEL_ROOT`` layouts, plus packaged-zip
          ancestor-walk);
        * per-variant table (precision / .bin name / size) — matches the
          hand-written SKILL.md that used to be authored manually
          (equivalence let us drop those hand-writes without regressing
          Agent context);
        * reminder that ``appbuilder_run`` is the tool-invocation channel
          (so LLM does not try to load ``.bin`` directly for tool calls).

        The manifest's ``skill.enabled`` defaults to ``True``
        (see ``_manifest_builder.py``) so this SKILL is auto-injected into
        chat sessions selecting this pack — Step 8.5 is a live feature, not
        reference material. Users can flip it to ``False`` per pack if they
        want prompt-budget control.

        IO error handling: ``write_text`` failures are recorded in ``errors``
        and ``log`` (matching ``_write_runner`` convention) instead of raising,
        so a filesystem hiccup on this optional file does not abort the whole
        export. Returns the written path or ``None`` on failure.
        """
        log.append("[INFO] Generating SKILL.md skeleton ...")
        skill_path = pack_dir / "SKILL.md"

        # Build the variants table from Step 6's variants[]. Precision label
        # is Precision.label (e.g. "fp16" / "int8" / "w8a16"); use .long_label
        # for the human-facing column (e.g. "FP16 · Highest accuracy").
        if variants:
            variant_rows = ["| Precision | Weight file | Size |",
                            "|---|---|---|"]
            for v in variants:
                size_mb = round(v.size_bytes / (1024 * 1024), 1)
                default_tag = " *(default)*" if v.is_default else ""
                variant_rows.append(
                    f"| `{v.precision.label}`{default_tag} "
                    f"({v.precision.long_label}) "
                    f"| `{v.context_bin_name}` "
                    f"| ~{size_mb} MB |"
                )
            variants_section = "\n".join(variant_rows) + "\n"
        else:
            variants_section = "_No variants declared._\n"

        content = (
            f"# {meta.display_name}\n"
            "\n"
            f"Model ID: `{meta.pack_id}`  \n"
            f"I/O: `{meta.input_kind}` → `{meta.output_kind}`  \n"
            f"Default precision: `{default_precision}`  \n"
            "Origin: **user-imported via QAI ModelBuilder (P4 pack)**. "
            "Weights live under "
            "`${APP_BUILDER_USER_MODEL_ROOT}/models/"
            f"{meta.pack_id}/` at dev-time; pack metadata under "
            "`${APP_BUILDER_USER_PACK_ROOT}/"
            f"{meta.pack_id}/`.\n"
            "\n"
            "## Available variants\n"
            "\n"
            f"{variants_section}"
            "\n"
            "## How to invoke\n"
            "\n"
            f"Use the `appbuilder_run` tool with `modelId = \"{meta.pack_id}\"`. "
            "Input/output schemas and available parameters come from "
            "`manifest.json` (`inputSchema` / `outputSchema` / `params`) — "
            "the host resolves everything, you do not need to touch `.bin` "
            "files or environment variables to make a tool call.\n"
            "\n"
            "## How to build a fullstack app around this model\n"
            "\n"
            f"When generating a WebUI (`data/app_builder/<app_id>/`) that "
            f"loads `{meta.pack_id}` in-process (rather than via "
            "`appbuilder_run`), your `backend/inference.py` MUST use the "
            "canonical weight-path resolver instead of hard-coding paths. "
            "Source of truth: this pack's `runner.py` — read its "
            "`_resolve_weights()` / `_resolve_model_dir()` function. It "
            "already handles all four cases correctly:\n"
            "\n"
            "1. **Dev host, built-in pack** — `$APP_BUILDER_MODEL_ROOT/<id>/<bin>`\n"
            "2. **Dev host, user-imported pack (P4)** — "
            "`$APP_BUILDER_USER_MODEL_ROOT/models/<id>/<bin>` "
            "(note the extra `models/` layer)\n"
            "3. **Packaged zip, any machine** — ancestor-walk from "
            "`__file__` for `<pkg>/models/<id>/`\n"
            "4. **Fallback** — clear `FileNotFoundError` against the "
            "canonical path\n"
            "\n"
            # NOTE (架构一致性): the two path strings below duplicate the
            # ``USER_PACK_REL`` / ``USER_WEIGHTS_REL`` + ``USER_WEIGHTS_MODELS_SUBDIR``
            # constants in ``qai.app_builder.domain.pack_layout_constants``.
            # We do NOT import that module here because ``qai.model_builder``
            # is architecturally upstream of ``qai.app_builder``, and a
            # reverse import would introduce cross-context coupling.
            # The strings appear **verbatim** (no f-string interpolation
            # inside the path prefix) so a contract-consistency test in
            # ``tests/unit/qai/app_builder/test_pack_layout_constants.py``
            # can grep them literally. Change either side → the test fails
            # → change the other side too.
            f'Because this pack is user-imported (P4), your `app.yaml` MUST '
            f'use the user-pack layout: `builtin: false` + '
            f'`pack_dir: "${{APP_ROOT}}/data/app_builder/user_models/{meta.pack_id}"` + '
            f'`model_dir: "${{APP_ROOT}}/data/app_builder/user_model_weights/models/{meta.pack_id}"`. '
            "Copy the runner's `_resolve_*` "
            "helper verbatim into `backend/inference.py`; do NOT reinvent "
            "path logic (it will miss one of the four cases and break "
            "either dev-time or packaged-distribution use).\n"
            "\n"
            "## Weight files\n"
            "\n"
            "Actual `.bin` locations are runtime concerns — you should "
            "never hard-code them. See the file `manifest.json`'s "
            "`assets.installPath` (and `variants[].assets.installPath`) "
            "for the canonical relative paths; the host anchors them to "
            "either the built-in or user weight root at runtime.\n"
        )
        try:
            skill_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            msg = f"skill_write_failed: {exc}"
            log.append(f"[ERROR]   SKILL.md: {msg}")
            errors.append(msg)
            return None
        log.append("[INFO]   SKILL.md: OK")
        return skill_path

    def _write_requirements(
        self,
        *,
        pack_id: str,
        pack_dir: Path,
        log: list[str],
    ) -> Path:
        """Step 9: write the minimal ``requirements.txt``."""
        log.append("[INFO] Generating requirements.txt ...")
        requirements_path = pack_dir / "requirements.txt"
        requirements_path.write_text(
            f"# Requirements for pack: {pack_id}\n"
            "# Add dependencies as needed\n"
            "numpy\n",
            encoding="utf-8",
        )
        return requirements_path

    def _write_examples(
        self,
        *,
        workspace: ModelWorkspace,
        examples_dir: Path,
        log: list[str],
    ) -> None:
        """Step 10: copy example outputs + write ``examples/LICENSES.md``."""
        log.append("[INFO] Collecting example outputs ...")
        for img in collect_example_images(workspace.output_dir):
            try:
                shutil.copy2(img, examples_dir / img.name)
                log.append(f"[INFO]   Copied: {img.name}")
            except OSError as exc:
                log.append(f"[WARN]   Could not copy example {img.name}: {exc}")
        (examples_dir / "LICENSES.md").write_text(
            _LICENSES_TEXT, encoding="utf-8"
        )

    def _collect_assets(
        self,
        *,
        workspace: ModelWorkspace,
        classify_result: ClassifyResult,
        assets_dir: Path,
        log: list[str],
    ) -> None:
        """Step 11: collect labels / vocab assets into ``assets/``."""
        log.append("[INFO] Collecting assets (labels, vocab, etc.) ...")
        _, asset_log = collect_assets(
            workdir=workspace.workdir,
            assets_dir=assets_dir,
            inference_manifest=dict(workspace.inference_manifest),
            taxonomy_task=classify_result.task,
        )
        log.extend(asset_log)

    def _write_provenance(
        self,
        *,
        workspace: ModelWorkspace,
        meta: _ExportMetadata,
        default_precision: Precision,
        provenance_dir: Path,
        log: list[str],
    ) -> None:
        """Step 12: copy source plan/report + write accuracy/import meta."""
        log.append("[INFO] Generating provenance files ...")
        if workspace.plan_path is not None and workspace.plan_path.is_file():
            try:
                shutil.copy2(workspace.plan_path, provenance_dir / "source_plan.md")
                log.append("[INFO]   Copied: source_plan.md")
            except OSError as exc:
                log.append(f"[WARN]   Could not copy source_plan.md: {exc}")
        if workspace.report_path is not None and workspace.report_path.is_file():
            try:
                shutil.copy2(workspace.report_path, provenance_dir / "source_REPORT.md")
                log.append("[INFO]   Copied: source_REPORT.md")
            except OSError as exc:
                log.append(f"[WARN]   Could not copy source_REPORT.md: {exc}")
        else:
            (provenance_dir / "source_REPORT.md").write_text(
                "# REPORT\n\nNo REPORT.md was available at export time.\n",
                encoding="utf-8",
            )
            log.append("[INFO]   Created placeholder: source_REPORT.md")

        accuracy = build_accuracy_summary(
            cosine_similarities=workspace.cosine_similarities,
            latencies_ms=workspace.latencies_ms,
            validation_passed=workspace.validation_passed,
            precision=default_precision,
        )
        (provenance_dir / "accuracy_summary.json").write_text(
            json.dumps(
                build_accuracy_summary_dict(accuracy),
                indent=2,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        log.append("[INFO]   Generated: accuracy_summary.json")

        (provenance_dir / "import_meta.json").write_text(
            json.dumps(
                build_import_meta_dict(
                    pack_id=meta.pack_id, workdir=workspace.workdir,
                ),
                indent=2,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        log.append("[INFO]   Generated: import_meta.json")

    def _write_candidate(
        self,
        *,
        workspace: ModelWorkspace,
        meta: _ExportMetadata,
        default_precision: Precision,
        default_bin: Path,
        default_info: dict[str, Any],
        runner_compiles: bool,
        manifest_path: Path,
        variants: tuple[Variant, ...],
        pack_dir: Path,
        log: list[str],
    ) -> tuple[dict[str, Any], bool, tuple[str, ...], Path]:
        """Step 13: compute checks + write ``_candidate.json``."""
        log.append("[INFO] Generating _candidate.json ...")
        checks: dict[str, Any] = {
            "plan_parsed": bool(workspace.model_name and default_precision.plan_key),
            "context_binary_found": True,
            "context_binary_size_ok": default_info["size"] >= MIN_CONTEXT_BIN_SIZE,
            "context_binary_sha256": default_info["sha"],
            "runner_compiles": runner_compiles,
            "manifest_generated": manifest_path.is_file(),
            "report_available": workspace.report_path is not None,
            "cosine_values_found": len(workspace.cosine_similarities) > 0,
        }
        all_pass = all([
            checks["plan_parsed"],
            checks["context_binary_found"],
            checks["context_binary_size_ok"],
            checks["runner_compiles"],
            checks["manifest_generated"],
        ])
        failed_checks: tuple[str, ...] = tuple(
            k for k, v in checks.items() if not v
        )

        candidate_dict = build_candidate_dict(
            pack_id=meta.pack_id,
            display_name=meta.display_name,
            source_workdir=workspace.workdir,
            weights_abs_path=default_bin,
            all_checks_pass=all_pass,
            checks=checks,
            variants=list(variants),
        )
        candidate_json_path = pack_dir / "_candidate.json"
        candidate_json_path.write_text(
            json.dumps(candidate_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return checks, all_pass, failed_checks, candidate_json_path

    @staticmethod
    def _log_completion(
        *,
        pack_dir: Path,
        all_pass: bool,
        failed_checks: tuple[str, ...],
        log: list[str],
    ) -> None:
        """Append the trailing ``=== Export Complete ===`` log block."""
        log.append("")
        log.append("[INFO] === Export Complete ===")
        log.append(f"[INFO] Pack directory: {pack_dir}")
        log.append(f"[INFO] Candidate ready: {all_pass}")
        if all_pass:
            log.append(
                "[INFO] The pack is structurally valid and ready for "
                "AppBuilder import."
            )
        else:
            log.append(
                "[ERROR] Some checks failed. Review the output above for details."
            )
            log.append(f"[ERROR] Failed checks: {list(failed_checks)}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_precisions(
        self,
        *,
        workspace: ModelWorkspace,
        command: ExportPackCommand,
        log: list[str],
    ) -> tuple[tuple[Precision, ...], Precision]:
        """Pick (precisions, default_precision) per the legacy rules.

        Source priority for ``precisions``:

        1. ``command.precisions`` (already normalised by the use case);
        2. plan-config ``PRECISION`` field (legacy single-precision path);
        3. auto-detect: scan ``output/`` for the first
           ``<model>_<label>.bin`` whose label is supported.

        The ``default_precision`` defaults to ``command.default_precision``
        when explicit, else the first item of the resolved list.
        """
        # Branch A: explicit precisions[] from the command.
        if command.precisions:
            precisions = tuple(
                Precision.from_token(t) for t in command.precisions
            )
        else:
            # Branch B: plan-config PRECISION (legacy single-precision).
            plan_precision = workspace.plan_config.get("PRECISION", "")
            if plan_precision:
                precisions = (Precision.from_token(plan_precision),)
            else:
                # Branch C: auto-detect from output/.
                precisions = tuple(self._autodetect_precisions(
                    workspace=workspace, log=log,
                ))

        if not precisions:
            raise InvalidPrecisionError(
                "no precisions resolved for export — provide --precisions, "
                "fill plan.md PRECISION, or place at least one "
                "<model>_<label>.bin under output/"
            )

        # De-dup while preserving order.
        seen: set[str] = set()
        deduped: list[Precision] = []
        for p in precisions:
            if p.plan_key in seen:
                continue
            seen.add(p.plan_key)
            deduped.append(p)
        precisions = tuple(deduped)

        if command.default_precision:
            default = Precision.from_token(command.default_precision)
        else:
            default = precisions[0]
        if default.plan_key not in {p.plan_key for p in precisions}:
            raise InvalidPrecisionError(
                f"default_precision={default.plan_key!r} is not in "
                f"precisions={[p.plan_key for p in precisions]!r}"
            )
        return precisions, default

    def _autodetect_precisions(
        self,
        *,
        workspace: ModelWorkspace,
        log: list[str],
    ) -> list[Precision]:
        """Scan ``output/`` for the first usable ``<model>_<label>.bin``."""
        if not workspace.output_dir.is_dir():
            return []
        # Probe the supported label set (matches legacy auto-detect).
        for label in ("fp16", "fp32", "int8", "w8a16", "w8a8", "w4a16", "int4"):
            prec = Precision.from_token(label)
            bin_path = find_context_binary(
                workspace.output_dir,
                model_name=workspace.model_name,
                precision=prec,
            )
            if bin_path is not None:
                log.append(
                    f"[INFO]   Auto-detected precision={prec.plan_key} "
                    f"from {bin_path.name}"
                )
                return [prec]
        # Final fallback: fp16 (legacy default).
        log.append("[INFO]   No context binary auto-detected; defaulting to fp16")
        return [Precision.from_token("fp16")]

    def _resolve_io_kinds(
        self,
        *,
        command: ExportPackCommand,
        classify_result: ClassifyResult,
        legacy_category: str,
    ) -> tuple[str, str]:
        """Pick ``(input_kind, output_kind)`` per legacy precedence."""
        if command.input_kind_override and command.output_kind_override:
            return command.input_kind_override, command.output_kind_override

        # Try the taxonomy.
        from_classifier = io_kinds_for_classification(classify_result)
        input_kind = command.input_kind_override or from_classifier[0]
        output_kind = command.output_kind_override or from_classifier[1]

        # Legacy category-based fallback when classifier said
        # ``("image", "json")`` for an unclassified model — pick a
        # specific kind only if the legacy_category provides one.
        if classify_result.task is None:
            legacy_io = _CATEGORY_IO_FALLBACK.get(legacy_category)
            if legacy_io is not None:
                input_kind = command.input_kind_override or legacy_io[0]
                output_kind = command.output_kind_override or legacy_io[1]

        # Validate kinds.
        if not IoKind.is_supported_token(input_kind):
            input_kind = "image"
        if not IoKind.is_supported_token(output_kind):
            output_kind = "json"
        return input_kind, output_kind

    def _build_variants(
        self,
        *,
        per_precision: dict[str, dict[str, Any]],
        default_precision: Precision,
        pack_id: str,
        latencies_ms: tuple[float, ...],
    ) -> tuple[Variant, ...]:
        """Build typed :class:`Variant` entries from the probed binaries."""
        variants: list[Variant] = []
        latency_int = int(latencies_ms[0]) if latencies_ms else 0
        for prec_key, info in per_precision.items():
            prec: Precision = info["precision"]
            bin_path: Path = info["bin"]
            variants.append(Variant(
                precision=prec,
                context_bin_path=bin_path,
                context_bin_name=bin_path.name,
                size_bytes=info["size"],
                sha256=info["sha"],
                mtime_iso=info["mtime_iso"],
                is_default=(prec.plan_key == default_precision.plan_key),
                install_path=f"models/{pack_id}/{bin_path.name}",
                latency_ms=latency_int,
                memory_mb=0,
            ))
        # Ensure exactly one default=True (defensive — should always
        # be the case given how we built ``per_precision``).
        if not any(v.is_default for v in variants):
            variants[0] = Variant(
                precision=variants[0].precision,
                context_bin_path=variants[0].context_bin_path,
                context_bin_name=variants[0].context_bin_name,
                size_bytes=variants[0].size_bytes,
                sha256=variants[0].sha256,
                mtime_iso=variants[0].mtime_iso,
                is_default=True,
                install_path=variants[0].install_path,
                latency_ms=variants[0].latency_ms,
                memory_mb=variants[0].memory_mb,
            )
        return tuple(variants)

    def _extract_input_shape(self, workspace: ModelWorkspace) -> list[int] | None:
        """Pull ``input_shape`` from inference manifest > plan.md."""
        try:
            shape = (
                ((workspace.inference_manifest.get("input") or {}))
                .get("shape")
            )
            if isinstance(shape, list) and shape:
                return [int(x) for x in shape]
        except (AttributeError, TypeError, ValueError):
            pass
        plan_shape = workspace.plan_config.get("INPUT_SHAPE", "")
        if plan_shape and not plan_shape.startswith("<!--"):
            try:
                return [int(x.strip()) for x in plan_shape.split(",")]
            except (ValueError, AttributeError):
                return None
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CATEGORY_IO_FALLBACK: dict[str, tuple[str, str]] = {
    "SR":         ("image", "image"),
    "OCR":        ("image", "text"),
    "ASR":        ("audio", "text"),
    "TTS":        ("text", "audio"),
    "CV":         ("image", "json"),
    "NLP":        ("text", "text"),
    "LLM":        ("text", "text"),
    "Audio":      ("audio", "audio"),
    "Multimodal": ("multi", "json"),
}


def _legacy_category_for(result: ClassifyResult) -> str:
    """Reverse-lookup the legacy ``category`` string from a classify result."""
    if result.task:
        legacy = legacy_for(result.group, result.task)
        if legacy:
            return legacy
    group_to_legacy = {
        "computer-vision": "CV",
        "audio":           "Audio",
        "generative-ai":   "LLM",
        "multimodal":      "Multimodal",
    }
    return group_to_legacy.get(result.group, "")


# Keep dataclass field tooling happy with empty-list defaults below
# (avoid mutable default antipattern by using ``field(default_factory=list)``
# whenever the real attribute lives on an instance).
_ = field

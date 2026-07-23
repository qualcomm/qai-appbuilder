"""Unit tests for ``apps.cli.commands.chat``'s local-first activation flow
(delivery plan Phase 2 §Step 6, redesigned §Step 9, extended §Step 10).

Covers: (a) an already-present local install skips download/install and goes
straight to provider registration; (b) nothing present drives the same
install use cases ``service-release install service/model`` use, then
registers + probes the ``"local-genie"`` provider; (c) ``_run_chat`` never
auto-triggers activation itself regardless of provider count — activation is
only ever reachable from inside the session via ``/model`` (Step 9); (d) a
non-TTY invocation never triggers activation, regardless of provider count
(must not hang); (e) a ``KeyboardInterrupt`` mid-install is caught, prints a
clear message, and never propagates as an unhandled crash; (f)/(g) an
explicit ``model_id`` (Step 10, the user's ``/model <choice>`` pick) is
honoured precisely — skipped only when THAT specific model is already
installed, and still downloaded even when a DIFFERENT model already is.
"""

from __future__ import annotations

from types import SimpleNamespace

from apps.cli._render import RenderOptions
from apps.cli.commands import chat as chat_mod
from qai.service_release.application.use_cases import (
    InstallModelCommand,
    InstallServiceCommand,
)
from qai.service_release.domain.value_objects import (
    CatalogModel,
    DownloadProgress,
    DownloadStatus,
    LocalItemStatus,
    ModelHardware,
    ModelInstallResult,
    ModelsLocalStatus,
    ServiceInstallResult,
    ServiceVersion,
    VersionsLocalStatus,
)


def _opts() -> RenderOptions:
    return RenderOptions(color=False, emoji=False)


class _AsyncResult:
    def __init__(self, value) -> None:
        self._value = value
        self.calls: list[tuple] = []

    async def execute(self, *args, **kwargs):
        self.calls.append(args or kwargs)
        return self._value


class _StreamDownload:
    """Stub ``StreamServiceDownloadUseCase``/``StreamModelDownloadUseCase``."""

    def __init__(self, save_path: str) -> None:
        self._save_path = save_path
        self.calls: list = []

    def execute(self, command):
        self.calls.append(command)

        async def _gen():
            yield DownloadProgress(
                task_id="t", status=DownloadStatus.DONE, save_path=self._save_path
            )

        return _gen()


def _service_release_container(
    *, service_installed: bool, model_id: str | None
) -> SimpleNamespace:
    versions_status = VersionsLocalStatus(
        versions={"1.0": LocalItemStatus(installed=service_installed)}
    )
    models_status = ModelsLocalStatus(
        models={"m1": LocalItemStatus(installed=model_id is not None)}
        if model_id
        else {}
    )
    return SimpleNamespace(
        get_versions_local_status_use_case=_AsyncResult(versions_status),
        get_models_local_status_use_case=_AsyncResult(models_status),
        list_service_versions_use_case=_AsyncResult(
            [ServiceVersion(version="1.0", download_url="http://x/svc.zip", is_recommended=True)]
        ),
        list_catalog_models_use_case=_AsyncResult(
            [
                CatalogModel(
                    model_id="m1",
                    name="Model 1",
                    hardware=ModelHardware.NPU,
                    download_url="http://x/model.zip",
                )
            ]
        ),
        stream_service_download_use_case=_StreamDownload("C:/tmp/svc.zip"),
        stream_model_download_use_case=_StreamDownload("C:/tmp/model.zip"),
        install_service_use_case=_AsyncResult(
            ServiceInstallResult(
                ok=True, root_path="C:/svc", exe_path="C:/svc/exe", version="1.0", zip_deleted=True
            )
        ),
        install_model_use_case=_AsyncResult(
            ModelInstallResult(ok=True, install_path="C:/models/m1", model_id="m1", zip_deleted=True)
        ),
    )


def _model_catalog_container(*, probe_ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        update_provider_config_use_case=_AsyncResult(None),
        probe_provider_use_case=_AsyncResult(
            SimpleNamespace(ok=probe_ok, error="" if probe_ok else "connection refused")
        ),
    )


def _service_release_container_multi(
    *, service_installed: bool, installed_model_ids: frozenset[str]
) -> SimpleNamespace:
    """Like :func:`_service_release_container` but with TWO catalog models
    (``m1``/``m2``), for Step 10's explicit-``model_id`` idempotency tests
    where "some model is installed" and "the REQUESTED model is installed"
    must be told apart.
    """

    versions_status = VersionsLocalStatus(
        versions={"1.0": LocalItemStatus(installed=service_installed)}
    )
    models_status = ModelsLocalStatus(
        models={mid: LocalItemStatus(installed=True) for mid in installed_model_ids}
    )
    return SimpleNamespace(
        get_versions_local_status_use_case=_AsyncResult(versions_status),
        get_models_local_status_use_case=_AsyncResult(models_status),
        list_service_versions_use_case=_AsyncResult(
            [ServiceVersion(version="1.0", download_url="http://x/svc.zip", is_recommended=True)]
        ),
        list_catalog_models_use_case=_AsyncResult(
            [
                CatalogModel(
                    model_id="m1",
                    name="Model 1",
                    hardware=ModelHardware.NPU,
                    download_url="http://x/m1.zip",
                ),
                CatalogModel(
                    model_id="m2",
                    name="Model 2",
                    hardware=ModelHardware.GPU,
                    download_url="http://x/m2.zip",
                ),
            ]
        ),
        stream_service_download_use_case=_StreamDownload("C:/tmp/svc.zip"),
        stream_model_download_use_case=_StreamDownload("C:/tmp/model.zip"),
        install_service_use_case=_AsyncResult(
            ServiceInstallResult(
                ok=True, root_path="C:/svc", exe_path="C:/svc/exe", version="1.0", zip_deleted=True
            )
        ),
        install_model_use_case=_AsyncResult(
            ModelInstallResult(ok=True, install_path="C:/models/m2", model_id="m2", zip_deleted=True)
        ),
    )


def _container_multi(
    *, service_installed: bool, installed_model_ids: frozenset[str]
) -> SimpleNamespace:
    return SimpleNamespace(
        service_release=_service_release_container_multi(
            service_installed=service_installed,
            installed_model_ids=installed_model_ids,
        ),
        model_catalog=_model_catalog_container(),
        settings=SimpleNamespace(model_runtime=SimpleNamespace(default_port=8910)),
    )


def _container(*, service_installed: bool, model_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        service_release=_service_release_container(
            service_installed=service_installed, model_id=model_id
        ),
        model_catalog=_model_catalog_container(),
        settings=SimpleNamespace(model_runtime=SimpleNamespace(default_port=8910)),
    )


# ---------------------------------------------------------------------------
# (a) already present → skip install, register directly
# ---------------------------------------------------------------------------


async def test_activate_local_model_skips_install_when_already_present(monkeypatch):
    monkeypatch.setattr(chat_mod.sys.stdin, "isatty", lambda: True)
    c = _container(service_installed=True, model_id="m1")

    ok = await chat_mod._activate_local_model(c, _opts())

    assert ok is True
    assert c.service_release.install_service_use_case.calls == []
    assert c.service_release.install_model_use_case.calls == []
    assert c.service_release.stream_service_download_use_case.calls == []
    assert c.service_release.stream_model_download_use_case.calls == []
    assert c.model_catalog.update_provider_config_use_case.calls != []


# ---------------------------------------------------------------------------
# (b) nothing present → drives install use cases, then registers provider
# ---------------------------------------------------------------------------


async def test_activate_local_model_installs_then_registers_when_absent(monkeypatch):
    monkeypatch.setattr(chat_mod.sys.stdin, "isatty", lambda: True)
    c = _container(service_installed=False, model_id=None)

    ok = await chat_mod._activate_local_model(c, _opts())

    assert ok is True
    install_service_call = c.service_release.install_service_use_case.calls[0][0]
    assert isinstance(install_service_call, InstallServiceCommand)
    assert install_service_call.version == "1.0"

    install_model_call = c.service_release.install_model_use_case.calls[0][0]
    assert isinstance(install_model_call, InstallModelCommand)
    assert install_model_call.model_id == "m1"

    update_call = c.model_catalog.update_provider_config_use_case.calls[0][0]
    assert update_call.provider_id == "local-genie"
    assert update_call.config["default_model"] == "m1"


# ---------------------------------------------------------------------------
# (c) `_run_chat` never auto-triggers activation itself (Step 9 redesign):
#     entering the session must never block on / silently start a real
#     download, regardless of provider count. Activation is only ever
#     reachable from inside the session via `/model` (see
#     test_chat_model_command.py for that path).
# ---------------------------------------------------------------------------


async def test_run_chat_never_auto_triggers_activation(monkeypatch):
    async def _has_provider(_c):
        return False  # zero providers — the interesting case to prove safe

    monkeypatch.setattr(chat_mod, "_precheck_cloud_provider", _has_provider)

    activate_calls = []

    async def _activate(_c, _opts):
        activate_calls.append(True)
        return True

    monkeypatch.setattr(chat_mod, "_activate_local_model", _activate)

    class _FakeContainer:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(chat_mod, "repl_container", lambda **_kw: _FakeContainer())

    import argparse

    args = argparse.Namespace(repo_root=None, config_file=None)
    # _run_chat proceeds past the precheck into REPL scaffolding that this
    # unit test does not stub; a plain AttributeError past the precheck
    # branch is expected and irrelevant here — only the precheck/activation
    # ordering matters.
    try:
        await chat_mod._run_chat(args)
    except Exception:
        pass

    assert activate_calls == []


# ---------------------------------------------------------------------------
# (d) non-TTY → activation never triggers, regardless of provider count
# ---------------------------------------------------------------------------


async def test_activate_local_model_never_runs_on_non_tty(monkeypatch):
    monkeypatch.setattr(chat_mod.sys.stdin, "isatty", lambda: False)
    c = _container(service_installed=False, model_id=None)

    ok = await chat_mod._activate_local_model(c, _opts())

    assert ok is False
    assert c.service_release.install_service_use_case.calls == []
    assert c.service_release.install_model_use_case.calls == []
    assert c.model_catalog.update_provider_config_use_case.calls == []


# ---------------------------------------------------------------------------
# (e) KeyboardInterrupt mid-install is caught, never propagates
# ---------------------------------------------------------------------------


async def test_activate_local_model_catches_keyboard_interrupt_during_install(
    monkeypatch, capsys
):
    monkeypatch.setattr(chat_mod.sys.stdin, "isatty", lambda: True)
    c = _container(service_installed=False, model_id=None)

    async def _raise_interrupt(_c, _opts):
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_mod, "_install_default_service", _raise_interrupt)

    ok = await chat_mod._activate_local_model(c, _opts())

    assert ok is False
    assert c.model_catalog.update_provider_config_use_case.calls == []
    err = capsys.readouterr().err
    assert "已中止本地模型激活" in err


# ---------------------------------------------------------------------------
# (f)/(g) Step 10: an explicit ``model_id`` is honoured precisely, not
#     short-circuited by "some other model is already installed".
# ---------------------------------------------------------------------------


async def test_activate_local_model_skips_download_when_requested_model_already_installed(
    monkeypatch,
):
    monkeypatch.setattr(chat_mod.sys.stdin, "isatty", lambda: True)
    c = _container_multi(service_installed=True, installed_model_ids=frozenset({"m2"}))

    ok = await chat_mod._activate_local_model(c, _opts(), model_id="m2")

    assert ok is True
    assert c.service_release.install_model_use_case.calls == []
    assert c.service_release.stream_model_download_use_case.calls == []
    update_call = c.model_catalog.update_provider_config_use_case.calls[0][0]
    assert update_call.config["default_model"] == "m2"


async def test_activate_local_model_downloads_requested_model_even_if_different_model_present(
    monkeypatch,
):
    """A different model already being installed ("m1") must not cause the
    explicitly-requested one ("m2") to be silently skipped."""

    monkeypatch.setattr(chat_mod.sys.stdin, "isatty", lambda: True)
    c = _container_multi(service_installed=True, installed_model_ids=frozenset({"m1"}))

    ok = await chat_mod._activate_local_model(c, _opts(), model_id="m2")

    assert ok is True
    install_model_call = c.service_release.install_model_use_case.calls[0][0]
    assert isinstance(install_model_call, InstallModelCommand)
    assert install_model_call.model_id == "m2"
    update_call = c.model_catalog.update_provider_config_use_case.calls[0][0]
    assert update_call.config["default_model"] == "m2"

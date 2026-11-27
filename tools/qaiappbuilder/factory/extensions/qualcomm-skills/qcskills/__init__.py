# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Built-in extension: Qualcomm Skills.

Imports Qualcomm external-skill deliveries (a ZIP, possibly nesting ZIPs, with a
README and an MCP wheel) and runs each one's MCP server. The model then finds
skills through that server (``search_skills`` -> ``get_skill``) instead of every
skill being loaded into the context.

Everything a package installs lives in this extension's data directory
(``<data>/extensions-data/qualcomm-skills/packages/<id>/``), so removing a
package (or purging the extension's data) removes all of it. The app's own
Python environment is never touched.

HTTP (``/api/ext/qualcomm-skills/...``, behind the app's login + CSRF):

    GET    packages                list installed packages
    POST   packages/stage          body: the ZIP bytes (?filename=) -> preview
    POST   packages/install        {"token", "allow_network"} -> installed package
    DELETE packages/stage/{token}  discard a preview
    DELETE packages/{package_id}   remove a package (and its MCP server)
    GET    packages/{package_id}/readme
    GET    status                  MCP master switch + per-package server state
"""

from __future__ import annotations

import threading
from typing import Any

from qai.ext import ExtensionContext, RouteRequest, RouteResponse

from .packages import PackageError, Packages

#: Serialises install / remove: pip is not safe to run twice into one directory.
_LOCK = threading.Lock()


def _error(exc: PackageError, status: int = 400) -> RouteResponse:
    return _problem(exc.code, str(exc), status)


def _problem(code: str, message: str, status: int) -> RouteResponse:
    # The app's front end only shows a message from the standard error envelope.
    return RouteResponse(
        body={"type": "ExtensionRouteError", "code": code, "message": message}, status=status
    )


def activate(ctx: ExtensionContext) -> None:
    store = Packages(ctx.data_dir, proxy_url=ctx.proxy_url)

    # One MCP server per installed package. They come and go with this
    # extension: a reload (after an install / removal) re-registers the set.
    for spec in store.server_specs():
        ctx.register_mcp_server(
            spec["name"], command=spec["command"], args=spec["args"], env=spec["env"]
        )

    def list_packages(_req: RouteRequest) -> dict[str, Any]:
        return {"packages": store.installed()}

    def stage(req: RouteRequest) -> Any:
        if not req.raw_body:
            return _problem("qcskills.empty", "no file", 400)
        filename = (req.query.get("filename") or "upload.zip").replace("\\", "/").rsplit("/", 1)[-1]
        try:
            preview = store.stage(req.raw_body, filename)
            return {"token": preview["token"], "preview": preview}
        except PackageError as exc:
            return _error(exc)

    def discard(req: RouteRequest) -> Any:
        try:
            store.discard(req.params["token"])
        except PackageError as exc:
            return _error(exc, 404)
        return None

    def install(req: RouteRequest) -> Any:
        body = req.body if isinstance(req.body, dict) else {}
        token = str(body.get("token") or "")
        with _LOCK:
            try:
                meta = store.install(token, allow_network=bool(body.get("allow_network", True)))
            except PackageError as exc:
                status = {"qcskills.unknown_token": 404, "qcskills.already_installed": 409}.get(
                    exc.code, 400
                )
                return _error(exc, status)
        # Reloading this extension re-registers the MCP servers (now including
        # the new one); the host brings the MCP registry in line afterwards.
        ctx.logger.info("installed %s %s", meta["name"], meta["version"])
        return RouteResponse(body={"installed": meta}, status=201)

    def remove(req: RouteRequest) -> Any:
        with _LOCK:
            try:
                store.remove(req.params["package_id"])
            except PackageError as exc:
                return _error(exc, 404)
            except PermissionError as exc:
                return _problem("qcskills.busy", f"files are in use: {exc}", 409)
        return {"removed": req.params["package_id"], "reload": True}

    def readme(req: RouteRequest) -> Any:
        try:
            text = store.readme(req.params["package_id"])
        except PackageError as exc:
            return _error(exc, 404)
        return {"readme": text}

    ctx.register_route("GET", "packages", list_packages)
    ctx.register_route("POST", "packages/stage", stage)
    ctx.register_route("DELETE", "packages/stage/{token}", discard)
    ctx.register_route("POST", "packages/install", install)
    ctx.register_route("DELETE", "packages/{package_id}", remove)
    ctx.register_route("GET", "packages/{package_id}/readme", readme)

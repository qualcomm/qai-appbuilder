// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// Resolve an App Builder asset path (a run INPUT/source image path written by a
// Pack runner) into a browser-usable HTTP URL.
//
// Background (problem ⑥, 2026-06-07): the App Builder output area shows a
// thumbnail of the run's source image. The run record's `inputs.image` holds an
// ABSOLUTE disk path (e.g. `C:/.../data/blobs/chat/2026-06-05/appbuilder-*/img.png`)
// because the chat-triggered run persists the input image under the chat blob
// dir `data/blobs/chat/` (ARCH-2, 2026-06-09 — previously `data/images/`).
// Feeding that raw path into `<img src>` makes the browser interpret it as a
// `file://` URL, which it refuses with "Not allowed to load local resource", so
// the thumbnail fails to load.
//
// V1 solved this with `frontend/js/utils/appbuilder-url.js :: resolveAssetUrl`,
// which maps repo-internal asset paths onto backend static mounts. V2 exposes
// the same mounts:
//   - chat images   `data/blobs/chat/`     -> `/api/images/files/`
//     (`apps/api/_spa_mount.py :: _mount_images` -> StaticFiles(DataPaths.blob_dir("chat")))
//   - runner OUTPUT  `data/outputs/`        -> `/api/appbuilder/files/outputs/`
//   - audio uploads  `data/uploads/audio/`  -> `/api/appbuilder/files/uploads/audio/`
//     (both via `apps/api/_spa_mount.py :: _mount_app_builder_files`)
// so we only need to perform the same path -> HTTP-URL rewrite on the frontend.
// The `/api/images/files/` and `/api/appbuilder/files/` URL prefixes are
// V1-locked contracts and unchanged; only the physical disk segment for chat
// images moved from `data/images/` to `data/blobs/chat/`.
//
// The runner-written OUTPUT artifacts (MeloTTS `audio_path`, super-resolution /
// segmentation `image_path`) land in the flat `data/outputs/` tree, NOT in the
// per-run artifact blob store (`data/blobs/app_builder/<run_id>/`). They are
// therefore served by the dedicated `data/outputs/` static mount above — the
// `/api/app-builder/artifacts/.../blob` route only serves artifacts actually
// persisted through `ArtifactStorePort`. Anything we cannot map is returned
// unchanged so the caller (e.g. `resolveUrl` in the workbench overlay) can fall
// back to the artifact-blob route for a true blob-store relative path.

// Repo-internal path segment -> backend static URL prefix.
// Order matters: the `uploads/audio` subtree must be checked before a broader
// prefix could shadow it (mirrors V1 `_MAP` ordering).
const ASSET_MAP: ReadonlyArray<{ readonly src: string; readonly dst: string }> =
  [
    { src: "data/uploads/audio/", dst: "/api/appbuilder/files/uploads/audio/" },
    { src: "data/outputs/", dst: "/api/appbuilder/files/outputs/" },
    { src: "data/blobs/chat/", dst: "/api/images/files/" },
  ];

/**
 * Map a runner-written asset path to a browser-usable URL (V1
 * `resolveAssetUrl` parity).
 *
 * - `data:` / `blob:` / `http(s):` URLs are passed through as-is.
 * - Paths containing a mapped repo segment (e.g. `data/blobs/chat/`) — whether
 *   relative (`data/blobs/chat/x.png`), root-anchored (`/data/blobs/chat/x.png`)
 *   or embedded in an absolute path (`C:/.../data/blobs/chat/x.png`) — are
 *   rewritten onto the corresponding backend static route.
 * - Everything else is returned unchanged.
 */
export function resolveAppBuilderAssetUrl(value: unknown): string {
  if (typeof value !== "string" || value === "") return "";
  if (/^(data:|blob:|https?:)/i.test(value)) return value;

  // Normalise backslashes so Windows absolute paths match the same way.
  const norm = value.replace(/\\/g, "/");

  for (const { src, dst } of ASSET_MAP) {
    // 1) "data/images/x.png"
    if (norm.startsWith(src)) return dst + norm.slice(src.length);
    // 2) "/data/images/x.png"
    if (norm.startsWith("/" + src)) return dst + norm.slice(src.length + 1);
    // 3) "C:/.../data/images/x.png" — scan for the first matching segment that
    //    sits on a path boundary (preceded by "/").
    const i = norm.indexOf(src);
    if (i > 0 && norm.charAt(i - 1) === "/") {
      return dst + norm.slice(i + src.length);
    }
  }

  return value;
}

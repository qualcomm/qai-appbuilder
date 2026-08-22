// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * pnpm hook: patch package exports for Node 25+ CJS strict-exports compat.
 *
 * Node 25 removed the CJS fallback that allowed require() to bypass the
 * "exports" field. Several packages in the Vue 3.5.x dependency tree have
 * incomplete exports maps:
 *
 * 1. entities@4.5.0 — has ./lib/decode.js but not ./decode
 *    (@vue/compiler-core CJS bundle does `require('entities/decode')`)
 *
 * 2. estree-walker@3.0.3 — exports only "import", no "require" condition
 *    (vitest loads it via CJS require())
 *
 * This hook patches these at resolution time so pnpm writes correct
 * package.json into node_modules. Works with any Node version.
 */
function readPackage(pkg, _context) {
  // --- entities: add ./decode subpath ---
  if (pkg.name === 'entities' && pkg.exports && !pkg.exports['./decode']) {
    pkg.exports['./decode'] = {
      require: './lib/decode.js',
      import: './lib/esm/decode.js',
    };
  }

  // --- estree-walker: add CJS "require" condition to "." export ---
  if (pkg.name === 'estree-walker' && pkg.exports && pkg.exports['.']) {
    const dot = pkg.exports['.'];
    if (dot && !dot.require && dot.import) {
      dot.require = dot.import; // ESM source also works as CJS in Node 25
    }
  }

  // Force all entities to 4.5.0 (consistent with pnpm.overrides in package.json)
  if (pkg.dependencies && pkg.dependencies.entities) {
    pkg.dependencies.entities = '4.5.0';
  }

  return pkg;
}

module.exports = { hooks: { readPackage } };

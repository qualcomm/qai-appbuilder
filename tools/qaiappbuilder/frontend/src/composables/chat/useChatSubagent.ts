// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatSubagent — sub-agent layered chat view composable.
 *
 * S5 PR-053: skeleton + types only. The real subagent visualisation
 * (collapsible block per delegation, child-of relationships, indent
 * tree) is delivered alongside chat WS routing in PR-054.
 *
 * The legacy `useChat.js` packed subAgentBlocks into a parallel ref
 * with a complicated reconciliation function. Here we expose an
 * append-only buffer with a tree view computed from `parentId`.
 */
import { computed, ref, type ComputedRef } from "vue";

export interface SubAgentBlock {
  readonly id: string;
  readonly parentId: string | null;
  readonly title: string;
  readonly status: "pending" | "running" | "completed" | "failed";
  readonly content: string;
  readonly createdAt: number;
}

export interface SubAgentTreeNode extends SubAgentBlock {
  readonly children: readonly SubAgentTreeNode[];
}

export interface UseChatSubagent {
  readonly blocks: ComputedRef<readonly SubAgentBlock[]>;
  readonly tree: ComputedRef<readonly SubAgentTreeNode[]>;
  add(block: SubAgentBlock): void;
  update(id: string, patch: Partial<Omit<SubAgentBlock, "id">>): void;
  clear(): void;
}

function buildTree(blocks: readonly SubAgentBlock[]): readonly SubAgentTreeNode[] {
  const byParent = new Map<string | null, SubAgentBlock[]>();
  for (const b of blocks) {
    const list = byParent.get(b.parentId) ?? [];
    list.push(b);
    byParent.set(b.parentId, list);
  }
  function buildChildren(parentId: string | null): readonly SubAgentTreeNode[] {
    const list = byParent.get(parentId);
    if (list === undefined) {
      return [];
    }
    return list.map((b) => ({ ...b, children: buildChildren(b.id) }));
  }
  return buildChildren(null);
}

export function useChatSubagent(): UseChatSubagent {
  const internal = ref<SubAgentBlock[]>([]);

  return {
    blocks: computed(() => internal.value),
    tree: computed(() => buildTree(internal.value)),
    add(block) {
      internal.value = [...internal.value, block];
    },
    update(id, patch) {
      internal.value = internal.value.map((b) =>
        b.id === id ? { ...b, ...patch } : b,
      );
    },
    clear() {
      internal.value = [];
    },
  };
}

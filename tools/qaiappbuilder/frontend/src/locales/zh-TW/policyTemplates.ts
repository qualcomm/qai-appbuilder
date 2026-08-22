// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// 真值源说明：本项目 i18n 已无自动生成管道（旧的 _L8-locale-gen.py 与
// _migrated/*.json 均未保留在仓库）。因此本文件就是当前唯一真值源，
// 必须手工维护。修改时严守 AGENTS.md §3.10 文件编码铁律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 编码，禁止双重编码损坏）。
//
// 类型：en/{ns}.ts 经主入口 en.ts 组装后由 typeof 推导出 MessageSchema；
// zh-CN / zh-TW 的同名子文件须保持与 en 完全一致的 key 结构（由 locale
// parity 测试 + tsc 强制）。
// =============================================================================

const policyTemplates = {
  applied: "範本套用成功",
  apply: "套用",
  applyFailed: "範本套用失敗",
  current: "目前",
  desc: "選擇一個策略範本快速套用預定義的安全配置。",
  description: "套用預先定義的安全策略範本。",
  empty: "無可用的策略範本。",
  title: "策略範本",
  clearTitle: "清空所有規則",
  clearHint:
    "移除全部規則，讓通用允許級聯（工作區 / 技能 / 自動審批 / 系統白名單 / 授權）驅動決策；未被這些覆蓋的存取會走彈窗詢問。",
  clearBtn: "清空規則",
  // 按內建範本 id 鍵入的分範本顯示文案；渲染時按 id 查找，查不到回退後端文字。
  items: {
    demo: {
      name: "示範",
      description:
        "只允許 AI 讀取專案目錄；對專案內寫入、執行以及專案外任何操作均彈窗詢問。",
    },
    development: {
      name: "開發",
      description:
        "允許 AI 讀寫專案目錄與暫存目錄；其他一切存取彈窗詢問。日常開發推薦。",
    },
    strict: {
      name: "嚴格",
      description:
        "無範本規則；所有操作走通用允許級聯，未涵蓋的存取均需使用者在彈窗中顯式批准。最適合高安全性環境。",
    },
  },
};

export default policyTemplates;

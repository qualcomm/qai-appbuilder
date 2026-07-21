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

const help = {
  about: "關於",
  ccPanelHtml: "(請見設定中的 Claude 說明)",
  ccText: "🤖 Claude Code 指令說明\n\n  /cc new <目錄路徑> [會話名稱]\n    建立新的 Claude Code 會話，繫結到指定專案目錄。\n    例：/cc new C:\\Projects\\MyApp 認證功能開發\n\n  /cc list  (/cc l)\n    列出你的所有 Claude Code 會話（ID、名稱、狀態）。\n\n  /cc use <序號>  (/cc u <序號>)\n    按 /cc list 序號切換會話（如 /cc use 1）。\n\n  /cc use <ID前綴>\n    按 ID 前綴切換會話（輸入 ID 前 8 位即可）。\n\n  /cc status  (/cc s)\n    查看當前 Claude Code 會話狀態、對話輪次和工具呼叫次數。\n\n  /cc models  (/cc ms)\n    查看 Claude Code 可用模型列表（帶序號），並顯示當前選中的模型。\n\n  /cc model  (/cc m)\n    查看當前 Claude Code 使用的模型。\n\n  /cc model <編號>  (/cc m <編號>)\n    按 /cc models 序號切換 Claude Code 模型（如 /cc model 1）。\n\n  /cc fork  (/cc f)\n    Fork 當前會話為新分支（保留原歷史，下次發訊息時產生新會話 ID）。\n    適合在關鍵節點儲存進度，然後嘗試不同方向。\n\n  /cc stop  (/cc st)\n    停止當前正在執行的 Claude Code 任務，停止後可立即發送新訊息繼續對話。\n\n  /cc cd [目錄路徑]\n    查看當前工作目錄（無參數），或修改當前 CC 會話繫結的工作目錄。\n\n  /cc rename <新名稱>  (/cc r <新名稱>)\n    重新命名當前 Claude Code 會話。\n\n  /cc close  (/cc c)\n    退出 CC 模式（會話保留，可用 /cc use 重新進入）。\n\n  /cc delete  (/cc d)\n    徹底刪除當前 Claude Code 會話（不可恢復）。\n\n  /cc help  (/cc h)\n    顯示此說明。\n\n  <普通訊息>（CC 會話啟用時）\n    直接發訊息即可與 Claude Code 對話，無需 /cc 前綴。\n    發送 /new 可切回普通 AI 對話模式（不影響 CC 會話）。\n\n💡 建立會話後，直接發訊息即可與 Claude Code 對話\n💡 /cc fork 可在關鍵節點儲存進度，然後嘗試不同方向\n💡 /cc stop 停止後可立即發送新訊息繼續對話\n💡 /cc close 退出後，會話仍保留，隨時可用 /cc use 重新進入\n💡 發送 /new 可切回普通 AI 對話模式",
  commands: "指令",
  mainText: "📖 微信 / 飛書 / Chat 指令說明\n\n⌨️ 普通對話指令：\n\n  /help  (/h)\n    顯示此說明。\n\n  /new  (/n)\n    儲存當前會話歷史後開啟新會話。\n\n  /clear  (/cl)\n    刪除當前會話歷史並開啟新會話。\n\n  /list [N]  (/l [N])\n    查看最近 N 條歷史會話。\n\n  /use <編號>  (/u <編號>)\n    切換到指定編號的歷史會話。\n\n  /status  (/s)\n    查看當前會話狀態。\n\n  /rename <新名稱>  (/rn)\n    重新命名當前會話。\n\n  /delete  (/del)\n    刪除當前會話（不可恢復）。\n\n  /stop  (/st)\n    停止當前任務。\n\n  /models  (/ms)\n    查看所有可用模型。\n\n  /model  (/m)\n    查看當前會話使用的模型。\n\n  /model <編號>\n    切換模型；/model 0 恢復跟隨全域設定。\n\n  /compact [輪次]  (/c)\n    裁剪當前會話歷史。\n\n  /reboot  (/r)\n    重新啟動 QAIModelBuilder 服務。\n\n🤖 Claude Code 指令：\n\n  /cc new <目錄路徑> [會話名稱]\n  /cc list  (/cc l)\n  /cc use <序號>  (/cc u <序號>)\n  /cc status  (/cc s)\n  /cc models  (/cc ms)\n  /cc model  (/cc m)\n  /cc fork  (/cc f)\n  /cc stop  (/cc st)\n  /cc cd [目錄路徑]\n  /cc rename <新名稱>  (/cc r <新名稱>)\n  /cc close  (/cc c)\n  /cc delete  (/cc d)\n  /cc help  (/cc h)\n\n🔷 Open Code 指令：\n\n  /oc new <目錄路徑> [會話名稱]\n  /oc list  (/oc l)\n  /oc use <序號>  (/oc u <序號>)\n  /oc status  (/oc s)\n  /oc models  (/oc ms)\n  /oc model  (/oc m)\n  /oc stop  (/oc st)\n  /oc rename <新名稱>  (/oc r)\n  /oc close  (/oc c)\n  /oc delete  (/oc d)\n  /oc help  (/oc h)\n\n💡 提示：本機模型不可用時，會自動切換到雲端模型。\n💡 Claude Code 需在 Settings > AI Coding 中啟用並配置認證資訊。\n💡 Open Code 需在 Settings > AI Coding > Open Code 中啟用並配置服務位址。",
  ocPanelHtml: "(請見設定中的 OpenCode 說明)",
  ocText: "🔷 Open Code 指令說明\n\n  /oc new <目錄路徑> [會話名稱]\n    建立新的 Open Code 會話，繫結到指定專案目錄。\n    例：/oc new C:\\Projects\\MyApp 認證功能開發\n\n  /oc list  (/oc l)\n    列出你的所有 Open Code 會話（ID、名稱、狀態、模型）。\n\n  /oc use <序號>  (/oc u <序號>)\n    按 /oc list 序號切換會話（如 /oc use 1）。\n\n  /oc use <ID前綴>\n    按 ID 前綴切換會話（輸入 ID 前 8 位即可）。\n\n  /oc status  (/oc s)\n    查看當前 Open Code 會話狀態、對話輪次和工具呼叫次數。\n\n  /oc models  (/oc ms)\n    查看 Open Code 可用模型列表（帶序號），並顯示當前選中的模型。\n\n  /oc model  (/oc m)\n    查看當前 Open Code 使用的模型。\n\n  /oc model <編號>  (/oc m <編號>)\n    按 /oc models 序號切換 Open Code 模型（如 /oc model 1）。\n\n  /oc stop  (/oc st)\n    停止當前正在執行的 Open Code 任務，停止後可立即發送新訊息繼續對話。\n\n  /oc rename <新名稱>  (/oc r <新名稱>)\n    重新命名當前 Open Code 會話。\n\n  /oc close  (/oc c)\n    退出 OC 模式（會話保留，可用 /oc use 重新進入）。\n\n  /oc delete  (/oc d)\n    徹底刪除當前 Open Code 會話（不可恢復）。\n\n  /oc help  (/oc h)\n    顯示此說明。\n\n  <普通訊息>（OC 會話啟用時）\n    直接發訊息即可與 Open Code 對話，無需 /oc 前綴。\n    發送 /new 可切回普通 AI 對話模式（不影響 OC 會話）。\n\n💡 建立會話後，直接發訊息即可與 Open Code 對話\n💡 /oc stop 停止後可立即發送新訊息繼續對話\n💡 /oc close 退出後，會話仍保留，隨時可用 /oc use 重新進入\n💡 發送 /new 可切回普通 AI 對話模式\n💡 Open Code 需在 Settings > AI Coding > Open Code 中啟用並配置服務位址",
  shortcuts: "快速鍵",
  title: "說明",
  version: "版本",
  webuiCcHint:
    "💡 網頁端無需 /cc 命令：請點輸入框上方的「Claude Code」按鈕進入 CC 模式，在會話面板裡新建 / 切換 / 重新命名 / 刪除會話。\n（/cc 系列命令在微信 / 飛書通道仍可使用。）",
  webuiOcHint:
    "💡 網頁端無需 /oc 命令：請點輸入框上方的「Open Code」按鈕進入 OC 模式，在會話面板裡新建 / 切換 / 重新命名 / 刪除會話。\n（/oc 系列命令在微信 / 飛書通道仍可使用。）",
};

export default help;

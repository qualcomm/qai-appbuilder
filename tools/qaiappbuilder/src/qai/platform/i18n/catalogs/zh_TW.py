# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

MESSAGES: dict[str, str] = {
    "_meta.locale": "繁體中文",
    "cli.build.help.summary": "Model Builder 互動式會話（agentic 模型轉換 REPL）",
    "cli.build.help.description": (
        "進入 Model Builder agentic 聊天會話：用自然語言 + 斜線命令引導雲端 "
        "Agent 完成模型轉換。--model-file 指定要轉換的模型檔案（可多次），"
        "--llm 指定 Agent 使用的雲端大型模型。會話內用 /model /precision "
        "/dataset /mode /run 調整參數,/help 查看全部命令。"
    ),
    "cli.build.help.model_file": (
        "要轉換的模型檔案路徑（→ tool_params.model_paths）。可重複傳入多個。"
        "注意:這是被轉換的檔案,不是 Agent 的大型模型（後者用 --llm）。"
    ),
    "cli.build.help.llm": "Agent 使用的雲端大型模型 id（→ model_hint）。CLI 不支援本機 LLM。",
    "cli.build.help.precision": (
        "量化精度（→ tool_params.quant_precision）,如 fp16 或 fp16,w8a8。"
        "可選層級: {levels}。"
    ),
    "cli.build.help.dataset": "校準/評測資料集路徑（→ tool_params.dataset_path）。",
    "cli.build.help.mode": "初始模式:interactive（預設）逐輪互動;batch 偏向一次性轉換。",
    "cli.build.help.resume": (
        "接續已有的 model-build 會話:提供 conversation-id 接續指定會話;"
        "不帶參數則盡力接續最近一個（best-effort）。"
    ),
    "cli.build.label.unset": "(尚未設定)",
    "cli.build.params.summary": (
        "  模型檔案 : {files}\n"
        "  量化精度 : {prec}\n"
        "  資料集   : {ds}\n"
        "  模式     : {mode}"
    ),
    "cli.build.error.no_cloud_provider": "未偵測到雲端模型,請先執行 qai config setup 設定 provider\n",
    "cli.build.banner.ready": "Model Builder 會話已就緒。\n",
    "cli.build.banner.conv_id": "  會話 id  : {conv_id}\n",
    "cli.build.banner.agent_llm": "  Agent LLM: {model_hint}\n",
    "cli.build.banner.hint": "以自然語言與 Agent 對話,或用斜線命令調整參數（/help 查看全部）。\n",
    "cli.build.hint.turn_interrupted": "\n（已中斷目前回合）\n",
    "cli.build.error.turn_failed": "\n回合失敗: {exc_type}: {exc}\n",
    "cli.build.model.current": "目前模型檔案: {cur}\n",
    "cli.build.model.set": "已設定模型檔案: {files}\n",
    "cli.build.precision.current": "目前量化精度: {prec}\n",
    "cli.build.precision.invalid": "無效精度層級: {invalid};可選: {levels}\n",
    "cli.build.precision.set": "已設定量化精度: {prec}\n",
    "cli.build.dataset.current": "目前資料集: {ds}\n",
    "cli.build.dataset.set": "已設定資料集: {ds}\n",
    "cli.build.params.header": "目前會話參數:\n",
    "cli.build.mode.current": "目前模式: {mode}（用 /mode batch|interactive 切換）\n",
    "cli.build.mode.set": "已切換模式: {mode}\n",
    "cli.build.error.no_model": "尚未設定模型檔案,請先 /model <path> 或啟動時用 --model-file。\n",
    "cli.build.error.no_last_message": "沒有可重送的上一則訊息。\n",
    "cli.build.stop.requested": "已請求中止目前回合。\n",
    "cli.build.clear.new": "已開啟新會話: {conv_id}\n",
    "cli.build.history.unavailable": "歷史讀取尚未接通（CLI 暫不可達該用例）。\n",
    "cli.build.error.history_failed": "讀取歷史失敗: {exc_type}: {exc}\n",
    "cli.build.history.empty": "（暫無歷史訊息）\n",
    "cli.build.status.unavailable": (
        "執行狀態查詢尚未接通（依賴 Model Builder 後端用例）。"
        "可用 /params 查看目前參數。\n"
    ),
    "cli.build.workspace.unavailable": "工作區檢視尚未接通（依賴 Model Builder 後端用例）。\n",
    "cli.build.promote.unavailable": (
        "匯出/晉升為 pack 尚未在 CLI 接通。"
        "轉換產物就緒後可執行: qai pack import <artifact>\n"
    ),
    "cli.build.cmd.help.help": "顯示全部命令",
    "cli.build.cmd.help.model": "<path...> 設定/檢視要轉換的模型檔案",
    "cli.build.cmd.help.precision": "<csv> 設定/檢視量化精度",
    "cli.build.cmd.help.dataset": "<path> 設定/檢視資料集",
    "cli.build.cmd.help.params": "檢視目前模型/精度/資料集/模式",
    "cli.build.cmd.help.mode": "batch|interactive 切換模式",
    "cli.build.cmd.help.run": "用目前參數發起一次標準轉換指令",
    "cli.build.cmd.help.retry": "重送上一則使用者訊息",
    "cli.build.cmd.help.stop": "中止目前回合",
    "cli.build.cmd.help.status": "檢視執行狀態（尚未接通）",
    "cli.build.cmd.help.workspace": "檢視工作區（尚未接通）",
    "cli.build.cmd.help.promote": "匯出為 pack（提示用 qai pack import）",
    "cli.build.cmd.help.history": "列印會話歷史訊息",
    "cli.build.cmd.help.clear": "開啟新會話",
    "cli.build.cmd.help.exit": "退出會話",
    "cli.build.run.head": "請將模型檔案 {files} 轉換為可在 NPU 上執行的格式",
    "cli.build.run.precision_suffix": ",量化精度 {precision}",
    "cli.build.run.dataset_suffix": ",使用資料集 {dataset} 做校準/評測",
    "cli.build.repl.goodbye": "再見。\n",
    "cli.build.repl.ctrl_c_hint": "\n（再次按 Ctrl+C 退出）\n",
    "cli.build.prompt.repl": "build › ",
    "cli.config.provider.label.anthropic": "Anthropic (Claude)",
    "cli.config.provider.label.openai_compat": "OpenAI 相容",
    "cli.config.provider.label.ollama": "Ollama",
    "cli.config.provider.label.generic_cloud": "通用雲端",
    "cli.config.provider.prompt.type_header": "provider 類型: ",
    "cli.config.provider.prompt.type_choice": "選擇 (Enter 預設 anthropic) › ",
    "cli.config.provider.prompt.endpoint": "Endpoint (Enter 用預設 {base_url}) › ",
    "cli.config.provider.prompt.default_model": "預設模型 (Enter 用 {default_model}) › ",
    "cli.config.provider.warn.saved_probe_failed": (
        "⚠ provider {provider_id} 已儲存但連通性測試失敗: {error}\n"
    ),
    "cli.config.setup.error.needs_tty": (
        "qai config setup 需要互動式終端;指令稿場景請用 "
        "qai config provider add --type ... --api-key-stdin\n"
    ),
    "cli.config.setup.welcome": "歡迎使用 QAIModelBuilder 設定精靈。我會逐步協助你完成設定。\n\n",
    "cli.config.setup.step1_header": "[1/2] 雲端模型 Provider\n",
    "cli.config.setup.prompt.add_provider": "  你要新增雲端 AI 模型嗎？(用於 Chat / Model Builder Agent)",
    "cli.config.setup.prompt.add_another": "  還要新增另一個 provider 嗎？",
    "cli.config.setup.step2_header": "\n[2/2] 預設偏好\n",
    "cli.config.setup.prompt.language": "  介面語言 [繁體中文] › ",
    "cli.config.setup.done": (
        "  ✓ 完成！設定已儲存。執行 qai config provider 查看,"
        "qai build / qai app 開始使用。\n"
    ),
    "cli.config.setup.skipped_no_id": "  (略過:未提供 provider id)\n",
    "cli.config.setup.probing": "  ✓ 正在測試連通性…\n",
    "cli.config.setup.probe_ok": (
        "  ✓ 已儲存 provider {provider_id}（{status} OK,可用模型: {models}）\n"
    ),
    "cli.config.setup.no_model_list": "(無模型列表)",
    "cli.config.setup.probe_failed": (
        "  ⚠ provider {provider_id} 已儲存,但連通性測試失敗: {error}\n"
    ),
    "cli.app.hint.input_kind": "  輸入類型: {kind}（用 --{flag}）\n",
    "cli.app.hint.variants": "  可用變體 (--variant): {ids}\n",
    "cli.app.hint.params_header": "  可用參數 (--param key=val):\n",
    "cli.app.ok.wrote_artifact": "已寫入產物: {dest}\n",
    "cli.app.ok.wrote_output_json": "已寫入輸出 JSON: {dest}\n",
    "cli.app.ok.wrote_annotated": "已寫入標註產物: {dest}\n",
    "cli.app.error.no_annotated": "未產生標註產物（該 Pack 不輸出標註圖）\n",
    "cli.app.error.pack_not_found": "qai app: 找不到 Pack '{pack}'。執行 `qai app`（不帶參數）查看可用 Pack。\n",
    "cli.app.hint.cancel_prompt": "\n(已取消,/exit 退出)\n",
    "cli.app.hint.repl_banner": "App Builder 會話 — Pack: {pack} (輸入類型: {kind})\n直接輸入內容（路徑或文字）按 Enter 執行;/help 查看命令,/exit 退出。\n",
    "cli.app.error.file_missing": "找不到檔案: {value}\n請輸入存在的檔案路徑（可將檔案拖入終端,或用檔案總管「複製路徑」）。\n",
    "cli.app.hint.turn_interrupted": "\n(本輪已中斷)\n",
    "cli.app.ok.current_pack": "目前 Pack: {pack}\n",
    "cli.app.ok.switched_pack": "已切換 Pack: {pack}\n",
    "cli.app.ok.variant_set": "變體: {variant}\n",
    "cli.app.hint.variant_default": "(預設)",
    "cli.app.ok.params_set": "參數: {params}\n",
    "cli.app.ok.params_status": "目前參數: {params}\n目前變體: {variant}\n",
    "cli.app.hint.no_examples": "該 Pack 沒有內建範例。\n",
    "cli.app.hint.examples_usage": "用 /examples <序號> 執行內建範例,\n或 /examples <序號> <你的檔案路徑> 用該範例的參數跑你自己的輸入。\n",
    "cli.app.error.bad_example_index": "無效的範例序號: {idx}（用 /examples 查看可用序號）\n",
    "cli.app.error.no_input_override": "該 Pack 的輸入類型不支援命令列覆寫,請直接用 /param 調整參數。\n",
    "cli.app.error.example_files_missing_header": "內建範例 [{idx}] 的樣本檔案未隨 Pack 附帶:\n",
    "cli.app.error.example_files_missing_item": "  缺失 {m}\n",
    "cli.app.hint.example_override_usage": "可改用自己的檔案執行該範例的參數:\n  /examples {idx} <你的檔案路徑>\n或直接在提示符輸入你的檔案路徑按 Enter 執行。\n",
    "cli.app.error.history_unavailable": "執行歷史無法使用（容器未接入 list_runs）。\n",
    "cli.app.hint.no_history": "暫無執行歷史。\n",
    "cli.app.hint.no_last_result": "還沒有執行結果。\n",
    "cli.app.hint.no_output_to_export": "沒有可匯出的輸出。\n",
    "cli.app.hint.out_usage": "用法: /out <path>\n",
    "cli.app.help.model": "切換 Pack: /model <pack>",
    "cli.app.help.variant": "設定變體: /variant <id>",
    "cli.app.help.param": "設定參數: /param key=val",
    "cli.app.help.params": "檢視目前參數/變體",
    "cli.app.help.examples": "列出/執行內建範例: /examples [序號]",
    "cli.app.help.history": "檢視執行歷史",
    "cli.app.help.last": "重新列印上次輸出",
    "cli.app.help.out": "匯出上次輸出: /out <path>",
    "cli.app.help.help": "檢視命令列表",
    "cli.app.help.exit": "退出會話",
    "channels.tool.icon.success": "\u2705",
    "channels.tool.icon.error": "\u274c",
    "channels.tool.icon.pending": "\u23f3",
    "channels.tool.batch_header": "\U0001f504 工具呼叫進度（第 {batch_index} 批）：",
    "channels.tool.action.read": "\U0001f4d6 讀取: {path}",
    "channels.tool.action.write": "\u270f\ufe0f 寫入: {path}",
    "channels.tool.action.edit": "\U0001f527 編輯: {path}",
    "channels.tool.action.multi_edit": "\U0001f527 多處編輯: {path}",
    "channels.tool.action.glob": "\U0001f50d 搜尋: {pattern}",
    "channels.tool.action.grep": "\U0001f50e 尋找: {pattern}",
    "channels.tool.action.bash": "\U0001f4bb 執行: {command}",
    "channels.tool.action.ls": "\U0001f4c2 列目錄: {path}",
    "channels.tool.action.todo_write": "\U0001f4dd 更新 Todo",
    "channels.tool.action.todo_read": "\U0001f4cb 讀取 Todo",
    "channels.tool.action.web_fetch": "\U0001f310 擷取網頁: {url}",
    "channels.tool.action.web_search": "\U0001f50d 網路搜尋: {query}",
    "channels.tool.action.bg_process": "\U0001f680 背景行程 {label}{secondary}",
    "channels.tool.action.fallback": "\U0001f528 {name}",
    "channels.conversation.error.unknown_verb": "⚠️ 未知會話指令: /{verb}",
    "channels.conversation.error.invalid_limit": "⚠️ 參數必須為正整數，例如：/list 10",
    "channels.conversation.error.list_failed": "⚠️ 查詢歷史記錄失敗：{exc}",
    "channels.conversation.error.invalid_index": "⚠️ 編號必須為正整數，例如：/use 2",
    "channels.conversation.error.index_min": "⚠️ 編號從 1 開始",
    "channels.conversation.error.use_failed": "⚠️ 切換會話失敗：{exc}",
    "channels.conversation.error.status_failed": "⚠️ 取得狀態失敗：{exc}",
    "channels.conversation.error.empty_name": "⚠️ 名稱不能為空",
    "channels.conversation.error.rename_failed": "⚠️ 重新命名失敗：{exc}",
    "channels.conversation.error.delete_failed": "⚠️ 刪除失敗：{exc}",
    "channels.conversation.hint.no_history": "📋 目前沒有歷史會話記錄\n\n傳送 /new 開啟新會話",
    "channels.conversation.hint.use_usage": "用法：/use <編號>\n\n傳送 /list 檢視歷史會話清單",
    "channels.conversation.hint.rename_usage": "用法：/rename <新名稱>\n\n例如：/rename 專案討論",
    "channels.conversation.list.header": "📋 歷史會話（最近 {count} 條）：\n",
    "channels.conversation.list.round_suffix": " · {round_count}輪",
    "channels.conversation.list.footer_marker": "▶=目前會話",
    "channels.conversation.list.footer_hint": "💡 /use <編號> 切換到指定會話",
    "channels.conversation_adapter.label.untitled": "（未命名）",
    "channels.conversation_adapter.label.follow_global": "跟隨全域設定",
    "channels.conversation_adapter.list.empty": "📋 尚無歷史會話記錄",
    "channels.conversation_adapter.error.index_out_of_range": (
        "⚠️ 編號 {index} 超出範圍（共 {total} 條歷史）\n\n"
        "發送 /list 查看列表"
    ),
    "channels.conversation_adapter.ok.switched": (
        "✅ 已切換到會話：{title}\n"
        "共 {rounds} 輪對話歷史\n\n"
        "直接發訊息即可繼續對話。"
    ),
    "channels.conversation_adapter.status.no_active": "📊 目前沒有活躍會話\n\n直接發訊息即可開始新對話。",
    "channels.conversation_adapter.status.header": "📊 目前會話狀態",
    "channels.conversation_adapter.status.name": "名稱：{title}",
    "channels.conversation_adapter.status.rounds": "對話輪數：{rounds} 輪",
    "channels.conversation_adapter.status.model": "目前模型：{model}",
    "channels.conversation_adapter.status.tool_calls": "工具呼叫：{count} 次",
    "channels.conversation_adapter.status.context": "上下文大小：{tokens} tokens（含歷史）",
    "channels.conversation_adapter.status.conv_id": "會話 ID：{conv_id}…",
    "channels.conversation_adapter.status.hint": "\n💡 /rename <名稱> 重新命名  /delete 刪除目前會話",
    "channels.conversation_adapter.error.no_active_conv": "⚠️ 目前沒有活躍會話，請先發送一條訊息開始對話",
    "channels.conversation_adapter.error.rename_unavailable": "⚠️ 重新命名功能目前不可用",
    "channels.conversation_adapter.ok.renamed": "✅ 目前會話已重新命名為：{new_name}",
    "channels.conversation_adapter.ok.deleted": "🗑 目前會話已刪除，已開啟新會話。",
    "channels.conversation_adapter.time.unknown": "未知時間",
    "channels.conversation_adapter.time.just_now": "剛剛",
    "channels.conversation_adapter.time.minutes_ago": "{n} 分鐘前",
    "channels.conversation_adapter.time.hours_ago": "{n} 小時前",
    "channels.conversation_adapter.time.days_ago": "{n} 天前",
    "channels.help.main": (
        "\U0001f4d6 微信 / 飛書 / Chat 指令說明\n"
        "\n"
        "\u2328\ufe0f 一般對話指令：\n"
        "\n"
        "  /help  (/h)\n"
        "    顯示此幫助資訊。\n"
        "\n"
        "  /new  (/n)\n"
        "    儲存目前會話歷史後開啟新會話，歷史記錄保留在 Chat 介面可查看。\n"
        "\n"
        "  /clear  (/cl)\n"
        "    刪除目前會話歷史（不儲存）後開啟新會話，歷史記錄將被永久移除。\n"
        "\n"
        "  /list [N]  (/l [N])\n"
        "    查看最近 N 條歷史會話（預設 5 條），顯示名稱、時間和對話輪數。\n"
        "\n"
        "  /use <編號>  (/u <編號>)\n"
        "    切換到指定編號的歷史會話繼續對話。\n"
        "\n"
        "  /status  (/s)\n"
        "    查看目前會話狀態（名稱、對話輪數）。\n"
        "\n"
        "  /rename <新名稱>  (/rn <新名稱>)\n"
        "    重新命名目前會話。\n"
        "\n"
        "  /delete  (/del)\n"
        "    刪除目前會話（不可復原），並開啟新會話。\n"
        "\n"
        "  /stop  (/st)\n"
        "    立即停止目前正在執行的任務（一般對話或 Claude Code 任務均支援）。\n"
        "\n"
        "  /models  (/ms)\n"
        "    查看所有可用模型列表（本機 + 雲端），並顯示目前正在使用的模型。\n"
        "\n"
        "  /model  (/m)\n"
        "    查看目前會話使用的模型。\n"
        "\n"
        "  /model <編號>  (/m <編號>)\n"
        "    按 /models 編號切換模型，也可直接輸入 model_id。發送 /model 0 恢復跟隨全域設定。\n"
        "\n"
        "  /compact  (/c)\n"
        "    壓縮目前會話的上下文。\n"
        "    /compact           立即強制壓縮，顯示壓縮前後 token 對比\n"
        "    /compact status    查詢目前 token 用量、digest 大小、升級提示\n"
        "    /compact migrate   基於目前 digest 派生新會話（P5 handoff）\n"
        "    /compact clear     清空壓縮檢查點（digest + ledger + 計數）\n"
        "\n"
        "  /reboot  (/r)\n"
        "    重新啟動 QAIModelBuilder 服務，重啟完成後微信通道將自動重新連線。\n"
        "\n"
        "\U0001f510 檔案存取授權指令（FileGuard）：\n"
        "\n"
        "  /grant read <路徑>  (/g read <路徑>)\n"
        "    為目前會話授予指定路徑的讀取權限（會話結束後自動清除）。\n"
        "\n"
        "  /grant write <路徑>  (/g write <路徑>)\n"
        "    為目前會話授予指定路徑的寫入權限。\n"
        "\n"
        "  /grant exec <路徑>  (/g exec <路徑>)\n"
        "    為目前會話授予在指定路徑執行指令的權限。\n"
        "\n"
        "  /grant list  (/g list)\n"
        "    查看目前會話已授權的路徑列表。\n"
        "\n"
        "  /grant revoke <op> <路徑>\n"
        "    撤銷指定授權。例：/grant revoke read C:/WoS_AI/data\n"
        "\n"
        "  \U0001f4a1 授權僅在目前會話內有效，會話結束後自動清除。\n"
        "  \U0001f4a1 若 AI 工具呼叫被拒絕，可用 /grant 預先授權路徑，或聯絡管理員在\n"
        "     Settings > Security > IM 通道授權設定 中開啟 WebUI 彈窗審批。\n"
        "\n"
        "\U0001f916 Claude Code AI 編程助手指令（別名 /code）：\n"
        "\n"
        "  /cc new <目錄路徑> [會話名稱]\n"
        "    建立新的 Claude Code 會話，綁定到指定專案目錄。\n"
        "\n"
        "  /cc list  (/cc l)\n"
        "    列出你的所有 Claude Code 會話（ID、名稱、狀態）。\n"
        "\n"
        "  /cc use <序號>  (/cc u <序號>)\n"
        "    按 /cc list 序號切換會話（如 /cc use 1）。\n"
        "\n"
        "  /cc use <ID前綴>\n"
        "    按 ID 前綴切換會話（輸入 ID 前 8 位即可）。\n"
        "\n"
        "  /cc status  (/cc s)\n"
        "    查看目前 Claude Code 會話狀態、對話輪次和工具呼叫次數。\n"
        "\n"
        "  /cc models  (/cc ms)\n"
        "    查看 Claude Code 可用模型列表（帶序號），並顯示目前選中的模型。\n"
        "\n"
        "  /cc model  (/cc m)\n"
        "    查看目前 Claude Code 使用的模型。\n"
        "\n"
        "  /cc model <編號>  (/cc m <編號>)\n"
        "    按 /cc models 序號切換 Claude Code 模型。\n"
        "\n"
        "  /cc fork  (/cc f)\n"
        "    Fork 目前會話為新分支（保留原歷史，下次發訊息時產生新會話 ID）。\n"
        "\n"
        "  /cc stop  (/cc st)\n"
        "    停止目前正在執行的 Claude Code 任務，停止後可立即傳送新訊息繼續對話。\n"
        "\n"
        "  /cc cd [目錄路徑]\n"
        "    查看目前工作目錄（無參數），或修改目前 CC 會話綁定的工作目錄。\n"
        "\n"
        "  /cc rename <新名稱>  (/cc r <新名稱>)\n"
        "    重新命名目前 Claude Code 會話。\n"
        "\n"
        "  /cc close  (/cc c)\n"
        "    退出 CC 模式（會話保留，可用 /cc use 重新進入）。\n"
        "\n"
        "  /cc delete  (/cc d)\n"
        "    徹底刪除目前 Claude Code 會話（不可復原）。\n"
        "\n"
        "  /cc help  (/cc h)\n"
        "    顯示 Claude Code 指令說明。\n"
        "\n"
        "  <一般訊息>（CC 會話啟用時）\n"
        "    直接發訊息即可與 Claude Code 對話，無需 /cc 前綴。\n"
        "    發送 /new 可切回一般 AI 對話模式（不影響 CC 會話）。\n"
        "\n"
        "\U0001f537 OpenCode AI 編程助手指令：\n"
        "\n"
        "  /oc new <目錄路徑> [會話名稱]\n"
        "    建立新的 OpenCode 會話，綁定到指定專案目錄。\n"
        "\n"
        "  /oc list  (/oc l)\n"
        "    列出你的所有 OpenCode 會話（ID、名稱、狀態、模型）。\n"
        "\n"
        "  /oc use <序號>  (/oc u <序號>)\n"
        "    按 /oc list 序號切換會話。\n"
        "\n"
        "  /oc status  (/oc s)\n"
        "    查看目前 OpenCode 會話狀態、對話輪次和工具呼叫次數。\n"
        "\n"
        "  /oc models  (/oc ms)\n"
        "    查看 OpenCode 可用模型列表。\n"
        "\n"
        "  /oc model [編號]  (/oc m [編號])\n"
        "    查看 / 切換 OpenCode 模型。\n"
        "\n"
        "  /oc stop  (/oc st)\n"
        "    停止目前 OpenCode 任務。\n"
        "\n"
        "  /oc rename <新名稱>  (/oc r <新名稱>)\n"
        "    重新命名目前 OpenCode 會話。\n"
        "\n"
        "  /oc close  (/oc c)\n"
        "    退出 OC 模式。\n"
        "\n"
        "  /oc delete  (/oc d)\n"
        "    徹底刪除目前 OpenCode 會話（不可復原）。\n"
        "\n"
        "  /oc help  (/oc h)\n"
        "    顯示 OpenCode 指令說明。\n"
        "\n"
        "\U0001f4a1 提示：本機模型不可用時，若已設定雲端模型，系統會自動切換並提前通知。\n"
        "\U0001f4a1 Claude Code 需在 Settings > AI Coding 中啟用並設定認證資訊。\n"
        "\U0001f4a1 OpenCode 需在 Settings > AI Coding > OpenCode 中啟用並設定服務位址。"
    ),
    "channels.help.cc": (
        "\U0001f916 Claude Code 指令說明（別名 /code）\n"
        "\n"
        "  /cc new <目錄路徑> [會話名稱]\n"
        "    建立新的 Claude Code 會話，綁定到指定專案目錄。\n"
        "\n"
        "  /cc list  (/cc l)\n"
        "    列出你的所有 Claude Code 會話。\n"
        "\n"
        "  /cc use <序號>  (/cc u <序號>)\n"
        "    按 /cc list 序號切換會話。\n"
        "\n"
        "  /cc use <ID前綴>\n"
        "    按 ID 前綴切換會話（輸入 ID 前 8 位即可）。\n"
        "\n"
        "  /cc status  (/cc s)\n"
        "    查看目前會話狀態、對話輪次和工具呼叫次數。\n"
        "\n"
        "  /cc models  (/cc ms)\n"
        "    查看 Claude Code 可用模型列表（帶序號），並顯示目前選中的模型。\n"
        "\n"
        "  /cc model [編號]  (/cc m [編號])\n"
        "    查看 / 切換 Claude Code 模型。\n"
        "\n"
        "  /cc fork  (/cc f)\n"
        "    Fork 目前會話為新分支（保留原歷史）。\n"
        "\n"
        "  /cc stop  (/cc st)\n"
        "    停止目前正在執行的 Claude Code 任務。\n"
        "\n"
        "  /cc cd [目錄路徑]\n"
        "    查看目前工作目錄，或修改 CC 會話綁定的工作目錄。\n"
        "\n"
        "  /cc rename <新名稱>  (/cc r <新名稱>)\n"
        "    重新命名目前會話。\n"
        "\n"
        "  /cc close  (/cc c)\n"
        "    退出 CC 模式（會話保留）。\n"
        "\n"
        "  /cc delete  (/cc d)\n"
        "    徹底刪除目前會話（不可復原）。\n"
        "\n"
        "  /cc help  (/cc h)\n"
        "    顯示此說明。\n"
        "\n"
        "\U0001f4a1 建立會話後，直接發訊息即可與 Claude Code 對話\n"
        "\U0001f4a1 /cc fork 可在關鍵節點儲存進度，然後嘗試不同方向\n"
        "\U0001f4a1 /cc stop 停止後可立即傳送新訊息繼續對話\n"
        "\U0001f4a1 /cc close 退出後，會話仍保留，隨時可用 /cc use 重新進入\n"
        "\U0001f4a1 發送 /new 可切回一般 AI 對話模式"
    ),
    "channels.help.oc": (
        "\U0001f537 OpenCode 指令說明\n"
        "\n"
        "  /oc new <目錄路徑> [會話名稱]\n"
        "    建立新的 OpenCode 會話。\n"
        "\n"
        "  /oc list  (/oc l)\n"
        "    列出你的所有 OpenCode 會話（ID、名稱、狀態、模型）。\n"
        "\n"
        "  /oc use <序號>  (/oc u <序號>)\n"
        "    按 /oc list 序號切換會話。\n"
        "\n"
        "  /oc use <ID前綴>\n"
        "    按 ID 前綴切換會話。\n"
        "\n"
        "  /oc status  (/oc s)\n"
        "    查看目前會話狀態、對話輪次和工具呼叫次數。\n"
        "\n"
        "  /oc models  (/oc ms)\n"
        "    查看 OpenCode 可用模型列表。\n"
        "\n"
        "  /oc model [編號]  (/oc m [編號])\n"
        "    查看 / 切換 OpenCode 模型。\n"
        "\n"
        "  /oc stop  (/oc st)\n"
        "    停止目前正在執行的 OpenCode 任務。\n"
        "\n"
        "  /oc rename <新名稱>  (/oc r <新名稱>)\n"
        "    重新命名目前會話。\n"
        "\n"
        "  /oc close  (/oc c)\n"
        "    退出 OC 模式（會話保留）。\n"
        "\n"
        "  /oc delete  (/oc d)\n"
        "    徹底刪除目前會話（不可復原）。\n"
        "\n"
        "  /oc help  (/oc h)\n"
        "    顯示此說明。\n"
        "\n"
        "\U0001f4a1 建立會話後，直接發訊息即可與 OpenCode 對話\n"
        "\U0001f4a1 /oc stop 停止後可立即傳送新訊息繼續對話\n"
        "\U0001f4a1 /oc close 退出後，會話仍保留，隨時可用 /oc use 重新進入\n"
        "\U0001f4a1 發送 /new 可切回一般 AI 對話模式\n"
        "\U0001f4a1 OpenCode 需在 Settings > AI Coding > OpenCode 中啟用並設定服務位址"
    ),
    "channels.ai_coding.session.stopped_send_new": "\u23f9\ufe0f 目前任務已停止，可以傳送新訊息繼續。",
    "channels.ai_coding.session.no_active_task": "\u2139\ufe0f 目前沒有正在執行的 AI 編程任務。",
    "channels.ai_coding.session.usage_new": "\u2753 用法：/{verb} new <目錄路徑> [會話名稱]",
    "channels.ai_coding.session.dir_not_found": "\u26a0\ufe0f 目錄不存在：{workspace_path}",
    "channels.ai_coding.session.created": "\u2705 已建立 /{verb} 會話 {sid}\n工作目錄：{workspace_path}",
    "channels.ai_coding.session.switched": "\u2705 已切換到會話 {session_id}",
    "channels.ai_coding.session.no_active": "\u2139\ufe0f 目前沒有活動的 AI 編程會話。",
    "channels.ai_coding.session.usage_rename": "\u2753 用法：/{verb} rename [會話id] <新標題>",
    "channels.ai_coding.session.renamed": "\u2705 已重新命名為：{new_title}",
    "channels.ai_coding.session.closed": "\u2705 已關閉會話 {sid}（歷史保留，可用 /{verb} list 重新開啟）",
    "channels.ai_coding.session.deleted": "\u2705 已刪除會話 {sid}",
    "channels.ai_coding.session.usage_cd": "\u2753 用法：/{verb} cd <新工作目錄>",
    "channels.ai_coding.session.current_dir_usage": "\U0001f4c1 目前工作目錄：{cur_dir}\n\n用法：/{verb} cd <新工作目錄>",
    "channels.ai_coding.session.cd_ok": "\u2705 已切換工作目錄：{new_path}",
    "channels.ai_coding.session.no_upstream_no_fork": "\u26a0\ufe0f 目前會話尚未開始對話（未建立連線），無法 fork",
    "channels.ai_coding.session.forked": "\u2705 已 fork 出新會話 {new_sid}",
    "channels.ai_coding.session.stopped": "\u23f9\ufe0f 目前任務已停止。",
    "channels.ai_coding.session.opencode_aborted": "\u23f9\ufe0f 已中止 OpenCode 任務。可以傳送新訊息繼續對話。",
    "channels.ai_coding.session.no_running_task": "\u2139\ufe0f 目前沒有正在執行的任務。",
    "channels.ai_coding.session.models_header": "\U0001f4cb /{verb} 可用模型：",
    "channels.ai_coding.session.model_not_in_list": "\n\u2139\ufe0f 目前模型：{current}（不在清單中）",
    "channels.ai_coding.session.models_hint": "\n傳送 /{verb} model <序號> 或 /{verb} model <模型名> 切換模型",
    "channels.ai_coding.session.model_current": "\U0001f5a5\ufe0f /{verb} 目前模型：{current}\n\n傳送 /{verb} models 檢視可用模型\n傳送 /{verb} model <序號|模型名> 切換",
    "channels.ai_coding.session.model_switched": "\u2705 /{verb} 模型已切換為：{new_model}",
    "channels.ai_coding.error.command_failed": "\u26a0\ufe0f /{verb} {sub} 執行失敗: {exc}",
    "channels.ai_coding.error.index_out_of_range": "\u26a0\ufe0f 序號 {ref} 超出範圍（目前共 {count} 個會話）。傳送 /cc list 檢視。",
    "channels.ai_coding.error.multiple_matches": "\u26a0\ufe0f 匹配到多個會話，請提供更長的 ID 前綴。",
    "channels.ai_coding.error.model_index_out_of_range": "\u26a0\ufe0f 序號 {arg} 超出範圍（目前共 {count} 個模型）。傳送 /{verb} models 檢視。",
    "channels.ai_coding.error.model_unavailable": "\u26a0\ufe0f /{verb} model 暫不可用（設定未註冊）。",
    "channels.ai_coding.usage.no_content": "（AI 編程助手未返回內容）",
    "channels.ai_coding.usage.turn_warning_with_count": "⚠️ 目前會話已達到 {tc} 輪對話，建議盡快建立新會話。",
    "channels.ai_coding.usage.turn_warning_generic": "⚠️ 目前會話輪次較多，建議盡快建立新會話。",
    "channels.ai_coding.progress.subagent_started_desc": "\U0001f916 子任務已啟動：{desc}",
    "channels.ai_coding.progress.subagent_started": "\U0001f916 子任務已啟動",
    "channels.ai_coding.progress.subagent_done_desc": "\u2705 子任務完成：{desc}",
    "channels.ai_coding.progress.subagent_done": "\u2705 子任務完成",
    "channels.ai_coding.progress.subagent_error_desc": "\u26a0\ufe0f 子任務出錯：{err}",
    "channels.ai_coding.progress.subagent_error": "\u26a0\ufe0f 子任務出錯",
    "channels.ai_coding.progress.tool_count": "{tool_uses} 工具",
    "channels.ai_coding.progress.subagent_running": "\u23f3 子任務進行中 [{task_short}]",
    "channels.chat.compact.unavailable_module": "\u26a0\ufe0f /compact 目前不可用：chat 模組未啟用",
    "channels.chat.compact.unavailable": "\u26a0\ufe0f /compact 目前不可用",
    "channels.chat.compact.failed": "\u26a0\ufe0f /compact 失敗: {error}",
    "channels.chat.compact.done": "\u2705 /compact: {before_tokens} \u2192 {after_tokens} tokens（節省 {savings}）；已用 {budget_tokens} 預算的 {ratio_after_pct}%{escalation_suffix}。",
    "channels.chat.compact.status_ok": "\U0001f4ca /compact status: {used_tokens} / {budget_tokens} tokens（{ratio_pct}%）。Digest：{digest_bytes} 位元組。Ledger：{ledger_count} 條。",
    "channels.chat.compact.status_escalate": "\u26a0\ufe0f /compact status: {used_tokens} / {budget_tokens} tokens（{ratio_pct}%）。Digest：{digest_bytes} 位元組。Ledger：{ledger_count} 條。建議使用 /compact migrate 遷移至新會話。",
    "channels.chat.compact.migrate_ok": "\u2705 /compact migrate: 已基於目前 digest 派生新會話（{new_conversation_id}）。",
    "channels.chat.compact.migrate_no_digest": "\u26a0\ufe0f /compact migrate: 目前會話尚未生成會話摘要，無法遷移。請先傳送 /compact 或讓會話繼續增長以生成摘要。",
    "channels.chat.compact.clear_ok": "\u2705 /compact clear: 已清空壓縮檢查點（digest、ledger、計數）。",
    "channels.chat.compact.clear_none": "\u2139\ufe0f /compact clear: 目前沒有可清空的壓縮檢查點。",
    "channels.chat.compact.no_savings": "\u2139\ufe0f /compact: 會話已充分壓縮，無可繼續節省的空間（{tokens} tokens，已用 {budget_tokens} 預算的 {ratio_pct}%）。",
    "channels.chat.compact.empty_history": "\u2139\ufe0f /compact: 目前對話為空，無需壓縮。",
    "channels.chat.compact.invalid": "\u26a0\ufe0f /compact: 未知子指令。可用：/compact、/compact status、/compact migrate、/compact clear。",
    "channels.chat.compact.escalation_suffix": "（建議使用 /compact migrate 遷移至新會話）",
    "channels.chat.compact.no_conversation": "\u2139\ufe0f /compact: 目前使用者尚無活躍會話，請先傳送一則訊息。",
    "channels.chat.warning.turn_count": "\u26a0\ufe0f 目前會話已達到 {turn_count} 輪對話，建議盡快清理歷史或建立新會話。",
    "channels.chat.warning.turn_generic": "\u26a0\ufe0f 目前會話輪次較多，建議盡快建立新會話。",
    "channels.chat.subagent.label_multi": "【子Agent {index}/{total}】",
    "channels.chat.subagent.label_solo": "【子Agent {index}】",
    "channels.chat.subagent.start_line": "\n{label} 開始執行...\n任務：{preview}\n",
    "channels.chat.subagent.done": "  \u2705 子Agent {index} 完成（{rounds} 輪）\n",
    "channels.chat.subagent.error": "  \u274c 子Agent {index} 出錯：{message}\n",
    "channels.chat.agent_summary.header": "\n---\n\U0001f4cb 主Agent總結：\n",
    "channels.chat.label.wechat": "\u5fae\u4fe1",
    "channels.chat.label.feishu": "\u98db\u66f8",
    "channels.dispatch.error.invalid_command": "\u26a0\ufe0f 指令格式錯誤: {exc}",
    "channels.dispatch.error.bridge_unavailable": "\u26a0\ufe0f 處理服務暫時不可用，請稍後重試。",
    "channels.dispatch.error.application": "\u26a0\ufe0f {exc}",
    "channels.dispatch.error.unhandled": "處理訊息時出錯，請稍後重試。",
    "channels.dispatch.error.conversation_uc_unavailable": "\u26a0\ufe0f 會話管理指令目前不可用",
    "channels.dispatch.thinking_ack": "正在思考\u2026",
    "channels.dispatch.stop.stopped": "\u23f9\ufe0f 目前任務已停止，可以傳送新訊息繼續。",
    "channels.dispatch.stop.no_task": "\u2139\ufe0f 目前沒有正在執行的任務。",
    "channels.dispatch.chat.empty_reply": "（模型未返回內容）",
    "channels.dispatch.ai_coding.empty_reply": "（AI 編程助手未返回內容）",
    "channels.dispatch.new.cleared": "目前會話已清除 \U0001f5d1",
    "channels.dispatch.new.opened": "已開啟新會話 \u2728",
    "channels.dispatch.model.unavailable": "\u26a0\ufe0f /model 目前不可用",
    "channels.dispatch.model.default_label": "預設（未指定）",
    "channels.dispatch.model.show": (
        "\U0001f5a5\ufe0f 目前模型：{cur}\n\n"
        "傳送 /models 檢視可用模型\n"
        "傳送 /model <序號|模型名> 切換\n"
        "傳送 /model 0 恢復平台預設"
    ),
    "channels.dispatch.model.index_out_of_range": (
        "\u26a0\ufe0f 序號 {arg} 超出範圍"
        "（目前共 {total} 個模型）。傳送 /models 檢視。"
    ),
    "channels.dispatch.model.failed": "\u26a0\ufe0f /model 失敗: {exc}",
    "channels.dispatch.model.reset_ok": "\u2705 已恢復平台預設模型",
    "channels.dispatch.model.switched": "\u2705 已切換模型: {model_id}{auto_load_msg}",
    "channels.dispatch.model.autoload": "\n\n正在載入模型 {model_name}\u2026",
    "channels.dispatch.model.autoload_with_status": "\n\n正在載入模型 {model_name}（{status}）\u2026",
    "channels.dispatch.model.local_unavailable_fallback": "\u26a0\ufe0f 本機模型目前不可用，已自動切換到雲端模型：{fallback_id}",
    "channels.dispatch.models.none": "\u2139\ufe0f 目前沒有可用模型。",
    "channels.dispatch.models.header": "\U0001f4cb 可用模型（共 {total}）：",
    "channels.dispatch.models.local_header": "【本機模型】",
    "channels.dispatch.models.cloud_header": "【雲端模型】",
    "channels.dispatch.models.status_running": "執行中",
    "channels.dispatch.models.status_unloaded": "未載入",
    "channels.dispatch.models.item": "  [{idx}] {name}  ({status})",
    "channels.dispatch.models.cloud_item": "  [{idx}] {cid}",
    "channels.dispatch.models.current": "\n目前模型：{current}",
    "channels.dispatch.models.hint": "\n傳送 /model <序號> 切換模型；/model 0 恢復預設",
    "channels.dispatch.grant.unavailable": "\u26a0\ufe0f /grant 目前不可用：安全模組未接入",
    "channels.dispatch.grant.usage": (
        "\u26a0\ufe0f /grant 用法：\n"
        "  /grant read <路徑>    — 授予讀取權限\n"
        "  /grant write <路徑>   — 授予寫入權限\n"
        "  /grant exec <路徑>    — 授予執行權限\n"
        "  /grant list           — 檢視目前授權\n"
        "  /grant revoke <op> <路徑> — 撤銷授權\n\n"
        "  例：/grant read C:/WoS_AI/data"
    ),
    "channels.dispatch.grant.revoke_usage": (
        "\u26a0\ufe0f /grant revoke 用法：\n"
        "  /grant revoke <op> <path>\n"
        "  其中 op \u2208 read / write / exec"
    ),
    "channels.dispatch.grant.invalid_op": (
        "\u26a0\ufe0f 無效的操作類型：{op}"
        "（應為 read / write / exec）"
    ),
    "channels.dispatch.grant.needs_op_and_path": "\u26a0\ufe0f /grant 需要指定路徑和操作 (read / write / exec)",
    "channels.dispatch.grant.usage_add": (
        "\u26a0\ufe0f /grant 用法：/grant <op> <path>\n"
        "  其中 op \u2208 read / write / exec\n"
        "  例：/grant read C:/WoS_AI/data"
    ),
    "channels.dispatch.reboot.unavailable": "\u26a0\ufe0f /reboot 目前不可用",
    "channels.dispatch.reboot.failed": "\u26a0\ufe0f /reboot 失敗: {exc}",
    "channels.dispatch.reboot.scheduled": "\u2705 已請求重新啟動服務，處理程序將在數秒內結束並由守護處理程序拉起。",
    "channels.dispatch.image.download_failed": "收到一張圖片（下載失敗，無法顯示）",
    "channels.ai_coding_notify.sync.question": "[WebUI 提問]\n{question}",
    "channels.ai_coding_notify.sync.reply": "[Claude Code 回覆]\n{reply}",
    "channels.ai_coding_notify.task_done.with_summary": "\u2705 任務完成\n{summary}",
    "channels.ai_coding_notify.task_done.plain": "\u2705 任務完成",
    "channels.grant.error.no_active_session": "\u26a0\ufe0f /grant 需要先 /cc 或 /oc 啟動會話",
    "channels.grant.error.needs_path": "\u26a0\ufe0f /grant 需要指定路徑",
    "channels.grant.error.needs_op": "\u26a0\ufe0f /grant 需要指定操作 (read / write / exec)",
    "channels.grant.error.needs_op_and_path": "\u26a0\ufe0f /grant 需要指定路徑和操作 (read / write / exec)",
    "channels.grant.error.revoke_needs_path": "\u26a0\ufe0f /grant revoke 需要指定路徑",
    "channels.grant.error.module_disabled": "\u26a0\ufe0f 安全模組未啟用 /grant 功能",
    "channels.grant.error.grant_failed": "\u26a0\ufe0f /grant 失敗: {exc}",
    "channels.grant.error.revoke_failed": "\u26a0\ufe0f /grant revoke 失敗: {exc}",
    "channels.grant.error.list_failed": "\u26a0\ufe0f /grant list 失敗: {exc}",
    "channels.grant.list.empty": "\u2139\ufe0f 目前會話沒有授權的路徑。",
    "channels.grant.list.header": "\U0001f4cb 目前會話授權清單：",
    "channels.grant.ok.granted": "\u2705 已授權: {path} ({op})",
    "channels.grant.ok.revoked": "\u2705 已撤銷: {path}",
    "channels.grant.revoke.not_found": "\u2139\ufe0f 目前會話沒有 {path} 的授權。",
    # ── Context-overflow recovery (chat streaming) ────────────────────────
    "chat.context_recovery.compressing": "上下文已滿 \u2014 正在壓縮對話歷史並自動重試\u2026",
    "chat.context_recovery.succeeded": "上下文已壓縮（{before_tokens} \u2192 {after_tokens} tokens），你的訊息已完整重新傳送。較早的歷史已被摘要，因此本次回覆稍慢。",
    "chat.context_recovery.failed_no_room": "上下文已滿且無法進一步壓縮。請傳送 /compact 手動壓縮歷史，或傳送 /new 開啟新工作階段繼續 \u2014 你的訊息沒有遺失。",
    "chat.context_recovery.unavailable_mid_tool": "上下文在工具執行過程中被佔滿，這種情況無法自動壓縮。等本輪結束後，請傳送 /compact 壓縮歷史，或傳送 /new 開啟新工作階段繼續。",
}

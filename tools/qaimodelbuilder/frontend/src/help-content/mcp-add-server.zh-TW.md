# MCP 伺服器：新增與疑難排解

**MCP**（Model Context Protocol）是一種讓 AI 助理連接外部工具與資料來源的開放協定。每一台 MCP 伺服器都是獨立處理程序，QAI ModelBuilder 透過 stdio 或 HTTP 通道與它對話，把它公開的工具載入目前的對話工具集。

## 新增一台伺服器

按下 **+ 新增**（Add），逐項填寫下列欄位：

![MCP 新增伺服器示意](/help-images/mcp-add-server/wireframe.svg)

- **Name**：為這台伺服器取一個好識別、唯一的短名稱（例如 `filesystem-home`）。僅在本機顯示，不會傳到遠端。
- **Command**：可執行檔名或絕對路徑。必須存在於目前的 PATH，或填入**完整路徑**（例如 `C:\Program Files\nodejs\node.exe`）。
- **Arguments**：命令列參數，每列一項最易讀。範例：`-y @modelcontextprotocol/server-filesystem C:\Users\me\projects`。
- **Environment variables**：若子處理程序需要環境變數，以 `KEY=VALUE` 一列一條填入。看似機敏的值不會寫入紀錄檔。

儲存前請先按 **🔌 測試連線**。QAI ModelBuilder 會啟動處理程序、完成握手、列出它宣告的工具，並把握手錯誤顯示在按鈕下方。

## 連線失敗？依此清單排查

![MCP 連線疑難排解流程圖](/help-images/mcp-add-server/troubleshoot-flowchart.svg)

1. **命令是否在 PATH？** 開新終端執行 `where <命令>`（Windows）或 `which <命令>`（macOS/Linux）。若無輸出，請安裝或改為絕對路徑。
2. **子處理程序是否真的啟動？** 觀察面板底部的紀錄區。`ENOENT` / `command not found` → 屬第 1 類；`Server exited` / `handshake timeout` 通常是**參數錯誤**或相依性缺漏（例如 `npx` 首次下載慢）。
3. **企業代理是否阻擋？** 官方 registry 的伺服器首次執行需下載套件。請在系統環境變數中設 `HTTPS_PROXY` / `HTTP_PROXY`，或填入此伺服器的 env vars 後重啟。
4. **仍然失敗？** 複製「測試連線」輸出的最後約 20 列，附到問題回報。

## 常見錯誤碼

| 現象 | 意義 | 處理 |
| :--- | :--- | :--- |
| `ENOENT` | 找不到可執行檔 | 檢查 Command 拼字 / PATH / 使用絕對路徑 |
| `handshake timeout` | 已啟動但 5 秒內未回應 | 通常是首次下載慢，重試；或參數不對 |
| `EACCES` | 權限被拒 | Windows：檢查防毒；*nix：`chmod +x` |
| `port in use` | HTTP 伺服器連接埠衝突 | 更換連接埠 / 停掉佔用者 |

> 建議先從官方 registry（**Filesystem**、**GitHub**、**Fetch**）挑一個試通，再拓展到自研或第三方伺服器。

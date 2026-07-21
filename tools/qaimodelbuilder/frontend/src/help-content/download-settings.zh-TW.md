# 下載設定與網路疑難排解

下載中心會將模型壓縮檔從遠端拉到你電腦上的本地目錄。預設使用 `aria2c` 多執行緒下載（大檔更快、支援續傳），並從「版本清單 URL」讀取可用模型列表。

## 常用欄位

- **儲存目錄（Save Dir）**：模型解壓後的落地位置。**請盡量避免路徑中含有空格 / 中文 / 特殊字元**——QNN runtime 對部分插件在非 ASCII 路徑下會靜默失敗。預設值 `QAIModelBuilder/downloads/` 是安全的。
- **Version list URL**：版本清單 JSON 位址（預設為本專案 GitHub Release）。若使用私有鏡像可改為自架 HTTPS/HTTP 位址。
- **Catalog URL**：可下載模型清單 JSON 位址。
- **fetch_timeout / download_timeout**：分別控制「抓清單」與「下載單一檔案」的逾時秒數。
- **SSL verify**：預設開啟。僅於內部憑證環境暫時關閉；線上環境請勿關閉。

## 下載失敗時

1. **企業網需要代理**：於此面板下方「代理設定」填入 HTTP/SOCKS 位址；同一代理會傳遞給 `aria2c` 子處理程序。
2. **逾時**：把 `fetch_timeout_seconds` 從預設 30 調高到 120，首次下載新模型且來源較慢時常見。
3. **`aria2c: command not found`**：Windows 版通常已附帶 `aria2c.exe`；若遺失，可從 <https://github.com/aria2/aria2/releases> 重新安裝。
4. **只下到一半**：`aria2c` 支援續傳——直接**重試同一模型**即可續下，不會重下已完成分段。
5. **SSL 錯誤**：先確認是否走了企業 MITM 代理。**請勿**永久停用 SSL 驗證。

## 官方參考

- aria2 官網：<https://aria2.github.io/>
- aria2 命令列手冊：<https://aria2.github.io/manual/en/html/aria2c.html>

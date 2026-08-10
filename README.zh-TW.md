# MeshTelegram Bridge

[English](README.md) | 繁體中文

MeshTelegram Bridge 可在加密的 Meshtastic MQTT 頻道與經授權的
Telegram 聊天室／主題之間，雙向轉送文字訊息。

## 主要功能

- 解密 Meshtastic MQTT 文字封包並轉送至 Telegram。
- 將指定 Telegram 聊天室的訊息廣播至 Meshtastic。
- 僅接受 channel hash 正確的加密封包；拒絕 MQTT plaintext `decoded` 封包。
- 依發送節點與 packet ID，在 60 秒內過濾 MQTT 重送封包。
- 靜默忽略未授權 Telegram 聊天室的訊息與命令。
- 偵測 Telegram 重複輪詢，顯示清楚的中文原因並停止衝突實例。
- 遵守 Meshtastic 233 bytes UTF-8 payload 上限；過長的 Telegram 訊息會直接
  丟棄且不傳送。
- 同時顯示終端日誌並寫入 UTF-8 輪替日誌；預設 `INFO` 不記錄訊息內容。
- 提供繁體中文設定工具，可驗證設定並測試 Telegram 與 MQTT 連線。
- 設定工具提供聊天分頁，可監看所有啟用路由，並發送文字到 Meshtastic、
  Telegram 或同時傳送到兩邊。
- 可設定最多五組一對一 Meshtastic 頻道與 Telegram 聊天室／主題路由。
- 設定工具會顯示 MQTT、Telegram 即時狀態，以及本次執行的轉送與丟棄統計。
- 可選用系統匣、登入後自動啟動與正式 Release 更新通知；預設皆不啟用。

## 使用 Windows 執行檔快速開始

1. 從[最新 GitHub Release](https://github.com/jorden2895/meshtelegram-bridge/releases/latest)
   下載 `MeshTelegramBridge.exe` 與 `MeshTelegramBridgeSettings.exe`。
2. 將兩個檔案放在同一個資料夾。
3. 開啟 `MeshTelegramBridgeSettings.exe`。
4. 填入 Telegram、MQTT 與虛擬節點設定，然後按下「驗證」。
5. 按下「測試連線」檢查 Telegram Token 與 MQTT 帳號。這項測試不會啟動
   Bridge，也不會傳送訊息。
6. 儲存設定後，啟動 `MeshTelegramBridge.exe`。
7. Bridge 執行期間，可在設定工具的「聊天」分頁監看訊息，或選擇路由後發送。

設定工具會在執行檔旁建立 `config.json`。此檔案包含 Telegram Bot Token、
MQTT 密碼與 Meshtastic 頻道金鑰，請勿公開、分享或提交至 Git。

## 設定檔

從原始碼執行時，請以 `config.json.example` 為範本。舊版單一路由
`config.json` 可直接沿用；新功能由 `features` 控制：

- `logging_level`：`DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL`。
- `telegram`：Bot Token 與唯一獲准使用 Bridge 的聊天室 ID。
- `mqtt`：Broker 位址、連接埠、帳號、根主題、頻道名稱與頻道金鑰。
- `node`：Meshtastic 虛擬節點 ID、完整名稱與簡短名稱。
- `bridge_ui.display_name`：介面發送訊息的前綴與監看來源名稱；舊設定未提供時
  預設為 `Bridge UI`。
- `routes`：啟用多頻道路由時使用，最多五組；每組包含頻道、金鑰、聊天室與
  可選的 Telegram 主題 ID。
- `features`：狀態統計、本機狀態 API、多路由、系統匣及更新選項。

`mqtt.root_topic` 會自動移除多餘的前後斜線並補上結尾斜線，但禁止 MQTT
wildcard `+`、`#`。MQTT 帳號與密碼可同時留空以使用匿名 Broker。節點簡短
名稱最多四個字元。

程式會在建立網路連線前驗證所有必填設定。若 MQTT 無法連線，Telegram
輪詢也不會繼續啟動，避免程式看似正常但實際只能連上一端。

## 狀態、系統匣與更新

本機狀態 API 只監聽 `127.0.0.1`，使用每次啟動隨機產生的權杖；設定工具將
超過五秒未更新的心跳視為離線。狀態內容不包含 Telegram Token、MQTT 帳號或
頻道金鑰，最近錯誤中的已知敏感值也會遮蔽。所有統計在 Bridge 重啟後歸零。

設定工具的「聊天」分頁會監看所有啟用路由，僅在 Bridge 記憶體保留最近 200
筆訊息；不會寫入磁碟，Bridge 重啟後即清空。發送時可選擇 Meshtastic、
Telegram 或兩邊同時，兩個目的地會分別回報結果。從介面發出的訊息會使用
設定的顯示名稱作為前綴（例如 `[基地台]: `），監看來源也會顯示相同名稱。
傳送至 Meshtastic 時，包含此前綴的完整 UTF-8 內容不得超過 233 bytes。
Bridge 未執行或停用本機狀態 API 時，聊天功能不可使用。

系統匣模式整合於 `MeshTelegramBridge.exe`。雙擊圖示或選擇「設定」可開啟
設定工具，選單也可結束 Bridge。Release 執行檔預設不建立主控台；未啟用系統匣
或勾選「顯示主控台」時才顯示。

更新功能只查詢正式 GitHub Release，不傳送裝置識別或使用統計。預設只通知，
也可設定為下載或延後安裝；下載的兩個 EXE 必須通過 GitHub 提供的 SHA-256
摘要驗證。預設檢查間隔為 24 小時，也可在設定工具立即檢查。

### 取得 Telegram 聊天室 ID

請將 Bot 加入預定使用的私人聊天室或群組。基於安全性，Bot 只會回覆已設定
的聊天室，因此首次設定時，需透過 Telegram Bot API 或可信任的 Chat ID 工具
取得 ID，再填入設定工具並重新啟動 Bridge。

同一個 Telegram Bot Token 同時間只能由一個程式進行輪詢。

## 在 Windows 從原始碼執行

需求：

- Python 3.10 或更新版本
- 已安裝 Python Launcher（`py`）的 Windows
- MQTT Broker 與 Meshtastic 頻道
- Telegram Bot Token 與目標聊天室 ID

雙擊 `setup_windows.bat`，程式會建立或修復 `.venv`，並安裝鎖定版本的執行
依賴。請勿從其他電腦複製 `.venv`，因為虛擬環境包含該電腦的絕對路徑。

手動安裝：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.json.example config.json
```

開啟設定工具：

```powershell
.\open_settings.bat
```

執行 Bridge：

```powershell
.\run_meshtelegram_bridge.bat
```

也可以執行 `python main.py`。按下 `Ctrl+C` 可安全停止程式。

查看目前版本：

```powershell
python main.py --version
```

## 日誌

Bridge 會在程式旁寫入 `MeshTelegramBridge.log`，單一檔案上限為 1 MiB，最多
保留五份；終端輸出仍會同時顯示。可從設定工具按下「開啟日誌資料夾」。

預設 `INFO` 只記錄連線及轉送相關資訊，不包含訊息本文。`DEBUG` 可能包含訊息
內容，僅建議在受控的除錯環境中暫時使用。

## 常見問題

- **批次檔閃一下就關閉：**請開啟命令提示字元或 PowerShell，在視窗中執行
  `.bat`，即可保留並閱讀錯誤訊息。
- **複製的 `.venv` 指向另一台電腦的 Python：**刪除該 `.venv`，並在目標
  電腦重新執行 `setup_windows.bat`。
- **Telegram 顯示重複輪詢衝突：**停止其他使用相同 Bot Token 的程式或電腦，
  僅保留一個 Bridge 實例。
- **MQTT 或 Telegram 無法連線：**開啟設定工具並使用「測試連線」，兩項服務
  會分別顯示結果。
- **Telegram 訊息沒有被轉送：**確認訊息來自指定聊天室，且完整 UTF-8 內容
  未超過 233 bytes。

## 開發與測試

執行不會連接 Telegram 或 MQTT 的單元測試：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

建置兩個獨立執行檔：

```powershell
.\build_release.ps1
```

建置流程以 `version.py` 作為唯一版本來源，並驗證 GitHub Release Tag 是否與
程式版本一致。

## 授權與上游專案

MeshTelegram Bridge 最初衍生自
[pdxlocations/connect](https://github.com/pdxlocations/connect)，該專案是透過
MQTT 運作、不需要實體節點的 Meshtastic 用戶端。本專案已大幅修改為專用的
Meshtastic 與 Telegram 橋接程式，並加入設定驗證、繁體中文設定 UI、安全檢查
與自動化測試。

上游專案與本衍生作品皆採用 GNU General Public License，詳見
[LICENSE](LICENSE)。

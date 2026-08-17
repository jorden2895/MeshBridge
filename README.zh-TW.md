# MeshBridge

MeshBridge 是 Windows 可攜式文字訊息橋接工具，可在 Meshtastic MQTT、Telegram 與 Discord 之間轉送訊息。3.1 版在單一 `MeshBridge.exe` 中加入關鍵字回應、地牛 EEW 轉送與 Cron 排程。

[English README](README.md)

## 主要功能

- 單一 Windows 執行檔、單一程序
- Meshtastic ↔ Telegram ↔ Discord 三方純文字橋接
- 最多 20 條可個別啟用或停用的路由
- 每條路由可選 Telegram、Discord 或同時使用兩者
- 顯示 Bridge、平台、路由與本次執行統計的儀表板
- 內建聊天監看與訊息發送，支援 `Ctrl+Enter`
- 路由清單可新增、刪除、排序與編輯
- 日誌會遮蔽敏感資訊，並可選擇「INFO（一般）」、「WARNING（警告）」或「DEBUG（詳細）」篩選
- 介面可選系統、淺色或深色主題
- 固定啟用系統匣，可選擇登入 Windows 後自動啟動
- 單一實例：再次執行程式會喚醒既有視窗
- 僅檢查正式 Release，下載時驗證 GitHub SHA-256
- 關鍵字完全相符／包含比對，命中後只回覆原來源
- 地牛 Wake Up! EEW 連動，可轉送到選定路由的全部平台
- 跟隨 Windows 本機時區的五欄 Cron 排程訊息

超過 Meshtastic 233 bytes 上限的訊息會直接丟棄，不另行通知。Discord 的圖片、附件及其他非文字內容會忽略。聊天紀錄與統計只保留在記憶體，關閉程式後歸零。

## 快速開始

1. 從最新 GitHub Release 下載 `MeshBridge.exe`。
2. 放到可寫入的資料夾後執行。
3. 依序完成 MQTT、平台、節點與路由設定。
4. 先按「測試連線」，再按「儲存並套用」。

按視窗右上角 X 只會縮到系統匣。系統匣右鍵選單可顯示主視窗、開啟設定、啟動／停止／重新啟動 Bridge，或完整結束程式。雙擊系統匣圖示會直接開啟「設定」頁；單擊不執行動作。

`config.json` 會放在執行檔旁，可能包含 Bot Token、MQTT 帳密及頻道金鑰，請勿公開或提交到 Git。

## 路由設定

每條啟用路由代表一個 Meshtastic 頻道，並可傳送至 Telegram、Discord 或兩者。每條路由至少要啟用一個目的地，支援只使用 Telegram 或只使用 Discord。

每條路由包含：

- 不重複的路由名稱與 Meshtastic 頻道名稱
- 解碼後為 16、24 或 32 bytes 的 Base64 頻道金鑰
- 選用的 Telegram 聊天室／主題
- 選用的 Discord 頻道

## 自動化

「自動化」頁可新增最多 50 條關鍵字規則與 50 項排程。自動化文字最多 233 UTF-8 bytes，以確保 Meshtastic、Telegram 與 Discord 都能使用。

修改規則後，請按自動化頁底部的「儲存並套用」；完成重新啟動後，新規則立即生效。

- 關鍵字支援「完全相符」與「包含」，忽略大小寫及前後空白；同一訊息只執行清單中第一條命中規則。
- 每條關鍵字規則可從下拉核取清單選擇一個或多個適用路由，無須手動輸入路由名稱。
- Cron 使用 `分鐘 小時 日期 月份 星期` 五欄格式，例如 `0 9 * * 1-5`；程式關閉、休眠或 Bridge 停止時錯過的排程不補送。
- 排程與 EEW 會送到選定路由所有已啟用的平台。

### 地牛 Wake Up! EEW

1. 在 MeshBridge「路由」頁逐一開啟需要接收警報路由的「啟用 EEW 自動發訊」，儲存並套用。
2. 在地牛 Wake Up! v4.2.0 的連動設定選擇同一支 `MeshBridge.exe`；「僅呼叫一次」可依需求選擇，並非必要條件。
3. 使用地牛內建的測試發送功能確認所有目的地。

地牛 v4.2.0 會傳入 `--local-intensity=5+`、`--remaining-time=20` 等具名參數，MeshBridge 會取用所在地震度與剩餘秒數。仍可用下列精簡格式手動診斷：

```powershell
.\MeshBridge.exe 5+ 20
```

手動診斷時仍可使用 `.\MeshBridge.exe --eew 5+ 20`。

若 MeshBridge 尚未執行或 Bridge 已停止，會在背景啟動後發送。此功能定位為私人／內部防災轉送，不是官方地震發布服務，也不會推測震央、規模或報號。

## 設定升級

v3.1 使用 `config_version: 5`。舊 v4 的 EEW 目標會自動轉成各路由的開關；遷移前會依原版本建立備份，若遷移失敗則不修改原始檔。

v3 移除已失效的 `multi_route_enabled`、`status_api`、`tray.enabled`、`tray.show_console`、全域 `discord.enabled` 與舊單一路由欄位。`MeshBridgeSettings.exe` 與 `open_settings.bat` 已不再使用。

## 從原始碼執行

建議在 Windows 使用 Python 3.14。

```powershell
.\setup_windows.bat
.\.venv\Scripts\python.exe .\main.py
```

不要把 `.venv` 複製到其他電腦；每台開發電腦都應自行執行 `setup_windows.bat`。

## 測試與封裝

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m bandit -r . -x ./.venv,./build,./dist,./tests -ll
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
.\build_release.ps1
```

封裝結果只會產生 `dist\MeshBridge.exe`。可用下列方式確認版本：

```powershell
.\dist\MeshBridge.exe --version
```

## 設定範例

請參考 [`config.json.example`](config.json.example)，不要提交實際使用的 `config.json`。

## 安全說明

- v3 的聊天、狀態及日誌都留在同一程序內，不再開放 localhost 狀態 HTTP API。
- 已知帳密、Token 與金鑰會從介面日誌及錯誤中遮蔽。
- 自動更新只接受官方 Release 中具 GitHub SHA-256 摘要的 `MeshBridge.exe`。
- Meshtastic 頻道加密不能取代可信任的 Broker 或傳輸層加密。

## 授權

請參考 [LICENSE](LICENSE)。

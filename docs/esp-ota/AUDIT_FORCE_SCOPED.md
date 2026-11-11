# ESP OTA audit — trước khi sửa (baseline 2026-11-11)

Nguồn đã đọc (không suy đoán):
- Deploy Agent `hungtranbkit/mesflow-agent@c87edd09d` (2.24.49): `agent.py` 5520-5735, `ota_control.py` (toàn bộ), `templates/kiosk/_ota_tab.html`.
- Production Agent `deploy.mesflow.net/agent/health` = `2.24.37-docker-runtime` (cũ hơn repo).
- MESFlow `hungtranbkit/mesflow@3bbc630e0`: `web/internal_ota.py`, `core/ota_readiness.py`, `db/repositories/execution.py` (verify_token_any, heartbeat), migrations 0027-0029.
- Firmware `mesflow-esp32-kiosk@24100836` (5.6.2-KIMEX-QR-HOLD): `esp/mesflow_app.cpp` 68-79, 1800-1850, 3238-3256, 4820-4990, 6552, 6764; `scripts/build-ota.sh`.
- Arduino core esp32 3.3.11: `cores/esp32/esp32-hal-misc.c:285-315`, `esp32s3-libs/3.3.11/sdkconfig`.

## 1. Luồng end-to-end hiện tại

```
Upload (.bin + version/hardware_model nhập tay, hoặc .zip 1 bin + 1 manifest)
  -> ota_control._save_firmware: sha256/size do Agent tính; version/hardware lấy TỪ FORM/MANIFEST (không kiểm binary)
  -> create_job(firmware, kiosk_ids chọn tay) : yêu cầu device online, so hardware_model string, 1 job mở/máy
Fleet: GET MES_URL/api/internal/kiosks (X-MESFlow-Internal-Token) -> ota_devices (PK kiosk_id, không instance, không xoá máy bị gỡ)
ESP poll 12s (backoff 25s..30min): GET {OTA_AGENT_BASE}/api/esp-ota/check?kiosk_id&current_version&hardware_model, header X-Kiosk-Token
  -> Agent gọi MESFlow /api/internal/kiosks/<uuid>/ota-readiness (token thiết bị) -> ota_ready (ui READY, queue 0, health OK)
  -> check_update: job RUNNING + device PENDING/DOWNLOADING; CHẶN nếu version <= current (VERSION_NOT_NEWER) => không thể force cùng version
  -> trả url = WORKSHOP_AGENT_PUBLIC_URL hoặc X-Forwarded-Host (header) + /api/esp-ota/file/<id>
ESP: otaIdleSafe (READY, không worker/operation/activeSession, không pending tx/offline/action queue) -> download HTTPS (CA pinned, không setInsecure)
  -> Content-Length == size, Update.write, SHA256 stream == sha256 -> Update.end -> NVS mf_ota{from,to,firmware,pending} -> reboot
Boot: confirmPendingOtaBoot (khi rt.online): to != FW_VERSION -> OTA_ROLLBACK; else OTA_BOOT_NEW_VERSION, mark valid nếu PENDING_VERIFY, OTA_HEALTHCHECK_OK
Event: POST /api/esp-ota/event -> record_event: map status, gán vào job mở mới nhất của kiosk_id (không kiểm firmware_id)
Success: (a) OTA_HEALTHCHECK_OK, hoặc (b) sync_devices: heartbeat inventory có firmware_version == job version => SUCCESS "RECONCILED_HEARTBEAT"
```

## 2. Lỗ hổng/khoảng trống so với đặc tả (bằng chứng)

| # | Phát hiện | Bằng chứng | Mục spec |
|---|---|---|---|
| G1 | Không có instance/ownership ID nào ở Agent; bảng `ota_devices` PK=`kiosk_id`; jobs không có scope. Agent chỉ có 1 `MES_URL` nên scope ngầm định = "MESFlow đang trỏ tới"; đổi `WORKSHOP_MES_URL`/khôi phục DB khác => jobs cũ vẫn được phát cho kiosk_id trùng. | ota_control.py:46-68, agent.py:46 | B |
| G2 | Firmware mặc định `OTA_AGENT_BASE=https://deploy.mesflow.net/agent` và loadConfig ép mọi URL `*.mesflow.net` không phải deploy.* về giá trị này; `otaAgentBase()` fallback `SERVER_BASE`. => kiosk khách hàng khác gửi `X-Kiosk-Token` tới Agent của Kimex. | mesflow_app.cpp:71,1802-1808,1846-1847 | B, F |
| G3 | Download URL dựng từ `X-Forwarded-Host` khi chưa đặt `WORKSHOP_AGENT_PUBLIC_URL`; ESP gắn `X-Kiosk-Token` vào URL đó, không kiểm cùng origin. | agent.py:5541-5551; mesflow_app.cpp:4857-4861 | B (lộ auth) |
| G4 | Không thể force cùng version: `check_update` trả `VERSION_NOT_NEWER`. | ota_control.py check_update | D |
| G5 | Không có snapshot/pin: job chỉ trỏ `firmware_id`, OK; nhưng không có batch cho "tất cả", create_job lỗi toàn bộ nếu 1 máy offline (`DEVICE_OFFLINE`) hoặc đã có job mở. | create_job | D |
| G6 | Profile = chuỗi `hardware_model` ("ES3C28P") do người upload nhập; Kiosk Linux và M910 cùng chuỗi => có thể nạp chéo. Không validate image header/chip/app_desc. | _save_firmware, mesflow_app.cpp:79 | C |
| G7 | SUCCESS giả: `sync_devices` đánh SUCCESS khi inventory báo version trùng (heartbeat bất kỳ, kể cả giả lập từ PC). | sync_devices "RECONCILED_HEARTBEAT" | G |
| G8 | Event không gắn job/artifact: event cũ/ACK lặp từ job trước áp lên job mới đang mở; không có lease/claim; DOWNLOADING bị bỏ dở không bao giờ hết hạn (kẹt vĩnh viễn, chặn job mới bởi unique index). | record_event, ota_one_open_job_device | D |
| G9 | Không có retry/backoff phía Agent; OTA_FAILED là terminal ngay. | record_event | D |
| G10 | Rollback tự động KHÔNG hoạt động thực tế: sdkconfig `CONFIG_APP_ROLLBACK_ENABLE=y` nhưng Arduino `initArduino()` gọi `esp_ota_mark_app_valid_cancel_rollback()` vì `verifyRollbackLater()` (weak) trả false; firmware không override => image mới crash sẽ boot-loop thay vì rollback. | esp32-hal-misc.c:285-315 | G |
| G11 | Safe point chờ vô hạn khi `activeSessionId>0`/hasWorker/hasOperation (phiên làm việc dài hạn đã lưu server). | otaIdleSafe | E |
| G12 | Boot confirm chỉ khi `rt.online`; ACK không có build/artifact identity ngoài `firmware_id` từ NVS. | mesflow_app.cpp:6764, 4970-4985 | G |
| G13 | MESFlow DB còn hệ OTA thứ hai (0027-0029: kiosk_firmware, kiosk_ota_deployments, stages, canary, maintenance window) — không có route Python nào dùng (grep `kiosk_ota_` trong app/mesflow chỉ ra rbac.py). Giữ lịch sử, không dùng làm engine. | migrations 0027-0029 | A |

Điểm tốt giữ nguyên: TLS fail-closed (không setInsecure), SHA256 stream + size, chỉ app .bin (từ chối merged), ghi slot inactive qua `Update`, backoff poll, OTA không làm hỏng `rt.online`, MESFlow outage => WAITING thay vì FAILED.

## 3. Bảng setting (trước khi sửa)

| Setting | Owner hiện tại | Effective/default | Source | Readers | Writers | Hướng xử lý |
|---|---|---|---|---|---|---|
| OTA Agent URL (ESP) | firmware NVS `mesflow_cfg/ota_agent` | `https://deploy.mesflow.net/agent` (compile) | compile default, NVS, maintenance UI `/maintenance/api-url` | otaAgentBase, checkForOta, otaEvent | loadConfig (ép về default), saveConfig, maintenance | Bỏ default compile; MESFlow quảng bá `MESFLOW_OTA_AGENT_URL` qua heartbeat (kênh đã xác thực) => ESP lưu NVS; giữ legacy NVS cũ; chặn gửi token tới default cũ khi SERVER_BASE không thuộc mesflow.net |
| MESFlow API URL | NVS `mesflow_cfg/server` | rỗng | setup portal, maintenance | mọi API | setup/maintenance | giữ nguyên |
| Poll interval | compile `OTA_CHECK_INTERVAL_MS` | 12s, backoff 25s→30min | compile | scheduleOtaCheck | — | giữ 12s; backoff tối đa 5 phút khi có job đang chờ |
| HW model | compile `HW_MODEL` | ES3C28P | compile | heartbeat, check | — | giữ (legacy), thêm `HW_PROFILE` compile per-profile |
| Agent public URL | Agent env `WORKSHOP_AGENT_PUBLIC_URL` | không đặt => Host header | env | _ota_download_url | — | bắt buộc env hoặc đường dẫn tương đối cùng origin; ESP kiểm origin |
| Max app size | Agent env `MESFLOW_OTA_PARTITION_SIZE` | 3342336 | env | _save_firmware | — | thay bằng `ota_slot_size` per-profile từ build manifest |
| Admin token | Agent env `MESFLOW_DEPLOY_AGENT_TOKEN`/`MESFLOW_OTA_PUBLISH_TOKEN` | — | env | _ota_admin_token_valid | — | giữ |
| Internal token | env `MESFLOW_INTERNAL_API_TOKEN` (cả 2 bên) | — | env | inventory/readiness | — | giữ; thêm fingerprint chống clone |
| Device token | MESFlow `kiosk_identities.token_hash`; ESP NVS identity | per-device | enrollment | readiness | bind | giữ, không clone |
| Rollout policy cũ | MESFlow DB `kiosk_ota_settings`, deployments/stages (0027-0029) | không có reader | migration | none | none | giữ bảng lịch sử, không dùng; tài liệu hoá là legacy |
| Scanner baud | NVS `mesflow_cfg/scan_baud` | 115200 compile, autodetect | runtime | scanner | autodetect/console | giữ, OTA không chạm NVS |
| Station | NVS `mesflow_cfg` + server approved | — | bind | — | bind | giữ |

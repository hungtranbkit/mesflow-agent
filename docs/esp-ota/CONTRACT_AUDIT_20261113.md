# Đối chiếu OTA MESFlow — 13/11/2026

## Kết luận

CHƯA ĐỦ ĐIỀU KIỆN nghiệm thu phần cứng hoặc phát hành production. Không flash,
không gọi API force, không deploy trong lần audit này. Nhánh sửa riêng ở cả ba
repo: `audit/ota-contract-20261113`, dựa trên đúng head của ba PR draft.
Không thay firmware version 5.6.6; build ID gắn commit nên binary sửa phải có
build ID/SHA mới, không được gọi là binary đã nghiệm thu.

## Baseline đã đọc và CI

| Repository / PR | Head đã audit | CI tại head đó |
|---|---|---|
| mesflow #50 | c65b08f762b2ea9280ba61e650fddb84bcbad51e | PostgreSQL Docker Tests 43165843676: success |
| mesflow-agent #1 | 9843f1859e44d3f6f6c9e97bdab04aa148dd1760 | Agent CI 43189058981: success |
| mesflow-esp32-kiosk #2 | 09db474c78dfb6223ed91c07e92c80db85c86677 | ESP OTA firmware CI 43189101195: success |

Ba PR còn draft, chưa merge. GitHub báo Agent #1 không mergeable với main; merge-tree xác nhận conflict ở agent.py.
Không giải quyết bằng cách ghi đè nhánh đang có người làm; cần reconcile trước merge.
PR firmware base là `fix/kiosk-qr-confirmation-hold-v562-20261111`, không phải main.

Log CI đã đọc:
- MESFlow: bốn nhóm Python lần lượt 429/925/45/628 pass, 10/14/1/2 skip;
  Playwright 607 pass, 4 skip. Các nhóm có deselection theo marker, không cộng
  thành số test duy nhất. Compose có API strict autobind=0 và PostgreSQL thật.
- Agent: gate 74 pass; full suite 512 pass, 34 skip, 1 deselect, 8 subtests.
  Test promotion template bị deselect do artifact runtime ngoài repository.
- Firmware: C++ host 35+15+27+19 checks; Python 42 pass; build-release và
  verify SHA/manifest/profile đều success. Đây không phải chạy firmware trên ESP.

## Hợp đồng xuyên ba repo

| Bước | Hợp đồng hiện tại | Đánh giá |
|---|---|---|
| Đăng ký mới | ESP POST /api/kiosk/connect: device_uuid, device_secret, profile, version; strict server tạo PENDING, HTTP 403 KIOSK_ENROLLMENT_PENDING + mã 6 ký tự | Có test PostgreSQL thật; firmware parsing/mã xác nhận mới là host/source tests |
| Duyệt | POST /api/kiosk-management/{id}/approve-enrollment, admin/manager, instance VERIFIED, station do admin chọn, TTL 1–60 phút | Không trả token cho browser; không tự duyệt fleet |
| Claim token | ESP gửi lại secret; repository khóa row, so hash, ACTIVE + token một lần | Đã vá recheck VERIFIED lúc claim; trước đó copied approval có thể sống trên clone |
| Token theo server | token_srv + alt_srv/alt_token/alt_station trong NVS; đổi server stash/unbind, quay lại restore; chặn đổi khi queue/pending tx; server thứ ba có thể bị từ chối | Giữ thiết kế hai server; chưa kiểm chứng NVS trên bo 40:D0 |
| Instance | DB system_meta UUID + binding theo secret; /api/internal/instance; inventory/readiness cùng instance_id | Agent bind một instance, mismatch fail closed; đổi domain không đổi UUID |
| Quảng bá Agent | MESFLOW_OTA_AGENT_URL qua heartbeat; WORKSHOP_AGENT_PUBLIC_URL dựng link download; HTTPS cùng origin | Hai URL phải cùng endpoint/path; không tạo setting mới |
| Artifact | release.json + per-profile app.bin/manifest; header/chip/app identity/SHA/size/partition slot được kiểm | M910 VERIFIED; Kiosk Linux UNVERIFIED bị loại khỏi build |
| Batch | inventory của instance, chọn kiosk_ids, snapshot artifact/digest; claim/lease/retry | Chỉ preview cho đúng một UUID trong nghiệm thu; không all-fleet |
| Boot | inactive slot, local self-check, partition check, running build/profile, event | Đã vá identity bắt buộc và ACK application-level; vẫn cần thử reboot/rollback thật |

Token validation của Agent gọi MESFlow readiness bằng token thiết bị; internal
inventory dùng token nội bộ. `cluster_id`/`generation_id` không thay instance_id.
`firmware_artifact_id` ở heartbeat ánh xạ sang `running_artifact_id` trong Agent;
query firmware dùng `artifact_id`. Smoke script trước đây gửi sai tên query,
đã sửa cùng payload build/profile để test không tiếp tục mô phỏng thiếu trường.

## Các bản vá

1. Agent `ota_control.py`: boot ACK thiếu version/build/profile trước đây vẫn
   có thể SUCCESS. Nay bắt buộc khớp artifact và ba trường identity.
   Giữ download/bootstrap legacy; firmware mới phải gửi compiled identity sau boot.
2. Agent: gửi lại ACK khi mất HTTP response xác nhận lại đúng target SUCCESS,
   không tác động batch kế tiếp; event duplicate trả lại quyết định applied đã lưu.
3. Firmware `otaEvent`: không coi HTTP 2xx là application ACK. Với Agent v2 cần
   ok=true và applied=true. Agent legacy không có applied vẫn dùng ok=true.
   Boot event ID ổn định theo target/artifact/claim/status để retry không mất ACK,
   bao gồm rollback và bootstrap từ firmware cũ.
4. MESFlow `_kiosk_enrollment_response`: chặn claim nếu instance không VERIFIED,
   kể cả approval còn TTL được copy từ database khác.
5. Firmware: bỏ log toàn bộ POST body vì /kiosk/connect chứa device_secret.

Không đổi migrations, không tạo engine hoặc hệ cấu hình mới.

## Migration PostgreSQL

0055 nối 0054, thêm hardware_profile/firmware_artifact_id; seed UUID bằng
ON CONFLICT DO NOTHING. Downgrade giữ UUID nhưng bỏ hai cột.
0056 nối 0055, thêm hash secret, station yêu cầu, station duyệt, TTL và người duyệt.
Cả hai dùng ADD COLUMN IF NOT EXISTS. Không có backfill đổi token hiện hữu.

CI baseline thực sự chạy PostgreSQL, không còn chỉ là tuyên bố trong PR body.
Tuy nhiên chưa thấy bài test upgrade→downgrade→upgrade trên bản sao dữ liệu
production, kiểm UUID/binding/token giữ nguyên; chưa có thử migration fail giữa
chừng. Không coi test schema sau upgrade là bằng chứng cho downgrade.
Migration 0056 không khai báo FK enroll_station_id; approval có kiểm station,
nhưng trường hợp station bị xóa trước claim cần bổ sung thử lỗi có kiểm soát.

## Kiểm thử bản vá tại workspace

- Agent: 79 test gate pass sau vá identity/replay và test mất response rollback;
  riêng hai suite scoped: 54 pass (gồm test mới).
- MESFlow: 18 unit/contract pass; ba case instance chưa verified đã FAIL trước vá,
  PASS sau vá. Có thêm test integration PostgreSQL clone-approval nhưng chưa chạy
  tại workspace do không có Docker/PostgreSQL; phải qua CI mới trước merge.
- Firmware: 42 Python pass; C++ 40 OTA + 15 keypad + 27 maintenance + 19 credential.
  Chưa build binary Arduino mới tại workspace, chưa nạp thiết bị. Host tests
  không xác nhận NVS, Wi-Fi, USB, partition, GM65 hoặc rollback thật.
- Diff whitespace check sạch. Có cảnh báo plugin anyio về pytest CallSpec2,
  không phải failure ứng dụng.

## Những phần còn thiếu / không được đánh đồng với PASS

1. Chưa có test tích hợp chạy MESFlow thật + Agent thật + firmware thật xuyên
   toàn bộ luồng; Agent route tests giả MESFlow, process smoke dùng fake MESFlow;
   phần lớn firmware Python tests kiểm source contract.
2. Chưa có bằng chứng mới từ bo 40:D0; không suy diễn VERIFIED profile thành
   nghiệm thu v5.6.6. Cần full MAC, device_uuid, partition, firmware hiện tại.
3. Response cấp token có thể bị mất sau commit ACTIVE: ESP không nhận token,
   secret replay bị từ chối theo single-use. Cần kiểm thử và ghi quy trình admin
   đưa đúng identity về PENDING rồi duyệt lại; không bật legacy autobind toàn hệ.
4. Legacy bootstrap không có target/claim trong NVS. Cần kiểm riêng lần nâng đầu
   và mất ACK; không dùng test force v2 để kết luận bootstrap đã ổn.
5. Auto rollback cần bootloader/partition thực tế hỗ trợ; hook phần mềm chưa đủ.
   Sau self-check mark valid, không được hứa bootloader tự rollback mọi lỗi về sau.
6. CI của baseline không chứng minh bản vá mới. Chờ build firmware + PostgreSQL
   integration mới. Agent còn conflict main. Không merge/publish lúc này.

## Quy trình nghiệm thu M910 — chỉ thực hiện sau phê duyệt thao tác thiết bị

### Gate 0: hồ sơ trước khi xin phép

- Thiết bị: một bo thử M910, dự kiến đuôi MAC 40:D0; **chưa xác nhận full MAC/UUID**.
  Không dùng 3A88 hoặc bo Kiosk Linux theo phỏng đoán. Đối chiếu serial/UUID trước.
- Profile: es3c28p-n16r8-m910-gm65uart1; flash 16MB, OPI PSRAM 8MB,
  partition SHA 1d9cca96de0fe07ad7fc0648b9878ddecd9ce565e38b589ad20fea698ed4c80c,
  slot 3342336 B; đối chiếu đọc thực tế.
- Firmware đích: v5.6.6 từ commit vá, ghi build ID + app SHA256 + release SHA256
  sau CI. Hiện chưa có binary mới được xác minh, không dùng SHA của bản cũ thay thế.
- Staging: một instance VERIFIED và Agent bind đúng ID; strict autobind=0,
  HTTPS/CA/NTP hoạt động; không copy token production vào staging.
- Backup: lưu firmware đã chạy tốt, partition/otadata và NVS hiện tại ở nơi hạn chế
  truy cập; không đưa secret vào log, issue hoặc báo cáo. Chuẩn bị USB cứu hộ.

### Gate 1: đăng ký và chuyển server

Sau phê duyệt chuyển cấu hình trên đúng bo: production→staging phải PENDING,
không dùng token production. Duyệt đúng mã 6 ký tự, đúng station, claim ACTIVE;
heartbeat và readiness cùng instance, profile đúng. Reboot vẫn giữ token staging.
Quay về production phải khôi phục token/station cũ. Thử đổi khi offline queue
chưa hết: phải từ chối và dữ liệu không mất. Thử mất response claim và quy trình
khôi phục chỉ một identity; ghi rõ cần thao tác admin hay tự phục hồi.

### Gate 2: OTA đúng một bo

Upload release vào staging và preview kiosk_ids=[UUID đã xác minh]: total=1,
artifact/SHA/profile đúng. Xin phép trước thao tác tạo batch thực thi.
Ghi chuỗi QUEUED → DOWNLOADING → FLASHING → REBOOTING → VERIFYING → SUCCESS.
SUCCESS chỉ sau local self-check và ACK matching identity được áp dụng.
Test thêm force cùng version một lần: không lặp cài lại sau SUCCESS/reboot.
Đối chiếu QR giữ 20 giây, START/FINISH, Multi-OP, GM65/keypad, offline queue.

### Gate 3: lỗi có kiểm soát, phê duyệt riêng nếu gây gián đoạn

- Sai SHA, sai profile, HTTPS/origin lỗi: không đổi boot slot.
- Giao dịch chưa ACK/queue còn dữ liệu: không flash; giải quyết queue rồi tiếp tục.
- Ngắt mạng tải; Agent/MESFlow unavailable; mất response boot ACK:
  không SUCCESS giả, không cài lặp, gửi lại ACK hồi phục được.
- Chỉ khi cho phép: thử fail boot/cắt nguồn trên bo thử với nguồn/USB cứu hộ;
  xác nhận rollback về partition cũ, dữ liệu/NVS còn nguyên, Agent không SUCCESS.

### Rollback và ảnh hưởng cần phê duyệt

Trong OTA bo tạm từ chối scan, reboot; chưa đo thời gian nên không hứa SLA.
Trước mark-valid: bootloader rollback nếu thử nghiệm chứng minh hoạt động.
Sau mark-valid: rollback bằng artifact cũ đúng profile được duyệt, hoặc USB restore
backup trên đúng bo; không xóa NVS. Dừng ở một bo nếu bất kỳ gate nào fail.
Chỉ sau khi toàn bộ bằng chứng PASS mới mở hồ sơ profile tiếp theo; Kiosk Linux
vẫn UNVERIFIED, không tự assign M910 và không tự phát OTA.

## Bằng chứng cần lưu mỗi lần chạy

Commit của ba repo; migration head; instance ID; full device UUID/MAC; profile;
version/build/SHA trước-sau; batch/target/claim (không token); serial đã lọc secret;
partition/state; timeline events; kết quả QR/transaction/queue; lỗi và rollback.
Chỉ gọi nghiệm thu hoàn tất khi các bước có bằng chứng từ thiết bị thật.

"""Process-level smoke of the ESP OTA Agent (real agent.py over HTTP, fake MESFlow, real release zip).
Not staging, not hardware: proves server behaviour only. Never prints secrets."""
import hashlib, http.cookiejar, json, os, re, sys, urllib.error, urllib.parse, urllib.request, uuid
BASE, FAKE, PKG = sys.argv[1], sys.argv[2], sys.argv[3]
ADMIN = {"Authorization": "Bearer " + os.environ["SMOKE_ADMIN_TOKEN"]}
TOK = {k: os.environ["SMOKE_TOK_" + k] for k in ("K1", "K2", "K3", "DUP", "DUPB")}
M910 = "es3c28p-n16r8-m910-gm65uart1"
results = []
def check(name, cond, info=""):
    results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info and not cond else ""))
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
jar = http.cookiejar.CookieJar(); opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
def req(method, path, body=None, headers=None, raw=False, base=BASE, multipart=None):
    h = dict(headers or {}); data = None
    if multipart:
        bnd = uuid.uuid4().hex; fname, content = multipart
        data = (f'--{bnd}\r\nContent-Disposition: form-data; name="package"; filename="{fname}"\r\nContent-Type: application/zip\r\n\r\n').encode() + content + f"\r\n--{bnd}--\r\n".encode()
        h["Content-Type"] = f"multipart/form-data; boundary={bnd}"
    elif body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        resp = opener.open(r, timeout=20); code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if raw: return code, payload
    try: return code, json.loads(payload or b"{}")
    except Exception: return code, {"_raw": payload[:200].decode("utf-8", "replace")}
def dev_check(kid, tok, ver, prof=M910, art=""):
    q = urllib.parse.urlencode({"kiosk_id": kid, "current_version": ver, "hardware_model": "ES3C28P", "hardware_profile": prof, "firmware_artifact_id": art, "api": 2})
    return req("GET", "/api/esp-ota/check?" + q, headers={"X-Kiosk-Token": tok})
def event(kid, tok, offer, status, **extra):
    body = {"kiosk_id": kid, "status": status, "firmware_id": offer["artifact_id"], "target_id": offer["target_id"], "claim_id": offer["claim_id"],
            "to_version": offer.get("version") or "5.6.3", "client_event_id": uuid.uuid4().hex, **extra}
    return req("POST", "/api/esp-ota/event", body, {"X-Kiosk-Token": tok})
def local(url):  # the advertised (public) origin is fixed by config; fetch the same path locally
    return "/api/esp-ota/file/" + url.split("/api/esp-ota/file/", 1)[1]
def install(kid, tok, ver):
    c, offer = dev_check(kid, tok, ver)
    if not offer.get("update_available"): return None, offer
    code, blob = req("GET", local(offer["url"]), headers={"X-Kiosk-Token": tok}, raw=True)
    ok_dl = code == 200 and hashlib.sha256(blob).hexdigest() == offer.get("sha256") and len(blob) == offer.get("size")
    for e in ("OTA_DOWNLOAD_START", "OTA_DOWNLOAD_COMPLETE", "OTA_REBOOTING", "OTA_BOOT_NEW_VERSION"): event(kid, tok, offer, e)
    return ok_dl, offer

# 0. scope + upload of the real verified release package
c, b = req("GET", "/api/esp-ota/overview", headers=ADMIN)
check("overview bound to MESFlow instance A", c == 200 and json.dumps(b).count("smoke-inst-A") > 0, f"{c} {str(b)[:160]}")
with open(PKG, "rb") as f: zipdata = f.read()
c, b = req("POST", "/api/esp-ota/releases", headers=ADMIN, multipart=(os.path.basename(PKG), zipdata))
check("upload real ec60eed release accepted", c == 201, f"{c} {str(b)[:200]}")
rel = b.get("release") or {}; rid = rel.get("release_id")
arts = rel.get("artifacts") or []
check("release has exactly the M910 artifact, sha 2bf1475b...", len(arts) == 1 and arts[0].get("sha256", "").startswith("2bf1475b") and arts[0].get("profile_id") == M910, str(arts)[:200])

# 1. preview: server-side fleet, offline + blocked counts
c, b = req("POST", "/api/esp-ota/preview", {"release_id": rid}, ADMIN); p = b.get("preview") or {}
items = {i["kiosk_id"]: i for i in p.get("items", [])}
check("preview covers the whole instance fleet (6)", p.get("total") == 6, str(p.get("total")))
check("preview online/offline counts 5/1", p.get("online") == 5 and p.get("offline") == 1, f"{p.get('online')}/{p.get('offline')}")
check("preview counts QUEUED3 WAITING_OFFLINE1 BLOCKED2", p.get("counts") == {"QUEUED": 3, "WAITING_OFFLINE": 1, "BLOCKED": 2}, str(p.get("counts")))
check("K4 without profile blocked PROFILE_REQUIRED (no latest fallback)", items.get("K4", {}).get("blocked_reason") == "PROFILE_REQUIRED")
check("Kiosk Linux K5 blocked INCOMPATIBLE_FIRMWARE (no cross-profile binary)", items.get("K5", {}).get("blocked_reason") == "INCOMPATIBLE_FIRMWARE")
check("K2 already on 5.6.3 is still QUEUED under force", items.get("K2", {}).get("planned_status") == "QUEUED")

# 2. one confirmation, matching preview
c, b = req("POST", "/api/esp-ota/force", {"release_id": rid, "confirm": True, "confirm_total": 99}, ADMIN)
check("stale confirmation refused PREVIEW_STALE", c == 409 and b.get("error") == "PREVIEW_STALE", f"{c} {b}")
c, b = req("POST", "/api/esp-ota/force", {"release_id": rid, "confirm": True, "confirm_total": 6}, ADMIN); batch = b.get("batch") or {}
check("force batch created with snapshot of 6", c == 201 and batch.get("total") == 6, f"{c} {str(b)[:200]}")
c, b2 = req("POST", "/api/esp-ota/force", {"release_id": rid, "confirm": True, "confirm_total": 6}, ADMIN)
c3, t = req("GET", "/api/esp-ota/targets?per_page=50", headers=ADMIN)
opened = [x for x in t.get("items", []) if x["status"] not in ("SUCCESS", "FAILED", "BLOCKED", "CANCELLED", "SKIPPED")]
check("repeat force never opens a 2nd target per device", len({x['kiosk_id'] for x in opened}) == len(opened), str([(x['kiosk_id'], x['status']) for x in opened]))
check("repeat force while waiting returns the SAME batch (no duplicate run)", b2.get("batch", {}).get("batch_id") == batch.get("batch_id") and b2["batch"].get("reused") is True, str(b2)[:160])

# 3. happy path K1: download pinned sha, SUCCESS only on boot proof
ok_dl, offer = install("K1", TOK["K1"], "5.6.2")
check("K1 offer is same-origin as configured public URL (no deploy.mesflow.net fallback)", offer.get("url", "").startswith("https://ota.smoke.invalid/agent/api/esp-ota/file/"), offer.get("url"))
check("K1 download = pinned artifact (sha/size)", ok_dl)
c, t = req("GET", "/api/esp-ota/targets?kiosk_id=K1", headers=ADMIN)
check("K1 after BOOT_NEW_VERSION is VERIFYING, not SUCCESS", t["items"][0]["status"] == "VERIFYING", t["items"][0]["status"])
event("K1", TOK["K1"], offer, "OTA_HEALTHCHECK_OK", hardware_profile=M910)
c, t = req("GET", "/api/esp-ota/targets?kiosk_id=K1", headers=ADMIN)
check("K1 SUCCESS only after HEALTHCHECK_OK for pinned artifact", t["items"][0]["status"] == "SUCCESS")
c, again = dev_check("K1", TOK["K1"], "5.6.3", art=offer["artifact_id"])
check("K1 not offered again after SUCCESS", again.get("update_available") is False, str(again)[:120])

# 4. force same version installs exactly once (K2 already 5.6.3)
ok_dl, offer2 = install("K2", TOK["K2"], "5.6.3")
check("K2 (same version) offered once under force", bool(offer2.get("update_available")) and ok_dl)
event("K2", TOK["K2"], offer2, "OTA_HEALTHCHECK_OK", hardware_profile=M910)
c, again = dev_check("K2", TOK["K2"], "5.6.3", art=offer2["artifact_id"])
check("K2 same-version not reinstalled after SUCCESS", again.get("update_available") is False)

# 5. device-id collision across instances
c, x = dev_check("DUP", TOK["DUPB"], "5.6.2")
check("DUP with instance-B token: no offer, INSTANCE_MISMATCH", x.get("update_available") is False and x.get("reason") == "INSTANCE_MISMATCH", str(x)[:150])
c, x = dev_check("K1", TOK["DUP"], "5.6.2")
check("token of DUP cannot act as K1", c == 401 or x.get("update_available") is False, f"{c} {str(x)[:100]}")
c, blob = req("GET", local(offer["url"]), headers={"X-Kiosk-Token": TOK["DUPB"]}, raw=True)
check("instance-B token cannot download A's artifact", c in (401, 403, 404), str(c))

# 6. failed boot (rollback) on DUP -> never SUCCESS, retried with backoff
ok_dl, offd = install("DUP", TOK["DUP"], "5.6.2")
event("DUP", TOK["DUP"], offd, "OTA_ROLLBACK", error_code="OTA_ROLLBACK")
c, t = req("GET", "/api/esp-ota/targets?kiosk_id=DUP", headers=ADMIN); st = t["items"][0]
check("DUP rollback -> not SUCCESS (retry/backoff or FAILED)", st["status"] != "SUCCESS" and (st["status"] == "FAILED" or st.get("wait_reason") == "RETRY_BACKOFF"), f"{st['status']} {st.get('wait_reason')} {st.get('error')}")
ev = event("DUP", TOK["DUP"], offd, "OTA_HEALTHCHECK_OK", hardware_profile=M910)
c, t = req("GET", "/api/esp-ota/targets?kiosk_id=DUP", headers=ADMIN)
check("late healthcheck under old claim does not flip to SUCCESS", t["items"][0]["status"] != "SUCCESS", t["items"][0]["status"])

# 7. cancel: waiting targets cancelled, SUCCESS rows untouched
c, b = req("POST", f"/api/esp-ota/batches/{batch['batch_id']}/cancel", {}, ADMIN)
c, t = req("GET", "/api/esp-ota/targets?per_page=50", headers=ADMIN); by = {x["kiosk_id"]: x["status"] for x in t["items"] if x["batch_id"] == batch["batch_id"]}
check("cancel: K3 (offline) CANCELLED, K1/K2 stay SUCCESS", by.get("K3") == "CANCELLED" and by.get("K1") == "SUCCESS" and by.get("K2") == "SUCCESS", str(by))

# 8. MESFlow now reports another instance: fail closed, no hidden rebind
req("POST", "/_smoke/instance", {"instance": "smoke-inst-B"}, {"X-MESFlow-Internal-Token": os.environ["SMOKE_INTERNAL"]}, base=FAKE)
import time; time.sleep(0.1)
c, b = req("POST", "/api/esp-ota/preview", {"release_id": rid}, ADMIN)
check("other instance -> preview refused INSTANCE_MISMATCH (even with cached identity)", c == 409 and b.get("error") == "INSTANCE_MISMATCH", f"{c} {str(b)[:120]}")
c, b = req("POST", "/api/esp-ota/instance/rebind", {"confirm_instance_id": "smoke-inst-B"}, ADMIN)
check("rebind via API token alone refused (needs owner login session)", c in (302, 401, 403), str(c))
c, b = req("GET", "/api/esp-ota/overview", headers=ADMIN)
check("overview under foreign instance fails closed INSTANCE_MISMATCH", c == 409 and b.get("error") == "INSTANCE_MISMATCH", f"{c} {str(b)[:120]}")
req("POST", "/_smoke/instance", {"instance": "smoke-inst-A"}, {"X-MESFlow-Internal-Token": os.environ["SMOKE_INTERNAL"]}, base=FAKE)
c, b = req("GET", "/api/esp-ota/overview", headers=ADMIN)
check("back on instance A: binding unchanged, history intact (no hidden rebind)", c == 200 and "smoke-inst-A" in json.dumps(b), f"{c} {str(b)[:160]}")
print(f"SUMMARY {sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)

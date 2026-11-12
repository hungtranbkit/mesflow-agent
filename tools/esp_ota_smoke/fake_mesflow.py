"""Fake MESFlow internal API for a LOCAL process-level smoke (not staging, not hardware, not production)."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
INTERNAL = os.environ["SMOKE_INTERNAL"]
M910 = "es3c28p-n16r8-m910-gm65uart1"; LINUX = "es3c28p-n16r8-kiosklinux-gm65uart1"
STATE = {"instance": "smoke-inst-A"}
def dev(k, prof, ver, online=True):
    return {"kiosk_id": k, "kiosk_name": k, "station_code": "ST-" + k, "hardware_model": "ES3C28P", "hardware_profile": prof,
            "firmware_version": ver, "firmware_build": "", "firmware_artifact_id": "", "online": online, "ota_capable": True}
FLEET = {"smoke-inst-A": [dev("K1", M910, "5.6.2"), dev("K2", M910, os.environ.get("SMOKE_RELEASE_VERSION", "5.6.3")), dev("K3", M910, "5.6.2", online=False),
                          dev("K4", "", "5.6.2"), dev("K5", LINUX, "5.6.0"), dev("DUP", M910, "5.6.2")],
         "smoke-inst-B": [dev("DUP", M910, "5.6.2")]}
# device token -> (kiosk_id, owning instance). DUP exists in both instances.
TOKENS = {os.environ["SMOKE_TOK_" + k]: (k, "smoke-inst-A") for k in ("K1", "K2", "K3", "DUP")}
TOKENS[os.environ["SMOKE_TOK_DUPB"]] = ("DUP", "smoke-inst-B")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body):
        data = json.dumps(body).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)
    def do_POST(self):  # smoke control: switch which instance this MESFlow reports
        if self.path == "/_smoke/instance" and self.headers.get("X-MESFlow-Internal-Token") == INTERNAL:
            STATE["instance"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["instance"]; return self.send(200, {"ok": True})
        self.send(404, {})
    def do_GET(self):
        inst = STATE["instance"]
        if self.path.endswith("/ota-readiness"):
            kid = self.path.split("/")[-2]; owner = TOKENS.get(self.headers.get("X-Kiosk-Token", ""))
            if not owner or owner[0] != kid: return self.send(403, {"ok": False})
            return self.send(200, {"ok": True, "kiosk_id": kid, "instance_id": owner[1], "instance_state": "VERIFIED", "ota_ready": True, "reason": "READY", "online": True})
        if self.headers.get("X-MESFlow-Internal-Token") != INTERNAL: return self.send(401, {"ok": False})
        if self.path == "/api/internal/instance":
            return self.send(200, {"ok": True, "instance_id": inst, "state": "VERIFIED", "display_name": "smoke", "public_url": "", "ota_agent_url": ""})
        if self.path == "/api/internal/kiosks":
            return self.send(200, {"ok": True, "instance_id": inst, "instance_state": "VERIFIED", "items": FLEET[inst]})
        self.send(404, {})
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()

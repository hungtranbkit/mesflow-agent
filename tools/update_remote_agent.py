import argparse
import base64
import json
import re
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument("--base",required=True);p.add_argument("--password",required=True);a=p.parse_args()
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    data=urllib.parse.urlencode({"username":"admin","password":a.password}).encode()
    page=opener.open(a.base.rstrip("/")+"/login",data,timeout=20).read().decode("utf-8","replace")
    match=re.search(r'name="csrf" value="([^"]+)"',page)
    if not match: raise RuntimeError("Login/CSRF failed")
    csrf=match.group(1)
    def run(command,timeout=30):
        body=json.dumps({"command":command,"timeout":timeout,"csrf":csrf}).encode()
        req=urllib.request.Request(a.base.rstrip("/")+"/api/ops/local-command",body,{"Content-Type":"application/json"})
        result=json.loads(opener.open(req,timeout=timeout+15).read())
        if not result.get("ok"): raise RuntimeError(result)
        return result.get("stdout","")
    def send(local,remote):
        run(f"python3 -c \"open('{remote}','wb').close()\"")
        encoded=base64.b64encode(Path(local).read_bytes()).decode()
        for i in range(0,len(encoded),2400):
            chunk=encoded[i:i+2400]
            run(f"python3 -c \"import base64;open('{remote}','ab').write(base64.b64decode('{chunk}'))\"")
    root=Path(__file__).resolve().parents[1]
    run("mkdir -p /opt/mesflow-agent/tests /opt/mesflow-agent/templates")
    for local,remote in [
        (root/"agent.py","/tmp/mesflow-agent.py.new"),
        (root/"templates/index.html","/tmp/mesflow-agent-index.html.new"),
        (root/"VERSION.txt","/tmp/mesflow-agent-VERSION.new"),
        (root/"tests/test_deploy_safety.py","/tmp/test_deploy_safety.py.new"),
    ]: send(local,remote)
    print(run("python3 -m py_compile /tmp/mesflow-agent.py.new && sha256sum /tmp/mesflow-agent.py.new /opt/mesflow-agent/agent.py"))
    command=("stamp=$(date +%Y%m%d_%H%M%S); backup=/opt/mesflow-agent/data/agent_backups/$stamp; "
             "mkdir -p $backup; cp -a /opt/mesflow-agent/agent.py /opt/mesflow-agent/templates/index.html /opt/mesflow-agent/VERSION.txt $backup/; "
             "install -m 0644 /tmp/mesflow-agent.py.new /opt/mesflow-agent/agent.py; "
             "install -m 0644 /tmp/mesflow-agent-index.html.new /opt/mesflow-agent/templates/index.html; "
             "install -m 0644 /tmp/mesflow-agent-VERSION.new /opt/mesflow-agent/VERSION.txt; "
             "install -m 0644 /tmp/test_deploy_safety.py.new /opt/mesflow-agent/tests/test_deploy_safety.py; echo BACKUP=$backup")
    print(run(command))

if __name__=="__main__": main()

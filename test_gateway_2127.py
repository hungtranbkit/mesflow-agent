from pathlib import Path
R=Path(__file__).resolve().parents[2]
A=R/"payload/mesflow-deploy-agent"
G=R/"infra/gateway/nginx/nginx.conf"

def test_version():
 assert (A/"VERSION.txt").read_text().strip()=="2.12.7-docker-runtime"
 assert 'image: mesflow-deploy-agent:2.12.7' in (A/"docker/compose.linux.yml").read_text()

def test_dedicated_deploy_host():
 s=G.read_text()
 assert "server_name deploy.mesflow.net;" in s
 assert "return 302 /agent/;" in s
 assert "proxy_pass http://mesflow_agent_backend;" in s
 assert 'X-Robots-Tag "noindex, nofollow, noarchive"' in s

def test_upload_proxy_tuned():
 s=G.read_text()
 assert "client_max_body_size 200m;" in s
 assert "proxy_request_buffering off;" in s
 assert "proxy_read_timeout 900s;" in s

def test_installer_updates_and_validates_gateway():
 s=(R/"install.sh").read_text()
 assert "nginx.conf.backup-" in s
 assert "docker exec mesflow-nginx nginx -t" in s
 assert "docker exec mesflow-nginx nginx -s reload" in s

def test_standalone_gateway_updater():
 s=(R/"update-gateway.sh").read_text()
 assert "deploy.mesflow.net" in s
 assert "mesflow-edge" in s
 assert "rollback" in s.lower()

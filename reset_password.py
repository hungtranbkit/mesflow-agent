from __future__ import annotations
import argparse, getpass, json, secrets
from datetime import datetime
from pathlib import Path
from werkzeug.security import generate_password_hash

def load(path):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except Exception:return {}
def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)
def recovery_code():
    raw=secrets.token_hex(8).upper();return '-'.join(raw[i:i+4] for i in range(0,16,4))
def normalize(v):return ''.join(ch for ch in v.upper() if ch.isalnum())
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--home',default=r'C:\WorkshopManagementAgent');ap.add_argument('--regenerate-recovery',action='store_true');a=ap.parse_args()
    home=Path(a.home);cfg=home/'config'/'agent.json';data=load(cfg)
    if not data.get('secret_key'):data['secret_key']=secrets.token_urlsafe(48)
    if a.regenerate_recovery:
        code=recovery_code();data['recovery_code_hash']=generate_password_hash(normalize(code));data['recovery_code_created_at']=datetime.now().strftime('%Y-%m-%d %H:%M:%S');save(cfg,data)
        print('\nNEW RECOVERY CODE:',code);print('Store it securely. The previous recovery code is now invalid.');return
    p1=getpass.getpass('New password (minimum 10 characters): ');p2=getpass.getpass('Confirm new password: ')
    if p1!=p2:raise SystemExit('[ERROR] Password confirmation does not match.')
    if len(p1)<10:raise SystemExit('[ERROR] Password must contain at least 10 characters.')
    data['password_hash']=generate_password_hash(p1);data['password_changed_at']=datetime.now().strftime('%Y-%m-%d %H:%M:%S');save(cfg,data)
    (home/'config'/'FIRST_LOGIN.txt').unlink(missing_ok=True)
    print('[OK] Administrator password has been reset.')
if __name__=='__main__':main()

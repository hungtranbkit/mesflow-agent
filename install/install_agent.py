from __future__ import annotations
import argparse, os, shutil, subprocess, sys
from pathlib import Path


def run(cmd):
    print('RUN:', ' '.join(str(x) for x in cmd), flush=True)
    subprocess.run([str(x) for x in cmd], check=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--home', default=r'C:\WorkshopManagementAgent')
    p.add_argument('--target', default=r'C:\WorkshopManagement')
    p.add_argument('--port', default='8090')
    p.add_argument('--mes-url', default='http://127.0.0.1:8080')
    p.add_argument('--qa-home', default=r'C:\MESFlowQACenter')
    p.add_argument('--qa-url', default='http://127.0.0.1:8095')
    a=p.parse_args()
    home=Path(a.home).resolve(); source=Path(__file__).resolve().parents[1]
    task_name='WorkshopUpdateAgent'
    subprocess.run(['schtasks','/End','/TN',task_name],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    home.mkdir(parents=True, exist_ok=True)
    for name in ('agent.py','reset_password.py','requirements.txt','VERSION.txt','templates','UPDATE_NOTES_V2.6.2.txt','UPDATE_NOTES_V2.6.3.txt','UPDATE_NOTES_V2.6.4.txt','UPDATE_NOTES_V2.6.5.txt','UPDATE_NOTES_V2.6.6.txt','UPDATE_NOTES_V2.7.0.txt'):
        src=source/name; dst=home/name
        if dst.exists(): shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        shutil.copytree(src,dst) if src.is_dir() else shutil.copy2(src,dst)
    venv=home/'.venv'; py=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not py.exists(): run([sys.executable,'-m','venv',venv])
    run([py,'-m','pip','install','--disable-pip-version-check','-r',home/'requirements.txt'])
    env_bat=home/'agent_env.bat'
    env_bat.write_text('@echo off\n'+f'set "WORKSHOP_AGENT_HOME={home}"\n'+f'set "WORKSHOP_TARGET_HOME={Path(a.target).resolve()}"\n'+f'set "WORKSHOP_AGENT_PORT={a.port}"\n'+f'set "WORKSHOP_MES_URL={a.mes_url}"\n'+f'set "MESFLOW_QA_HOME={Path(a.qa_home).resolve()}"\n'+f'set "MESFLOW_QA_URL={a.qa_url}"\n',encoding='utf-8')
    start=home/'start_agent.bat'
    start.write_text('@echo off\ncall "%~dp0agent_env.bat"\ncd /d "%~dp0"\n"%~dp0.venv\\Scripts\\python.exe" agent.py\n',encoding='utf-8')
    task_cmd=f'cmd.exe /c "{start}"'
    run(['schtasks','/Create','/TN',task_name,'/SC','ONSTART','/RU','SYSTEM','/RL','HIGHEST','/TR',task_cmd,'/F'])
    subprocess.run(['schtasks','/Run','/TN',task_name],check=False)
    print('\nInstalled successfully.')
    print(f'Open: http://SERVER-IP:{a.port}/agent/')
    print(f'Initial credentials: {home / "config" / "FIRST_LOGIN.txt"}')
    print('Open Windows Firewall port manually if remote PCs cannot connect.')

if __name__=='__main__': main()

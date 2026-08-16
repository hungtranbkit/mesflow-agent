(function(){
  const shell=document.querySelector('.ops-shell');
  if(!shell)return;
  const toggle=document.querySelector('.ops-sidebar-toggle');
  const close=()=>shell.classList.remove('nav-open');
  if(toggle)toggle.addEventListener('click',()=>shell.classList.toggle('nav-open'));
  document.querySelector('.ops-scrim')?.addEventListener('click',close);
  document.addEventListener('keydown',event=>{if(event.key==='Escape')close()});
})();

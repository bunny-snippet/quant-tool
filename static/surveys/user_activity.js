/* Persistent Supplier Activity tabs; supplier tools load on first visit. */
(() => {
  const switcher = document.querySelector('[data-user-activity-tabs]');
  if (!switcher) return;
  const tabs = [...switcher.querySelectorAll('[data-activity-tab]')];
  const panels = new Map([...document.querySelectorAll('[data-activity-panel]')].map(p=>[p.dataset.activityPanel,p]));
  const host = switcher.parentElement;
  const loaded = new Set([...document.querySelectorAll('script[data-activity-script]')].map(s=>s.dataset.activityScript));
  const loading = new Map();
  const status = document.createElement('div');
  status.className='reports-load-status'; status.setAttribute('role','status'); host.append(status);
  let intent=0;
  async function ensure(tab) {
    const name=tab.dataset.activityTab;
    const group=name==='suppliers'?'suppliers':'users';
    if (panels.has(name) && loaded.has(name)) return;
    if (loading.has(group)) return loading.get(group);
    const work=(async()=>{
      const url=new URL(tab.href,location.href);
      if(url.origin!==location.origin) throw new Error('Invalid activity link.');
      const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),20000);
      let doc;
      try {
        const response=await fetch(url,{credentials:'same-origin',signal:controller.signal});
        if(!response.ok) throw new Error('Unable to load activity ('+response.status+').');
        doc=new DOMParser().parseFromString(await response.text(),'text/html');
        if(!doc.querySelector('[data-activity-panel="'+name+'"]')) throw new Error('Session expired or access changed. Reload the page.');
      } finally {clearTimeout(timer);}
      for(const panel of doc.querySelectorAll('[data-activity-panel]')) {
        if(panels.has(panel.dataset.activityPanel)) continue;
        panel.hidden=true; host.append(panel); panels.set(panel.dataset.activityPanel,panel);
      }
      for(const source of doc.querySelectorAll('script[data-activity-script]')) {
        const key=source.dataset.activityScript;
        if(loaded.has(key)) continue;
        const src=new URL(source.getAttribute('src'),location.href);
        if(src.origin!==location.origin || !/^\/static\/(surveys|vendors)\//.test(src.pathname)) throw new Error('Invalid activity asset.');
        await new Promise((resolve,reject)=>{
          const script=document.createElement('script'); script.src=src.href; script.dataset.activityScript=key;
          script.onload=resolve; script.onerror=()=>{script.remove();reject(new Error('Activity script failed. Select the tab to retry.'));};
          document.body.append(script);
        });
        loaded.add(key);
      }
    })();
    loading.set(group,work);
    try {await work;} finally {loading.delete(group);}
  }
  async function activate(tab,{focus=false,updateUrl=false}={}) {
    if(!tab) return;
    const token=++intent, selected=tab.dataset.activityTab;
    try {
      if(!panels.has(selected)||!loaded.has(selected)) {
        status.textContent='Loading activity…'; switcher.setAttribute('aria-busy','true'); await ensure(tab);
      }
      if(token!==intent) return;
      tabs.forEach(t=>{const active=t===tab;t.classList.toggle('active',active);t.setAttribute('aria-selected',String(active));t.tabIndex=active?0:-1;});
      panels.forEach((p,key)=>{p.hidden=key!==selected;});
      if(focus) tab.focus();
      if(updateUrl&&location.href!==tab.href) history.pushState({},'',tab.href);
      document.title='Supplier Activity · Survey Workspace';
      document.dispatchEvent(new CustomEvent('user-activity:change',{detail:{panel:selected}}));
      window.FilterSelectionChips?.sync(); status.textContent='';
    } catch(error) {
      if(token===intent) status.textContent=error.name==='AbortError'?'Activity request timed out. Select the tab to retry.':error.message;
    } finally {if(token===intent) switcher.removeAttribute('aria-busy');}
  }
  tabs.forEach((tab,index)=>{
    tab.addEventListener('click',event=>{
      if(event.button||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey) return;
      event.preventDefault();activate(tab,{updateUrl:true});
    });
    tab.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowLeft'?-1:1)+tabs.length)%tabs.length;
      activate(tabs[next],{focus:true,updateUrl:true});
    });
  });
  window.addEventListener('popstate',()=>activate(tabs.find(t=>t.href===location.href)));
  activate(tabs.find(t=>t.classList.contains('active'))||tabs[0]);
})();


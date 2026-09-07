/* Read-only dashboard browser logic, tested without live traffic or a browser. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../static/surveys/partner_dashboard.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function report(section = 'client', count = 3) {
  const access = Object.fromEntries(['hits','completes','conversion','accepted','rejected','pending','revenue','rpc','share','surveys','review','trend','table','date','partner','segment'].map(k=>[k,true]));
  const summary = {hits:10, completes:4, accepted:2, rejected:1, pending:1, conversion:40, revenue:'8.00', rpc:'.80', currency:'USD'};
  return {section, access, summary, previous:{...summary,hits:5}, rows:Array.from({length:count},(_,i)=>({...summary,id:String(i+1),name:i===0?'<unsafe>':'Partner '+i,segment_name:'Branch one',surveys:2,share:25})),
    options:{partners:[{id:'1',name:'<unsafe>'}],segments:[{id:'US',name:'United States'}]},trend:[{label:'Monday',hits:10,completes:4}],
    period:'Last 7 days',comparison_label:'Previous 7 days',definition:'Entry-period journeys',generated_at:'2026-09-08T00:00:00Z'};
}
function app(section, response = report(section)) {
  const element = (dataset={}) => ({dataset,innerHTML:'',textContent:'',value:'',hidden:false,attributes:{},listeners:{},
    classList:{toggle(){}},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];},
    addEventListener(k,fn){this.listeners[k]=fn;},querySelector(){return this.icon ||= {textContent:''};},
    closest(){return this.panel ||= {hidden:false};},insertAdjacentHTML(_,s){this.innerHTML+=s;}});
  const ids = new Map(), get = id => {if(!ids.has(id))ids.set(id,element());return ids.get(id);};
  const root=get('partnerDashboard'); root.dataset={section,endpoint:'/dashboard/'+section+'/'};
  const ranges=['24h','7d','month'].map(range=>element({range}));
  const sorts=['hits','completes','revenue'].map(sort=>element({sort}));
  const headers=sorts.map(el=>element({sortHeader:el.dataset.sort}));
  root.querySelectorAll = selector => ({'[data-range]':ranges,'[data-sort]':sorts,'[data-sort-header]':headers,'[data-metric]':[]}[selector] || []);
  const calls=[], pending=[];
  const answer = data => ({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>data});
  let mode='auto', next=response;
  vm.runInNewContext(script,{document:{getElementById:get},Intl,URLSearchParams,AbortController,setTimeout,clearTimeout,
    fetch:(url,opts)=>{calls.push({url,opts});if(mode==='error')return Promise.resolve({ok:false,status:403,headers:{get:()=> 'text/html'}});if(mode==='pending')return new Promise(resolve=>pending.push(data=>resolve(answer(data))));return Promise.resolve(answer(next));}});
  return {get,calls,ranges,sorts,headers,pending,set(data){next=data;},mode(v){mode=v;},
    change(id,value,event='change'){get(id).value=value;get(id).listeners[event]({target:get(id)});}};
}
for(const section of ['client','supplier']) {
  test(section+': real response renders, safe text, filters and reset',async()=>{
    const ui=app(section);await tick();
    assert.match(ui.calls[0].url,/format=json&range=7d/);
    assert.match(ui.get('pdRows').innerHTML,/&lt;unsafe&gt;/);
    assert.doesNotMatch(ui.get('pdRows').innerHTML,/<unsafe>/);
    assert.match(ui.get('pdReview').innerHTML,/75.0%/);
    ui.change('pdPartner','1');await tick();assert.match(ui.calls.at(-1).url,/partner=1/);
    ui.change('pdSegment',section==='client'?'US':'1');await tick();assert.match(ui.calls.at(-1).url,/segment=/);
    ui.ranges[2].listeners.click();await tick();assert.match(ui.calls.at(-1).url,/range=month/);
    ui.get('pdFilters').listeners.reset({preventDefault(){}});await tick();
    assert.doesNotMatch(ui.calls.at(-1).url,/partner=|segment=/);
  });
  test(section+': search stays table-local, pagination and sorting',async()=>{
    const ui=app(section,report(section,25));await tick();
    assert.equal(ui.get('pdPage').textContent,'1 / 2');
    ui.get('pdNext').listeners.click();assert.equal(ui.get('pdPage').textContent,'2 / 2');
    const requests=ui.calls.length;
    ui.change('pdSearch','not-present','input');
    assert.match(ui.get('pdRows').innerHTML,/No journeys match/);
    assert.equal(ui.calls.length,requests);
    ui.sorts[1].listeners.click();assert.equal(ui.headers[1].attributes['aria-sort'],'ascending');
  });
  test(section+': permission changes hide panels and failures clear stale data',async()=>{
    const ui=app(section);await tick();
    const restricted=report(section);restricted.access.review=false;restricted.access.table=false;restricted.access.trend=false;
    ui.set(restricted);ui.ranges[0].listeners.click();await tick();
    for(const id of ['pdRows','pdReview','pdTrend']){assert.equal(ui.get(id).closest().hidden,true);assert.equal(ui.get(id).innerHTML,'');}
    ui.mode('error');ui.ranges[1].listeners.click();await tick();
    assert.equal(ui.get('pdError').hidden,false);assert.match(ui.get('pdError').textContent,/no longer have access/);
    assert.equal(ui.get('pdKpis').innerHTML,'');
    assert.equal(ui.get('partnerDashboard').attributes['aria-busy'],'false');
  });
  test(section+': older request cannot replace a newer filter result',async()=>{
    const ui=app(section);await tick();ui.mode('pending');
    ui.ranges[0].listeners.click();ui.ranges[2].listeners.click();
    assert.equal(ui.calls.at(-2).opts.signal.aborted,true);
    const latest=report(section);latest.period='Current month';ui.pending[1](latest);await tick();
    const old=report(section);old.period='Old';ui.pending[0](old);await tick();
    assert.equal(ui.get('pdPeriod').textContent,'Current month');
  });
}

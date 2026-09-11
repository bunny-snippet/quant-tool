const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Execute the actual renderers without booting the page or issuing API requests.
const source = fs.readFileSync(path.join(__dirname, '../static/surveys/dashboard.js'), 'utf8');
function setup(width = 700) {
  const hosts = Object.fromEntries(['dashboardCompleteDetails', 'volumeChart', 'financeChart', 'statusBreakdown', 'dashboardTopSuppliers', 'dashboardTable-client', 'dashboardTable-supplier', 'dashboardTableRows-client', 'dashboardTableRows-supplier'].map(id => [id, {
    clientWidth: width, innerHTML: '', children: [], appendChild(el) { this.children.push(el); }, querySelectorAll() { return []; },
  }]));
  const document = {
    getElementById: id => hosts[id],
    createElement: () => ({textContent: '', setAttribute() {}, get innerHTML() { return this.textContent.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); }}),
  };
  const context = vm.createContext({document, URLSearchParams, location:{search:''}, window:{matchMedia:()=>({matches:true})}, Intl});
  const end = source.indexOf('  function render(data)');
  vm.runInContext(source.slice(0,end) + 'globalThis.renderers={renderOperationalInsights,renderVolume,renderFinance,renderStatus,renderTopSuppliers,renderPartnerTables,chartScale};})();',context);
  return {hosts, ...context.renderers};
}
const point = (overrides={}) => ({label:'8 September · 8 AM',short_label:'8 AM',hits:100,completes:25,revenue:50,rpc:.5,average_cpi:2,conversion_rate:25,incidence_rate:30,...overrides});

test('merged completes shows hourly average, last hour and real peak without invented empty peak',()=>{
  const {hosts,renderOperationalInsights}=setup();
  const range={start:'2026-09-08T00:00:00Z',end:'2026-09-08T12:00:00Z'};
  renderOperationalInsights({range,summary:{completes:120,last_hour_completes:8},traffic_chart:{points:[point({completes:30}),point({completes:90,short_label:'10 AM'})]}});
  assert.match(hosts.dashboardCompleteDetails.innerHTML,/>10.0<\/b>/);
  assert.match(hosts.dashboardCompleteDetails.innerHTML,/Last hr: 8/);
  assert.match(hosts.dashboardCompleteDetails.innerHTML,/>90<\/b>.*10 AM/);
  renderOperationalInsights({range,summary:{},traffic_chart:{points:[point({completes:0})]}});
  assert.match(hosts.dashboardCompleteDetails.innerHTML,/No completes/);
  assert.doesNotMatch(hosts.dashboardCompleteDetails.innerHTML,/8 AM|NaN|Infinity/);
});

test('empty traffic never invents a resolved outcome',()=>{
  const {hosts,renderStatus}=setup(); renderStatus({});
  assert.match(hosts.statusBreakdown.innerHTML,/Resolved outcomes<\/small><strong>0<\/strong>/);
  assert.doesNotMatch(hosts.statusBreakdown.innerHTML,/NaN|Infinity/);
});

test('rejected is a third count bar and invoices plot even with no current journeys',()=>{
  const {hosts,renderVolume,renderFinance}=setup();
  renderVolume([point({rejected:7})]);
  assert.match(hosts.volumeChart.innerHTML,/bi-volume-rejected/);
  assert.match(hosts.volumeChart.innerHTML,/Client rejected 7/);
  renderFinance([point({hits:0,completes:0,revenue:0,invoiced_revenue:40})],'USD');
  assert.match(hosts.financeChart.innerHTML,/bi-invoiced-line/);
  assert.match(hosts.financeChart.innerHTML,/bi-invoiced-dot/);
  assert.doesNotMatch(hosts.financeChart.innerHTML,/bi-rpc-dot|No activity|NaN/);
});

test('finance keeps RPC alongside revenue bars and invoice line with separate units',()=>{
  const {hosts,renderFinance}=setup();
  renderFinance([point({invoiced_revenue:35}),point({invoiced_revenue:65})],'USD');
  const svg=hosts.financeChart.innerHTML;
  for(const name of ['bi-finance-bar','bi-invoiced-line','bi-rpc-line','bi-rpc-dot']) assert.match(svg,new RegExp(name));
  assert.match(svg,/RPC · per entrant/);
  assert.match(svg,/Invoiced revenue \$35.00/);
  assert.match(svg,/RPC \$0.50/);
  assert.match(svg,/bi-right-axis/);
  assert.doesNotMatch(svg,/bi-rate-grid/); // One shared plot, not the old detached RPC panel.
  renderFinance([point({invoiced_revenue:35,rpc:null,average_cpi:null})],'USD');
  assert.match(hosts.financeChart.innerHTML,/bi-invoiced-line/);
  assert.doesNotMatch(hosts.financeChart.innerHTML,/bi-rpc-line|RPC \$/);
});

test('partner tables escape names and hide permission-redacted data',()=>{
  const {hosts,renderPartnerTables}=setup();
  renderPartnerTables({client:[{name:'<img>',completes:4,accepted:2,rejected:1,share:100}],supplier:[]});
  assert.match(hosts['dashboardTableRows-client'].innerHTML,/&lt;img&gt;/);
  assert.match(hosts['dashboardTableRows-client'].innerHTML,/25.0%/);
  assert.doesNotMatch(hosts['dashboardTableRows-client'].innerHTML,/100.0%/);
  assert.match(hosts['dashboardTableRows-supplier'].innerHTML,/No activity/);
  renderPartnerTables({client:null,supplier:null});
  assert.equal(hosts['dashboardTable-client'].hidden,true);
  assert.equal(hosts['dashboardTableRows-client'].innerHTML,'');
});

test('traffic plots count and rate on separate aligned axes with accessible exact values',()=>{
  const {hosts,renderVolume}=setup(); renderVolume([point()], 'Last 24 hours');
  const svg=hosts.volumeChart.innerHTML;
  assert.match(svg,/Entrants \/ completes/); assert.match(svg,/Conversion/);
  assert.match(svg,/100%/); assert.match(svg,/tabindex="0"/);
  assert.match(svg,/Completes 25/); assert.match(svg,/Conversion 25.0%/);
  assert.doesNotMatch(svg,/NaN|Infinity|bi-average-line/);
});
test('finance respects redacted metrics and handles revenue-only, rate-only and restricted views',()=>{
  const {hosts,renderFinance}=setup();
  renderFinance([point({revenue:null,rpc:null,average_cpi:null})],'USD');
  assert.match(hosts.financeChart.innerHTML,/not available/);
  renderFinance([point({rpc:null,average_cpi:null})],'USD');
  assert.match(hosts.financeChart.innerHTML,/bi-finance-bar/); assert.doesNotMatch(hosts.financeChart.innerHTML,/bi-rpc-line/);
  renderFinance([point({revenue:null,rpc:null})],'USD');
  assert.match(hosts.financeChart.innerHTML,/Average CPI · per complete/);
  assert.doesNotMatch(hosts.financeChart.innerHTML,/bi-finance-bar|Revenue \$/);
});
test('zero-entrant gaps are not rendered as a zero rate or joined across missing data',()=>{
  const {hosts,renderVolume}=setup();
  renderVolume([point(),point({hits:0,completes:0,conversion_rate:0}),point()]);
  assert.equal((hosts.volumeChart.innerHTML.match(/class="bi-rate-dot"/g)||[]).length,2);
  assert.equal((hosts.volumeChart.innerHTML.match(/class="bi-chart-line bi-conversion-line"/g)||[]).length,2);
});
test('empty, narrow and long timelines render safely and escape provider labels',()=>{
  const {hosts,renderVolume}=setup(320);
  renderVolume([]); assert.match(hosts.volumeChart.innerHTML,/No data/);
  renderVolume([point({hits:0,completes:0,revenue:0})]); assert.match(hosts.volumeChart.innerHTML,/No activity/);
  renderVolume(Array.from({length:31},()=>point({label:'<img src=x>',short_label:'<script>'})));
  assert.doesNotMatch(hosts.volumeChart.innerHTML,/<img|<script|NaN|Infinity/);
  assert.match(hosts.volumeChart.innerHTML,/&lt;script&gt;/);
});
test('nice scales cover small counts and money without fractional count ticks',()=>{
  const {chartScale}=setup();
  for(const maximum of [0,1,3,928,10234]) {
    const scale=chartScale(maximum); assert.ok(scale.maximum>=maximum);
    assert.ok(scale.ticks.every(Number.isInteger)); assert.ok(scale.ticks.length<=6);
  }
  assert.ok(chartScale(.09,.01).maximum>=.09);
});
test('performer label and initial use supplied branch/user and no unassigned subtitle',()=>{
  const {hosts,renderTopSuppliers}=setup();
  renderTopSuppliers([{name:'Compound Infotech',branch_name:'',completes:501},{name:'Yoginder Chauhan',branch_name:'',completes:1}]);
  assert.match(hosts.dashboardTopSuppliers.innerHTML,/>C<\/span>/);
  assert.match(hosts.dashboardTopSuppliers.innerHTML,/Yoginder Chauhan/);
  assert.doesNotMatch(hosts.dashboardTopSuppliers.innerHTML,/Direct traffic|Unassigned branch/);
});

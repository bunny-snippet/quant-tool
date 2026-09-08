const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Execute the actual renderers without booting the page or issuing API requests.
const source = fs.readFileSync(path.join(__dirname, '../static/surveys/dashboard.js'), 'utf8');
function setup(width = 700) {
  const hosts = Object.fromEntries(['volumeChart', 'financeChart', 'dashboardTopSuppliers'].map(id => [id, {
    clientWidth: width, innerHTML: '', children: [], appendChild(el) { this.children.push(el); }, querySelectorAll() { return []; },
  }]));
  const document = {
    getElementById: id => hosts[id],
    createElement: () => ({textContent: '', setAttribute() {}, get innerHTML() { return this.textContent.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); }}),
  };
  const context = vm.createContext({document, URLSearchParams, location:{search:''}, window:{matchMedia:()=>({matches:true})}, Intl});
  const end = source.indexOf('  function populateFinancialYears(');
  vm.runInContext(source.slice(0,end) + 'globalThis.renderers={renderVolume,renderFinance,renderTopSuppliers,chartScale};})();',context);
  return {hosts, ...context.renderers};
}
const point = (overrides={}) => ({label:'8 September · 8 AM',short_label:'8 AM',hits:100,completes:25,revenue:50,rpc:.5,average_cpi:2,conversion_rate:25,incidence_rate:30,...overrides});

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

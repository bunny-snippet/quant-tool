const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function picker(value='') {
  const handlers={}, input={value,matches:()=>true};
  vm.runInNewContext(fs.readFileSync('static/surveys/date_filters.js','utf8'),{Intl,Date:class extends Date {constructor(){super('2026-09-08T10:15:00Z');}},document:{addEventListener:(type,fn)=>{handlers[type]=fn;}}});
  return {input,fire:event=>handlers[event]({target:input})};
}
test('blank picker opens at current IST time, cancelling does not apply a hidden date filter',()=>{
  const p=picker();p.fire('focusin');assert.equal(p.input.value,'2026-09-08T15:45');
  p.fire('focusout');assert.equal(p.input.value,'');
});
test('back date becomes midnight before page handlers; later explicit time is preserved',()=>{
  const p=picker();p.fire('focusin');p.input.value='2026-09-07T15:45';p.fire('input');assert.equal(p.input.value,'2026-09-07T00:00');
  p.input.value='2026-09-07T13:25';p.fire('input');p.fire('change');p.fire('focusout');assert.equal(p.input.value,'2026-09-07T13:25');
});
test('reopening a saved filter never rewrites its time, clearing stays empty',()=>{
  const p=picker('2026-08-02T19:10');p.fire('focusin');p.fire('focusout');assert.equal(p.input.value,'2026-08-02T19:10');
  p.fire('focusin');p.input.value='';p.fire('input');p.fire('focusout');assert.equal(p.input.value,'');
});

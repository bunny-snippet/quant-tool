const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function picker(value='') {
  const handlers={}, input={value,matches:()=>true};
  vm.runInNewContext(fs.readFileSync('static/surveys/date_filters.js','utf8'),{Intl,Date:class extends Date {constructor(){super('2026-09-08T10:15:00Z');}},document:{addEventListener:(type,fn)=>{handlers[type]=fn;}}});
  return {input,fire:(event,extra={})=>handlers[event]({target:input,...extra})};
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

test('From and To preserve a manually supplied date and time together',()=>{
  for (const time of ['09:30','23:59','00:00']) {
    const p=picker();p.fire('focusin');
    p.input.value='2026-08-28T'+time;p.fire('input');p.fire('change');p.fire('focusout');p.fire('change');
    assert.equal(p.input.value,'2026-08-28T'+time);
  }
});

test('change after blur cannot reset the manually edited To time',()=>{
  const p=picker();p.fire('focusin');
  p.input.value='2026-08-28T15:45';p.fire('input');assert.equal(p.input.value,'2026-08-28T00:00');
  p.input.value='2026-08-28T23:59';p.fire('input');p.fire('focusout');p.fire('change');
  assert.equal(p.input.value,'2026-08-28T23:59');
});

test('typing time before changing the date retains the explicit time',()=>{
  const p=picker();p.fire('focusin');p.input.value='2026-09-08T11:15';p.fire('input');
  p.input.value='2026-08-28T11:15';p.fire('input');p.fire('change');
  assert.equal(p.input.value,'2026-08-28T11:15');
});

test('an event without focus history preserves the entire explicit value',()=>{
  const p=picker('2026-08-28T23:59');p.fire('change');
  assert.equal(p.input.value,'2026-08-28T23:59');
});

test('partial manual edits and paste are not replaced by default midnight',()=>{
  const p=picker();p.fire('focusin');p.input.value='';p.fire('input');
  p.input.value='2026-08-28T15:45';p.fire('input');p.fire('change');assert.equal(p.input.value,'2026-08-28T15:45');
  p.fire('focusout');p.fire('focusin');p.input.value='2026-08-27T15:45';p.fire('input',{inputType:'insertFromPaste'});
  assert.equal(p.input.value,'2026-08-27T15:45');
});

test('date-only edits to saved values still reset, today keeps the carried time',()=>{
  const p=picker('2026-08-29T09:15');p.fire('focusin');p.input.value='2026-08-28T09:15';p.fire('change');
  assert.equal(p.input.value,'2026-08-28T00:00');
  p.fire('focusout');p.fire('focusin');p.input.value='2026-09-08T00:00';p.fire('change');
  assert.equal(p.input.value,'2026-09-08T00:00');
});

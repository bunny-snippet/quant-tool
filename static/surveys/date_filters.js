/* Only report filters, never supplier scheduling/expiry forms. Times are IST. */
(() => {
  const selector = '.datetime-control input[type="datetime-local"]';
  const drafts = new WeakMap();
  const nowValue = () => {
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date());
    const p = Object.fromEntries(parts.map(x=>[x.type,x.value]));
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  };
  document.addEventListener('focusin', event => {
    const input=event.target; if (!input.matches(selector)) return;
    const initial=input.value;
    if (!initial) input.value=nowValue();
    drafts.set(input,{date:input.value.slice(0,10),seed:initial ? null : input.value});
  });
  function changed(event) {
    const input=event.target; if (!input.matches(selector)) return;
    const draft=drafts.get(input) || {date:''};
    const date=input.value.slice(0,10);
    if (date && date!==draft.date && date < nowValue().slice(0,10)) input.value=date+'T00:00';
    drafts.set(input,{date,seed:null});
  }
  // Capture normalizes before individual page handlers build API/export queries.
  document.addEventListener('input',changed,true);
  document.addEventListener('change',changed,true);
  document.addEventListener('focusout',event=>{
    const input=event.target, draft=drafts.get(input);
    if (draft?.seed && input.value===draft.seed) input.value='';
    drafts.delete(input);
  });
})();

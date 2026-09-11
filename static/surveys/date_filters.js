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
    drafts.set(input,{value:input.value,seed:initial ? null : input.value,timeEdited:false});
  });
  function changed(event) {
    const input=event.target; if (!input.matches(selector)) return;
    const draft=drafts.get(input);
    const value=input.value;
    // A native segmented editor can briefly report an empty value while the
    // user types. Do not treat the next complete date/time as a date-only edit.
    if (!value) { drafts.set(input,{value:'',seed:null,timeEdited:false}); return; }
    const date=value.slice(0,10), time=value.slice(11);
    const previousDate=draft?.value.slice(0,10), previousTime=draft?.value.slice(11);
    const explicitTime=!!draft?.timeEdited || (!!previousTime && time!==previousTime)
      || event.inputType==='insertFromPaste';
    // Reset only an observed date-only change carrying forward the old time.
    // A combined date/time edit, or an event without a known baseline, wins.
    if (previousDate && date!==previousDate && date < nowValue().slice(0,10)
        && time===previousTime && !explicitTime) input.value=date+'T00:00';
    drafts.set(input,{value:input.value,seed:null,timeEdited:explicitTime});
  }
  // Capture normalizes before individual page handlers build API/export queries.
  document.addEventListener('input',changed,true);
  document.addEventListener('change',changed,true);
  document.addEventListener('focusout',event=>{
    const input=event.target, draft=drafts.get(input);
    if (draft?.seed && input.value===draft.seed) input.value='';
    // Some browsers dispatch change after blur. Keep the committed baseline
    // so that final change cannot mistake an explicit time for a new date.
    if (draft) drafts.set(input,{value:input.value,seed:null,timeEdited:false});
  });
})();

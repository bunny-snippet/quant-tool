(() => {
  const drawer = document.getElementById('vaultAnswerDrawer');
  const backdrop = document.getElementById('vaultAnswerBackdrop');
  const body = document.getElementById('vaultAnswerDrawerBody');
  const count = document.getElementById('vaultAnswerCount');
  const closeButton = document.getElementById('closeVaultAnswers');
  if (!drawer || !backdrop || !body) return;

  let closeTimer = null;

  function openAnswers(button) {
    const template = document.getElementById(button.dataset.vaultAnswerTarget || '');
    if (!template) return;
    if (closeTimer) window.clearTimeout(closeTimer);
    body.replaceChildren(template.content.cloneNode(true));
    const answerTotal = body.querySelectorAll('.vault-answer-list article').length;
    count.textContent = `${answerTotal} captured answer${answerTotal === 1 ? '' : 's'}`;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add('vault-drawer-open');
    window.requestAnimationFrame(() => {
      drawer.classList.add('open');
      backdrop.classList.add('open');
      closeButton?.focus();
    });
  }

  function closeAnswers() {
    drawer.classList.remove('open');
    backdrop.classList.remove('open');
    document.body.classList.remove('vault-drawer-open');
    closeTimer = window.setTimeout(() => {
      drawer.hidden = true;
      backdrop.hidden = true;
      body.replaceChildren();
    }, 220);
  }

  document.addEventListener('click', (event) => {
    const trigger = event.target.closest('[data-vault-answer-target]');
    if (trigger) openAnswers(trigger);
  });
  closeButton?.addEventListener('click', closeAnswers);
  backdrop.addEventListener('click', closeAnswers);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !drawer.hidden) closeAnswers();
  });
})();

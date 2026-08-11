(() => {
  const shell = document.querySelector('.app-shell');
  const menu = document.getElementById('menuButton');
  const scrim = document.getElementById('scrim');
  if (!shell || !menu) return;
  const isMobile = () => window.matchMedia('(max-width: 900px)').matches;
  const setSidebar = (open) => {
    shell.dataset.sidebar = open ? 'open' : 'closed';
    menu.setAttribute('aria-expanded', String(open));
  };
  menu.addEventListener('click', () => setSidebar(shell.dataset.sidebar !== 'open'));
  scrim?.addEventListener('click', () => setSidebar(false));
  window.addEventListener('resize', () => { if (!isMobile() && shell.dataset.sidebar === 'closed') setSidebar(true); });
  if (isMobile()) setSidebar(false);
})();


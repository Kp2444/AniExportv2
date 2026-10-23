    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    const btnTheme = document.getElementById('btnTheme');

    function applyTheme(mode) {
      document.body.classList.toggle('light', mode === 'light');
      btnTheme.textContent = mode === 'light' ? 'Dark mode' : 'Light mode';
    }

    let themeMode = null;
    try {
      themeMode = localStorage.getItem('aniexport.theme');
    } catch (e) {}
    if (themeMode !== 'light' && themeMode !== 'dark') themeMode = 'dark';
    applyTheme(themeMode);

    btnTheme.addEventListener('click', () => {
      themeMode = themeMode === 'light' ? 'dark' : 'light';
      try {
        localStorage.setItem('aniexport.theme', themeMode);
      } catch (e) {}
      applyTheme(themeMode);
    });

    window.addEventListener('beforeunload', (e) => {
      saveStateNow();
      if (!isProcessing) return;
      e.preventDefault();
      e.returnValue = '';
    });

    // A snapshot outlives the page, so the undo stays offered after a reload.
    (async () => {
      // A snapshot outlives the page, so the undo stays offered after a
      // reload. The newest only seeds the button; the dialog lists them all.
      try {
        const found = await refreshSnapshots();
        if (found.length && !lastWriteSnapshot) {
          lastWriteSnapshot = found[0].name;
          lastWriteCreated = found[0].deletable || 0;
          updateWriteAftermath();
        }
      } catch (e) {}
    })();

    // Initial load: restore the previous session, or start clean.
    if (!restoreState()) {
      updateMediaTypeUI();
      loadData();
      setDestination('', false);   // first run: ask rather than assume
    }
    updateAccountChip();
  
    // The resolve run has always had Cancel and Pause. The write, undo and
    // copy runs register with the same control block and check `cancel`
    // between batches, but never showed the controls at all - so a long copy
    // could not be stopped from the page. Pause stays hidden for them: only
    // the resolve loop actually waits on it, and a button that does nothing is
    // worse than no button.
    function showRunCancel(runId) {
      activeRunId = runId || null;
      btnCancel.disabled = false;
      btnPause.style.display = 'none';
      runControls.classList.add('show');
    }

    function hideRunCancel() {
      runControls.classList.remove('show');
      btnPause.style.display = '';
      activeRunId = null;
    }

    async function sendRunControl(action) {
      try {
        const res = await fetch('/api/run-control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: action, run_id: activeRunId }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) showToast(payload.error || 'Could not control the run', 'warning');
      } catch (e) {
        showToast('Could not reach the local server', 'error');
      }
    }

    btnCancel.addEventListener('click', () => {
      btnCancel.disabled = true;
      sendRunControl('cancel');
    });

    btnPause.addEventListener('click', () => {
      isPaused = !isPaused;
      btnPause.textContent = isPaused ? 'Resume' : 'Pause';
      sendRunControl(isPaused ? 'pause' : 'resume');
    });

    // R27: some people want the data, not an import file. Built from the rows
    // on screen rather than a new endpoint - the server already refuses to be
    // told where to write, and this needs nothing it does not have.
    const CSV_COLUMNS = [
      ['title', i => i.title],
      ['pasted', i => i.pasted || i.original || ''],
      ['mal_id', i => i.id || ''],
      ['anilist_id', i => i.anilist_id || ''],
      ['type', i => i.type || ''],
      ['year', i => i.year || ''],
      ['status', i => i.status || ''],
      ['score', i => (i.score === undefined ? '' : i.score)],
      // progressFields() returns [field, unit] pairs, not an object - so the
      // old `.read` / `.total` were both undefined and every row exported a
      // progress derived from the wrong field. The value a person actually set
      // wins; the derived one only fills a blank.
      ['progress', i => progressValue(i, progressFields()[0][0])],
      ['total', i => i[PROGRESS_TOTAL_FIELD[progressFields()[0][0]]] || ''],
      ['volumes_read', i => (mediaType() === 'manga' ? progressValue(i, 'my_read_volumes') : '')],
      ['volumes', i => (mediaType() === 'manga' ? (i.volumes || '') : '')],
      ['start_date', i => i.start_date || ''],
      ['finish_date', i => i.finish_date || ''],
      ['tags', i => i.tags || ''],
      ['comments', i => i.comments || ''],
      ['needs_review', i => (i.needs_review ? 'yes' : '')],
      ['review_reason', i => i.review_reason || ''],
      ['known_alias', i => (i.alias_hit ? 'yes' : '')],
    ];

    function progressValue(item, field) {
      const own = item[field];
      return (own === undefined || own === null || own === '') ? derivedProgress(item, field) : own;
    }

    function csvCell(value) {
      const text = value === null || value === undefined ? '' : String(value);
      // Excel reads a leading =, +, - or @ as a formula, so a title starting
      // with one is prefixed. RFC 4180 quoting otherwise.
      const guarded = /^[=+\-@]/.test(text) ? "'" + text : text;
      return /[",\r\n]/.test(guarded) ? '"' + guarded.replace(/"/g, '""') + '"' : guarded;
    }

    btnDownloadCsv.addEventListener('click', () => {
      const rows = currentAnimes.filter(i => i.enabled !== false);
      if (!rows.length) {
        showToast('No rows selected', 'warning');
        return;
      }
      const lines = [CSV_COLUMNS.map(c => c[0]).join(',')];
      rows.forEach(item => {
        lines.push(CSV_COLUMNS.map(c => csvCell(c[1](item))).join(','));
      });
      // A BOM, because Excel reads a UTF-8 CSV as the system codepage without
      // one and every non-ASCII title arrives mangled.
      offerDownload(stampedName(mediaType() === 'manga' ? 'aniexport_manga' : 'aniexport_anime', 'csv'),
                    String.fromCharCode(65279) + lines.join(String.fromCharCode(13) + String.fromCharCode(10)),
                    'text/csv');
      showToast(rows.length + ' rows exported as CSV');
    });

    btnPreview.addEventListener('click', async () => {
      setBusy(btnPreview, true);
      try {
        const res = await fetch('/api/preview-xml?media=' + mediaType());
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          showToast(payload.error || 'Nothing to preview yet', 'warning');
          return;
        }
        previewTitle.textContent = payload.name + ' (' + payload.bytes.toLocaleString() + ' bytes'
          + (payload.truncated ? ', truncated' : '') + ')';
        previewBody.textContent = payload.xml;
        showModal(previewModal);
      } catch (e) {
        showToast('Could not reach the local server', 'error');
      } finally {
        setBusy(btnPreview, false);
      }
    });

    btnPreviewClose.addEventListener('click', () => hideModal(previewModal));
    previewModal.addEventListener('click', (e) => {
      if (e.target === previewModal) hideModal(previewModal);
    });
    // Opening a dialog without moving focus leaves a keyboard user outside it,
    // still tabbing the page behind the backdrop.
    let modalReturnFocus = null;
    function showModal(modal) {
      if (!modal.classList.contains('show')) {
        modalReturnFocus = document.activeElement;
      }
      modal.classList.add('show');
      syncModalLock();
      const first = modal.querySelector('input:not([type=hidden]):not([disabled]), textarea, select, button');
      if (first) first.focus();
    }

    function hideModal(modal) {
      const wasOpen = modal.classList.contains('show');
      modal.classList.remove('show');
      if (wasOpen && modalReturnFocus && document.contains(modalReturnFocus)) {
        modalReturnFocus.focus();
        modalReturnFocus = null;
      }
      syncModalLock();
    }

    // The scrim stops the mouse reaching the page behind a dialog; nothing
    // stopped the wheel. Scrolling moved the content under the dialog, which
    // is disorienting and easy to do by accident on a trackpad. The gutter is
    // reserved on <html> already, so locking here shifts nothing.
    function syncModalLock() {
      // On <html>, not <body>: <html> is the scrolling element, and the
      // lock has no effect from <body> while that is true.
      document.documentElement.classList.toggle('modal-open', !!openModal());
    }

    function openModal() {
      const stack = document.querySelectorAll('.modal-backdrop.show');
      return stack.length ? stack[stack.length - 1] : null;
    }

    function tabbableIn(root) {
      return [...root.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),'
        + ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter(el => el.offsetParent !== null || el === document.activeElement);
    }

    // Without this, Tab walked straight out of the dialog and went on driving
    // the page behind it - which the scrim says you cannot touch.
    function trapTab(e) {
      const modal = openModal();
      if (!modal) return;
      const items = tabbableIn(modal);
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (!modal.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && active === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault(); first.focus();
      }
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') trapTab(e);
      if (e.key === 'Escape') {
        // A popover sits above everything, including a dialog it was opened
        // from, so it closes first and the dialog underneath stays.
        if (popoverIsOpen()) {
          closePopovers(true);
          return;
        }
        // Queried, not a hand-kept list: the last two dialogs added were both
        // missed by the old one. The topmost only - closing every open dialog
        // meant one Escape could take a write plan with it. Nothing in the UI
        // opens a second dialog over a first today, so this is a guard.
        const stack = document.querySelectorAll('.modal-backdrop.show');
        if (stack.length) hideModal(stack[stack.length - 1]);
      }
      const inDialog = !!document.querySelector('.modal-backdrop.show');
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !isProcessing && !inDialog) {
        e.preventDefault();
        btnProcess.click();
      }
      if (e.key === '?' && !e.ctrlKey && !e.metaKey && !e.altKey && !inDialog) {
        const tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
        e.preventDefault();
        openShortcuts();
      }
    });
    btnPreviewCopy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(previewBody.textContent || '');
        showToast('XML copied');
      } catch (e) {
        showToast('Clipboard blocked by the browser', 'warning');
      }
    });

    toggleArchive.addEventListener('change', saveState);
    toggleFresh.addEventListener('change', saveState);

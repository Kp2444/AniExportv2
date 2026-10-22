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
      ['progress', i => derivedProgress(i, progressFields().read)],
      ['total', i => i[progressFields().total] || ''],
      ['start_date', i => i.start_date || ''],
      ['finish_date', i => i.finish_date || ''],
      ['tags', i => i.tags || ''],
      ['comments', i => i.comments || ''],
      ['needs_review', i => (i.needs_review ? 'yes' : '')],
      ['review_reason', i => i.review_reason || ''],
      ['known_alias', i => (i.alias_hit ? 'yes' : '')],
    ];

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
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        // Every backdrop, rather than a hand-kept list: the last two dialogs
        // added were both missed by the old one.
        document.querySelectorAll('.modal-backdrop.show').forEach(hideModal);
      }
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !isProcessing) {
        e.preventDefault();
        btnProcess.click();
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

    // =====================================================================
    // The shell: three stages (Source, Review, Send), the source tabs, the
    // popovers, the shortcut sheet, and the split between "what new rows
    // start as" and "set the selected rows to". This is presentation and flow;
    // the behaviour it drives lives in the sections before it.
    //
    // Element handles are looked up when they are used rather than held in
    // consts. Earlier sections call into this one (updateStats -> updateLayout),
    // and a const declared here is not initialised until this section has run.
    // =====================================================================
    function byId(id) { return document.getElementById(id); }

    // The table header sticks under the top bar, and scroll padding keeps a
    // keyboard-selected row clear of both bars. Both bars change height with
    // the width and the destination, so their heights are published live.
    if (window.ResizeObserver) {
      const bars = new Map([[document.querySelector('.appbar'), '--appbar-h'],
                            [byId('sendBar'), '--sendbar-h']]);
      const observer = new ResizeObserver(entries => entries.forEach(e =>
        document.documentElement.style.setProperty(bars.get(e.target), e.target.offsetHeight + 'px')));
      bars.forEach((_, el) => observer.observe(el));

      // Below about 820px the table scrolls sideways inside its panel. A
      // scroll box reachable only by mouse is unusable from the keyboard, so
      // it becomes a focusable region exactly while it actually scrolls -
      // never a pointless tab stop on a width where everything fits.
      const wrap = document.querySelector('.table-wrapper');
      if (wrap) {
        const syncScrollRegion = () => {
          const scrolls = wrap.scrollWidth > wrap.clientWidth + 1;
          if (scrolls) {
            wrap.setAttribute('tabindex', '0');
            wrap.setAttribute('role', 'region');
            wrap.setAttribute('aria-label', 'Resolved titles, scrolls sideways');
          } else {
            wrap.removeAttribute('tabindex');
            wrap.removeAttribute('role');
            wrap.removeAttribute('aria-label');
          }
        };
        // Width alone decides it: the table's min-width is fixed, so whether
        // it overflows changes with the wrapper, not with the row count.
        new ResizeObserver(syncScrollRegion).observe(wrap);
      }
    }

    // `disabled` does nothing to an <a>: the download links carried the class
    // and aria-disabled, looked and announced as disabled, and still followed
    // their href - to a 404 when no export had been built.
    document.addEventListener('click', (e) => {
      const link = e.target.closest && e.target.closest('a[aria-disabled="true"]');
      if (link) { e.preventDefault(); e.stopPropagation(); }
    }, true);

    // ---- stages ---------------------------------------------------------
    // Everything about which stage is showing is derived from the data, so it
    // cannot drift: no rows means Source, rows or a running resolve means
    // Review with Send underneath. `sourceOpen` is the one thing a person sets
    // by hand - reopening the source to add more titles.
    function updateLayout() {
      const body = document.body;
      const hasRows = currentAnimes.length > 0;
      const reviewing = hasRows || isProcessing;
      body.dataset.stage = reviewing ? 'review' : 'source';
      body.dataset.sourceOpen = (!reviewing || sourceOpen) ? 'true' : 'false';
      body.dataset.destination = destination || 'none';
      body.dataset.running = isProcessing ? 'true' : 'false';
      body.dataset.sourceTab = sourceTab;

      const current = (!reviewing || sourceOpen) ? 'source' : 'review';
      const selected = currentAnimes.filter(i => i.enabled !== false && isActionable(i)).length;
      [['source', 'stepSource'], ['review', 'stepReview'], ['send', 'stepSend']].forEach(([name, id]) => {
        const step = byId(id);
        if (!step) return;
        step.classList.toggle('is-current', name === current);
        if (name === current) step.setAttribute('aria-current', 'step');
        else step.removeAttribute('aria-current');
      });
      byId('stepSource').classList.toggle('is-done', hasRows && current !== 'source');
      byId('stepReview').disabled = !reviewing;
      byId('stepSend').disabled = !hasRows;
      // Send is not a page of its own - it is the bar under the table - so it
      // is never "current". It says when there is something ready to go.
      byId('stepSend').classList.toggle('is-ready', hasRows && !!destination && selected > 0);

      const summary = byId('sourceSummary');
      if (summary) {
        const noun = mediaType() === 'manga' ? 'manga' : 'anime';
        // "in the list" is implied by the list sitting right below it, and at
        // phone width it was what pushed this onto a second line.
        summary.textContent = hasRows
          ? currentAnimes.length + ' ' + noun + ' row' + (currentAnimes.length === 1 ? '' : 's')
          : (isProcessing ? 'Resolving your list...' : '');
      }

      const process = byId('btnProcess');
      if (process) {
        process.title = destination ? '' : 'Choose where the list is going first';
      }
      updateOptionsCount();
      updateSendAccount();
    }

    function openSource(focus) {
      sourceOpen = true;
      updateLayout();
      window.scrollTo({ top: 0, behavior: 'smooth' });
      if (focus) {
        // One selector at a time: querySelector with a list returns the first
        // match in document order, which was the Paste button above the box.
        const panel = byId(SOURCE_TABS.find(t => t[0] === sourceTab)[2]);
        const first = panel && (panel.querySelector('textarea')
          || panel.querySelector('input:not([type=file])') || panel.querySelector('button'));
        if (first) first.focus({ preventScroll: true });
      }
    }

    function closeSource() {
      sourceOpen = false;
      updateLayout();
    }

    byId('stepSource').addEventListener('click', () => openSource(true));
    byId('btnEditSource').addEventListener('click', () => openSource(true));
    byId('stepReview').addEventListener('click', () => {
      closeSource();
      const search = byId('searchInput');
      if (search) search.focus();
    });
    byId('stepSend').addEventListener('click', () => {
      closeSource();
      // The one action the destination makes primary, or the question that
      // has to be answered before there is one.
      const target = destination === 'anilist' ? byId('btnWrite')
                   : destination === 'xml' ? byId('btnDownloadXml')
                   : document.querySelector('#sendChooser [data-destination]');
      if (target) target.focus();
    });

    // ---- the destination gate --------------------------------------------
    // The server ranks candidates differently by destination (prefer_mal_id),
    // so resolving before choosing ranked every row for a MAL file. The button
    // stays clickable on purpose: a disabled button cannot say why.
    function nudgeDestination() {
      openSource(false);
      const prompt = byId('destinationPrompt');
      prompt.classList.remove('attention');
      void prompt.offsetWidth;            // restart the animation
      prompt.classList.add('attention');
      prompt.scrollIntoView({ block: 'center', behavior: 'smooth' });
      const first = prompt.querySelector('.choice');
      if (first) first.focus({ preventScroll: true });
    }

    // A legacy session, or an import, can leave rows with no destination. The
    // header picker is hidden until one is chosen, so the send bar asks.
    byId('sendChooser').addEventListener('click', (e) => {
      const choice = e.target.closest('[data-destination]');
      if (choice) setDestination(choice.dataset.destination);
    });

    byId('btnReresolve').addEventListener('click', () => {
      if (!txtArea.value.trim()) {
        selectSourceTab('paste');
        openSource(true);
        showToast('The pasted list is empty - paste it again to resolve it for this destination', 'warning');
        return;
      }
      byId('btnProcess').click();
    });

    // ---- source tabs ------------------------------------------------------
    const SOURCE_TABS = [
      ['paste', 'tabPaste', 'panelPaste'],
      ['file', 'tabFile', 'panelFile'],
      ['anilist', 'btnAniListImport', 'panelAniList'],
    ];

    function selectSourceTab(name, focus) {
      if (!SOURCE_TABS.some(t => t[0] === name)) name = 'paste';
      sourceTab = name;
      SOURCE_TABS.forEach(([key, tabId, panelId]) => {
        const on = key === name;
        const tab = byId(tabId);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
        tab.tabIndex = on ? 0 : -1;
        byId(panelId).hidden = !on;
      });
      document.body.dataset.sourceTab = name;
      if (focus) byId(SOURCE_TABS.find(t => t[0] === name)[1]).focus();
    }

    const sourceTabList = document.querySelector('.source-tabs');
    sourceTabList.addEventListener('click', (e) => {
      const tab = e.target.closest('[role="tab"]');
      if (tab) selectSourceTab(tab.dataset.tab);
    });
    // Arrow keys move between tabs and select as they go, as a tab list should.
    sourceTabList.addEventListener('keydown', (e) => {
      const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (keys.indexOf(e.key) < 0) return;
      e.preventDefault();
      const order = SOURCE_TABS.map(t => t[0]);
      let at = order.indexOf(sourceTab);
      if (e.key === 'ArrowLeft') at = (at - 1 + order.length) % order.length;
      if (e.key === 'ArrowRight') at = (at + 1) % order.length;
      if (e.key === 'Home') at = 0;
      if (e.key === 'End') at = order.length - 1;
      selectSourceTab(order[at], true);
    });

    // Dropping an export onto the zone does what choosing it does.
    const fileDrop = byId('fileDrop');
    ['dragenter', 'dragover'].forEach(name => {
      fileDrop.addEventListener(name, (e) => { e.preventDefault(); fileDrop.classList.add('is-dragging'); });
    });
    ['dragleave', 'drop'].forEach(name => {
      fileDrop.addEventListener(name, () => fileDrop.classList.remove('is-dragging'));
    });
    fileDrop.addEventListener('drop', (e) => {
      e.preventDefault();
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) importMalFile(file);
    });

    // ---- popovers ---------------------------------------------------------
    const POPOVERS = [
      ['btnAccount', 'anilistPanel'],
      ['btnOptions', 'optionsPanel'],
      ['btnTools', 'toolsMenu'],
    ];

    function popoverIsOpen() {
      return POPOVERS.some(([, panelId]) => !byId(panelId).hidden);
    }

    function closePopovers(returnFocus) {
      POPOVERS.forEach(([btnId, panelId]) => {
        const panel = byId(panelId);
        if (panel.hidden) return;
        panel.hidden = true;
        byId(btnId).setAttribute('aria-expanded', 'false');
        if (returnFocus) byId(btnId).focus();
      });
      document.body.classList.remove('account-over-modal');
    }

    // Set while the click that opened a popover is still bubbling. Opening one
    // from anything other than its own trigger - Write with no token, the
    // Connect link - otherwise reached the document's outside-click handler on
    // the same event and closed the panel it had just opened.
    let popoverJustOpened = false;

    function openPopover(btnId, panelId) {
      closePopovers(false);
      popoverJustOpened = true;
      setTimeout(() => { popoverJustOpened = false; }, 0);
      byId(panelId).hidden = false;
      byId(btnId).setAttribute('aria-expanded', 'true');
      const first = byId(panelId).querySelector('input:not([type=checkbox]), button, input');
      if (first) first.focus();
    }

    POPOVERS.forEach(([btnId, panelId]) => {
      byId(btnId).addEventListener('click', (e) => {
        e.stopPropagation();
        if (!byId(panelId).hidden) closePopovers(false);
        else openPopover(btnId, panelId);
      });
    });

    // A click anywhere outside an open popover closes it.
    document.addEventListener('click', (e) => {
      if (!popoverIsOpen() || popoverJustOpened) return;
      if (e.target.closest('.popover') || e.target.closest('.popover-anchor > button')) return;
      closePopovers(false);
    });

    // A menu item that opens a dialog should not leave the menu hanging open
    // behind it.
    byId('toolsMenu').addEventListener('click', (e) => {
      if (e.target.closest('#btnTheme') || e.target.closest('#btnReset')) return;
      if (e.target.closest('.menu-item')) closePopovers(false);
    });

    // ---- the AniList account ------------------------------------------------
    // One connection, used by the write, undo, retry, the private-list import
    // and the account copy. It lives in the app bar so every one of them can
    // reach it - including from inside a dialog, which is why the bar lifts
    // above the dialog's backdrop while the panel is open.
    function openAccount() {
      openPopover('btnAccount', 'anilistPanel');
      // After openPopover, not before: it starts by closing every popover,
      // and closing drops this class - so set first, it never took effect and
      // the panel opened underneath the dialog's backdrop.
      if (document.querySelector('.modal-backdrop.show')) {
        document.body.classList.add('account-over-modal');
      }
      byId('anilistToken').focus();
    }

    document.addEventListener('click', (e) => {
      const trigger = e.target.closest('[data-open-account]');
      if (!trigger) return;
      e.preventDefault();
      e.stopPropagation();
      openAccount();
    });

    function updateAccountChip() {
      const chip = byId('btnAccount');
      const bad = !anilistUser && tokenStatus.classList.contains('bad');
      chip.classList.toggle('is-connected', !!anilistUser);
      chip.classList.toggle('is-bad', bad);
      byId('accountLabel').textContent = anilistUser ? anilistUser.name : 'Connect AniList';
      chip.title = anilistUser ? 'Connected to AniList as ' + anilistUser.name : 'Connect your AniList account';
      updateSendAccount();
    }

    function updateSendAccount() {
      const el = byId('sendAccount');
      if (!el) return;
      el.innerHTML = anilistUser
        ? 'Writing as <strong>' + escapeHtml(anilistUser.name) + '</strong>'
        : '<button type="button" class="link-button" data-open-account>Connect AniList</button> to write';
    }

    // A token typed but never checked is not a connection: say so on the chip.
    byId('anilistToken').addEventListener('input', () => {
      if (anilistUser) {
        anilistUser = null;
        tokenStatus.textContent = 'Token changed - press Check to connect with it.';
        tokenStatus.className = 'token-status';
        updateAccountChip();
      }
    });
    byId('anilistToken').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); byId('btnTokenTest').click(); }
    });

    // ---- resolve options --------------------------------------------------
    // Folded away because they are optional, so a non-default one has to
    // announce itself on the button or the fold hides state that changes a run.
    // The sequels switch does nothing for manga, so it is hidden and not counted.
    function updateOptionsCount() {
      const badge = byId('optionsCount');
      if (!badge) return;
      const changed = [toggleFresh, toggleArchive, toggleSequels]
        .filter(t => t.checked && t.closest('.toggle-card').style.display !== 'none').length;
      badge.textContent = String(changed);
      badge.hidden = changed === 0;
    }
    byId('optionsPanel').addEventListener('change', updateOptionsCount);

    // ---- set the selected rows ---------------------------------------------
    // The defaults in step 1 decide what new rows start as. These decide what
    // existing rows become. They used to be one pair of controls doing both,
    // so changing the default for the next paste silently re-stamped the list.
    function syncBulkStatus() {
      const select = byId('bulkStatus');
      if (!select) return;
      select.innerHTML = '<option value="">status...</option>' + STATUS_SETS[mediaType()]
        .map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
    }

    const bulkStatus = byId('bulkStatus');
    const bulkScore = byId('bulkScore');

    bulkStatus.addEventListener('change', () => {
      const value = bulkStatus.value;
      if (!value) return;
      const count = bulkTargets().rows.length;
      applyBulk('status', value);
      bulkStatus.value = '';
      showToast(count + ' row' + (count === 1 ? '' : 's') + ' set to ' + value);
    });

    // `change`, never `input`: a select only commits a choice, so this cannot
    // re-stamp the list on every keystroke the way the old number box did.
    bulkScore.addEventListener('change', () => {
      if (bulkScore.value === '') return;
      const value = parseInt(bulkScore.value, 10) || 0;
      const count = bulkTargets().rows.length;
      applyBulk('score', value);
      bulkScore.value = '';
      showToast(count + ' row' + (count === 1 ? '' : 's') + (value ? ' scored ' + value : ' left unscored'));
    });

    // ---- small things the review needs --------------------------------------
    byId('btnClearFilter').addEventListener('click', () => {
      searchInput.value = '';
      statusFilter = '';
      showAllRows = false;
      renderTable(currentAnimes);
      updateStats();
      searchInput.focus();
    });

    // ---- keyboard shortcuts --------------------------------------------------
    // Every one of these existed and none was discoverable.
    const shortcutsModal = byId('shortcutsModal');
    function openShortcuts() {
      closePopovers(false);
      showModal(shortcutsModal);
    }
    byId('btnShortcuts').addEventListener('click', openShortcuts);
    byId('btnShortcutsInline').addEventListener('click', openShortcuts);
    byId('btnShortcutsClose').addEventListener('click', () => hideModal(shortcutsModal));
    shortcutsModal.addEventListener('click', (e) => {
      if (e.target === shortcutsModal) hideModal(shortcutsModal);
    });

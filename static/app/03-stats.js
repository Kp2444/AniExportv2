    function filteredCount() {
      return currentAnimes.filter(matchesFilters).length;
    }

    // Two callers used to write this line with different arithmetic:
    // renderTable counted the rows it had actually drawn, updateStats counted
    // every row that matched. Below the 300-row cap they agree; above it the
    // number jumped the moment anything called one instead of the other.
    function updateFilterCount() {
      const matching = filteredCount();
      const shown = showAllRows ? matching : Math.min(matching, ROW_CAP);
      const selected = currentAnimes.filter(i => i.enabled !== false).length;
      filterCountLabel.textContent = 'Showing ' + shown + ' of ' + currentAnimes.length
        + ' (' + selected + ' selected)';
    }

    function updateStats() {
      const enabledItems = currentAnimes.filter(i => i.enabled !== false && isActionable(i));
      const totalCount = currentAnimes.length;
      const enabledCount = enabledItems.length;

      statTotal.textContent = totalCount;
      statNormalized.textContent = enabledCount === totalCount ? String(totalCount) : `${enabledCount} of ${totalCount}`;
      if (mediaType() === 'manga') {
        const totalCh = enabledItems.reduce((acc, curr) => acc + (curr.chapters || 0), 0);
        const totalVol = enabledItems.reduce((acc, curr) => acc + (curr.volumes || 0), 0);
        statEpisodes.textContent = totalCh.toLocaleString() + ' ch / ' + totalVol.toLocaleString() + ' vol';
      } else {
        const totalEps = enabledItems.reduce((acc, curr) => acc + (curr.episodes || 0), 0);
        statEpisodes.textContent = totalEps.toLocaleString();
      }

      updateFilterCount();

      const missed = currentAnimes.filter(i => !canXml(i) && !canWrite(i));
      const anilistOnly = currentAnimes.filter(i => !canXml(i) && canWrite(i));
      const notes = [];
      const noun = mediaType() === 'manga' ? 'manga' : 'anime';
      if (missed.length > 0) {
        // Blaming the list for a network outage is how a tool loses trust.
        notes.push(networkFailures > 0
          ? `<strong>${missed.length}</strong> ${noun} ${missed.length === 1 ? 'title' : 'titles'} could not be checked: AniList was unreachable for ${networkFailures} of them. Re-run before assuming the titles are wrong.`
          : `<strong>${missed.length}</strong> ${noun} ${missed.length === 1 ? 'title has' : 'titles have'} no MyAnimeList ID on AniList and cannot be exported. Check the spelling, or try the romaji title.`);
      }
      if (anilistOnly.length > 0) {
        notes.push(destination === 'xml'
          ? `<strong>${anilistOnly.length}</strong> ${anilistOnly.length === 1 ? 'title exists' : 'titles exist'} on AniList but ${anilistOnly.length === 1 ? 'has' : 'have'} no MyAnimeList ID, so MAL XML cannot carry ${anilistOnly.length === 1 ? 'it' : 'them'}. Switch the destination to AniList to keep ${anilistOnly.length === 1 ? 'it' : 'them'}.`
          : `<strong>${anilistOnly.length}</strong> ${anilistOnly.length === 1 ? 'title is' : 'titles are'} on AniList without a MyAnimeList ID. ${anilistOnly.length === 1 ? 'It' : 'They'} cannot go into the MAL XML file, but <strong>Write to AniList</strong> sends ${anilistOnly.length === 1 ? 'it' : 'them'} straight to your account.`);
      }
      if (mergedTitles.length > 0) {
        const names = mergedTitles.slice(0, 4).map(m => escapeHtml(m.original || m.title)).join(', ');
        const more = mergedTitles.length > 4 ? ` and ${mergedTitles.length - 4} more` : '';
        notes.push(`<strong>${mergedTitles.length}</strong> pasted ${mergedTitles.length === 1 ? 'line' : 'lines'} matched an entry already in the list and ${mergedTitles.length === 1 ? 'was' : 'were'} merged: ${names}${more}.`);
      }
      unresolvedBar.classList.toggle('show', notes.length > 0);
      if (notes.length > 0) {
        unresolvedText.innerHTML = notes.join('<br>');
      }

      if (enabledCount > 0) {
        const xmlable = enabledItems.filter(canXml).length;
        exportHeadline.textContent = `${enabledCount} ${mediaType() === 'manga' ? 'manga' : 'anime'} selected`;
        const onDisk = exportFileInfo
          ? `${exportFileInfo.count} ${exportFileInfo.count === 1 ? 'entry' : 'entries'} in ${exportFileInfo.name}. `
          : '';
        exportSubline.textContent = onDisk + (xmlable === enabledCount
          ? 'MAL-compatible XML for import through MyAnimeList or AniList.'
          : `Only ${xmlable} of ${enabledCount} can travel in MAL XML; the rest have no MyAnimeList ID.`);
        btnDownloadXml.classList.remove('disabled');
        btnDownloadXml.removeAttribute('aria-disabled');
        btnDownloadGz.classList.remove('disabled');
        btnDownloadGz.removeAttribute('aria-disabled');
        statXmlValid.textContent = 'Valid';
        statXmlValid.style.color = 'var(--success)';
      } else {
        exportHeadline.textContent = `No ${mediaType() === 'manga' ? 'manga' : 'anime'} selected`;
        exportSubline.textContent = `Select at least one ${mediaType() === 'manga' ? 'manga' : 'anime'} row to enable the download.`;
        btnDownloadXml.classList.add('disabled');
        btnDownloadXml.setAttribute('aria-disabled', 'true');
        btnDownloadGz.classList.add('disabled');
        btnDownloadGz.setAttribute('aria-disabled', 'true');
        statXmlValid.textContent = 'Empty';
        statXmlValid.style.color = 'var(--text-muted)';
      }

      // A run owns the export files until it finishes; never offer a stale download.
      if (isProcessing) {
        setDownloadEnabled(btnDownloadXml, false);
        setDownloadEnabled(btnDownloadGz, false);
        exportHeadline.textContent = `Resolving... ${enabledCount} ready so far`;
        exportSubline.textContent = 'Download unlocks when the run finishes.';
      }
    }

    function updateStatsAndExport() {
      updateStats();
      saveState();
      const enabledItems = currentAnimes.filter(i => i.enabled !== false && isActionable(i));
      clearTimeout(exportDebounceTimer);
      exportDebounceTimer = setTimeout(async () => {
        try {
          const res = await fetch('/api/update-export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              entries: enabledItems.map(({ candidates, ...rest }) => rest),
              media_type: mediaType(),
              status: selectStatus.value,
              score: parseInt(inputScore.value, 10) || 0,
              archive: toggleArchive.checked
            })
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            if (err.error) showToast(err.error, 'error');
          }
          await refreshExportState();
        } catch (e) {
          // ignore background sync error
        }
      }, 250);
    }

    function setDownloadEnabled(btn, enabled) {
      btn.classList.toggle('disabled', !enabled);
      if (enabled) {
        btn.removeAttribute('aria-disabled');
      } else {
        btn.setAttribute('aria-disabled', 'true');
      }
    }

    // Ask the server what it actually holds. The client's own guess is used for the
    // instant feedback; this is the authority once a write has landed.
    async function refreshExportState() {
      try {
        const res = await fetch('/api/export-state');
        if (!res.ok) return;
        const state = await res.json();
        const info = state[mediaType()];
        if (!info) return;
        btnDownloadXml.href = '/api/download/xml?media=' + mediaType();
        btnDownloadGz.href = '/api/download/gz?media=' + mediaType();
        btnDownloadXml.download = info.xml_name;
        btnDownloadGz.download = info.gz_name;
        setDownloadEnabled(btnDownloadXml, info.xml);
        setDownloadEnabled(btnDownloadGz, info.gz);
        // Record what the server holds; updateStats owns the wording so the two
        // do not overwrite each other's message.
        exportFileInfo = (info.xml && info.count !== null && info.count !== undefined)
          ? { name: info.xml_name, count: info.count }
          : null;
        updateStats();
      } catch (e) {
        // leave the client-side state as-is
      }
    }

    [btnDownloadXml, btnDownloadGz].forEach(btn => {
      btn.addEventListener('click', (e) => {
        if (btn.classList.contains('disabled')) {
          e.preventDefault();
          showToast('No export generated yet for this media type', 'warning');
        }
      });
    });

    function updateSelectAllState() {
      const visible = currentAnimes.filter(matchesFilters);
      if (visible.length === 0) {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = false;
        return;
      }
      const allChecked = visible.every(i => i.enabled !== false);
      const someChecked = visible.some(i => i.enabled !== false);
      selectAllCheckbox.checked = allChecked;
      selectAllCheckbox.indeterminate = !allChecked && someChecked;
    }

    function pickerHtml(item, idx) {
      const list = Array.isArray(item.candidates) ? item.candidates : [];
      if (list.length < 2) return '';
      const options = list.map((c, i) => {
        const counts = mediaType() === 'manga'
          ? `${c.chapters || '?'} ch`
          : `${c.episodes || '?'} eps`;
        // A candidate with no MAL id is still valid for a direct AniList write;
        // labelling it "MAL 0" would read as broken.
        const ident = Number(c.id) > 0 ? `MAL ${c.id}` : `AniList ${c.anilist_id} (no MAL ID)`;
        const label = `${c.title} - ${ident} - ${c.type} - ${counts}`;
        return `<option value="${i}" ${Number(c.id) === Number(item.id) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
      }).join('');
      return `<select class="match-picker" data-index="${idx}" title="Pick the correct match for this title">${options}</select>`;
    }

    // A row can be exportable by one route and not the other. Older saved rows
    // predate the flags, so fall back to the ids they do carry.
    function canXml(item) {
      return item.can_export_xml !== undefined
        ? !!item.can_export_xml
        : Number(item.id) > 0;
    }

    function canWrite(item) {
      return item.can_write_anilist !== undefined
        ? !!item.can_write_anilist
        : Number(item.anilist_id) > 0;
    }

    function isActionable(item) {
      if (destination === 'xml') return canXml(item);
      if (destination === 'anilist') return canWrite(item);
      return canXml(item) || canWrite(item);   // no destination chosen yet
    }

    function rowBadge(item) {
      if (canXml(item)) return { cls: 'tag-type', text: item.type || 'TV' };
      if (canWrite(item)) return { cls: 'status-pill status-anilist-only', text: 'AniList only' };
      return { cls: 'status-pill status-unresolved', text: 'Not found' };
    }

    function setDestination(value, persist) {
      destination = (value === 'anilist' || value === 'xml') ? value : '';
      destinationSelect.value = destination;
      destinationPrompt.classList.toggle('show', destination === '');
      destinationField.style.display = destination === '' ? 'none' : '';
      anilistPanel.classList.toggle('show', destination === 'anilist');
      btnAniListImport.style.display = destination === '' ? 'none' : '';
      // Copying writes to an account, so it belongs with the AniList destination.
      btnCopyAccount.style.display = destination === 'anilist' ? '' : 'none';
      btnWrite.style.display = destination === 'anilist' ? '' : 'none';
      updateWriteAftermath();
      // Naming the file format while the user is sending rows to an account
      // is the kind of small lie that makes people distrust the rest.
      const action = destination === 'anilist' ? 'Resolve titles' : 'Normalize & Generate XML';
      document.querySelectorAll('#btnProcessLabel, #emptyStateAction')
        .forEach(el => { el.textContent = action; });
      renderTable(currentAnimes);
      updateStats();
      if (persist !== false) saveState();
    }

    // Mirrors deduplicate_entries() on the server: MAL id when present, else title.
    const STATE_KEY = 'aniexport.state';
    let stateSaveTimer = null;

    function statePayload(includeResults, includeCandidates) {
      const base = {
        v: 1,
        mediaType: mediaType(),
        status: selectStatus.value,
        score: parseInt(inputScore.value, 10) || 0,
        sequels: !!toggleSequels.checked,
        archive: !!toggleArchive.checked,
        fresh: !!toggleFresh.checked,
        destination: destination,
        text: txtArea.value,
        updated: Date.now(),
      };
      if (!includeResults) return base;
      base.results = currentAnimes.map(item => {
        if (includeCandidates) return item;
        const copy = Object.assign({}, item);
        delete copy.candidates;
        return copy;
      });
      return base;
    }

    // A long run is expensive to repeat, so the session survives a refresh.
    // Candidates are the bulky part: drop them before dropping the rows themselves.
    function saveStateNow() {
      const attempts = [[true, true], [true, false], [false, false]];
      for (let i = 0; i < attempts.length; i++) {
        try {
          window.localStorage.setItem(STATE_KEY, JSON.stringify(statePayload(attempts[i][0], attempts[i][1])));
          return;
        } catch (e) {
          // quota or disabled storage - try a smaller payload, then give up quietly
        }
      }
    }

    function saveState() {
      clearTimeout(stateSaveTimer);
      stateSaveTimer = setTimeout(saveStateNow, 300);
    }

    function clearSavedState() {
      try {
        window.localStorage.removeItem(STATE_KEY);
      } catch (e) {
        // storage unavailable: nothing was saved either
      }
    }

    // Everything the session remembers, back to first-run values. Without this
    // a saved session was permanent: Clear only emptied the textarea and the
    // rows came back on every reopen.
    function resetSession() {
      clearTimeout(stateSaveTimer);
      clearSavedState();
      currentAnimes = [];
      expandedRows.clear();
      statusFilter = '';
      sortKey = '';
      sortDir = 0;
      showAllRows = false;
      searchInput.value = '';
      txtArea.value = '';
      inputScore.value = '0';
      toggleSequels.checked = false;
      toggleArchive.checked = false;
      toggleFresh.checked = false;
      setDestination('', false);
      mediaTypeSelect.value = 'anime';
      // Start over means start over: the set-aside rows go too.
      rowsByMedia.anime = [];
      rowsByMedia.manga = [];
      lastMediaType = 'anime';
      document.querySelectorAll('th.sortable').forEach(th => {
        const mark = th.querySelector('.sort-mark');
        if (mark) mark.textContent = '';
        th.setAttribute('aria-sort', 'none');
      });
      updateMediaTypeUI();
      renderTable(currentAnimes);
      updateStats();
      refreshExportState();
      updateLineCounter();
    }

    let resetArmed = null;
    btnReset.addEventListener('click', () => {
      // Two steps on purpose: a long run is expensive to repeat.
      if (!resetArmed) {
        btnReset.textContent = 'Confirm reset';
        btnReset.classList.add('btn-danger');
        resetArmed = setTimeout(() => {
          resetArmed = null;
          btnReset.textContent = 'Start over';
          btnReset.classList.remove('btn-danger');
        }, 4000);
        return;
      }
      clearTimeout(resetArmed);
      resetArmed = null;
      btnReset.textContent = 'Start over';
      btnReset.classList.remove('btn-danger');
      resetSession();
      showToast('Session cleared. Files already written to disk are untouched.');
    });

    function restoreState() {
      // Loading with #reset discards the saved session before it is applied.
      if (window.location.hash === '#reset') {
        clearSavedState();
        return false;
      }
      let raw = null;
      try {
        raw = window.localStorage.getItem(STATE_KEY);
      } catch (e) {
        return false;
      }
      if (!raw) return false;
      let saved;
      try {
        saved = JSON.parse(raw);
      } catch (e) {
        return false;
      }
      if (!saved || saved.v !== 1) return false;

      if (saved.mediaType === 'manga' || saved.mediaType === 'anime') {
        mediaTypeSelect.value = saved.mediaType;
        lastMediaType = saved.mediaType;
      }
      // Rebuild the media-specific controls first, then restore the values that
      // depend on them - the status options only exist after this call.
      updateMediaTypeUI();
      if (typeof saved.status === 'string') {
        const allowed = STATUS_SETS[mediaType()];
        if (allowed.indexOf(saved.status) >= 0) selectStatus.value = saved.status;
      }
      if (typeof saved.score === 'number') inputScore.value = Math.max(0, Math.min(10, saved.score));
      toggleSequels.checked = !!saved.sequels;
      toggleArchive.checked = !!saved.archive;
      toggleFresh.checked = !!saved.fresh;
      setDestination(saved.destination, false);
      if (typeof saved.text === 'string') txtArea.value = saved.text;
      updateLineCounter();

      if (Array.isArray(saved.results) && saved.results.length) {
        currentAnimes = saved.results;
        renderTable(currentAnimes);
        // Re-sync the server's files so a download matches what is on screen.
        updateStatsAndExport();
        showToast(`Restored ${currentAnimes.length} rows from your last session`);
      } else {
        renderTable([]);
        updateStats();
      }
      return true;
    }

    // Start completely empty every time
    async function loadData() {
      renderTable([]);
      statTotal.textContent = '0';
      statNormalized.textContent = '0';
      statEpisodes.textContent = '0';
      statXmlValid.textContent = 'Not Generated';
      statXmlValid.style.color = 'var(--text-subtle)';
    }

    // Process & Normalize
    btnProcess.addEventListener('click', async () => {
      if (isProcessing) return;
      // The server ranks candidates differently for each destination
      // (prefer_mal_id), so resolving before one is chosen ranked every row
      // for a MAL file and kept that ranking after AniList was picked.
      if (!destination) {
        showToast('Choose where this list is going first - it changes which match each title gets', 'warning');
        nudgeDestination();
        return;
      }
      if (!txtArea.value.trim()) {
        selectSourceTab('paste');
        openSource(true);
        showToast('Paste a list first', 'warning');
        return;
      }
      isProcessing = true;
      sourceOpen = false;
      btnProcess.disabled = true;

      try {
        const payload = {
          content: txtArea.value,
          media_type: mediaType(),
          status: selectStatus.value,
          score: parseInt(inputScore.value, 10) || 0,
          expand_sequels: mediaType() === 'anime' && toggleSequels.checked,
          archive: toggleArchive.checked,
          use_cache: !toggleFresh.checked,
          destination: destination
        };

        currentAnimes = [];
        expandedRows.clear();
        showAllRows = false;
        networkFailures = 0;
        mergedTitles = [];
        renderTable(currentAnimes);
        updateStats();
        activeRunId = null;
        isPaused = false;
        btnPause.textContent = 'Pause';
        btnPause.disabled = false;
        btnCancel.disabled = false;
        runControls.classList.add('show');
        progressCard.style.display = 'flex';
        progressBarFill.style.transform = 'scaleX(0)';
        progressCountLabel.textContent = '0 / 0';
        progressPercentLabel.textContent = '0%';
        progressCurrentTitle.textContent = 'Starting normalization...';
        progressCurrentSub.textContent = 'Connecting to AniList GraphQL...';

        const res = await fetch('/api/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          showToast(errData.error || 'Error processing list', res.status === 409 ? 'warning' : 'error');
          progressCard.style.display = 'none';
          runControls.classList.remove('show');
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let cancelledRun = false;
        // Set when the server has said all it is going to say. Breaking out of the
        // chunk loop alone would leave the outer reader awaiting a read that only
        // resolves when the socket closes.
        let streamDone = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split(String.fromCharCode(10) + String.fromCharCode(10));
          buffer = parts.pop();

          for (const chunk of parts) {
            const trimmed = chunk.trim();
            if (!trimmed.startsWith('data: ')) continue;
            try {
              const event = JSON.parse(trimmed.slice(6));
              if (event.type === 'progress') {
                const ratio = Math.min(1, Math.max(0, event.percent / 100));
                progressBarFill.style.transform = 'scaleX(' + ratio + ')';
                progressCountLabel.textContent = event.current + ' / ' + event.total;
                if (event.eta_seconds !== undefined || event.interval) {
                  // Server knows the batch size; per-title maths overestimated by ~8x.
                  const left = event.eta_seconds !== undefined
                    ? event.eta_seconds
                    : Math.max(0, event.total - event.current) * event.interval;
                  const mins = Math.floor(left / 60), secs = Math.round(left % 60);
                  progressCountLabel.title = `~${mins > 0 ? mins + 'm ' : ''}${secs}s remaining at AniList's rate limit`;
                  etaLabel.textContent = left > 0 ? `~${mins > 0 ? mins + 'm ' : ''}${secs}s left` : 'finishing...';
                }
                progressPercentLabel.textContent = Math.round(event.percent) + '%';
                progressCurrentTitle.textContent = `[${event.current}/${event.total}] ${event.title}`;
                if (event.stage === 'retry') {
                  // `done` does not advance during retries; say so rather than
                  // letting the bar sit still and look stuck.
                  progressCurrentSub.textContent = 'Retrying titles that did not match on the first pass...';
                } else if (event.resolved_title && event.resolved_title !== event.title) {
                  // Only when AniList landed on a different name. Most titles
                  // match their own spelling, and "-> Chainsaw Man" under
                  // "Chainsaw Man" was the same line twice.
                  progressCurrentSub.textContent = '-> ' + event.resolved_title;
                } else if (event.resolved_title) {
                  progressCurrentSub.textContent = 'Matched';
                } else {
                  progressCurrentSub.textContent = 'Resolving metadata via AniList & MAL...';
                }
                if (Array.isArray(event.entries) && event.entries.length) {
                  const seen = new Set(currentAnimes.map(entryKey));
                  let added = false;
                  event.entries.forEach(entry => {
                    const key = entryKey(entry);
                    if (seen.has(key)) return;   // same dedupe the final list applies
                    seen.add(key);
                    currentAnimes.push({ ...entry, enabled: enabledFor(entry) });
                    added = true;
                  });
                  if (added) {
                    renderTable(currentAnimes);
                    updateStats();   // display only - the run still owns the files
                  }
                }
              } else if (event.type === 'started') {
                activeRunId = event.run_id;
              } else if (event.type === 'paused') {
                isPaused = true;
                btnPause.textContent = 'Resume';
                progressCurrentSub.textContent = 'Paused - nothing is being requested.';
              } else if (event.type === 'resumed') {
                isPaused = false;
                btnPause.textContent = 'Pause';
              } else if (event.type === 'cancelled') {
                const existing = new Map(currentAnimes.map(i => [entryKey(i), i]));
                currentAnimes = (event.entries || []).map(e => {
                  const row = existing.get(entryKey(e));
                  return row || { ...e, enabled: enabledFor(e) };
                });
                renderTable(currentAnimes);
                showToast('Run cancelled - ' + (event.resolved || 0) + ' titles resolved', 'warning');
                try { reader.cancel(); } catch (err) {}
                cancelledRun = true;
                streamDone = true;
                break;
              } else if (event.type === 'network_error') {
                networkFailures = event.count || 0;
                progressCurrentSub.textContent = 'AniList could not be reached for '
                  + networkFailures + ' titles - they are not necessarily missing.';
              } else if (event.type === 'error') {
                showToast(event.error || 'Could not build the export', 'error');
                progressCard.style.display = 'none';
              } else if (event.type === 'rate_limit') {
                progressCurrentSub.textContent = `AniList rate limit reached (30 requests/min) - waiting ${event.seconds}s, this is normal for long lists...`;
              } else if (event.type === 'complete' && event.ok) {
                progressBarFill.style.transform = 'scaleX(1)';
                progressPercentLabel.textContent = '100%';
                // Keep the row objects already on screen so any status/progress edits
                // made while the run was going are not thrown away; take the server's
                // deduped order and membership as authoritative.
                const existing = new Map(currentAnimes.map(i => [entryKey(i), i]));
                currentAnimes = event.entries.map(e => {
                  const row = existing.get(entryKey(e));
                  return row || { ...e, enabled: enabledFor(e) };
                });
                selectAllCheckbox.checked = true;
                selectAllCheckbox.indeterminate = false;
                renderTable(currentAnimes);
                updateStatsAndExport();
                mergedTitles = event.merged || [];
                resolvedFor = destination;
                refreshExportState();
                // Reading a status or a progress number off a pasted line
                // changes what the row holds, so it is said out loud rather
                // than left for the user to notice.
                const fromLines = event.from_lines
                  ? ' - ' + event.from_lines + ' read status, score or progress from the line'
                  : '';
                const noun = mediaType() === 'manga' ? 'manga' : 'anime';
                // The file is built either way, but under AniList the next step
                // is a write, and announcing "XML generated" named the wrong one.
                showToast((destination === 'anilist'
                  ? event.entries.length + ' ' + noun + ' rows resolved. Review them, then write'
                  : 'XML built from ' + event.entries.length + ' ' + noun + ' rows; unresolved rows are left out')
                  + fromLines);
                try { reader.cancel(); } catch(e) {}
                streamDone = true;
                break;
              }
            } catch (errParse) {
              // Ignore partial JSON
            }
          }
          if (streamDone) break;
        }
      } catch (err) {
        showToast('Error communicating with local server', 'error');
      } finally {
        isProcessing = false;
        btnProcess.disabled = false;
        activeRunId = null;
        isPaused = false;
        runControls.classList.remove('show');
        // A cancelled run wrote nothing, so re-sync the files to what is on screen.
        updateStatsAndExport();
        setTimeout(() => {
          progressCard.style.display = 'none';
        }, 1500);
      }
    });

    let exportDebounceTimer = null;

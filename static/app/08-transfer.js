    // One merge for both importers: a MAL export file and an AniList list
    // arrive in the same shape, and a second copy of this would drift.
    function mergeImported(payload) {
      // An import lands rows, so the source folds and the table takes over.
      sourceOpen = false;
      if (payload.media_type !== mediaType()) {
        mediaTypeSelect.value = payload.media_type;
        updateMediaTypeUI();            // keep the rows; only the labels change
      }

      // Imported values win on the fields the source actually carries; the
      // row's AniList identity and candidate list are kept.
      const byId = new Map(currentAnimes.filter(i => Number(i.id) > 0).map(i => [Number(i.id), i]));
      const byAniList = new Map(currentAnimes.filter(i => Number(i.anilist_id) > 0)
        .map(i => [Number(i.anilist_id), i]));
      let matched = 0;
      let added = 0;
      payload.entries.forEach(entry => {
        // An AniList row may have no MAL id at all, so fall back to matching on
        // the AniList id - which a MAL export never carries, so nothing changes
        // for that path.
        const existing = byId.get(Number(entry.id))
          || (Number(entry.anilist_id) > 0 ? byAniList.get(Number(entry.anilist_id)) : null);
        if (existing) {
          const keep = {
            anilist_id: existing.anilist_id || entry.anilist_id,
            candidates: existing.candidates,
            cover: existing.cover || entry.cover,
            year: existing.year || entry.year,
            original: existing.original,
            can_write_anilist: existing.can_write_anilist || entry.can_write_anilist,
          };
          Object.assign(existing, entry, keep);
          existing.enabled = isActionable(existing);
          matched += 1;
        } else if (addUnmatchedImports.checked) {
          currentAnimes.push({ ...entry, original: entry.original || entry.title,
                               enabled: enabledFor(entry) });
          added += 1;
        }
      });
      renderTable(currentAnimes);
      updateStatsAndExport();
      const extra = payload.anilist_only
        ? `, ${payload.anilist_only} AniList-only (no MAL id)` : '';
      showToast(`Imported: ${matched} matched, ${added} added, ${payload.skipped} skipped${extra}`);
    }

    btnCopyAccount.addEventListener('click', () => {
      copyPlan.innerHTML = '';
      btnCopyRun.disabled = true;
      showModal(copyModal);
    });
    btnCopyCancel.addEventListener('click', () => hideModal(copyModal));
    copyModal.addEventListener('click', (e) => {
      if (e.target === copyModal) hideModal(copyModal);
    });
    copySourceUser.addEventListener('input', () => { btnCopyRun.disabled = true; });

    function copyStageLine(stage) {
      const label = stage.media_type === 'manga' ? 'Manga' : 'Anime';
      if (!stage.found) return `<div><strong>${label}</strong>: the source has none.</div>`;
      const bits = [`<strong>${stage.found}</strong> on the source`,
                    `<strong>${stage.creates}</strong> to add`,
                    `<strong>${stage.updates}</strong> to overwrite`];
      if (stage.unchanged) bits.push(`${stage.unchanged} already identical`);
      const skipped = stage.skipped.length
        ? ` ${stage.skipped.length} cannot be written (no AniList id).` : '';
      const extra = stage.extra_on_destination
        ? ` <span class="warn">${stage.extra_on_destination} on the destination are not on the
            source and will be left as they are.</span>` : '';
      return `<div><strong>${label}</strong>: ${bits.join(', ')}.${skipped}${extra}</div>`;
    }

    btnCopyCheck.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      const source = copySourceUser.value.trim();
      if (!token) {
        copyPlan.innerHTML = '<div class="warn">Paste and check the destination token first.</div>';
        return;
      }
      if (!source) {
        copyPlan.innerHTML = '<div class="warn">Name the account to copy from.</div>';
        return;
      }
      btnCopyCheck.disabled = true;
      copyPlan.innerHTML = 'Reading both accounts...';
      try {
        const res = await fetch('/api/anilist-copy-preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source_username: source, token: token, policy: 'overwrite' }),
        });
        const plan = await res.json().catch(() => ({}));
        if (!res.ok || !plan.ok) {
          copyPlan.innerHTML = `<div class="warn">${escapeHtml(plan.error || 'That did not work.')}</div>`;
          return;
        }
        const totalWrites = plan.stages.reduce((n, st) => n + st.total, 0);
        copyPlan.innerHTML =
          `<div>From <strong>${escapeHtml(plan.source)}</strong> to
             <strong>${escapeHtml(plan.destination)}</strong>.</div>`
          + plan.stages.map(copyStageLine).join('')
          + (totalWrites ? '' : '<div class="warn">Nothing to write.</div>');
        btnCopyRun.disabled = totalWrites === 0;
      } catch (e) {
        copyPlan.innerHTML = '<div class="warn">Could not reach the local server.</div>';
      } finally {
        btnCopyCheck.disabled = false;
      }
    });

    // A copy can stop half way - after the anime stage has written and before
    // the manga stage finishes. Whatever landed has to stay undoable.
    function rememberCopySnapshots(snapshots) {
      if (!snapshots.length) return;
      lastWriteSnapshots = snapshots.slice();
      lastWriteSnapshot = snapshots[snapshots.length - 1];
      updateWriteAftermath();
    }

    btnCopyRun.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      const source = copySourceUser.value.trim();
      if (!token || !source) return;
      hideModal(copyModal);
      isProcessing = true;
      btnWrite.disabled = true;
      btnCopyAccount.disabled = true;
      progressCard.style.display = 'flex';
      progressBarFill.style.transform = 'scaleX(0)';
      progressCurrentTitle.textContent = 'Copying ' + source + '...';
      progressCurrentSub.textContent = '';
      const snapshots = [];
      try {
        const res = await fetch('/api/anilist-copy', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_username: source, token: token, confirm: true, policy: 'overwrite',
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          showToast(err.error || 'The copy could not start', 'error');
          return;
        }
        await streamSse(res, (event) => {
          if (event.type === 'started') {
            showRunCancel(event.run_id);
          } else if (event.type === 'stage_started') {
            // Reading a whole list takes several requests and used to show
            // nothing at all until the first row was written.
            progressCurrentTitle.textContent =
              'Reading ' + event.media_type + ' from ' + source + '...';
            progressCurrentSub.textContent = 'nothing has been written yet';
          } else if (event.type === 'stage') {
            if (event.snapshot) snapshots.push(event.snapshot);
            progressCurrentSub.textContent =
              `${event.media_type}: ${event.creates} to add, ${event.updates} to overwrite`;
          } else if (event.type === 'write_progress') {
            progressBarFill.style.transform = 'scaleX(' + Math.min(1, event.percent / 100) + ')';
            progressCountLabel.textContent = event.current + ' / ' + event.total;
            progressPercentLabel.textContent = Math.round(event.percent) + '%';
            progressCurrentTitle.textContent =
              `[${event.media_type} ${event.current}/${event.total}] ${event.title}`;
            if (event.eta_seconds !== undefined) {
              const mins = Math.floor(event.eta_seconds / 60);
              const secs = Math.round(event.eta_seconds % 60);
              etaLabel.textContent = event.eta_seconds > 0
                ? '~' + (mins > 0 ? mins + 'm ' : '') + secs + 's left'
                : 'finishing...';
            }
          } else if (event.type === 'error') {
            rememberCopySnapshots(snapshots);
            showToast(event.error || 'The copy failed', 'error');
            return false;
          } else if (event.type === 'cancelled') {
            rememberCopySnapshots(snapshots);
            showToast('Copy cancelled after ' + event.written + ' entries', 'warning');
            return false;
          } else if (event.type === 'copy_complete') {
            const stages = event.stages || {};
            const added = Object.values(stages).reduce((n, st) => n + st.created, 0);
            const over = Object.values(stages).reduce((n, st) => n + st.updated, 0);
            const bad = Object.values(stages).reduce((n, st) => n + st.failed_count, 0);
            // Two media types means two snapshots, and undo takes one at a time.
            lastWriteSnapshots = event.snapshots || [];
            lastWriteSnapshot = lastWriteSnapshots[lastWriteSnapshots.length - 1] || null;
            lastWriteCreated = added;
            lastWriteFailed = 0;
            updateWriteAftermath();
            showToast(`Copied to ${event.destination}: ${added} added, ${over} overwritten`
              + (bad ? `, ${bad} failed` : ''), bad ? 'warning' : 'success');
            if (bad) {
              unresolvedBar.classList.add('show');
              const lines = [];
              Object.keys(stages).forEach(mt => {
                (stages[mt].failed || []).slice(0, 3).forEach(f => {
                  lines.push(escapeHtml(mt + ': ' + f.title + ' (' + f.error + ')'));
                });
              });
              unresolvedText.innerHTML = `<strong>${bad}</strong> entries failed to copy: `
                + lines.join('; ');
            }
            return false;
          }
        });
      } catch (e) {
        rememberCopySnapshots(snapshots);
        showToast('Lost contact with the local server', 'error');
      } finally {
        isProcessing = false;
        btnWrite.disabled = false;
        btnCopyAccount.disabled = false;
        hideRunCancel();
        setTimeout(() => { progressCard.style.display = 'none'; }, 1500);
      }
    });

    // Reading an AniList list is a source, not a dialog: it is a tab in step 1
    // now (09-shell.js handles the tab itself).

    btnListImportGo.addEventListener('click', async () => {
      const username = listImportUser.value.trim();
      const token = anilistToken.value.trim();
      if (!username && !token) {
        listImportStatus.textContent = 'Type a username, or connect your AniList account to read your own list.';
        listImportStatus.className = 'token-status bad';
        return;
      }
      btnListImportGo.disabled = true;
      listImportStatus.textContent = 'Reading the list from AniList...';
      listImportStatus.className = 'token-status';
      try {
        const res = await fetch('/api/anilist-import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            media_type: mediaType(),
            username: username,
            token: username ? '' : token,
          }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          listImportStatus.textContent = payload.error || 'That list could not be read.';
          listImportStatus.className = 'token-status bad';
          return;
        }
        if (!payload.entries.length) {
          listImportStatus.textContent = 'That list has no ' + mediaType() + ' entries.';
          listImportStatus.className = 'token-status bad';
          return;
        }
        listImportStatus.textContent = '';
        listImportStatus.className = 'token-status';
        mergeImported(payload);
      } catch (e) {
        listImportStatus.textContent = 'Could not reach the local server.';
        listImportStatus.className = 'token-status bad';
      } finally {
        btnListImportGo.disabled = false;
      }
    });

    destinationSelect.addEventListener('change', () => setDestination(destinationSelect.value));
    destinationPrompt.addEventListener('click', (e) => {
      const choice = e.target.closest('[data-destination]');
      if (!choice) return;
      setDestination(choice.dataset.destination);
      // The button just answered disappears with the prompt; without this the
      // keyboard focus fell back to the top of the page.
      if (sourceTab === 'paste') txtArea.focus();
    });

    ['dragenter', 'dragover'].forEach(name => {
      txtArea.addEventListener(name, (e) => {
        e.preventDefault();
        txtArea.style.borderColor = 'var(--border-focus)';
      });
    });
    ['dragleave', 'drop'].forEach(name => {
      txtArea.addEventListener(name, () => { txtArea.style.borderColor = ''; });
    });
    txtArea.addEventListener('drop', async (e) => {
      e.preventDefault();
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!file) return;
      try {
        txtArea.value = await file.text();
        updateLineCounter();
        saveState();
        showToast('Loaded ' + file.name);
      } catch (err) {
        showToast('Could not read that file', 'error');
      }
    });

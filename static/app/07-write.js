    function writableRows() {
      return currentAnimes.filter(i => i.enabled !== false && canWrite(i));
    }

    // What the server said the last preview would change, and the per-row
    // per-field escape hatches the user ticked against it.
    let lastWritePlan = null;
    let writeOverrides = {};
    let lastWriteSnapshot = null;
    // A copy writes anime and manga separately, so it leaves two snapshots and
    // undoing it means replaying both, newest first.
    let lastWriteSnapshots = [];
    let lastWriteRunId = null;
    let lastWriteFailed = 0;
    let lastWriteCreated = 0;

    const AL_STATUS_LABELS = {
      CURRENT: { anime: 'Watching', manga: 'Reading' },
      PLANNING: { anime: 'Plan to watch', manga: 'Plan to read' },
      COMPLETED: { anime: 'Completed', manga: 'Completed' },
      DROPPED: { anime: 'Dropped', manga: 'Dropped' },
      PAUSED: { anime: 'On hold', manga: 'On hold' },
      REPEATING: { anime: 'Rewatching', manga: 'Rereading' },
    };

    function diffFieldLabel(field) {
      const manga = mediaType() === 'manga';
      if (field === 'progress') return manga ? 'chapters' : 'episodes';
      if (field === 'progressVolumes') return 'volumes';
      if (field === 'scoreRaw') return 'score';
      if (field === 'repeat') return manga ? 'rereads' : 'rewatches';
      if (field === 'startedAt') return 'start date';
      if (field === 'completedAt') return 'finish date';
      return field;
    }

    function diffValueText(field, value) {
      if (value === null || value === undefined || value === '') return 'none';
      if (field === 'status') {
        const row = AL_STATUS_LABELS[value];
        return row ? row[mediaType() === 'manga' ? 'manga' : 'anime'] : String(value);
      }
      // The server asks AniList for POINT_100 on both sides of the comparison,
      // so the scale is stated rather than left to look like a 1-10 score.
      if (field === 'scoreRaw') return Number(value) ? Number(value) + '/100' : 'none';
      if (field === 'startedAt' || field === 'completedAt') {
        if (!value.year) return 'none';
        const parts = [String(value.year)];
        if (value.month) parts.push(String(value.month).padStart(2, '0'));
        if (value.month && value.day) parts.push(String(value.day).padStart(2, '0'));
        return parts.join('-');
      }
      if (field === 'notes') {
        const text = String(value);
        return text.length > 60 ? text.slice(0, 60) + '...' : text;
      }
      return String(Number(value) || 0);
    }

    function writeOverrideFor(id, field) {
      const row = writeOverrides[id];
      return row ? row[field] : undefined;
    }

    function setWriteOverride(id, field, value) {
      if (!writeOverrides[id]) writeOverrides[id] = {};
      if (value) writeOverrides[id][field] = value;
      else delete writeOverrides[id][field];
      if (!Object.keys(writeOverrides[id]).length) delete writeOverrides[id];
    }

    function pickBoxHtml(id, field, action, label, on) {
      return '<label class="diff-pick"><input type="checkbox" data-write-override="' + action
        + '" data-id="' + id + '" data-field="' + escapeHtml(field) + '"'
        + (on ? ' checked' : '') + '> ' + escapeHtml(label) + '</label>';
    }

    function diffLineHtml(row, d) {
      const forced = writeOverrideFor(row.anilist_id, d.field) === 'mine';
      // The arrow always shows what the row ASKED for, so a blocked field does
      // not render as "147 -> 147" and read like a no-op. `applied` is what the
      // account will actually hold, which differs when the policy keeps the
      // account's value or when AniList overrides us on a status change.
      // Ticking "keep mine" beats the policy, so the outcome becomes the row's
      // own value - but it does not beat AniList, which overrides us whatever
      // anyone ticks (measured: a forced chapter count on a row that stays
      // completed is still clamped to the total).
      const outcome = (forced && !d.by_anilist) ? d.to : d.applied;
      const outcomeText = diffValueText(d.field, outcome);
      // A field we never send has no asked-for value; show what AniList does.
      const asked = (d.to === null || d.to === undefined) ? outcome : d.to;
      const askedText = diffValueText(d.field, asked);
      const kept = askedText !== outcomeText;
      const bits = [
        '<span class="diff-field">' + escapeHtml(diffFieldLabel(d.field)) + '</span>',
        '<span class="diff-from">' + escapeHtml(diffValueText(d.field, d.from)) + '</span>',
        '<span class="diff-arrow">-&gt;</span>',
        '<span class="diff-to' + (kept ? ' overridden' : '') + '">' + escapeHtml(askedText) + '</span>',
      ];
      if (kept) {
        bits.push('<span class="diff-note warn">' + (d.by_anilist
          ? 'AniList sets ' + escapeHtml(outcomeText) + ' itself'
          : 'stays ' + escapeHtml(outcomeText)) + '</span>');
      } else if (d.by_anilist) {
        bits.push('<span class="diff-note warn">AniList sets this itself</span>');
      } else if (forced) {
        bits.push('<span class="diff-note warn">forced</span>');
      }
      if (d.blocked) {
        bits.push(pickBoxHtml(row.anilist_id, d.field, 'mine', 'keep mine', forced));
      }
      return '<div class="diff-line' + (d.blocked ? ' blocked' : '') + '">' + bits.join('') + '</div>';
    }

    function staleDateLineHtml(row) {
      const clearing = writeOverrideFor(row.anilist_id, 'completedAt') === 'clear';
      return '<div class="diff-line blocked">'
        + '<span class="diff-field">' + escapeHtml(diffFieldLabel('completedAt')) + '</span>'
        + '<span class="diff-note warn">' + (clearing
            ? 'will be cleared, so the entry is no longer both in progress and finished'
            : 'this row stops being completed but keeps its finish date on AniList')
        + '</span>'
        + pickBoxHtml(row.anilist_id, 'completedAt', 'clear', 'clear the finish date', clearing)
        + '</div>';
    }

    function writeRowHtml(row) {
      const lines = (row.diff || []).map(d => diffLineHtml(row, d));
      if (row.stale_completed_at) lines.push(staleDateLineHtml(row));
      return '<div class="diff-row"><div class="diff-head">' + escapeHtml(row.title)
        + '<span class="diff-tag">' + (row.update ? 'update' : 'new') + '</span></div>'
        + lines.join('') + '</div>';
    }

    function renderWritePlan(plan) {
      const changing = (plan.rows || []).filter(r => (r.diff || []).length || r.stale_completed_at);
      const open = writePlan.querySelector('.diff-list');
      const keepTop = open ? open.scrollTop : 0;
      const skippedNote = plan.skipped.length
        ? `<div>${plan.skipped.length} row${plan.skipped.length === 1 ? '' : 's'} will be skipped: ${escapeHtml(plan.skipped.slice(0, 3).map(sk => sk.title).join(', '))}${plan.skipped.length > 3 ? '...' : ''}</div>`
        : '';
      const unchangedNote = plan.unchanged
        ? `<div>${plan.unchanged} already match your account and will not be touched.</div>`
        : '';
      const conflictNote = plan.conflicts
        ? `<div class="warn">${plan.conflicts} row${plan.conflicts === 1 ? '' : 's'} would send a value backwards. Those fields keep what AniList has unless you tick "keep mine".</div>`
        : '';
      const list = changing.length
        ? '<div class="diff-list">' + changing.map(writeRowHtml).join('') + '</div>'
        : '<div>Nothing on your account changes.</div>';
      writePlan.innerHTML = `
        <div>Account: <strong>${escapeHtml(plan.viewer)}</strong></div>
        <div><strong>${plan.creates}</strong> ${plan.creates === 1 ? 'entry' : 'entries'} will be added,
             <strong>${plan.updates}</strong> already on your list will be updated.</div>
        ${skippedNote}${unchangedNote}${conflictNote}
        ${list}
        <div class="warn">This modifies your AniList account. Entries already on your list are
          updated in place, never duplicated, and the state before this write is saved so it
          can be undone.</div>`;
      const again = writePlan.querySelector('.diff-list');
      if (again) again.scrollTop = keepTop;
    }

    writePlan.addEventListener('change', (e) => {
      const box = e.target && e.target.closest ? e.target.closest('input[data-write-override]') : null;
      if (!box) return;
      setWriteOverride(box.dataset.id, box.dataset.field,
                       box.checked ? box.dataset.writeOverride : null);
      if (lastWritePlan) renderWritePlan(lastWritePlan);
    });

    function updateWriteAftermath() {
      const live = destination === 'anilist';
      btnUndo.style.display = (live && lastWriteSnapshot) ? '' : 'none';
      btnRetry.style.display = (live && lastWriteFailed > 0 && lastWriteRunId) ? '' : 'none';
      btnRetry.textContent = 'Retry ' + lastWriteFailed + ' failed';
    }

    // One reader for both streams. Returning false from the handler stops it.
    async function streamSse(res, onEvent) {
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let done = false;
      while (!done) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const parts = buffer.split(String.fromCharCode(10) + String.fromCharCode(10));
        buffer = parts.pop();
        for (const part of parts) {
          const trimmed = part.trim();
          if (!trimmed.startsWith('data: ')) continue;
          let event;
          try {
            event = JSON.parse(trimmed.slice(6));
          } catch (errParse) {
            continue;
          }
          if (onEvent(event) === false) { done = true; break; }
        }
      }
      try { reader.cancel(); } catch (e) {}
    }

    // The browser decides where a download goes, which is the same reason
    // there is no "export to this path" field: the server would have to accept
    // a filesystem path from a web page.
    function offerDownload(name, text, mime) {
      const blob = new Blob([text], { type: mime + ';charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    }

    function stampedName(stem, extension) {
      const now = new Date();
      const pad = n => String(n).padStart(2, '0');
      return stem + '_' + now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate())
        + '_' + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds())
        + '.' + extension;
    }

    function stripForServer(item) {
      const { candidates, ...rest } = item;
      return rest;
    }

    btnTokenTest.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      if (!token) {
        tokenStatus.textContent = 'Paste a token first.';
        tokenStatus.className = 'token-status bad';
        return;
      }
      tokenStatus.textContent = 'Checking...';
      tokenStatus.className = 'token-status';
      try {
        const res = await fetch('/api/anilist-test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: token }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          anilistUser = null;
          tokenStatus.textContent = payload.error || 'That token did not work.';
          tokenStatus.className = 'token-status bad';
          return;
        }
        anilistUser = { id: payload.id, name: payload.name };
        tokenStatus.textContent = 'Connected as ' + payload.name + '.';
        tokenStatus.className = 'token-status ok';
      } catch (e) {
        tokenStatus.textContent = 'Could not reach the local server.';
        tokenStatus.className = 'token-status bad';
      }
    });

    btnWrite.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      const rows = writableRows();
      if (!token) {
        showToast('Paste and check an AniList token first', 'warning');
        return;
      }
      if (!rows.length) {
        showToast('No rows with an AniList id are selected', 'warning');
        return;
      }
      writePlan.innerHTML = 'Checking your AniList list...';
      lastWritePlan = null;
      writeOverrides = {};
      showModal(writeModal);
      try {
        const res = await fetch('/api/anilist-preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token: token,
            media_type: mediaType(),
            status: selectStatus.value,
            score: parseInt(inputScore.value, 10) || 0,
            entries: rows.map(stripForServer),
          }),
        });
        const plan = await res.json().catch(() => ({}));
        if (!res.ok || !plan.ok) {
          writePlan.innerHTML = escapeHtml(plan.error || 'Could not read your AniList list.');
          return;
        }
        lastWritePlan = plan;
        renderWritePlan(plan);
      } catch (e) {
        writePlan.innerHTML = 'Could not reach the local server.';
      }
    });

    // R25: the diff is the most carefully built thing in this tool and it
    // existed only for as long as the dialog was open. Before sending 300 rows
    // to an account, being able to keep the plan is worth a button.
    btnWritePlanSave.addEventListener('click', () => {
      if (!lastWritePlan) {
        showToast('Nothing to save yet', 'warning');
        return;
      }
      const plan = {
        saved_at: new Date().toISOString(),
        media_type: mediaType(),
        account: lastWritePlan.viewer || '',
        totals: {
          rows: lastWritePlan.total, creates: lastWritePlan.creates,
          updates: lastWritePlan.updates, unchanged: lastWritePlan.unchanged,
          conflicts: lastWritePlan.conflicts, skipped: lastWritePlan.skipped,
        },
        // The overrides are part of the plan: without them the diff does not
        // describe what would actually have been sent.
        overrides: writeOverrides,
        rows: lastWritePlan.rows,
      };
      offerDownload(stampedName('aniexport_write_plan', 'json'),
                    JSON.stringify(plan, null, 2), 'application/json');
      showToast('Write plan saved');
    });

    btnWriteCancel.addEventListener('click', () => hideModal(writeModal));
    writeModal.addEventListener('click', (e) => {
      if (e.target === writeModal) hideModal(writeModal);
    });

    btnWriteConfirm.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      const rows = writableRows();
      if (!token || !rows.length) return;
      hideModal(writeModal);
      await runWrite({
        token: token,
        confirm: true,
        media_type: mediaType(),
        status: selectStatus.value,
        score: parseInt(inputScore.value, 10) || 0,
        overrides: writeOverrides,
        entries: rows.map(stripForServer),
      }, 'Writing to AniList...');
    });

    btnRetry.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      if (!token) {
        showToast('Paste and check an AniList token first', 'warning');
        return;
      }
      if (!lastWriteRunId || !lastWriteFailed) return;
      await runWrite({
        token: token,
        confirm: true,
        retry: true,
        run_id: lastWriteRunId,
        media_type: mediaType(),
      }, 'Retrying the rows that failed...');
    });

    async function runWrite(body, label) {
      isProcessing = true;
      btnWrite.disabled = true;
      btnUndo.disabled = true;
      btnRetry.disabled = true;
      progressCard.style.display = 'flex';
      progressBarFill.style.transform = 'scaleX(0)';
      progressCurrentTitle.textContent = label;
      progressCurrentSub.textContent = '';
      try {
        const res = await fetch('/api/anilist-write', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          showToast(err.error || 'The write could not start', 'error');
          return;
        }
        await streamSse(res, (event) => {
          if (event.type === 'started') {
            showRunCancel(event.run_id);
          } else if (event.type === 'write_progress') {
            progressBarFill.style.transform = 'scaleX(' + Math.min(1, event.percent / 100) + ')';
            progressCountLabel.textContent = event.current + ' / ' + event.total;
            progressPercentLabel.textContent = Math.round(event.percent) + '%';
            progressCurrentTitle.textContent = `[${event.current}/${event.total}] ${event.title}`;
            progressCurrentSub.textContent = event.ok ? 'Saved' : 'Failed';
            if (event.eta_seconds !== undefined) {
              const mins = Math.floor(event.eta_seconds / 60);
              const secs = Math.round(event.eta_seconds % 60);
              etaLabel.textContent = event.eta_seconds > 0
                ? '~' + (mins > 0 ? mins + 'm ' : '') + secs + 's left'
                : 'finishing...';
            }
          } else if (event.type === 'plan') {
            if (event.snapshot) lastWriteSnapshot = event.snapshot;
            progressCurrentSub.textContent = `${event.creates} to add, ${event.updates} to update`;
          } else if (event.type === 'error') {
            // The existing-entries read failed, so nothing was sent: writing
            // blind would duplicate entries the account already has.
            showToast(event.error || 'AniList could not be read', 'error');
            return false;
          } else if (event.type === 'write_complete') {
            const failedCount = event.failed_count !== undefined
              ? event.failed_count : (event.failed || []).length;
            lastWriteRunId = event.run_id || null;
            lastWriteFailed = failedCount;
            lastWriteCreated = event.created || 0;
            if (event.snapshot) lastWriteSnapshot = event.snapshot;
            lastWriteSnapshots = event.snapshot ? [event.snapshot] : [];
            updateWriteAftermath();
            showToast(`AniList updated: ${event.created} added, ${event.updated} updated`
              + (failedCount ? `, ${failedCount} failed` : ''), failedCount ? 'warning' : 'success');
            if (failedCount) {
              unresolvedBar.classList.add('show');
              unresolvedText.innerHTML = `<strong>${failedCount}</strong> AniList ${failedCount === 1 ? 'write' : 'writes'} failed: `
                + event.failed.slice(0, 3).map(f => escapeHtml(f.title + ' (' + f.error + ')')).join('; ');
            }
            return false;
          } else if (event.type === 'cancelled') {
            if (event.snapshot) lastWriteSnapshot = event.snapshot;
            updateWriteAftermath();
            // Rows go out eight to a request, so a cancel lands between
            // batches: the rest of the batch in flight is already written.
            showToast('Write cancelled after ' + event.written
              + ' entries (the batch in progress still finished)', 'warning');
            return false;
          }
        });
      } catch (e) {
        showToast('Lost contact with the local server', 'error');
      } finally {
        isProcessing = false;
        btnWrite.disabled = false;
        btnUndo.disabled = false;
        btnRetry.disabled = false;
        hideRunCancel();
        setTimeout(() => { progressCard.style.display = 'none'; }, 1500);
      }
    }

    btnUndoCancel.addEventListener('click', () => hideModal(undoModal));
    undoModal.addEventListener('click', (e) => {
      if (e.target === undoModal) hideModal(undoModal);
    });

    // R26: the server keeps five snapshots and serves all of them; the page
    // used to read [0] and leave the other four unreachable. A copy writes two
    // - one per media type - so a flat list of timestamps shows one operation
    // as two unrelated rows, and the media type has to be on the label.
    let knownSnapshots = [];

    function snapshotLabel(entry) {
      const when = entry.written_at
        ? new Date(entry.written_at * 1000).toLocaleString()
        : entry.name;
      const kind = entry.media_type === 'manga' ? 'manga' : 'anime';
      const created = entry.creates ? ', ' + entry.creates + ' created' : '';
      return when + ' - ' + kind + ', ' + entry.rows + ' rows' + created;
    }

    async function refreshSnapshots() {
      try {
        const res = await fetch('/api/anilist-snapshots');
        const payload = await res.json().catch(() => ({}));
        knownSnapshots = payload.snapshots || [];
      } catch (e) {
        knownSnapshots = [];
      }
      return knownSnapshots;
    }

    function renderSnapshotPicker() {
      const many = knownSnapshots.length > 1;
      undoSnapshot.style.display = many ? '' : 'none';
      undoSnapshotLabel.style.display = many ? '' : 'none';
      if (!many) return;
      undoSnapshot.innerHTML = knownSnapshots.map(entry =>
        `<option value="${escapeHtml(entry.name)}">${escapeHtml(snapshotLabel(entry))}</option>`
      ).join('');
      const chosen = knownSnapshots.find(e => e.name === lastWriteSnapshot);
      undoSnapshot.value = (chosen || knownSnapshots[0]).name;
    }

    function chosenSnapshot() {
      return (undoSnapshot.style.display !== 'none' && undoSnapshot.value)
        ? undoSnapshot.value : lastWriteSnapshot;
    }

    undoSnapshot.addEventListener('change', () => {
      const entry = knownSnapshots.find(e => e.name === undoSnapshot.value);
      lastWriteCreated = entry ? (entry.deletable || 0) : 0;
      describeUndo();
    });

    btnUndo.addEventListener('click', async () => {
      if (!lastWriteSnapshot) return;
      await refreshSnapshots();
      renderSnapshotPicker();
      const entry = knownSnapshots.find(e => e.name === chosenSnapshot());
      if (entry) lastWriteCreated = entry.deletable || 0;
      describeUndo();
      showModal(undoModal);
    });

    function describeUndo() {
      const deleteBox = lastWriteCreated
        ? `<div class="warn"><label class="diff-pick"><input type="checkbox" id="undoDeleteCreated">
             also delete the ${lastWriteCreated} ${lastWriteCreated === 1 ? 'entry' : 'entries'} this
             write created</label> Deleting removes them from your account for good; leaving this
             unticked keeps them exactly as the write left them.</div>`
        : '';
      undoPlan.innerHTML = `
        <div>Putting your account back the way it was before that write.</div>
        <div>Rows that already existed are restored field by field, including notes and dates.</div>
        ${deleteBox}
        <div class="warn">AniList stamps a start date itself when an entry becomes in-progress
          without one, so a row that had no start date before may keep the one AniList added.</div>`;
    }

    btnUndoConfirm.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      if (!token) {
        showToast('Paste and check an AniList token first', 'warning');
        return;
      }
      if (!lastWriteSnapshot) return;
      // A copy writes anime and manga separately and leaves a snapshot for
      // each, so undoing it means replaying every one - newest first, so the
      // account walks back the way it came. Picking an older snapshot by hand
      // undoes exactly that one: the pair only belongs together when it is the
      // pair this session just wrote.
      const picked = chosenSnapshot();
      const pending = (picked === lastWriteSnapshot && lastWriteSnapshots.length
                         ? lastWriteSnapshots.slice()
                         : [picked]).reverse();
      const deleteBox = document.getElementById('undoDeleteCreated');
      hideModal(undoModal);
      isProcessing = true;
      btnWrite.disabled = true;
      btnUndo.disabled = true;
      progressCard.style.display = 'flex';
      progressBarFill.style.transform = 'scaleX(0)';
      progressCurrentTitle.textContent = 'Undoing the last write...';
      progressCurrentSub.textContent = '';
      let restoredAll = 0;
      let deletedAll = 0;
      let failedAll = 0;
      let unrecoverableAll = 0;
      let stopped = false;
      try {
        for (const snapshotName of pending) {
        const res = await fetch('/api/anilist-undo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token: token,
            confirm: true,
            snapshot: snapshotName,
            delete_created: !!(deleteBox && deleteBox.checked),
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          // A snapshot whose only rows were creates has nothing to undo while
          // the delete box is unticked, and answers 400. That is not a failure
          // of the run as a whole, so the next snapshot still gets its turn.
          if (res.status !== 400) {
            showToast(err.error || 'The undo could not start', 'error');
            stopped = true;
            break;
          }
          continue;
        }
        await streamSse(res, (event) => {
          if (event.type === 'started') {
            showRunCancel(event.run_id);
          } else if (event.type === 'write_progress') {
            progressBarFill.style.transform = 'scaleX(' + Math.min(1, event.percent / 100) + ')';
            progressCountLabel.textContent = event.current + ' / ' + event.total;
            progressPercentLabel.textContent = Math.round(event.percent) + '%';
            progressCurrentTitle.textContent = `[${event.current}/${event.total}] ${event.title}`;
            progressCurrentSub.textContent = event.ok ? 'Restored' : 'Failed';
          } else if (event.type === 'plan') {
            progressCurrentSub.textContent = `${event.restores} to restore, ${event.deletes} to delete`;
          } else if (event.type === 'undo_complete') {
            restoredAll += event.restored;
            deletedAll += event.deleted;
            failedAll += event.failed_count;
            unrecoverableAll += (event.unrecoverable || []).length;
            if (event.failed_count || (event.unrecoverable || []).length) {
              unresolvedBar.classList.add('show');
              unresolvedText.innerHTML = '<strong>Undo was partial.</strong> '
                + (event.failed || []).slice(0, 3)
                    .map(f => escapeHtml(f.title + ' (' + f.error + ')')).join('; ')
                + (event.unrecoverable || []).slice(0, 3)
                    .map(u => escapeHtml(' ' + u.title + ' (' + u.reason + ')')).join('; ');
            }
            return false;
          } else if (event.type === 'cancelled') {
            showToast('Undo cancelled after ' + event.written + ' rows', 'warning');
            stopped = true;
            return false;
          }
        });
        if (stopped) break;
        }
        if (!stopped) {
          const notes = [];
          if (failedAll) notes.push(failedAll + ' failed');
          if (unrecoverableAll) notes.push(unrecoverableAll + ' could not be undone');
          showToast(`Undo done: ${restoredAll} restored, ${deletedAll} deleted`
            + (notes.length ? ' (' + notes.join(', ') + ')' : ''),
            notes.length ? 'warning' : 'success');
        }
      } catch (e) {
        showToast('Lost contact with the local server', 'error');
      } finally {
        isProcessing = false;
        btnWrite.disabled = false;
        btnUndo.disabled = false;
        hideRunCancel();
        setTimeout(() => { progressCard.style.display = 'none'; }, 1500);
      }
    });

    btnImport.addEventListener('click', () => importFile.click());

    importFile.addEventListener('change', async () => {
      const file = importFile.files && importFile.files[0];
      importFile.value = '';            // let the same file be picked twice
      if (!file) return;
      let text;
      try {
        if (file.name.toLowerCase().endsWith('.gz')) {
          if (typeof DecompressionStream === 'undefined') {
            showToast('This browser cannot unpack .gz - import the .xml instead', 'warning');
            return;
          }
          const stream = file.stream().pipeThrough(new DecompressionStream('gzip'));
          text = await new Response(stream).text();
        } else {
          text = await file.text();
        }
      } catch (err) {
        showToast('Could not read that file', 'error');
        return;
      }

      let payload;
      try {
        const res = await fetch('/api/parse-mal-xml', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ xml: text }),
        });
        payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          showToast(payload.error || 'That file could not be imported', 'error');
          return;
        }
      } catch (err) {
        showToast('Could not reach the local server', 'error');
        return;
      }

      mergeImported(payload);
    });

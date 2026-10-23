    function sortValue(item) {
      if (sortKey === 'id') return Number(item.id) || 0;
      if (sortKey === 'title') return String(item.title || '').toLowerCase();
      if (sortKey === 'type') return String(item.type || '').toLowerCase();
      if (sortKey === 'status') return String(item.status || '').toLowerCase();
      if (sortKey === 'count') {
        return mediaType() === 'manga' ? (Number(item.chapters) || 0) : (Number(item.episodes) || 0);
      }
      return 0;
    }

    function applySort(list) {
      if (!sortKey || sortDir === 0) return list;
      // decorate-sort-undecorate keeps it stable, so equal rows hold their order
      return list
        .map((item, i) => ({ item, i }))
        .sort((a, b) => {
          const av = sortValue(a.item);
          const bv = sortValue(b.item);
          if (av < bv) return -1 * sortDir;
          if (av > bv) return 1 * sortDir;
          return a.i - b.i;
        })
        .map(pair => pair.item);
    }

    const CHIPS = [
      { id: '', label: 'All' },
      { id: 'reading', label: 'Reading' },
      { id: 'completed', label: 'Completed' },
      { id: 'planned', label: 'Planned' },
      { id: 'paused', label: 'Paused' },
      { id: 'dropped', label: 'Dropped' },
      { id: 'anilistonly', label: 'AniList only' },
      { id: 'unresolved', label: 'Not found' },
      { id: 'review', label: 'Needs review' },
    ];

    function chipLabel(chip) {
      // The list is one media type at a time; naming both is noise.
      if (chip.id === 'reading') return mediaType() === 'manga' ? 'Reading' : 'Watching';
      return chip.label;
    }

    // The server flags a row with a short token; the sentence lives here,
    // with the rest of the interface copy.
    const REVIEW_REASONS = {
      'one-shot': 'The best match AniList offered is a one-shot, which usually means the real series was not in the results',
      'not yet released': 'The best match AniList offered has not been released yet',
      'only match, one chapter': 'AniList returned one entry and it is a single chapter, which is usually a fragment rather than the series',
    };

    // Why a row was flagged, in the row itself: the chip is a filter, not an
    // explanation, and "needs review" without a reason is just nagging.
    // An exact alias is the one piece of positive evidence worth showing: it
    // explains why a row whose title looks nothing like what was pasted is
    // nevertheless right, which is otherwise the most alarming thing on screen.
    function aliasMarkHtml(item) {
      if (!item.alias_hit) return '';
      return '<span class="alias-mark" title="AniList lists the title you pasted'
        + ' as another name for this entry">known alias</span>';
    }

    function reviewMarkHtml(item) {
      if (!item.needs_review) return '';
      const gap = item.runner_up_gap;
      // A named reason wins: it describes the entry itself, which is more
      // concrete than any statement about how the title scored. The
      // parentheses matter - || binds tighter than ?:, so without them a
      // flagged row would always take the inversion branch.
      const why = REVIEW_REASONS[item.review_reason]
        || ((gap !== undefined && gap !== null && gap < 0)
          ? 'A candidate further down the list matched your title better than this one did'
          : 'This title barely resembles the one you pasted'
            + (item.match_ratio !== undefined && item.match_ratio !== null
               ? ' (' + Math.round(item.match_ratio * 100) + '% alike)' : ''));
      return '<span class="needs-review-mark" title="' + escapeHtml(why) + '">needs review</span>';
    }

    function chipBucket(item) {
      if (!canXml(item) && !canWrite(item)) return 'unresolved';
      if (!canXml(item)) return 'anilistonly';
      const status = String(item.status || '').toLowerCase();
      if (status === 'reading' || status === 'watching' || status === 'rereading' || status === 'rewatching') return 'reading';
      if (status === 'completed') return 'completed';
      if (status === 'plan to read' || status === 'plan to watch') return 'planned';
      if (status === 'paused' || status === 'on-hold') return 'paused';
      if (status === 'dropped') return 'dropped';
      return 'other';
    }

    function renderChips() {
      const counts = {};
      currentAnimes.forEach(item => {
        const bucket = chipBucket(item);
        counts[bucket] = (counts[bucket] || 0) + 1;
        // "Needs review" is a flag rather than a bucket, so it is counted
        // separately or the chip would never appear.
        if (item.needs_review) counts.review = (counts.review || 0) + 1;
      });
      chipRow.innerHTML = CHIPS.filter(chip => chip.id === '' || counts[chip.id])
        .map(chip => {
          const n = chip.id === '' ? currentAnimes.length : (counts[chip.id] || 0);
          return `<button class="chip ${statusFilter === chip.id ? 'active' : ''}" data-chip="${chip.id}">${escapeHtml(chipLabel(chip))} ${n}</button>`;
        }).join('');
    }

    function matchesFilters(item) {
      const q = searchInput.value.toLowerCase().trim();
      // "Needs review" cuts across the status buckets - a row can be both
      // Completed and doubtful - so it filters on the flag, not the bucket.
      if (statusFilter === 'review') {
        if (!item.needs_review) return false;
      } else if (statusFilter && chipBucket(item) !== statusFilter) return false;
      if (!q) return true;
      const title = (item.title || '').toLowerCase();
      const orig = (item.original || '').toLowerCase();
      return title.includes(q) || orig.includes(q)
        || String(item.id || '').includes(q) || String(item.anilist_id || '').includes(q);
    }

    function entryKey(entry) {
      const id = Number(entry.id) || 0;
      return id > 0 ? 'id:' + id : 'title:' + String(entry.title || '').trim().toLowerCase();
    }

    // Titles the user has chosen to skip for good, per media type. Unchecking a
    // box stays per-session as it always has; this list only changes when the
    // button is pressed, so the checkbox keeps its original meaning.
    const IGNORE_KEY = 'aniexport.ignored';

    function readIgnoreStore() {
      try {
        return JSON.parse(window.localStorage.getItem(IGNORE_KEY) || '{}') || {};
      } catch (e) {
        return {};
      }
    }

    function loadIgnored() {
      const all = readIgnoreStore();
      return new Set(Array.isArray(all[mediaType()]) ? all[mediaType()] : []);
    }

    function saveIgnored() {
      const all = readIgnoreStore();
      all[mediaType()] = Array.from(ignoredKeys);
      try {
        window.localStorage.setItem(IGNORE_KEY, JSON.stringify(all));
      } catch (e) {
        showToast('Could not save the skip list - browser storage is full or blocked.', 'error');
      }
    }

    function enabledFor(entry) {
      return isActionable(entry) && !ignoredKeys.has(entryKey(entry));
    }

    function updateIgnoredLabel() {
      const n = ignoredKeys.size;
      btnForgetIgnored.textContent = 'Forget ' + n + ' skipped';
      btnForgetIgnored.style.display = n ? '' : 'none';
    }

    function fixRowHtml(item, idx) {
      if (isActionable(item)) return '';
      return `<div class="fix-row">
        <input class="fix-input" data-index="${idx}" placeholder="Search title or MAL ID..."
          aria-label="Find a match for ${escapeHtml(item.original || item.title)}">
        <button class="btn btn-secondary btn-fix" data-index="${idx}">Find</button>
      </div>`;
    }

    function detailRowHtml(item, idx) {
      if (!expandedRows.has(idx)) return '';
      const val = key => (item[key] === undefined || item[key] === null ? '' : String(item[key]));
      const dateVal = key => {
        const raw = val(key);
        return raw === '0000-00-00' ? '' : raw;
      };
      return `<tr class="detail-row" data-detail-for="${idx}"><td colspan="6">
        <div class="detail-head">Extra fields for <strong>${escapeHtml(item.title)}</strong> - blank means unset</div>
        <div class="detail-grid">
          <label>Score (0-10)
            <input type="number" min="0" max="10" class="detail-input" data-index="${idx}" data-field="score" value="${escapeHtml(val('score'))}">
          </label>
          <label>Start date
            <input type="date" class="detail-input" data-index="${idx}" data-field="start_date" value="${escapeHtml(dateVal('start_date'))}">
          </label>
          <label>Finish date
            <input type="date" class="detail-input" data-index="${idx}" data-field="finish_date" value="${escapeHtml(dateVal('finish_date'))}">
          </label>
          <label>Tags
            <input type="text" class="detail-input" data-index="${idx}" data-field="tags" value="${escapeHtml(val('tags'))}">
          </label>
          <label class="detail-wide">Comments
            <input type="text" class="detail-input" data-index="${idx}" data-field="comments" value="${escapeHtml(val('comments'))}">
          </label>
        </div>
      </td></tr>`;
    }

    function statusPillClass(status) {
      return STATUS_PILL[String(status || '').toLowerCase()] || 'status-completed';
    }

    function statusCellHtml(item, idx) {
      // Per-row status. The list-wide dropdown still stamps every row as before;
      // this only adds the ability to differ, which a real mixed list needs.
      const current = item.status || 'Completed';
      const set = STATUS_SETS[mediaType()];
      const known = set.indexOf(current) >= 0;
      const options = set.map(v =>
        `<option value="${escapeHtml(v)}" ${v === current ? 'selected' : ''}>${escapeHtml(v)}</option>`).join('');
      const orphan = known ? '' : `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)}</option>`;
      return `<select class="row-status status-pill ${statusPillClass(current)}" data-index="${idx}"
        ${isActionable(item) ? '' : 'disabled'}
        aria-label="Status for ${escapeHtml(item.title)}">${orphan}${options}</select>`;
    }

    function progressHtml(item, idx) {
      const exportable = isActionable(item);
      // One column, not two: an editable count against its total reads as progress.
      // Separate "total" and "read" columns forced the eye to join them every row.
      return '<div class="progress-cell">' + progressFields().map(([field, unit]) => {
        const value = item[field] === undefined || item[field] === null ? '' : item[field];
        const total = Number(item[PROGRESS_TOTAL_FIELD[field]] || 0);
        return `<label class="progress-line">
          <input type="number" class="progress-input" min="0" ${total > 0 ? `max="${total}"` : ''}
            data-index="${idx}" data-field="${field}" value="${value}"
            placeholder="${derivedProgress(item, field)}" ${exportable ? '' : 'disabled'}
            aria-label="${escapeHtml(unit)} read for ${escapeHtml(item.title)}">
          <span class="progress-total">/ ${total > 0 ? total : '?'} ${escapeHtml(unit)}</span>
        </label>`;
      }).join('') + '</div>';
    }

    // Render Table
    function renderTable(items) {
      renderChips();
      if (typeof updateBulkLabels === 'function') updateBulkLabels();
      const matched = applySort(items.filter(matchesFilters));
      const capped = showAllRows ? matched : matched.slice(0, ROW_CAP);

      // Every control in a row is addressed by its index into currentAnimes.
      // Finding that with indexOf once per row made drawing the table O(n^2) -
      // fine at the 300-row cap, and the reason "Show all" on a few thousand
      // rows crawled. Built once, read in constant time.
      const rowIndex = new Map();
      currentAnimes.forEach((item, i) => rowIndex.set(item, i));

      updateFilterCount();

      if (matched.length > capped.length) {
        rowCapNotice.style.display = 'block';
        rowCapNotice.innerHTML = `Showing the first ${capped.length} of ${matched.length} matching rows. <button id="btnShowAll" class="btn btn-secondary btn-xs">Show all</button>`;
      } else {
        rowCapNotice.style.display = 'none';
        rowCapNotice.innerHTML = '';
      }

      if (capped.length === 0) {
        tableBody.innerHTML = '';
        emptyState.hidden = false;
        // Three different empties: nothing yet, nothing yet because a run has
        // only just started, and rows that the filter is hiding.
        emptyState.dataset.mode = currentAnimes.length ? 'filter' : (isProcessing ? 'running' : 'none');
        updateSelectAllState();
        return;
      }

      emptyState.hidden = true;
      tableBody.innerHTML = capped.map(item => {
        const globalIdx = rowIndex.get(item);
        const malUrl = item.id ? `https://myanimelist.net/${mediaType()}/${item.id}` : '';
        const alUrl = item.anilist_id ? `https://anilist.co/${mediaType()}/${item.anilist_id}` : '';
        // Prefer what the user typed over our tidied version of it, so a
        // clean-up that went wrong is visible rather than hidden behind it.
        const pastedText = item.pasted || item.original;
        const hasRenamed = pastedText && pastedText.toLowerCase() !== item.title.toLowerCase();
        const isChecked = item.enabled !== false && isActionable(item);

        return `
          <tr class="${isChecked ? '' : 'row-excluded'}">
            <td class="cell-checkbox" style="text-align: center; cursor: pointer;">
              <input type="checkbox" class="table-checkbox row-checkbox" data-index="${globalIdx}" ${isChecked ? 'checked' : ''} ${isActionable(item) ? '' : 'disabled'} aria-label="Include ${escapeHtml(item.title)}">
            </td>
            <td>
              <div class="title-row">
                ${item.cover
                  ? `<img class="cover-thumb" src="${escapeHtml(item.cover)}" alt="" loading="lazy">`
                  : '<span class="cover-thumb cover-empty" aria-hidden="true"></span>'}
                <div class="title-stack">
                  <div class="cell-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
                  <div class="title-meta">
                    ${malUrl ? `<a href="${malUrl}" class="id-link" target="_blank" rel="noopener noreferrer" title="View on MyAnimeList">MAL ${item.id}</a>` : `<span class="id-missing">${alUrl ? 'no MAL ID' : 'not found on AniList'}</span>`}
                    ${alUrl ? `<a href="${alUrl}" class="id-link" target="_blank" rel="noopener noreferrer" title="View on AniList">AL ${item.anilist_id}</a>` : ''}
                    ${hasRenamed ? `<span class="cell-original" title="${escapeHtml(pastedText)}">from ${escapeHtml(pastedText)}</span>` : ''}
                    ${reviewMarkHtml(item)}${aliasMarkHtml(item)}
                  </div>
                  ${pickerHtml(item, globalIdx)}
                  ${fixRowHtml(item, globalIdx)}
                </div>
              </div>
            </td>
            <td class="cell-type">
              <span class="${rowBadge(item).cls}">${escapeHtml(rowBadge(item).text)}</span>
              ${item.year ? `<span class="tag-year">${item.year}</span>` : ''}
            </td>
            <td class="cell-count">${progressHtml(item, globalIdx)}</td>
            <td class="cell-status">${statusCellHtml(item, globalIdx)}</td>
            <td class="cell-more" style="text-align:center;">
              <button class="btn-detail" data-detail="${globalIdx}"
                aria-label="More fields for ${escapeHtml(item.title)}"
                title="Score, dates, tags, comments">${expandedRows.has(globalIdx) ? '&#9662;' : '&#8943;'}</button>
            </td>
          </tr>
          ${detailRowHtml(item, globalIdx)}
        `;
      }).join('');

      updateSelectAllState();
    }

    selectAllCheckbox.addEventListener('change', () => {
      const isChecked = selectAllCheckbox.checked;
      currentAnimes.forEach(item => {
        if (!isActionable(item)) { item.enabled = false; return; }
        if (matchesFilters(item)) item.enabled = isChecked;
      });
      renderTable(currentAnimes);
      updateStatsAndExport();
    });

    // --- 6.4: shift-click a range, and drive the list from the keyboard ---
    let lastClickedRow = -1;
    let focusedRow = -1;

    function visibleIndexes() {
      // What is actually on screen. renderTable caps at ROW_CAP, so the raw
      // filter result would let arrow navigation walk off into rows that were
      // never rendered - silently, with no focus ring and nothing to toggle.
      const matched = currentAnimes.map((item, i) => matchesFilters(item) ? i : -1)
        .filter(i => i >= 0);
      return showAllRows ? matched : matched.slice(0, ROW_CAP);
    }

    function setRowFocus(index) {
      focusedRow = index;
      document.querySelectorAll('tr.row-focus').forEach(tr => tr.classList.remove('row-focus'));
      const box = tableBody.querySelector('.row-checkbox[data-index="' + index + '"]');
      const tr = box && box.closest('tr');
      if (tr) {
        tr.classList.add('row-focus');
        tr.scrollIntoView({ block: 'nearest' });
      }
    }

    tableBody.addEventListener('click', (e) => {
      const box = e.target.closest && e.target.closest('.row-checkbox');
      if (!box) return;
      const index = parseInt(box.dataset.index, 10);
      if (isNaN(index)) return;
      if (e.shiftKey && lastClickedRow >= 0 && lastClickedRow !== index) {
        // Range over what is on screen, not over raw indexes: a filtered-out
        // row between the two ends is not something the user can see.
        const order = visibleIndexes();
        const from = order.indexOf(lastClickedRow);
        const to = order.indexOf(index);
        if (from >= 0 && to >= 0) {
          const [lo, hi] = from < to ? [from, to] : [to, from];
          const wanted = box.checked;
          order.slice(lo, hi + 1).forEach(i => {
            if (isActionable(currentAnimes[i])) currentAnimes[i].enabled = wanted;
          });
          renderTable(currentAnimes);
          updateStatsAndExport();
        }
      }
      lastClickedRow = index;
      setRowFocus(index);
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== ' ') return;
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      // Space is how a keyboard user activates a focused button or link;
      // swallowing it here broke every one of them.
      if (tag === 'button' || tag === 'a' || e.target.isContentEditable) return;
      if (document.querySelector('.modal-backdrop.show')) return;
      // Only drive the table once the user is actually in it. Claiming the
      // arrow keys globally meant the page could no longer be scrolled with
      // them, before the user had shown any interest in row navigation.
      const inTable = focusedRow >= 0 || (e.target.closest && e.target.closest('#animeTable'));
      if (!inTable) return;
      const order = visibleIndexes();
      if (!order.length) return;
      if (e.key === ' ') {
        if (focusedRow < 0) return;
        const item = currentAnimes[focusedRow];
        if (!item || !isActionable(item)) return;
        e.preventDefault();
        item.enabled = item.enabled === false;
        renderTable(currentAnimes);
        updateStatsAndExport();
        setRowFocus(focusedRow);
        return;
      }
      e.preventDefault();
      const at = order.indexOf(focusedRow);
      const step = e.key === 'ArrowDown' ? 1 : -1;
      const next = at < 0 ? (step > 0 ? 0 : order.length - 1)
                          : Math.min(order.length - 1, Math.max(0, at + step));
      setRowFocus(order[next]);
    });

    tableBody.addEventListener('change', (e) => {
      if (e.target && e.target.classList.contains('match-picker')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const item = currentAnimes[idx];
        const pick = item && Array.isArray(item.candidates)
          ? item.candidates[parseInt(e.target.value, 10)] : null;
        if (!pick) return;
        // Replace the identity/metadata only; status, score and progress stay.
        Object.assign(item, pick, { enabled: isActionable(pick) });
        renderTable(currentAnimes);
        updateStatsAndExport();
        showToast('Match changed to ' + pick.title);
        return;
      }

      if (e.target && e.target.classList.contains('detail-input')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const field = e.target.dataset.field;
        const item = currentAnimes[idx];
        if (!item || !field) return;
        const raw = e.target.value.trim();
        if (raw === '') {
          delete item[field];
        } else if (field === 'score') {
          item.score = Math.max(0, Math.min(10, Math.floor(Number(raw) || 0)));
          // A row read back from AniList carries the exact POINT_100 score.
          // Once a person edits the 0-10 value, that raw number is no longer
          // what they mean, and leaving it would make the edit a no-op.
          delete item.score_raw;
          e.target.value = item.score;
        } else {
          item[field] = raw;
        }
        updateStatsAndExport();
        return;
      }

      if (e.target && e.target.classList.contains('row-status')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const item = currentAnimes[idx];
        if (!item) return;
        item.status = e.target.value;
        // Recolour in place rather than re-rendering, so the select keeps focus.
        e.target.className = 'row-status status-pill ' + statusPillClass(item.status);
        updateStatsAndExport();
        return;
      }

      if (e.target && e.target.classList.contains('progress-input')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const field = e.target.dataset.field;
        const item = currentAnimes[idx];
        if (!item || !field) return;
        const raw = e.target.value.trim();
        if (raw === '') {
          delete item[field];           // fall back to the derived value
        } else {
          let value = Math.max(0, Math.floor(Number(raw) || 0));
          const total = Number(item[PROGRESS_TOTAL_FIELD[field]] || 0);
          if (total > 0 && value > total) value = total;  // MAL rejects progress past the total
          item[field] = value;
          e.target.value = value;
        }
        updateStatsAndExport();
        return;
      }

      if (e.target && e.target.classList.contains('row-checkbox')) {
        const idx = parseInt(e.target.dataset.index, 10);
        if (!isNaN(idx) && currentAnimes[idx]) {
          currentAnimes[idx].enabled = e.target.checked;
          const tr = e.target.closest('tr');
          if (tr) {
            tr.classList.toggle('row-excluded', !e.target.checked);
          }
          updateSelectAllState();
          updateStatsAndExport();
        }
      }
    });

    async function repairRow(idx, queryText) {
      const item = currentAnimes[idx];
      const text = String(queryText || '').trim();
      if (!item || !text) return;
      const digitsOnly = text.length > 0 && Array.from(text).every(ch => ch >= '0' && ch <= '9');
      const url = digitsOnly ? '/api/lookup' : '/api/search';
      const body = digitsOnly
        ? { mal_id: text, media_type: mediaType() }
        : { query: text, media_type: mediaType() };
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          showToast(payload.error || 'Nothing found for that search', 'warning');
          return;
        }
        const found = digitsOnly ? payload.entry : (payload.entries || [])[0];
        const list = digitsOnly ? [payload.entry] : (payload.entries || []);
        if (!found) {
          showToast('Nothing found for that search', 'warning');
          return;
        }
        // Taking entries[0] silently is the same confidently-wrong failure the
        // fixer exists to prevent. Hand ambiguity to the fixer, which already
        // renders the candidates; only apply outright when there is nothing to
        // choose between.
        const doubtful = list.length > 1
          || (found.match_ratio !== undefined && found.match_ratio !== null
              && found.match_ratio < REVIEW_RATIO_FLOOR);
        if (doubtful && !digitsOnly) {
          item.candidates = list;
          openFixerWith(idx, list, text);
          return;
        }
        // Replace identity and metadata; the user's own choices stay put.
        Object.assign(item, found, { enabled: isActionable(found), needs_review: false });
        delete item.unresolved;      // the placeholder's flag, now untrue
        delete item.review_reason;   // and the reason it was flagged for
        if (list.length > 1) item.candidates = list;
        renderTable(currentAnimes);
        updateStatsAndExport();
        showToast('Matched to ' + found.title);
      } catch (err) {
        showToast('Could not reach the local server', 'error');
      }
    }

    // Allow clicking anywhere inside the checkbox cell to toggle
    tableBody.addEventListener('click', (e) => {
      const detailBtn = e.target.closest('.btn-detail');
      if (detailBtn) {
        const idx = parseInt(detailBtn.dataset.detail, 10);
        if (expandedRows.has(idx)) {
          expandedRows.delete(idx);
        } else {
          expandedRows.add(idx);
        }
        renderTable(currentAnimes);
        return;
      }

      const fixBtn = e.target.closest('.btn-fix');
      if (fixBtn) {
        const idx = parseInt(fixBtn.dataset.index, 10);
        const input = tableBody.querySelector(`.fix-input[data-index="${idx}"]`);
        const typed = input ? input.value.trim() : '';
        if (typed) {
          repairRow(idx, typed);            // they already typed something specific
        } else {
          openFixer(idx);                   // otherwise show them the candidates
        }
        return;
      }

      const cell = e.target.closest('.cell-checkbox');
      if (cell && e.target.tagName !== 'INPUT') {
        const cb = cell.querySelector('.row-checkbox');
        if (cb) {
          cb.checked = !cb.checked;
          cb.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
    });

    btnCopyUnresolved.addEventListener('click', async () => {
      // The same rows the fixer works through. "No MAL id" was the old test,
      // which under an AniList write copied rows that write perfectly well.
      const missed = currentAnimes.filter(i => !isActionable(i)).map(i => i.pasted || i.original || i.title);
      if (!missed.length) return;
      try {
        await navigator.clipboard.writeText(missed.join(String.fromCharCode(10)));
        showToast(`Copied ${missed.length} unmatched title(s)`);
      } catch (e) {
        showToast('Clipboard blocked - the unmatched rows are listed in the table', 'warning');
      }
    });

    tableBody.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || !e.target.classList.contains('fix-input')) return;
      e.preventDefault();
      repairRow(parseInt(e.target.dataset.index, 10), e.target.value);
    });

    searchInput.addEventListener('input', () => {
      showAllRows = false;
      renderTable(currentAnimes);
    });

    chipRow.addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      statusFilter = chip.dataset.chip || '';
      showAllRows = false;
      renderTable(currentAnimes);
      updateStats();
    });

    rowCapNotice.addEventListener('click', (e) => {
      if (!e.target.closest('#btnShowAll')) return;
      showAllRows = true;
      renderTable(currentAnimes);
    });

    function toggleSort(th) {
      const key = th.dataset.sort;
      if (sortKey !== key) {
        sortKey = key;
        sortDir = 1;
      } else if (sortDir === 1) {
        sortDir = -1;
      } else {
        sortKey = '';
        sortDir = 0;
      }
      document.querySelectorAll('th.sortable').forEach(other => {
        const mark = other.querySelector('.sort-mark');
        if (mark) mark.textContent = '';
        other.setAttribute('aria-sort', 'none');
      });
      if (sortKey) {
        const mark = th.querySelector('.sort-mark');
        if (mark) mark.textContent = sortDir === 1 ? '^' : 'v';
        th.setAttribute('aria-sort', sortDir === 1 ? 'ascending' : 'descending');
      }
      renderTable(currentAnimes);
    }

    document.querySelectorAll('th.sortable').forEach(th => {
      th.addEventListener('click', () => toggleSort(th));
      th.addEventListener('keydown', event => {
        // A header you can only reach with a mouse is not a control.
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          toggleSort(th);
        }
      });
    });

    // Which rows a bulk control touches. Selected-and-visible when anything is
    // selected, otherwise every visible row - so filtering to "Dropped" and
    // stamping them all still works without ticking each one first. The label
    // says which of the two is in force, either way.
    function bulkTargets() {
      const selected = currentAnimes.filter(i => isActionable(i) && i.enabled !== false
                                                 && matchesFilters(i));
      if (selected.length) return { rows: selected, scope: 'selected' };
      return { rows: currentAnimes.filter(i => matchesFilters(i)), scope: 'visible' };
    }

    function updateBulkLabels() {
      const { rows, scope } = bulkTargets();
      const label = rows.length
        ? 'Applies to ' + rows.length + ' ' + scope + ' row' + (rows.length === 1 ? '' : 's')
        : 'No rows to apply to';
      document.querySelectorAll('.bulk-scope').forEach(el => { el.textContent = label; });
    }

    function applyBulk(field, value) {
      const { rows } = bulkTargets();
      // Previously this stamped currentAnimes outright, overwriting unchecked
      // rows, rows hidden by the filter, and anything hand-edited.
      rows.forEach(item => {
        item[field] = value;
        // Same reason as the per-row editor: a stamped score replaces whatever
        // the account had, so the exact value it was read at is now stale.
        if (field === 'score') delete item.score_raw;
      });
      renderTable(currentAnimes);
      if (currentAnimes.length > 0) updateStatsAndExport();
      updateBulkLabels();
    }

    // Step 1's defaults: what new rows start as. Setting rows that already
    // exist is the review toolbar's job (09-shell.js). One pair of controls
    // used to do both, so changing the default for the next paste re-stamped
    // the whole list. `change` rather than `input` either way: bound to
    // `input` the old box acted on every keystroke, so "10" wrote a 1 first.
    selectStatus.addEventListener('change', saveState);
    inputScore.addEventListener('change', () => {
      inputScore.value = Math.max(0, Math.min(10, parseInt(inputScore.value, 10) || 0));
      saveState();
    });

    // ---- fixing unmatched rows -------------------------------------------
    let fixQueue = [];
    let fixPos = 0;
    let fixFixed = 0;

    // Every row that cannot be used for the current destination, which is
    // exactly the set that shows a Find button. Keying this off "no ids at all"
    // instead meant clicking Find on an AniList-only row under the XML
    // destination opened the fixer on a different title.
    function unmatchedIndexes() {
      return currentAnimes.map((item, i) => isActionable(item) ? -1 : i).filter(i => i >= 0);
    }

    function openFixer(startIndex) {
      fixQueue = unmatchedIndexes();
      if (!fixQueue.length) {
        showToast('Nothing left to fix');
        return;
      }
      fixFixed = 0;
      let wanted = fixQueue.indexOf(startIndex);
      if (wanted < 0 && startIndex >= 0 && currentAnimes[startIndex]) {
        fixQueue.unshift(startIndex);      // asked for a specific row: honour it
        wanted = 0;
      }
      fixPos = wanted >= 0 ? wanted : 0;
      showModal(fixModal);
      showFixCurrent();
    }

    function showFixCurrent() {
      if (fixPos >= fixQueue.length) {
        hideModal(fixModal);
        showToast(fixFixed ? `Fixed ${fixFixed} of ${fixQueue.length}` : 'Nothing changed');
        return;
      }
      const item = currentAnimes[fixQueue[fixPos]];
      if (!item) { fixPos += 1; showFixCurrent(); return; }
      const text = item.original || item.title || '';
      fixOriginal.textContent = text;
      fixQuery.value = text;
      fixCounter.textContent = `${fixPos + 1} of ${fixQueue.length}`;
      fixResults.innerHTML = '<div class="fix-hint">Press Search, or paste an AniList link and press Enter.</div>';
      fixQuery.focus();
      fixQuery.select();
    }

    // The years an entry ran, as one field: a single year when it started and
    // finished in the same one, an open range while it is still running.
    function runYears(d) {
      if (!d || !d.start_year) return '';
      if (!d.end_year) return d.start_year + '-';
      return d.end_year === d.start_year ? String(d.start_year) : d.start_year + '-' + d.end_year;
    }

    // Every title AniList knows this entry by, minus the one already shown as
    // the heading. This is usually what tells two near-identical entries apart.
    function otherTitles(entry, d) {
      const shown = String(entry.title || '').toLowerCase();
      const seen = new Set([shown]);
      const out = [];
      [d.romaji, d.english, d.native].concat(d.synonyms || []).forEach(t => {
        const key = String(t || '').toLowerCase();
        if (!key || seen.has(key)) return;
        seen.add(key);
        out.push(t);
      });
      return out;
    }

    function resultRowHtml(entry, i) {
      const d = entry.details || {};
      const counts = mediaType() === 'manga'
        ? `${entry.chapters || '?'} ch / ${entry.volumes || '?'} vol`
        : `${entry.episodes || '?'} eps`;
      const years = runYears(d) || (entry.year ? String(entry.year) : '');
      const bits = [escapeHtml(entry.type || ''), escapeHtml(years), escapeHtml(d.status || ''), counts,
                    d.mean_score ? d.mean_score + '% rated' : '',
                    d.popularity ? Number(d.popularity).toLocaleString() + ' users' : '']
                   .filter(Boolean);
      // The one thing that decides whether a row can travel as MAL XML, so it
      // is a flag rather than another grey item in the list.
      const ident = Number(entry.id) > 0
        ? `<span class="fix-flag">MAL ${entry.id}</span>`
        : '<span class="fix-flag warn">No MAL ID - AniList only</span>';
      const alts = otherTitles(entry, d);
      return `<button class="fix-result" data-pick="${i}">
        ${entry.cover ? `<img class="cover-thumb" src="${escapeHtml(entry.cover)}" alt="" loading="lazy">`
                      : '<span class="cover-thumb cover-empty" aria-hidden="true"></span>'}
        <span class="grow">
          <span class="name">${escapeHtml(entry.title || '')}</span>
          ${alts.length ? `<span class="alt">${escapeHtml(alts[0])}</span>` : ''}
          <span class="meta">
            ${ident}<span>AniList ${entry.anilist_id || '?'}</span>${bits.map(b => `<span>${b}</span>`).join('')}
          </span>
          ${(d.genres || []).length
            ? `<span class="fix-genres">${d.genres.map(g => `<span class="fix-genre">${escapeHtml(g)}</span>`).join('')}</span>`
            : ''}
          ${alts.length > 1
            ? `<span class="synonyms"><b>Also known as:</b> ${escapeHtml(alts.slice(1).join(' | '))}</span>`
            : ''}
          ${d.description ? `<span class="desc">${escapeHtml(d.description)}</span>` : ''}
        </span>
      </button>`;
    }

    let fixCandidates = [];

    function renderFixResults(entries) {
      fixResults.innerHTML = (entries || []).map(resultRowHtml).join('')
        || '<div class="fix-hint">Nothing found.</div>';
    }

    async function runFixSearch() {
      const query = fixQuery.value.trim();
      if (!query) return;
      fixResults.innerHTML = '<div class="fix-hint">Searching AniList...</div>';
      try {
        const res = await fetch('/api/resolve-one', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: query, media_type: mediaType(), id_kind: fixIdKind.value }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!payload.ok) {
          fixCandidates = [];
          fixResults.innerHTML = `<div class="fix-hint">${escapeHtml(payload.error || 'Nothing found.')}</div>`;
          return;
        }
        fixCandidates = payload.entries || [];
        renderFixResults(fixCandidates);
      } catch (e) {
        fixResults.innerHTML = '<div class="fix-hint">Could not reach the local server.</div>';
      }
    }

    function applyFix(entry) {
      const idx = fixQueue[fixPos];
      const item = currentAnimes[idx];
      if (!item || !entry) return;
      // Identity and metadata change; the user's status, score and progress stay.
      Object.assign(item, entry, { enabled: isActionable(entry) });
      // The ratio describes the original search and means nothing once a
      // person has chosen, so choosing clears the flag.
      item.needs_review = false;
      delete item.review_reason;     // a stale reason outlives the flag otherwise
      delete item.unresolved;        // the placeholder's flag, now untrue
      if (fixCandidates.length > 1) item.candidates = fixCandidates;
      fixFixed += 1;
      fixPos += 1;
      renderTable(currentAnimes);
      updateStatsAndExport();
      showFixCurrent();
    }

    btnRemember.addEventListener('click', () => {
      const excluded = currentAnimes.filter(item => isActionable(item) && item.enabled === false);
      if (!excluded.length) {
        showToast('Nothing is unchecked, so there is nothing to skip.', 'warning');
        return;
      }
      excluded.forEach(item => ignoredKeys.add(entryKey(item)));
      saveIgnored();
      updateIgnoredLabel();
      showToast(excluded.length + (excluded.length === 1 ? ' title' : ' titles')
        + ' will stay unchecked in future lists.');
    });

    btnForgetIgnored.addEventListener('click', () => {
      const n = ignoredKeys.size;
      ignoredKeys = new Set();
      saveIgnored();
      updateIgnoredLabel();
      showToast('No longer skipping ' + n + (n === 1 ? ' title' : ' titles') + '.');
    });

    // Open the fixer on one row with results already in hand, so the inline
    // Find button can hand over instead of guessing.
    function openFixerWith(index, entries, queryText) {
      fixQueue = [index];
      fixPos = 0;
      fixFixed = 0;
      showModal(fixModal);
      const item = currentAnimes[index];
      fixOriginal.textContent = (item && (item.original || item.title)) || queryText;
      fixQuery.value = queryText;
      fixCounter.textContent = '1 of 1';
      fixCandidates = entries;
      renderFixResults(entries);
    }

    btnFixUnmatched.addEventListener('click', () => openFixer(-1));
    btnFixSearch.addEventListener('click', runFixSearch);
    fixIdKind.addEventListener('change', runFixSearch);
    fixQuery.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runFixSearch(); }
    });
    fixResults.addEventListener('click', (e) => {
      const pick = e.target.closest('[data-pick]');
      if (pick) applyFix(fixCandidates[parseInt(pick.dataset.pick, 10)]);
    });
    btnFixBack.addEventListener('click', () => {
      if (fixPos > 0) { fixPos -= 1; showFixCurrent(); }
    });
    btnFixSkip.addEventListener('click', () => { fixPos += 1; showFixCurrent(); });

    // Search as you type, so the fixer does not need the button pressed. Long
    // enough not to fire a request per keystroke on a slow list.
    let fixSearchTimer = null;
    fixQuery.addEventListener('input', () => {
      if (fixSearchTimer) clearTimeout(fixSearchTimer);
      const text = fixQuery.value.trim();
      if (text.length < 3) return;
      fixSearchTimer = setTimeout(() => { runFixSearch(); }, 450);
    });
    btnFixClose.addEventListener('click', () => {
      hideModal(fixModal);
      if (fixFixed) showToast(`Fixed ${fixFixed} title${fixFixed === 1 ? '' : 's'}`);
    });
    fixModal.addEventListener('click', (e) => {
      if (e.target === fixModal) hideModal(fixModal);
    });

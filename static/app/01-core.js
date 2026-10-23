
    // Substituted by the server from the calibrated module constant.
    const REVIEW_RATIO_FLOOR = __REVIEW_RATIO_FLOOR__;
    let currentAnimes = [];
    // Rows belong to one media type: an anime row cannot live in a manga list,
    // so a switch has to put them down. It used to drop them on the floor -
    // silently, and `saveState` then persisted the empty list, so one mis-click
    // on the dropdown destroyed a long resolve with no way back. They are set
    // aside per type now, and switching back picks them up again.
    const rowsByMedia = { anime: [], manga: [] };
    let lastMediaType = 'anime';
    let ignoredKeys = new Set();
    let isProcessing = false;
    // The shell's own state (09-shell.js): which source tab is showing, whether
    // the folded source has been reopened by hand, and which destination the
    // last resolve ranked its matches for.
    let sourceTab = 'paste';
    let sourceOpen = false;
    let resolvedFor = '';

    // Elements
    const txtArea = document.getElementById('animeTextInput');
    const mediaTypeSelect = document.getElementById('mediaType');
    const lineCountLabel = document.getElementById('lineCountLabel');
    const selectStatus = document.getElementById('selectStatus');
    const inputScore = document.getElementById('inputScore');
    const btnProcess = document.getElementById('btnProcess');
    const btnPaste = document.getElementById('btnPaste');
    const btnClear = document.getElementById('btnClear');
    const btnSample = document.getElementById('btnSample');
    const toggleSequels = document.getElementById('toggleSequels');
    const searchInput = document.getElementById('searchInput');
    const tableBody = document.getElementById('tableBody');
    const emptyState = document.getElementById('emptyTableState');
    const selectAllCheckbox = document.getElementById('selectAllCheckbox');
    const progressCard = document.getElementById('progressCard');
    const progressBarFill = document.getElementById('progressBarFill');
    const progressCurrentTitle = document.getElementById('progressCurrentTitle');
    const progressCurrentSub = document.getElementById('progressCurrentSub');
    const progressCountLabel = document.getElementById('progressCountLabel');
    const progressPercentLabel = document.getElementById('progressPercentLabel');
    const etaLabel = document.getElementById('etaLabel');
    const exportHeadline = document.getElementById('exportHeadline');
    const exportSubline = document.getElementById('exportSubline');
    const btnDownloadXml = document.getElementById('btnDownloadXml');
    const btnDownloadGz = document.getElementById('btnDownloadGz');
    const statTotal = document.getElementById('statTotal');
    const statNormalized = document.getElementById('statNormalized');
    const statEpisodes = document.getElementById('statEpisodes');
    const statXmlValid = document.getElementById('statXmlValid');
    const filterCountLabel = document.getElementById('filterCountLabel');
    const toast = document.getElementById('toast');
    const toastMessage = document.getElementById('toastMessage');
    const countColumnHeading = document.getElementById('countColumnHeading');
    const unresolvedBar = document.getElementById('unresolvedBar');
    const unresolvedText = document.getElementById('unresolvedText');
    const btnCopyUnresolved = document.getElementById('btnCopyUnresolved');
    const btnReset = document.getElementById('btnReset');
    const btnImport = document.getElementById('btnImport');
    const fixModal = document.getElementById('fixModal');
    const fixOriginal = document.getElementById('fixOriginal');
    const fixQuery = document.getElementById('fixQuery');
    const fixIdKind = document.getElementById('fixIdKind');
    const fixResults = document.getElementById('fixResults');
    const fixCounter = document.getElementById('fixCounter');
    const btnFixSearch = document.getElementById('btnFixSearch');
    const btnFixSkip = document.getElementById('btnFixSkip');
    const btnFixBack = document.getElementById('btnFixBack');
    const btnFixClose = document.getElementById('btnFixClose');
    const btnFixUnmatched = document.getElementById('btnFixUnmatched');
    const anilistPanel = document.getElementById('anilistPanel');
    const anilistToken = document.getElementById('anilistToken');
    const btnTokenTest = document.getElementById('btnTokenTest');
    const btnRemember = document.getElementById('btnRemember');
    const btnForgetIgnored = document.getElementById('btnForgetIgnored');
    const tokenStatus = document.getElementById('tokenStatus');
    const btnWrite = document.getElementById('btnWrite');
    const writeModal = document.getElementById('writeModal');
    const writePlan = document.getElementById('writePlan');
    const btnWriteConfirm = document.getElementById('btnWriteConfirm');
    const btnWritePlanSave = document.getElementById('btnWritePlanSave');
    const btnDownloadCsv = document.getElementById('btnDownloadCsv');
    const undoSnapshot = document.getElementById('undoSnapshot');
    const undoSnapshotLabel = document.getElementById('undoSnapshotLabel');
    const btnWriteCancel = document.getElementById('btnWriteCancel');
    const undoModal = document.getElementById('undoModal');
    const undoPlan = document.getElementById('undoPlan');
    const btnUndo = document.getElementById('btnUndo');
    const btnAniListImport = document.getElementById('btnAniListImport');
    const btnCopyAccount = document.getElementById('btnCopyAccount');
    const copyModal = document.getElementById('copyModal');
    const copySourceUser = document.getElementById('copySourceUser');
    const copyPlan = document.getElementById('copyPlan');
    const btnCopyCheck = document.getElementById('btnCopyCheck');
    const btnCopyRun = document.getElementById('btnCopyRun');
    const btnCopyCancel = document.getElementById('btnCopyCancel');
    const listImportUser = document.getElementById('listImportUser');
    const listImportStatus = document.getElementById('listImportStatus');
    const btnListImportGo = document.getElementById('btnListImportGo');
    const btnRetry = document.getElementById('btnRetry');
    const btnUndoConfirm = document.getElementById('btnUndoConfirm');
    const btnUndoCancel = document.getElementById('btnUndoCancel');
    const importFile = document.getElementById('importFile');
    const destinationSelect = document.getElementById('destination');
    const destinationPrompt = document.getElementById('destinationPrompt');
    const destinationField = document.getElementById('destinationField');
    const runControls = document.getElementById('runControls');
    const btnPause = document.getElementById('btnPause');
    const btnCancel = document.getElementById('btnCancel');
    const chipRow = document.getElementById('chipRow');
    const rowCapNotice = document.getElementById('rowCapNotice');
    const toggleArchive = document.getElementById('toggleArchive');
    const toggleFresh = document.getElementById('toggleFresh');
    const addUnmatchedImports = document.getElementById('addUnmatchedImports');
    const btnPreview = document.getElementById('btnPreview');
    const previewModal = document.getElementById('previewModal');
    const previewBody = document.getElementById('previewBody');
    const previewTitle = document.getElementById('previewTitle');
    const btnPreviewCopy = document.getElementById('btnPreviewCopy');
    const btnPreviewClose = document.getElementById('btnPreviewClose');

    const ROW_CAP = 300;
    let showAllRows = false;
    let statusFilter = '';
    let sortKey = '';
    let sortDir = 0;            // 0 = off, 1 = asc, -1 = desc
    let activeRunId = null;
    let isPaused = false;
    let networkFailures = 0;
    let mergedTitles = [];
    // '' until the user picks. Not defaulted on their behalf: the two
    // destinations accept different titles, so guessing hides rows.
    let destination = '';
    let exportFileInfo = null;
    // Memory only, deliberately: not localStorage, not the saved session, never
    // sent anywhere except this machine's own server.
    let anilistUser = null;
    const expandedRows = new Set();

    // AniList's own vocabulary. The server maps these onto MAL's XML values:
    // Paused -> On-Hold, Rereading/Rewatching -> Reading/Watching + the repeat flag.
    const STATUS_SETS = {
      manga: ['Reading', 'Plan to read', 'Completed', 'Rereading', 'Paused', 'Dropped'],
      anime: ['Watching', 'Plan to watch', 'Completed', 'Rewatching', 'Paused', 'Dropped'],
    };

    const STATUS_PILL = {
      'reading': 'status-watching', 'watching': 'status-watching',
      'rereading': 'status-watching', 'rewatching': 'status-watching',
      'completed': 'status-completed',
      'plan to read': 'status-planned', 'plan to watch': 'status-planned',
      'paused': 'status-paused', 'on-hold': 'status-paused',
      'dropped': 'status-dropped',
    };

    // Mirrors the server: a finished entry (or an in-progress repeat) counts as
    // fully read/watched; anything else starts at zero. Shown as a placeholder so
    // an untouched field stays derived rather than frozen at today's value.
    function isRepeatStatus(status) {
      const s = String(status || '').toLowerCase();
      return s === 'rereading' || s === 'rewatching';
    }

    function derivedProgress(item, field) {
      const done = String(item.status || '').toLowerCase() === 'completed' || isRepeatStatus(item.status);
      const total = field === 'my_read_volumes' ? item.volumes
                  : field === 'my_read_chapters' ? item.chapters
                  : item.episodes;
      return done ? (Number(total) || 0) : 0;
    }

    const PROGRESS_TOTAL_FIELD = {
      'my_read_chapters': 'chapters',
      'my_read_volumes': 'volumes',
      'my_watched_episodes': 'episodes',
    };

    function progressFields() {
      return mediaType() === 'manga'
        ? [['my_read_chapters', 'ch'], ['my_read_volumes', 'vol']]
        : [['my_watched_episodes', 'ep']];
    }

    function mediaType() {
      return mediaTypeSelect.value === 'manga' ? 'manga' : 'anime';
    }

    function updateMediaTypeUI() {
      const isManga = mediaType() === 'manga';
      // Same order in both sets, so a switch carries the equivalent state across
      // by index (Reading <-> Watching, Rereading <-> Rewatching, ...).
      const from = STATUS_SETS[isManga ? 'anime' : 'manga'];
      const to = STATUS_SETS[isManga ? 'manga' : 'anime'];
      const previous = selectStatus.value;
      const sameIdx = from.indexOf(previous);
      const carried = sameIdx >= 0 ? to[sameIdx] : (to.indexOf(previous) >= 0 ? previous : 'Completed');
      selectStatus.innerHTML = to.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
      selectStatus.value = carried;
      syncBulkStatus();
      // The source has three tabs now, so its heading names the list rather
      // than one way of supplying it.
      document.getElementById('listPanelTitle').textContent = isManga ? 'Your manga list' : 'Your anime list';
      document.querySelector('label[for="animeTextInput"]').textContent = `One ${isManga ? 'manga' : 'anime'} per line`;
      // One column now holds "read / total", so it carries the label and the
      // trailing control column stays unlabelled.
      countColumnHeading.textContent = isManga ? 'Read / Total' : 'Watched / Total';
      // By id, not by position: the third .stat-label used to be assumed.
      const episodesLabel = document.getElementById('statEpisodesLabel');
      if (episodesLabel) episodesLabel.textContent = isManga ? 'Chapters / volumes' : 'Episodes';
      toggleSequels.closest('.toggle-card').style.display = isManga ? 'none' : '';
      // The String.fromCharCode(10) calls in this file are a leftover: the page
      // used to be a Python string literal inside gui.py, which consumed
      // backslashes, so a JS escape could not be written at all. This is a real
      // .js file now and an ordinary escape would work - they are left alone
      // only to keep the move that extracted this file byte-for-byte reviewable.
      txtArea.placeholder = (isManga
        ? ['Berserk - 374 ch - dropped', 'One Piece', 'Vagabond (completed) 9', 'Fullmetal Alchemist', '...']
        : ['Sousou no Frieren 12/28 9/10 watching', 'Attack on Titan', 'Solo Leveling [ptw]', '...']
      ).join(String.fromCharCode(10));
      btnDownloadXml.download = isManga ? 'myanimelist_manga_export.xml' : 'myanimelist_export.xml';
      btnDownloadGz.download = isManga ? 'myanimelist_manga_export.xml.gz' : 'myanimelist_export.xml.gz';
      ignoredKeys = loadIgnored();
      updateIgnoredLabel();
      updateLineCounter();
      refreshExportState();
    }

    mediaTypeSelect.addEventListener('change', () => {
      const previous = lastMediaType;
      const next = mediaType();
      updateMediaTypeUI();
      if (previous !== next) {
        rowsByMedia[previous] = currentAnimes;
        currentAnimes = rowsByMedia[next] || [];
        lastMediaType = next;
        renderTable(currentAnimes);
        updateStatsAndExport();
        const kept = rowsByMedia[previous].length;
        if (currentAnimes.length) {
          showToast(`Back to ${currentAnimes.length} ${next} row${currentAnimes.length === 1 ? '' : 's'}`);
        } else if (kept) {
          showToast(`${kept} ${previous} row${kept === 1 ? '' : 's'} set aside - switch back to return to them`);
        }
      }
      saveState();
    });

    toggleSequels.addEventListener('change', saveState);

    // Marks a button as working. Disabling one says "you cannot press this";
    // it does not say "because I am busy", and these start requests that take
    // whole seconds. The spinner lives in CSS so it survives reduced motion,
    // where a frozen indicator would read as a hung app.
    function setBusy(button, on) {
      if (!button) return;
      button.classList.toggle('is-busy', !!on);
      if (on) button.setAttribute('aria-busy', 'true');
      else button.removeAttribute('aria-busy');
    }

    function showToast(msg, type = 'success') {
      toastMessage.textContent = msg;
      toast.className = 'toast show toast-' + type;
      setTimeout(() => {
        toast.className = 'toast';
      }, 3500);
    }

    function updateLineCounter() {
      const text = txtArea.value;
      const lines = text.split(String.fromCharCode(10)).filter(l => l.trim() && !l.trim().startsWith('#'));
      lineCountLabel.textContent = lines.length + (mediaType() === 'manga' ? ' manga' : ' anime');
    }

    txtArea.addEventListener('input', () => {
      updateLineCounter();
      saveState();
    });

    // Paste from clipboard
    btnPaste.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          txtArea.value = text;
          updateLineCounter();
          showToast('Pasted from clipboard');
        } else {
          txtArea.focus();
          showToast('Clipboard is empty', 'warning');
        }
      } catch (e) {
        txtArea.focus();
        showToast('Press Ctrl+V to paste into the text area', 'warning');
      }
    });

    // Clear textarea
    btnClear.addEventListener('click', () => {
      txtArea.value = '';
      updateLineCounter();
      saveState();
      showToast(currentAnimes.length
        ? 'Text cleared. Use Start over to also clear the results table.'
        : 'Text area cleared');
    });

    // Load sample list
    btnSample.addEventListener('click', () => {
      const sampleTitles = mediaType() === 'manga'
        ? ['Berserk', 'One Piece', 'Vagabond', 'Fullmetal Alchemist', 'Dandadan', 'Chainsaw Man', 'Vinland Saga']
        : ['Sousou no Frieren', 'Shingeki no Kyojin', 'Solo Leveling', 'Dandadan', 'Chainsaw Man', 'KonoSuba', 'Jujutsu Kaisen'];
      txtArea.value = sampleTitles.join(String.fromCharCode(10));
      updateLineCounter();
      showToast('Sample list loaded');
    });

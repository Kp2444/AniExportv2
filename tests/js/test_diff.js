// Behaviour tests for the write-diff renderer, running the shipped source.
const assert = require('assert');
const { extract } = require('./extract');

const src = extract([
  'AL_STATUS_LABELS', 'diffFieldLabel', 'diffValueText', 'writeOverrideFor',
  'setWriteOverride', 'pickBoxHtml', 'diffLineHtml', 'staleDateLineHtml',
  'writeRowHtml', 'renderWritePlan', 'updateWriteAftermath',
]);

let media = 'manga';
let writeOverrides = {};
let lastWritePlan = null;
let lastWriteSnapshot = null;
let lastWriteRunId = null;
let lastWriteFailed = 0;
let lastWriteCreated = 0;
let destination = 'anilist';

function mediaType() { return media; }
function escapeHtml(t) {
  return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
const writePlan = { innerHTML: '', querySelector: () => null };
const btnUndo = { style: {} };
const btnRetry = { style: {}, textContent: '' };

eval(src);

let checks = 0;
function is(actual, expected, what) {
  checks++;
  assert.strictEqual(actual, expected, what + ' -> got ' + JSON.stringify(actual));
}
function has(haystack, needle, what) {
  checks++;
  assert.ok(String(haystack).includes(needle), what + ' -> missing ' + needle
    + ' in ' + String(haystack).slice(0, 400));
}
function hasNot(haystack, needle, what) {
  checks++;
  assert.ok(!String(haystack).includes(needle), what + ' -> unexpected ' + needle);
}

// --- field labels follow the media type ------------------------------------
media = 'manga';
is(diffFieldLabel('progress'), 'chapters', 'manga progress is chapters');
is(diffFieldLabel('repeat'), 'rereads', 'manga repeat is rereads');
is(diffFieldLabel('progressVolumes'), 'volumes', 'volumes are volumes');
is(diffFieldLabel('scoreRaw'), 'score', 'scoreRaw is shown as score');
media = 'anime';
is(diffFieldLabel('progress'), 'episodes', 'anime progress is episodes');
is(diffFieldLabel('repeat'), 'rewatches', 'anime repeat is rewatches');

// --- values are never shown in a scale the user did not set ----------------
media = 'manga';
is(diffValueText('status', 'CURRENT'), 'Reading', 'CURRENT reads as Reading for manga');
is(diffValueText('status', 'REPEATING'), 'Rereading', 'REPEATING reads as Rereading');
media = 'anime';
is(diffValueText('status', 'CURRENT'), 'Watching', 'CURRENT reads as Watching for anime');
is(diffValueText('status', 'REPEATING'), 'Rewatching', 'REPEATING reads as Rewatching');
is(diffValueText('status', 'WAT'), 'WAT', 'an unknown enum is shown verbatim, not blank');
is(diffValueText('scoreRaw', 80), '80/100', 'the score scale is stated');
is(diffValueText('scoreRaw', 0), 'none', 'a zero score is "none", not 0/100');
is(diffValueText('progress', null), 'none', 'a null number is none');
is(diffValueText('progress', 0), '0', 'an actual zero progress still prints 0');
is(diffValueText('completedAt', { year: 2026, month: 10, day: 19 }), '2026-10-19', 'full date');
is(diffValueText('completedAt', { year: 2026, month: null, day: null }), '2026', 'year only');
is(diffValueText('completedAt', { year: 2026, month: 3, day: null }), '2026-03', 'year and month');
is(diffValueText('completedAt', { year: null, month: 3, day: 1 }), 'none', 'no year means none');
is(diffValueText('notes', ''), 'none', 'an empty note is none, not a pair of quotes');
is(diffValueText('notes', 'x'.repeat(80)).length, 63, 'a long note is truncated');

// --- a blocked field shows what will actually be sent ----------------------
media = 'manga';
writeOverrides = {};
const row = {
  title: 'Vinland Saga', anilist_id: 30642, update: true, stale_completed_at: false,
  diff: [{ field: 'progress', from: 224, to: 142, applied: 224, blocked: true }],
};
let html = writeRowHtml(row);
has(html, 'Vinland Saga', 'the title is shown');
has(html, '>update<', 'an existing entry is tagged as an update');
// The arrow shows what the row asked for, struck through, so a blocked field
// never renders as "224 -> 224" and read like nothing is happening.
has(html, 'class="diff-to overridden">142<', 'the asked-for value is shown, struck');
has(html, 'stays 224', 'and the outcome is stated');
hasNot(html, 'class="diff-to">224<', 'the outcome must not sit on both sides of the arrow');
has(html, 'keep mine', 'a blocked field offers the override');

// ticking "keep mine" flips the shown outcome with no server round trip
setWriteOverride(30642, 'progress', 'mine');
is(writeOverrideFor(30642, 'progress'), 'mine', 'the override is recorded');
html = writeRowHtml(row);
has(html, 'class="diff-to">142<', 'forcing drops the strike: the row value now lands');
hasNot(html, 'stays 224', 'and the kept-value note goes away');
has(html, 'checked', 'and the box stays ticked across a re-render');
has(html, '>forced<', 'the forced case is labelled');

// a field AniList overrides, with no asked-for value of our own
html = writeRowHtml({ title: 'Boys', anilist_id: 9, update: true, stale_completed_at: false,
  diff: [{ field: 'repeat', from: 4, to: null, applied: 5, blocked: false, by_anilist: true }] });
has(html, 'class="diff-from">4<', 'the account value is shown');
has(html, 'class="diff-to">5<', 'and what AniList will make it, with no strike');
has(html, 'AniList sets this itself', 'attributed, not blamed on the user');
hasNot(html, 'keep mine', 'there is nothing to override');

// a field we ask for AND AniList overrides
html = writeRowHtml({ title: 'Boys', anilist_id: 9, update: true, stale_completed_at: false,
  diff: [{ field: 'progress', from: 147, to: 40, applied: 147, blocked: false, by_anilist: true }] });
has(html, 'class="diff-to overridden">40<', 'the asked-for value is struck');
has(html, 'AniList sets 147 itself', 'and the real outcome is attributed to AniList');
setWriteOverride(30642, 'progress', null);
is(writeOverrideFor(30642, 'progress'), undefined, 'clearing removes the override');
is(Object.keys(writeOverrides).length, 0, 'and leaves no empty row behind');

// --- an unblocked field has no toggle at all -------------------------------
html = writeRowHtml({
  title: 'A', anilist_id: 1, update: false, stale_completed_at: false,
  diff: [{ field: 'progress', from: null, to: 40, applied: 40, blocked: false }],
});
has(html, '>new<', 'a create is tagged as new');
has(html, 'class="diff-from">none<', 'a create has no before value');
hasNot(html, 'keep mine', 'nothing to override when nothing is blocked');

// --- the stale completion date gets its own explicit action ----------------
writeOverrides = {};
const stale = { title: 'Punpun', anilist_id: 34632, update: true, stale_completed_at: true,
                diff: [{ field: 'status', from: 'COMPLETED', to: 'CURRENT',
                         applied: 'CURRENT', blocked: false }] };
html = writeRowHtml(stale);
has(html, 'clear the finish date', 'the stale date offers a clear');
has(html, 'keeps its finish date', 'and explains what happens if left alone');
setWriteOverride(34632, 'completedAt', 'clear');
html = writeRowHtml(stale);
has(html, 'will be cleared', 'ticking it says the date goes');
has(html, 'data-write-override="clear"', 'and sends "clear", not "mine"');

// --- the plan summary ------------------------------------------------------
writeOverrides = {};
renderWritePlan({
  viewer: 'kevin', creates: 1, updates: 2, unchanged: 44, conflicts: 1,
  skipped: [{ title: 'Nope' }],
  rows: [row, { title: 'Quiet', anilist_id: 2, update: true, diff: [], stale_completed_at: false }],
});
has(writePlan.innerHTML, 'kevin', 'the account is named');
has(writePlan.innerHTML, '44 already match', 'unchanged rows collapse to a count');
has(writePlan.innerHTML, 'row would send a value backwards', 'conflicts are counted');
has(writePlan.innerHTML, '1 row will be skipped', 'skipped rows are listed');
has(writePlan.innerHTML, 'Vinland Saga', 'a changing row is listed');
hasNot(writePlan.innerHTML, 'Quiet', 'a row with an empty diff is not listed');
has(writePlan.innerHTML, 'can be undone', 'the warning mentions the snapshot');

// one unchanged row reads as "1 already matches", not "1 already match"
renderWritePlan({ viewer: 'kevin', creates: 0, updates: 1, unchanged: 1, conflicts: 0,
                  skipped: [], rows: [{ title: 'Quiet', anilist_id: 2, update: true,
                                        diff: [], stale_completed_at: false }] });
has(writePlan.innerHTML, '1 already matches', 'a single unchanged row agrees with its verb');
has(writePlan.innerHTML, 'Nothing on your account changes', 'an all-quiet plan says so');
hasNot(writePlan.innerHTML, 'diff-list', 'and renders no empty list box');

// --- a hostile title cannot break out of the markup ------------------------
html = writeRowHtml({ title: '<img src=x onerror=alert(1)>', anilist_id: 3, update: true,
                      stale_completed_at: false,
                      diff: [{ field: 'notes', from: '</div><script>bad()</script>',
                               to: 'ok', applied: 'ok', blocked: false }] });
hasNot(html, '<img', 'the title is escaped');
hasNot(html, '<script', 'so is a note read back from the account');

// --- the undo and retry buttons follow the last run ------------------------
destination = 'anilist';
lastWriteSnapshot = null;
lastWriteFailed = 0;
lastWriteRunId = null;
updateWriteAftermath();
is(btnUndo.style.display, 'none', 'no undo before a write');
is(btnRetry.style.display, 'none', 'no retry before a write');
lastWriteSnapshot = '.aniexport_undo_1.json';
lastWriteFailed = 3;
lastWriteRunId = 'abc';
updateWriteAftermath();
is(btnUndo.style.display, '', 'undo appears once a snapshot exists');
is(btnRetry.style.display, '', 'retry appears once rows have failed');
is(btnRetry.textContent, 'Retry 3 failed', 'and says how many');
destination = 'xml';
updateWriteAftermath();
is(btnUndo.style.display, 'none', 'both hide when the destination is not AniList');
is(btnRetry.style.display, 'none', 'retry hides too');

console.log('  write diff      ' + checks + ' assertions');

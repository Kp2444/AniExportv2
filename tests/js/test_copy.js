// The account-copy Check result, run against the shipped source.
//
// This used to be a sentence with five numbers in it, ending in an inline
// <span class="warn"> that the dialog's own `.write-plan .warn` rule turned
// into a padded, tinted box. An inline box wraps with the background covering
// only its first line, so the tail collided with the text beside it.
const assert = require('assert');
const { extract } = require('./extract');

global.escapeHtml = t => String(t == null ? '' : t)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const src = extract(['copyFigure', 'copyStageLine']);
eval(src + ';global.copyStageLine = copyStageLine;');

let checks = 0;
function ok(c, what) { checks++; assert.ok(c, what); }
function no(c, what) { checks++; assert.ok(!c, what); }

const full = copyStageLine({ media_type: 'anime', found: 257, creates: 0, updates: 257,
                             unchanged: 257, skipped: [], extra_on_destination: 3 });

// --- the numbers are figures, not prose ------------------------------------
ok(full.includes('class="copy-stage"'), 'a stage is its own block');
ok(full.includes('>Anime<'), 'the media type is named');
['On the source', 'To add', 'To overwrite', 'Already identical',
 'Only on the destination'].forEach(label => {
  ok(full.includes('<dt>' + label + '</dt>'), 'missing figure: ' + label);
});
ok(full.includes('<dd>257</dd>'), 'the counts are values, not sentence fragments');

// --- nothing inline can box itself again -----------------------------------
no(/<span[^>]*class="[^"]*\bwarn\b/.test(full),
   'no inline warn span: inside the dialog body it renders as a box and wraps badly');
no(full.includes('are not on the source and will be left'),
   'the sentence is gone - the warning under the list already says it');

// --- the two that change the destination are marked ------------------------
const live = copyStageLine({ media_type: 'manga', found: 10, creates: 4, updates: 6,
                             unchanged: 0, skipped: [], extra_on_destination: 0 });
ok(live.includes('copy-fig is-live'), 'to-add and to-overwrite are highlighted when non-zero');
no(/Already identical/.test(live), 'a zero "identical" count is not shown at all');
no(/Only on the destination/.test(live), 'nor a zero destination-only count');

// a stage that changes nothing must not highlight anything
const quiet = copyStageLine({ media_type: 'anime', found: 5, creates: 0, updates: 0,
                              unchanged: 5, skipped: [], extra_on_destination: 0 });
no(quiet.includes('is-live'), 'nothing to add or overwrite means nothing is highlighted');

// --- unwritable rows are called out, not buried ----------------------------
const skipped = copyStageLine({ media_type: 'manga', found: 9, creates: 1, updates: 1,
                                unchanged: 0, skipped: [1, 2, 3], extra_on_destination: 0 });
ok(skipped.includes('No AniList id'), 'rows that cannot be written are counted');
ok(skipped.includes('copy-fig is-warn'), 'and marked as a warning');
ok(skipped.includes('<dd>3</dd>'), 'with the real count');

// --- an empty source reads as a sentence, because there are no figures -----
const empty = copyStageLine({ media_type: 'manga', found: 0, creates: 0, updates: 0,
                              unchanged: 0, skipped: [], extra_on_destination: 0 });
ok(empty.includes('The source has none.'), 'an empty stage says so plainly');
no(empty.includes('<dl'), 'and renders no empty figure list');

// --- a hostile media label cannot break out --------------------------------
const nasty = copyStageLine({ media_type: 'anime', found: 1, creates: 0, updates: 1,
                              unchanged: 0, skipped: ['<img src=x>'], extra_on_destination: 0 });
no(nasty.includes('<img src=x'), 'skipped rows are counted, never interpolated raw');

console.log('  account copy    ' + checks + ' assertions');

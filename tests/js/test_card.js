// The fixer's candidate card, run against the shipped source.
const assert = require('assert');
const { extract } = require('./extract');

let media = 'manga';
global.mediaType = () => media;
global.escapeHtml = t => String(t == null ? '' : t)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const src = extract(['runYears', 'otherTitles', 'resultRowHtml']);
eval(src + ';global.resultRowHtml = resultRowHtml; global.runYears = runYears; global.otherTitles = otherTitles;');

let checks = 0;
function ok(cond, what) { checks++; assert.ok(cond, what); }
function eq(a, b, what) { checks++; assert.strictEqual(a, b, what); }

// runYears states the run, not a bare start year
eq(runYears({ start_year: 2017, end_year: 2020 }), '2017-2020');
eq(runYears({ start_year: 2022, end_year: 2022 }), '2022');
eq(runYears({ start_year: 2024, end_year: null }), '2024-', 'a running series must not look finished');
eq(runYears({}), '');

// the heading title is never repeated in the alternates
checks++;
assert.deepStrictEqual(
  otherTitles({ title: 'Vinland Saga' },
    { romaji: 'Vinland Saga', english: 'Vinland Saga', native: 'ヴィンランド・サガ', synonyms: ['VS', 'vinland saga'] }),
  ['ヴィンランド・サガ', 'VS'], 'duplicates must collapse, case-insensitively');

// the real payload from a live search
const entry = {
  id: 0, anilist_id: 148167, title: 'Go-toubun no Hanayome: 122 + 1', type: 'One-shot',
  chapters: 1, volumes: 0, cover: 'https://x/c.jpg',
  details: { romaji: 'Go-toubun no Hanayome: 122 + 1', english: '', native: '五等分の花嫁',
             synonyms: ['5-toubun no Hanayome: 122 + 1'], genres: ['Comedy', 'Romance'],
             mean_score: 69, popularity: 2942, status: 'Finished',
             start_year: 2022, end_year: 2022, url: 'https://anilist.co/manga/148167',
             description: 'A bonus chapter.' },
};
const html = resultRowHtml(entry, 0);
['No MAL ID - AniList only', 'AniList 148167', '1 ch / ? vol', 'Finished', '2022',
 '69% rated', '2,942 users', 'Comedy', 'Romance', '五等分の花嫁',
 'Also known as:', 'A bonus chapter.'].forEach(bit => {
  ok(html.includes(bit), 'card is missing: ' + bit);
});
ok(html.includes('data-pick="0"'), 'the card must stay selectable');

// a hit WITH a MAL id shows the id, not the warning
const withMal = resultRowHtml({ id: 112589, anilist_id: 101177, title: 'Fly Me to the Moon',
  type: 'Manga', chapters: 0, volumes: 0, details: {} }, 3);
ok(withMal.includes('>MAL 112589<'), 'the MAL id is shown');
ok(!withMal.includes('AniList only'), 'and the AniList-only warning is not');
ok(!withMal.includes('Also known as'), 'no alternates means no empty label');
ok(!withMal.includes('undefined'), 'a sparse entry must not render undefined');
ok(!withMal.includes('NaN'), 'nor NaN');

// a row resolved in bulk carries no details at all; it must still render
const noDetails = resultRowHtml({ id: 1, anilist_id: 2, title: 'X', type: 'Manga' }, 1);
ok(!noDetails.includes('undefined') && !noDetails.includes('NaN'),
   'a detail-less row still renders cleanly');

// anime counts, and injection stays escaped
media = 'anime';
const nasty = resultRowHtml({ id: 0, anilist_id: 5, title: '<img src=x onerror=alert(1)>',
  episodes: 12, details: { synonyms: ['<script>'], genres: ['<b>'], description: '" onmouseover="' } }, 2);
ok(nasty.includes('12 eps'), 'anime counts in episodes');
ok(!nasty.includes('<img src=x'), 'the title must be escaped');
ok(!nasty.includes('<script>'), 'synonyms must be escaped');
ok(!nasty.includes('<b>'), 'genres must be escaped');
ok(!nasty.includes('" onmouseover="'), 'the description must be escaped');

console.log('  candidate card  ' + checks + ' assertions');

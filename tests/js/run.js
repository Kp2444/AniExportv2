// Entry point for the browser-behaviour tests:  node tests/js/run.js
//
// Each file runs in its own child process, because they stub different
// globals and eval different slices of the page script; sharing one process
// would let one suite's stubs answer another suite's calls.
const { execFileSync } = require('child_process');
const path = require('path');

const suites = ['test_diff.js', 'test_card.js', 'test_skiplist.js'];
let failed = 0;

console.log('browser behaviour (node ' + process.version + ')');
for (const suite of suites) {
  try {
    process.stdout.write(execFileSync(process.execPath, [path.join(__dirname, suite)],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  } catch (err) {
    failed++;
    console.error('  FAILED ' + suite);
    process.stderr.write(String(err.stdout || '') + String(err.stderr || ''));
  }
}

if (failed) {
  console.error(failed + ' of ' + suites.length + ' suites failed');
  process.exit(1);
}
console.log('all ' + suites.length + ' suites passed');

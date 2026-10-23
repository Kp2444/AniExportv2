// Pull named declarations out of the shipped page script.
//
// These tests run the real source, not a copy: the point is that editing
// static/app/*.js changes what they exercise. An earlier version of this
// suite kept hand-made extracts beside the tests, which could drift from the
// code without anything noticing.
//
// The sections are one IIFE, so every top-level declaration sits at exactly
// four spaces and its closing line is `    }` or `    };`. That indentation is
// the anchor. Anything that cannot be found, or that comes out with unbalanced
// braces, throws rather than silently testing nothing.
const fs = require('fs');
const path = require('path');

const APP_DIR = path.join(__dirname, '..', '..', 'static', 'app');

function sources() {
  return fs.readdirSync(APP_DIR).filter(f => f.endsWith('.js')).sort()
    .map(f => ({ name: f, lines: fs.readFileSync(path.join(APP_DIR, f), 'utf8').split(/\r?\n/) }));
}

function findOne(name) {
  const fnHead = new RegExp('^    function ' + name + '\\s*\\(');
  const constHead = new RegExp('^    const ' + name + '\\s*=');
  for (const file of sources()) {
    for (let i = 0; i < file.lines.length; i++) {
      const line = file.lines[i];
      const isFn = fnHead.test(line);
      const isConst = constHead.test(line);
      if (!isFn && !isConst) continue;
      if (isConst && /;\s*$/.test(line)) return { name, file: file.name, text: line };
      const close = isFn ? '    }' : '    };';
      for (let j = i + 1; j < file.lines.length; j++) {
        if (file.lines[j] === close) {
          const text = file.lines.slice(i, j + 1).join('\n');
          const opens = (text.match(/\{/g) || []).length;
          const closes = (text.match(/\}/g) || []).length;
          if (opens !== closes) {
            throw new Error(`extract: ${name} in ${file.name} has unbalanced braces `
              + `(${opens} open, ${closes} close) - the indentation anchor no longer holds`);
          }
          return { name, file: file.name, text };
        }
      }
      throw new Error(`extract: no closing "${close}" found for ${name} in ${file.name}`);
    }
  }
  throw new Error(`extract: ${name} is not declared at the top level of any static/app/*.js - `
    + `it was renamed, moved or deleted, and this test is no longer testing it`);
}

// Returns the joined source of every name, in the order asked for.
function extract(names) {
  return names.map(findOne).map(hit => hit.text).join('\n\n');
}

module.exports = { extract, findOne };

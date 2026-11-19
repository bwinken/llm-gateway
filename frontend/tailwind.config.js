// Tailwind settings for the web pages. scripts/build_css.sh reads this file and
// writes app/static/css/tailwind.css, the one stylesheet base.html loads.
// How to rebuild after changing a template: docs/frontend-build.md.
module.exports = {
  // Every utility gets !important, as the pages always had (they were built
  // in the browser with this setting before): page <style> blocks must not
  // win over a class written on the element.
  important: true,
  // Where Tailwind looks for class names. Only these files are read, and a
  // class name must appear in them whole: 'text-' + colour is never found.
  // Paths are relative to this file, wherever the build is run from.
  content: {
    relative: true,
    files: ['../app/templates/**/*.html'],
  },
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        n0: '#0c0d12', n1: '#13141a', n2: '#191a22', n3: '#1f2029',
        n4: '#262731', n5: '#2e3040', n6: '#3e4050',
        // Text, brightest to dimmest. t3 (secondary) and t4 (hints, labels) are
        // read on n0..n3: t4 keeps at least 4.5:1 against n2, the card colour
        // (it was #4e5569, 2.3:1, and explanations set in it were hard to read).
        t1: '#eef0f4', t2: '#c4c9d4', t3: '#9aa1b3', t4: '#7b8296',
        ab: '#6398ff', ac: '#4dd9e8', ag: '#3dd68c',
        aa: '#f5a623', ar: '#ff5c5c', av: '#a78bfa',
      },
    },
  },
};

// usage: node measure.js <dir with email.html> [screenshots]
const { chromium } = require('playwright');
const path = require('path');
const dir = path.resolve(process.argv[2]);
const shots = process.argv[3] === 'shots';

(async () => {
  const browser = await chromium.launch();
  const out = {};
  for (const w of [360, 375, 800]) {
    const page = await browser.newPage({ viewport: { width: w, height: 800 }, deviceScaleFactor: 1 });
    await page.goto('file://' + path.join(dir, 'email.html'));
    out[w] = await page.evaluate(() => {
      const de = document.documentElement;
      const r = { scrollWidth: de.scrollWidth, height: de.scrollHeight, screens: +(de.scrollHeight / 800).toFixed(1) };
      // widest elements
      const tables = [...document.querySelectorAll('table.vt, table.mt')];
      r.vtWidths = [...document.querySelectorAll('table.vt')].map(t => Math.round(t.getBoundingClientRect().width));
      r.mtMaxWidth = Math.max(0, ...[...document.querySelectorAll('table.mt')].map(t => Math.round(t.getBoundingClientRect().width)));
      // hide tests
      const vts = [...document.querySelectorAll('table.vt')];
      const widthWith = (hide) => { hide.forEach(t => t.style.display = 'none'); const v = de.scrollWidth; hide.forEach(t => t.style.display = ''); return v; };
      r.widthWithoutPackages = vts.length ? widthWith([vts[0]]) : null;
      r.widthWithoutVacTables = vts.length ? widthWith(vts) : null;
      // section headings: find by text
      const heads = {};
      for (const el of document.querySelectorAll('div,td,h2,h3')) {
        const t = (el.childNodes.length === 1 && el.firstChild.nodeType === 3) ? el.textContent.trim() : '';
        if (['מבצעים כלל-אתריים', 'קריאטין', 'הסימס 4', 'מבצעי בשר בסופרמרקטים', 'דילים לחופשות'].includes(t) && !(t in heads))
          heads[t] = Math.round(el.getBoundingClientRect().top + scrollY);
      }
      r.sectionTop = heads;
      // vacation rows
      r.vt = vts.map(t => {
        const rows = [...t.querySelectorAll('tr')].filter(tr => tr.querySelector('a.go'));
        const hs = rows.map(x => x.getBoundingClientRect().height);
        const first = rows[0];
        return {
          rows: rows.length, total: Math.round(hs.reduce((a, b) => a + b, 0)),
          avg: Math.round(hs.reduce((a, b) => a + b, 0) / (hs.length || 1)), max: Math.round(Math.max(0, ...hs)),
          cols: first ? [...first.children].map(td => Math.round(td.getBoundingClientRect().width)) : [],
          tableHeight: Math.round(t.getBoundingClientRect().height),
        };
      });
      const mts = [...document.querySelectorAll('table.mt')];
      const mrows = mts.flatMap(t => [...t.querySelectorAll('tr')].slice(1));
      const mh = mrows.map(x => x.getBoundingClientRect().height);
      r.mt = { tables: mts.length, rows: mrows.length, avgRow: Math.round(mh.reduce((a, b) => a + b, 0) / (mh.length || 1)),
               total: Math.round(mts.reduce((a, t) => a + t.getBoundingClientRect().height, 0)),
               cols: mrows[0] ? [...mrows[0].children].map(td => Math.round(td.getBoundingClientRect().width)) : [] };
      // font sizes of text-bearing elements
      let n = 0, small = 0, min = 99; const hist = {};
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const seen = new Set();
      while (walker.nextNode()) {
        const node = walker.currentNode; if (!node.textContent.trim()) continue;
        const el = node.parentElement; if (seen.has(el)) continue; seen.add(el);
        const fs = parseFloat(getComputedStyle(el).fontSize); n++; if (fs < 12) small++; if (fs < min) min = fs;
        hist[fs] = (hist[fs] || 0) + 1;
      }
      r.text = { elements: n, below12: small, min, hist };
      const links = [...document.querySelectorAll('a')].map(a => a.getBoundingClientRect()).filter(b => b.width > 0);
      r.links = { count: links.length, under44h: links.filter(b => b.height < 44).length, minH: Math.round(Math.min(...links.map(b => b.height))) };
      const go = document.querySelector('a.go'); if (go) { const b = go.getBoundingClientRect(); r.goButton = [Math.round(b.width), Math.round(b.height)]; }
      r.overflowing = [...document.querySelectorAll('table,div,td')].filter(e => e.getBoundingClientRect().left < -1 || e.getBoundingClientRect().right > innerWidth + 1).length;
      return r;
    });
    if (shots && w !== 360) {
      await page.screenshot({ path: path.join(dir, `full-${w}.png`), fullPage: true });
    }
    await page.close();
  }
  console.log(JSON.stringify(out, null, 1));
  await browser.close();
})();

// The Caddie page: the scorecard from the summary embedded in the page, and a chat that posts the
// conversation to /api/caddie and reads the answer back as NDJSON events (activity, delta, done, error).
// The browser keeps the conversation; the server keeps nothing and never sends the API key here.
// Answers are rendered by the small markdown renderer below: text is HTML-escaped first, and only this
// file's own tags are added, so nothing from the model can inject markup. A link becomes <a> only for an
// http(s) URL (escaped, opened in a new tab); any other link shows as its text. No external scripts.
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const root = $('caddie');
  if (!root) return;
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const MAX_TURNS = Number(root.dataset.maxTurns) || 16;
  const MAX_ANSWER = Number(root.dataset.maxAnswer) || 20000; // what one earlier answer may take in the conversation
  let CARD = {};
  try { CARD = JSON.parse(($('cd-card-data') || {}).textContent || '{}') || {}; } catch (e) { CARD = {}; }

  // ---------------------------------------------------------------- the card
  function fmtDate(iso) {
    if (!iso) return '–';
    const [y, m, d] = String(iso).split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function renderCard() {
    if (CARD.data_through) $('cd-asof').textContent = 'through ' + fmtDate(CARD.data_through);
    const kp = $('cd-kpis');
    kp.textContent = '';
    for (const k of CARD.kpis || []) {
      const lab = el('div', 'lab', k.label);
      if (k.note) lab.append(el('small', null, k.note));
      kp.append(lab, el('div', 'val', k.value == null ? '–' : String(k.value)));
    }
    if (!(CARD.kpis || []).length) kp.append(el('div', 'lab', 'No rounds yet.'), el('div', 'val', ''));

    // sparkline: strokes over par per 9 holes (drawn inverted: better = up) with a 5-round average
    const rs = (CARD.rounds || []).filter((r) => typeof r.to_par_per9 === 'number');
    const svg = $('cd-spark');
    svg.textContent = '';
    if (rs.length >= 2) {
      const W = 300, H = 120, L = 30, R = 8, T = 8, B = 18;
      const ys = rs.map((r) => r.to_par_per9);
      const lo = Math.floor(Math.min(...ys) / 5) * 5;
      const hi = Math.max(Math.ceil(Math.max(...ys) / 5) * 5, lo + 5);
      const x = (i) => L + (W - L - R) * (i / (rs.length - 1));
      const y = (v) => T + (H - T - B) * (v - lo) / (hi - lo);
      const ns = 'http://www.w3.org/2000/svg';
      const mk = (t, a) => { const n = document.createElementNS(ns, t); for (const k in a) n.setAttribute(k, a[k]); return n; };
      for (const v of [lo, (lo + hi) / 2, hi]) {
        svg.append(mk('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: 'var(--cd-line)', 'stroke-width': 1 }));
        const t = mk('text', { x: L - 6, y: y(v) + 4, 'text-anchor': 'end' });
        t.textContent = (v > 0 ? '+' : '') + Math.round(v);
        svg.append(t);
      }
      const avg = rs.map((r, i) => {
        const w = rs.slice(Math.max(0, i - 4), i + 1).map((q) => q.to_par_per9);
        return w.reduce((a, b) => a + b, 0) / w.length;
      });
      svg.append(mk('polyline', { points: avg.map((v, i) => x(i) + ',' + y(v)).join(' '), fill: 'none',
        stroke: 'var(--cd-flag)', 'stroke-width': 2.5, 'stroke-linejoin': 'round' }));
      rs.forEach((r, i) => {
        const c = mk('circle', { cx: x(i), cy: y(r.to_par_per9), r: r.holes === 18 ? 4 : 3.2, fill: 'var(--cd-fairway)' });
        const tt = mk('title', {});
        tt.textContent = `${fmtDate(r.date)} · ${r.course || ''} · ${r.gross ?? ''} (${r.to_par > 0 ? '+' : ''}${r.to_par ?? ''})`;
        c.append(tt);
        svg.append(c);
      });
      const t1 = mk('text', { x: L, y: H - 3 }); t1.textContent = fmtDate(rs[0].date);
      const t2 = mk('text', { x: W - R, y: H - 3, 'text-anchor': 'end' }); t2.textContent = fmtDate(rs[rs.length - 1].date);
      svg.append(t1, t2);
    }

    const tb = $('cd-yardage').tBodies[0];
    tb.textContent = '';
    for (const c of CARD.club_medians || []) {
      if (c.median == null) continue;
      const useLive = (c.n_live || 0) >= 3 && c.median_live != null;
      const tr = el('tr');
      tr.append(el('td', null, c.club), el('td', useLive ? 'live' : null, Math.round(useLive ? c.median_live : c.median) + ' yd'),
        el('td', null, String(c.n)));
      tb.append(tr);
    }
    if (!tb.rows.length) {
      const tr = el('tr'); const td = el('td', null, 'No tracked shots yet.'); td.colSpan = 3; tr.append(td); tb.append(tr);
    }
  }

  // ---------------------------------------------------------------- markdown (escape first, then our tags)
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

  function emph(t) { // t is already escaped
    return t
      .replace(/\*\*(?=\S)([^\n]*?\S)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^\w])__(?=\S)([^\n]*?\S)__(?!\w)/g, '$1<strong>$2</strong>')
      .replace(/(^|[^\w*])\*(?=[^\s*])([^*\n]*?[^\s*])?\*(?![\w*])/g, (m, pre, body) => (body === undefined ? m : pre + '<em>' + body + '</em>'))
      .replace(/(^|[^\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/g, '$1<em>$2</em>');
  }

  // [text](url): only http(s) URLs become links; the URL is escaped into a quoted attribute.
  const LINK = /\[([^\]\n]+)\]\(\s*<?(https?:\/\/[^\s<>()]+)>?\s*\)/g;
  const OTHER_LINK = /\[([^\]\n]+)\]\((?:[^()\n]|\([^()\n]*\))*\)/g; // shown as its text only

  function prose(s) { // raw text outside code spans -> escaped HTML with emphasis and links
    let out = '', last = 0, m;
    LINK.lastIndex = 0;
    while ((m = LINK.exec(s))) {
      out += emph(esc(s.slice(last, m.index).replace(OTHER_LINK, '$1')));
      out += '<a href="' + esc(m[2]) + '" target="_blank" rel="noopener noreferrer">' + emph(esc(m[1])) + '</a>';
      last = m.index + m[0].length;
    }
    return out + emph(esc(s.slice(last).replace(OTHER_LINK, '$1')));
  }

  function inline(s) { // code spans first, so their contents are never formatted
    return String(s).split(/(`[^`\n]+`)/g)
      .map((p, i) => (i % 2 ? '<code>' + esc(p.slice(1, -1)) + '</code>' : prose(p)))
      .join('');
  }

  function align(sep) { // the |:--|--:|:-:| row -> '', 'r' or 'c' per column
    return splitRow(sep).map((c) => (/^:-+:$/.test(c) ? 'c' : /^-+:$/.test(c) ? 'r' : ''));
  }
  const cell = (tag, cls, html) => '<' + tag + (cls ? ' class="' + cls + '"' : '') + '>' + html + '</' + tag + '>';

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|')) s = s.slice(0, -1);
    return s.split('|').map((c) => c.trim());
  }

  const LIST = /^\s*([-*+]|\d{1,3}[.)])\s+(.*)$/;
  const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function markdown(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = [];
    const flush = () => { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      let m;
      if (/^\s*```/.test(line)) {
        flush();
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      if (!line.trim()) { flush(); continue; }
      if ((m = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) {
        flush();
        const lvl = m[1].length <= 2 ? 3 : 4;
        out.push(`<h${lvl}>` + inline(m[2]) + `</h${lvl}>`);
        continue;
      }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push('<hr>'); continue; }
      if (line.includes('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes('-')) {
        flush();
        const head = splitRow(line);
        const al = align(lines[i + 1]);
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(splitRow(lines[i++]));
        i--;
        out.push('<div class="tbl"><table><thead><tr>' + head.map((c, k) => cell('th', al[k], inline(c))).join('') +
          '</tr></thead><tbody>' + rows.map((r) => '<tr>' + head.map((_, k) => cell('td', al[k], inline(r[k] || ''))).join('') +
          '</tr>').join('') + '</tbody></table></div>');
        continue;
      }
      if ((m = LIST.exec(line))) {
        flush();
        const ordered = /\d/.test(m[1]);
        const items = [m[2]];
        while (i + 1 < lines.length) {
          const next = lines[i + 1];
          const n = LIST.exec(next);
          if (n && /\d/.test(n[1]) === ordered) { items.push(n[2]); i++; }
          else if (!n && next.trim() && /^\s{2,}\S/.test(next)) { items[items.length - 1] += ' ' + next.trim(); i++; }
          else break;
        }
        const tag = ordered ? 'ol' : 'ul';
        out.push(`<${tag}>` + items.map((t) => '<li>' + inline(t) + '</li>').join('') + `</${tag}>`);
        continue;
      }
      if ((m = /^\s*>\s?(.*)$/.exec(line))) {
        flush();
        const q = [m[1]];
        while (i + 1 < lines.length && /^\s*>/.test(lines[i + 1])) q.push(lines[++i].replace(/^\s*>\s?/, ''));
        out.push('<blockquote>' + inline(q.join(' ')) + '</blockquote>');
        continue;
      }
      para.push(line.trim());
    }
    flush();
    return out.join('');
  }
  window.caddieMarkdown = markdown; // for poking at in the console

  // ---------------------------------------------------------------- chat
  const thread = $('cd-thread'), intro = $('cd-intro'), q = $('cd-q'), send = $('cd-send'), form = $('cd-form');
  const turns = []; // [{role, content}]: the conversation lives here, in this tab only
  let busy = false, ctl = null;
  const smooth = () => (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth');
  const scrollEnd = (node) => node.scrollIntoView({ block: 'end', behavior: smooth() });

  function addUser(text) { const m = el('div', 'cd-msg user', text); thread.append(m); scrollEnd(m); return m; }
  function addBot() {
    const m = el('div', 'cd-msg bot');
    const act = el('div', 'cd-activity'); act.hidden = true;
    const ans = el('div', 'cd-answer'); ans.append(el('span', 'cd-thinking', 'Thinking…'));
    const note = el('div', 'cd-note'); note.hidden = true;
    m.append(el('div', 'cd-who', 'Caddie'), act, ans, note);
    thread.append(m);
    scrollEnd(m);
    return { m, act, ans, note };
  }
  function setBusy(b) {
    busy = b;
    send.textContent = b ? 'Stop' : 'Ask';
    send.classList.toggle('stop', b);
    send.disabled = !b && !q.value.trim();
    for (const c of document.querySelectorAll('.cd-chip')) c.disabled = b;
  }
  function warn(view, text) {
    view.ans.textContent = '';
    view.note.hidden = false;
    view.note.classList.add('warn');
    view.note.textContent = text;
  }

  async function ask(text) {
    text = (text || '').trim();
    if (!text || busy) return;
    intro.hidden = true;
    addUser(text);
    turns.push({ role: 'user', content: text });
    while (turns.length > MAX_TURNS) turns.splice(0, 2);
    const view = addBot();
    ctl = new AbortController();
    setBusy(true);
    let answer = '', ok = false, failed = false, garbled = false;

    const handle = (ev) => {
      if (!ev || typeof ev !== 'object') return;
      if (ev.type === 'activity') {
        view.act.hidden = false;
        view.act.append(el('span', null, String(ev.text || '')));
      } else if (ev.type === 'delta') {
        answer += String(ev.text || '');
        view.ans.innerHTML = markdown(answer);
        view.m.scrollIntoView({ block: 'start', behavior: smooth() }); // read a long answer from its top
      } else if (ev.type === 'done') {
        ok = true;
        const bits = [ev.model, ev.lookups ? ev.lookups + (ev.lookups === 1 ? ' lookup' : ' lookups') : null];
        view.note.hidden = false;
        view.note.textContent = (ev.truncated ? 'The answer was cut short; ask for less at a time. ' : '') +
          bits.filter(Boolean).join(' · ');
      } else if (ev.type === 'error') {
        failed = true;
        const banner = $('cd-keybanner');
        warn(view, ev.code === 'no_key' && banner && !banner.hidden
          ? 'Add your Muse Spark key first: the steps are at the top of this page.'
          : String(ev.message || 'The Caddie could not answer. Ask again.'));
      }
    };
    const line = (text) => { // one NDJSON line; a line that isn't JSON is noted, not fatal
      let ev;
      try { ev = JSON.parse(text); } catch (e) { garbled = true; return; }
      handle(ev);
    };

    try {
      const res = await fetch('/api/caddie', {
        method: 'POST', signal: ctl.signal, credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', Accept: 'application/x-ndjson' },
        body: JSON.stringify({ messages: turns }),
      });
      if (!res.ok || !res.body) {
        let msg = 'The Caddie could not take that question (' + res.status + ').';
        try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) { /* not JSON */ }
        failed = true;
        warn(view, msg);
      } else {
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let nl;
          while ((nl = buf.indexOf('\n')) >= 0) {
            const text = buf.slice(0, nl).trim();
            buf = buf.slice(nl + 1);
            if (text) line(text);
          }
        }
        if (buf.trim()) line(buf.trim());
        if (!ok && !failed) {
          failed = true;
          warn(view, garbled ? "The app's reply couldn't be read. Ask again; if it keeps happening, restart golf serve."
            : 'The answer stopped before it finished. Ask again.');
        }
      }
    } catch (e) {
      if (e && e.name === 'AbortError') {
        if (!answer) view.ans.textContent = '';
        view.note.hidden = false;
        view.note.textContent = 'Stopped.';
      } else {
        failed = true;
        warn(view, 'Lost the connection to the app. Is golf serve still running? Then ask again.');
      }
    } finally {
      // An earlier answer goes back with the next question; a very long one is cut to its start (the server
      // would cut it anyway), so one long answer never blocks the conversation.
      if (ok && answer) turns.push({ role: 'assistant', content: answer.length > MAX_ANSWER ? answer.slice(0, MAX_ANSWER - 100) + '\n\n[...cut]' : answer });
      else turns.pop(); // the question stays on screen but leaves the conversation the Caddie sees
      ctl = null;
      setBusy(false);
      if (!ok) scrollEnd(view.m);
    }
  }

  q.addEventListener('input', () => {
    q.style.height = 'auto';
    q.style.height = Math.min(q.scrollHeight, 180) + 'px';
    if (!busy) send.disabled = !q.value.trim();
  });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && busy && ctl) { e.preventDefault(); ctl.abort(); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!busy) form.requestSubmit(); // while an answer loads, Enter keeps the next question in the box
    }
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (busy) { if (ctl && e.submitter === send) ctl.abort(); return; } // only the Stop button (or Esc) stops
    const text = q.value;
    q.value = '';
    q.style.height = 'auto';
    ask(text);
  });
  $('cd-clear').addEventListener('click', () => {
    if (busy && ctl) ctl.abort();
    turns.length = 0;
    for (const m of [...thread.querySelectorAll('.cd-msg')]) m.remove();
    intro.hidden = false;
    q.focus();
  });
  for (const c of document.querySelectorAll('.cd-chip')) c.addEventListener('click', () => ask(c.textContent));

  renderCard();
  setBusy(false);
})();

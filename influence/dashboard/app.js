(function () {
  const money = (v) => v == null || isNaN(v) ? '—' : '$' + new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(v);
  const pct0 = d3.format('.0%');
  const pct1 = d3.format('.1%');
  const growth = (v) => v == null || isNaN(v) ? '—' : (v >= 0 ? '+' : '') + d3.format('.0%')(v);
  const int = d3.format(',');
  const n = (x) => (x === '' || x == null) ? null : Number(x);
  const C = () => dash.colors;
  const REGION = { US: 0, Europe: 1 };
  const PAGES = ['overview', 'venues', 'outcomes', 'companies', 'method'];
  let page = 'overview';
  let selected = null;
  let sortKey = 'us_lobby_usd', sortDir = -1;

  // ---- computed headline figures
  dash.calc('headline', {
    inputs: ['profile', 'trend', 'panel', 'reach'],
    title: 'Headline figures',
    description: 'Totals for 2024 from the company-year panel, EU shares and tech growth from the US–EU trend, and cross-border totals from the venue summary.',
    fn: (profile, trend, panel, reach) => {
      const sum = (rows, f) => rows.reduce((a, r) => a + (Number(r[f]) || 0), 0);
      const y24 = panel.filter(r => String(r.year) === '2024');
      const t = (g, y) => trend.find(r => r.group === g && String(r.year) === String(y)) || {};
      const grow = (g, f) => Number(t(g, 2016)[f]) ? Number(t(g, 2024)[f]) / Number(t(g, 2016)[f]) - 1 : null;
      const us = reach.find(r => r.hq_region === 'US') || {};
      const eu = reach.find(r => r.hq_region === 'Europe') || {};
      return [{
        n_companies: profile.length,
        n_both: profile.filter(r => Number(r.us_lobby_usd) > 0 && Number(r.eu_lobby_usd) > 0).length,
        us_lobby_2024: sum(y24, 'us_lobby_usd'),
        eu_lobby_2024: sum(y24, 'eu_lobby_usd'),
        ec_meetings_2024: sum(y24, 'ec_meetings'),
        eu_share_2016: Number(t('All', 2016).eu_share),
        eu_share_2024: Number(t('All', 2024).eu_share),
        tech_eu_growth: grow('Technology', 'eu_lobby_usd'),
        tech_us_growth: grow('Technology', 'us_lobby_usd'),
        us_hq_eu_lobby: Number(us.eu_lobby_usd),
        eu_hq_us_lobby: Number(eu.us_lobby_usd),
        us_hq_eu_growth: grow('US', 'eu_lobby_usd'),
        eu_hq_us_growth: grow('Europe', 'us_lobby_usd'),
      }];
    }
  });

  const FORMAT = {
    us_lobby_2024: money, eu_lobby_2024: money, us_hq_eu_lobby: money, eu_hq_us_lobby: money,
    ec_meetings_2024: int, n_both: int, n_companies: int, n: int,
    eu_share_2016: pct1, eu_share_2024: pct1,
    tech_eu_growth: growth, tech_us_growth: growth, us_hq_eu_growth: growth, eu_hq_us_growth: growth,
    spearman_rho: d3.format('.2f'), within_sector_rho: d3.format('.2f'),
  };

  // ---- helpers
  function status(id) { return dash.data(id).status; }
  function clear(el) { el.querySelectorAll('svg, .ci-tip, .ci-fail, .ci-small-inner').forEach(x => x.remove()); }
  function fail(el, st) {
    if (st === 'loading') { el.classList.add('dash-skeleton'); return true; }
    el.classList.remove('dash-skeleton');
    if (st !== 'ok') { const f = document.createElement('div'); f.className = 'ci-fail'; f.textContent = 'Failed to load'; el.appendChild(f); return true; }
    return false;
  }
  function svgIn(el, W, H, label) {
    return d3.select(el).append('svg').attr('width', '100%').attr('viewBox', `0 0 ${W} ${H}`).attr('role', 'img').attr('aria-label', label);
  }
  function tip(el) { const t = document.createElement('div'); t.className = 'ci-tip'; t.hidden = true; el.appendChild(t); return t; }
  function showTip(el, t, head, rows, px, py) {
    t.replaceChildren();
    const h = document.createElement('div'); h.className = 'ci-tip-h'; h.textContent = head; t.appendChild(h);
    rows.forEach(([k, v, color]) => {
      const r = document.createElement('div'); r.className = 'ci-row';
      const a = document.createElement('span'); a.style.display = 'inline-flex'; a.style.alignItems = 'center'; a.style.gap = '6px';
      if (color) { const sw = document.createElement('i'); sw.className = 'ci-sw'; sw.style.background = color; a.appendChild(sw); }
      a.appendChild(document.createTextNode(k));
      const b = document.createElement('span'); b.textContent = v;
      r.append(a, b); t.appendChild(r);
    });
    t.hidden = false;
    const W = el.clientWidth;
    let x = px + 14; if (x + t.offsetWidth > W) x = px - t.offsetWidth - 14; if (x < 0) x = 0;
    t.style.left = x + 'px'; t.style.top = Math.max(0, py - t.offsetHeight / 2) + 'px';
  }
  function legend(id, items) {
    const box = document.getElementById(id); if (!box) return;
    box.replaceChildren(...items.map(([label, color, line]) => {
      const s = document.createElement('span'); const sw = document.createElement('i');
      sw.className = 'ci-sw' + (line ? ' ci-line' : ''); sw.style.background = color;
      s.append(sw, document.createTextNode(label)); return s;
    }));
  }
  function grid(svg, y, x0, x1, ticks, fmt) {
    ticks.forEach(v => {
      svg.append('line').attr('x1', x0).attr('x2', x1).attr('y1', y(v)).attr('y2', y(v)).attr('stroke', 'var(--cds-chart-grid)');
      svg.append('text').attr('x', x0 - 6).attr('y', y(v)).attr('dy', '0.32em').attr('text-anchor', 'end').text(fmt(v));
    });
  }
  function ratio(el, svgW) { return el.clientWidth / svgW; }

  // ---- text marks
  function fillMarks() {
    document.querySelectorAll('.ci [data-source][data-field]:not(th):not(table):not(dl):not(dd)').forEach(el => {
      const d = dash.data(el.dataset.source);
      if (d.status !== 'ok') { el.textContent = '—'; if (d.status !== 'loading') el.classList.remove('dash-skeleton'); return; }
      el.classList.remove('dash-skeleton');
      let row;
      if (el.dataset.where) {
        const w = new URLSearchParams(el.dataset.where);
        row = d.data.find(r => Array.from(w.entries()).every(([k, v]) => String(r[k]) === v));
      } else row = d.data[Number(el.dataset.row || 0)];
      const v = row ? row[el.dataset.field] : null;
      const f = FORMAT[el.dataset.field];
      el.textContent = v == null || v === '' ? '—' : f ? f(Number(v)) : v;
    });
  }

  // ---- Overview: venue bars
  function drawReach() {
    const el = document.getElementById('chart-reach'); clear(el);
    legend('legend-reach', [['US headquarters', C()[0]], ['European headquarters', C()[1]]]);
    const d = dash.data('reach'); if (fail(el, d.status)) return;
    const venues = [['US federal', 'us_lobby_usd'], ['EU', 'eu_lobby_usd'], ['Germany', 'de_lobby_usd'], ['France', 'fr_lobby_usd']];
    const rows = ['US', 'Europe'].map(r => d.data.find(x => x.hq_region === r) || {});
    const W = el.clientWidth || 600, bh = 12, gap = 18, m = { top: 6, right: 70, bottom: 22, left: 86 };
    const H = m.top + venues.length * (bh * 2 + gap) + m.bottom;
    const max = d3.max(venues, ([, f]) => d3.max(rows, r => Number(r[f]) || 0)) || 1;
    const x = d3.scaleLinear([0, max], [m.left, W - m.right]).nice();
    const svg = svgIn(el, W, H, 'Lobbying by venue and headquarters region');
    x.ticks(4).forEach(v => {
      svg.append('line').attr('x1', x(v)).attr('x2', x(v)).attr('y1', m.top).attr('y2', H - m.bottom).attr('stroke', 'var(--cds-chart-grid)');
      svg.append('text').attr('x', x(v)).attr('y', H - 6).attr('text-anchor', 'middle').text(money(v));
    });
    venues.forEach(([label, f], i) => {
      const y0 = m.top + i * (bh * 2 + gap);
      svg.append('text').attr('x', m.left - 8).attr('y', y0 + bh).attr('dy', '0.32em').attr('text-anchor', 'end').attr('class', 'ci-strong').text(label);
      rows.forEach((r, j) => {
        const v = Number(r[f]) || 0, y = y0 + j * (bh + 2);
        svg.append('rect').attr('x', x(0)).attr('y', y).attr('width', Math.max(1, x(v) - x(0))).attr('height', bh).attr('rx', 3).attr('fill', C()[j]);
        svg.append('text').attr('x', x(v) + 6).attr('y', y + bh / 2).attr('dy', '0.32em').text(money(v));
      });
    });
  }

  // ---- Venues
  function drawShare() {
    const el = document.getElementById('chart-share'); clear(el);
    const groups = [['All companies', 'All', 'var(--color-fg)'], ['US headquarters', 'US', C()[0]], ['European headquarters', 'Europe', C()[1]]];
    legend('legend-share', groups.map(([l, , c]) => [l, c, true]));
    const d = dash.data('trend'); if (fail(el, d.status)) return;
    const series = groups.map(([l, g, c]) => ({ l, c, pts: d.data.filter(r => r.group === g).map(r => ({ y: +r.year, v: +r.eu_share, us: +r.us_lobby_usd, eu: +r.eu_lobby_usd })).sort((a, b) => a.y - b.y) }));
    const years = series[0].pts.map(p => p.y);
    const W = el.clientWidth || 600, H = 320, m = { top: 12, right: 16, bottom: 26, left: 40 };
    const x = d3.scaleLinear(d3.extent(years), [m.left, W - m.right]);
    const y = d3.scaleLinear([0, 0.6], [H - m.bottom, m.top]);
    const svg = svgIn(el, W, H, 'EU share of US plus EU lobbying over time');
    grid(svg, y, m.left, W - m.right, [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6], pct0);
    years.forEach((yr, i) => { if (W > 500 || i % 2 === 0) svg.append('text').attr('x', x(yr)).attr('y', H - 8).attr('text-anchor', 'middle').text(yr); });
    series.forEach(s => {
      svg.append('path').attr('d', d3.line().x(p => x(p.y)).y(p => y(p.v))(s.pts)).attr('fill', 'none').attr('stroke', s.c).attr('stroke-width', 2).attr('stroke-linecap', 'round').attr('stroke-linejoin', 'round');
      const last = s.pts[s.pts.length - 1];
      svg.append('circle').attr('cx', x(last.y)).attr('cy', y(last.v)).attr('r', 3.5).attr('fill', s.c);
    });
    const t = tip(el), rule = svg.append('line').attr('y1', m.top).attr('y2', H - m.bottom).attr('stroke', 'var(--cds-chart-axis)').attr('opacity', 0);
    svg.append('rect').attr('x', m.left).attr('y', m.top).attr('width', W - m.left - m.right).attr('height', H - m.top - m.bottom).attr('fill', 'transparent')
      .on('mousemove', ev => {
        const [px, py] = d3.pointer(ev); const yr = Math.round(x.invert(px));
        rule.attr('x1', x(yr)).attr('x2', x(yr)).attr('opacity', 1);
        showTip(el, t, String(yr), series.map(s => { const p = s.pts.find(q => q.y === yr) || {}; return [s.l, pct1(p.v), s.c]; }), x(yr) * ratio(el, W), py * ratio(el, W));
      }).on('mouseleave', () => { t.hidden = true; rule.attr('opacity', 0); });
  }

  function drawSectors() {
    const el = document.getElementById('chart-sectors'); el.replaceChildren();
    const d = dash.data('trend'); if (d.status !== 'ok') { el.classList.toggle('dash-skeleton', d.status === 'loading'); return; }
    el.classList.remove('dash-skeleton');
    const skip = new Set(['All', 'US', 'Europe']);
    const sectors = Array.from(new Set(d.data.map(r => r.group))).filter(g => !skip.has(g));
    const rows = sectors.map(g => ({ g, pts: d.data.filter(r => r.group === g).map(r => ({ y: +r.year, v: +r.eu_share })).sort((a, b) => a.y - b.y) }))
      .filter(s => s.pts.length).sort((a, b) => (b.pts[b.pts.length - 1].v - b.pts[0].v) - (a.pts[a.pts.length - 1].v - a.pts[0].v));
    rows.forEach(s => {
      const box = document.createElement('div'); box.className = 'ci-small';
      const h = document.createElement('h3'); const first = s.pts[0].v, last = s.pts[s.pts.length - 1].v;
      h.textContent = `${s.g}: ${pct0(first)} → ${pct0(last)}`; box.appendChild(h);
      el.appendChild(box);
      const W = box.clientWidth || 240, H = 120, m = { top: 6, right: 8, bottom: 18, left: 30 };
      const x = d3.scaleLinear(d3.extent(s.pts, p => p.y), [m.left, W - m.right]);
      const y = d3.scaleLinear([0, 0.8], [H - m.bottom, m.top]);
      const svg = svgIn(box, W, H, `EU share for ${s.g}`);
      grid(svg, y, m.left, W - m.right, [0, 0.4, 0.8], pct0);
      [s.pts[0].y, s.pts[s.pts.length - 1].y].forEach(yr => svg.append('text').attr('x', x(yr)).attr('y', H - 4).attr('text-anchor', 'middle').text(yr));
      const col = last > first ? C()[1] : 'var(--cds-chart-muted)';
      svg.append('path').attr('d', d3.line().x(p => x(p.y)).y(p => y(p.v))(s.pts)).attr('fill', 'none').attr('stroke', col).attr('stroke-width', 2).attr('stroke-linejoin', 'round');
      svg.append('circle').attr('cx', x(s.pts[s.pts.length - 1].y)).attr('cy', y(last)).attr('r', 3).attr('fill', col);
    });
  }

  function drawMovers() {
    const el = document.getElementById('chart-movers'); clear(el);
    const d = dash.data('shift'); if (fail(el, d.status)) return;
    const prof = dash.data('profile').status === 'ok' ? dash.data('profile').data : [];
    const name = id => (prof.find(p => p.company_id === id) || {}).name || id;
    const all = d.data.map(r => ({ id: r.company_id, a: +r.eu_share_2016_17, b: +r.eu_share_2023_24, c: +r.change, region: r.hq_region }))
      .sort((p, q) => q.c - p.c);
    const pick = all.slice(0, 12).concat(all.slice(-12));
    const W = el.clientWidth || 600, rowH = 17, m = { top: 18, right: 20, bottom: 24, left: 150 };
    const H = m.top + pick.length * rowH + 14 + m.bottom;
    const x = d3.scaleLinear([0, 1], [m.left, W - m.right]);
    const svg = svgIn(el, W, H, 'Companies whose EU share of lobbying changed most');
    [0, 0.25, 0.5, 0.75, 1].forEach(v => {
      svg.append('line').attr('x1', x(v)).attr('x2', x(v)).attr('y1', m.top).attr('y2', H - m.bottom).attr('stroke', 'var(--cds-chart-grid)');
      svg.append('text').attr('x', x(v)).attr('y', H - 6).attr('text-anchor', 'middle').text(pct0(v));
    });
    svg.append('text').attr('x', m.left).attr('y', 10).attr('class', 'ci-strong').text('Moved toward Brussels');
    svg.append('text').attr('x', m.left).attr('y', m.top + 12 * rowH + 12).attr('class', 'ci-strong').text('Moved toward Washington');
    const t = tip(el);
    pick.forEach((r, i) => {
      const y = m.top + i * rowH + (i >= 12 ? 14 : 0) + rowH / 2;
      const col = C()[REGION[r.region] ?? 0];
      svg.append('text').attr('x', m.left - 8).attr('y', y).attr('dy', '0.32em').attr('text-anchor', 'end').text(name(r.id));
      svg.append('line').attr('x1', x(r.a)).attr('x2', x(r.b)).attr('y1', y).attr('y2', y).attr('stroke', col).attr('stroke-width', 2).attr('opacity', 0.6);
      svg.append('circle').attr('cx', x(r.a)).attr('cy', y).attr('r', 4).attr('fill', 'var(--color-panel)').attr('stroke', col).attr('stroke-width', 1.5);
      svg.append('circle').attr('cx', x(r.b)).attr('cy', y).attr('r', 4).attr('fill', col);
      svg.append('rect').attr('x', 0).attr('y', y - rowH / 2).attr('width', W).attr('height', rowH).attr('fill', 'transparent')
        .on('mousemove', ev => { const [px, py] = d3.pointer(ev, el); showTip(el, t, name(r.id), [['2016–17', pct1(r.a)], ['2023–24', pct1(r.b)], ['Change', (r.c >= 0 ? '+' : '') + d3.format('.1f')(r.c * 100) + ' pts']], px, py); })
        .on('mouseleave', () => { t.hidden = true; });
    });
  }

  // ---- Outcomes: scatters
  function scatter(elId, { xf, yf, xlog, ylog, xfmt, yfmt, filter, label, yDomain, ref, xTitle, yTitle }) {
    const el = document.getElementById(elId); clear(el);
    const d = dash.data('profile'); if (fail(el, d.status)) return;
    let pts = d.data.filter(r => n(r[xf]) != null && n(r[yf]) != null && (!filter || filter(r)))
      .map(r => ({ r, x: n(r[xf]), y: n(r[yf]) }));
    if (xlog) pts = pts.filter(p => p.x > 0);
    if (pts.length < 3) {
      const f = document.createElement('div'); f.className = 'ci-fail'; f.textContent = 'Not enough data yet'; el.appendChild(f); return;
    }
    const W = el.clientWidth || 500, H = 320, m = { top: 12, right: 14, bottom: 40, left: 54 };
    const posY = pts.filter(p => p.y > 0).map(p => p.y);
    const yMin = ylog ? (d3.min(posY) || 1) / 2 : null;
    const x = xlog ? d3.scaleLog(d3.extent(pts, p => p.x), [m.left, W - m.right]).nice() : d3.scaleLinear(d3.extent(pts, p => p.x), [m.left, W - m.right]).nice();
    const y = ylog ? d3.scaleLog([yMin, d3.max(posY) * 1.2], [H - m.bottom, m.top]).clamp(true) : d3.scaleLinear(yDomain || d3.extent(pts, p => p.y), [H - m.bottom, m.top]).nice();
    const svg = svgIn(el, W, H, label);
    const yt = ylog ? y.ticks(4).filter((v, i, a) => a.length < 6 || i % 2 === 0) : y.ticks(5);
    grid(svg, y, m.left, W - m.right, yt, yfmt);
    const xt = xlog ? x.ticks(4).filter(v => Math.log10(v) % 1 === 0) : x.ticks(5);
    xt.forEach(v => svg.append('text').attr('x', x(v)).attr('y', H - m.bottom + 14).attr('text-anchor', 'middle').text(xfmt(v)));
    svg.append('text').attr('x', (m.left + W - m.right) / 2).attr('y', H - 4).attr('text-anchor', 'middle').text(xTitle);
    svg.append('text').attr('transform', `translate(12,${(m.top + H - m.bottom) / 2}) rotate(-90)`).attr('text-anchor', 'middle').text(yTitle);
    if (ref != null) svg.append('line').attr('x1', m.left).attr('x2', W - m.right).attr('y1', y(ref)).attr('y2', y(ref)).attr('stroke', 'var(--cds-chart-reference)').attr('stroke-dasharray', '4 4');
    const t = tip(el);
    svg.append('g').selectAll('circle').data(pts).join('circle')
      .attr('cx', p => x(p.x)).attr('cy', p => y(ylog && p.y <= 0 ? yMin : p.y)).attr('r', 4.5)
      .attr('fill', p => ylog && p.y <= 0 ? 'var(--color-panel)' : C()[REGION[p.r.hq_region] ?? 0])
      .attr('stroke', p => C()[REGION[p.r.hq_region] ?? 0]).attr('stroke-width', 1.5).attr('fill-opacity', 0.75)
      .style('cursor', 'pointer')
      .on('mousemove', (ev, p) => { const [px, py] = d3.pointer(ev, el); showTip(el, t, p.r.name, [[p.r.sector, ''], [xTitle, xfmt(p.x)], [yTitle, yfmt(p.y)]], px, py); })
      .on('mouseleave', () => { t.hidden = true; })
      .on('click', (ev, p) => { selected = p.r.company_id; show('companies'); });
  }
  function drawOutcomes() {
    legend('legend-sc', [['US headquarters', C()[0]], ['European headquarters', C()[1]]]);
    const nonExtr = r => r.sector !== 'Energy' && r.sector !== 'Mining';
    scatter('chart-sc-tax', { xf: 'lobby_per_bn_revenue_usd', yf: 'book_etr', xlog: true, xfmt: money, yfmt: pct0, filter: nonExtr, yDomain: [-0.1, 0.45], ref: null,
      label: 'Lobbying per billion of revenue against book tax rate', xTitle: 'Lobbying per $1bn revenue', yTitle: 'Book tax rate' });
    scatter('chart-sc-havens', { xf: 'lobby_per_bn_revenue_usd', yf: 'havens_per_bn_revenue', xlog: true, ylog: true, xfmt: money, yfmt: d3.format('.2~f'),
      filter: r => r.subsidiary_low_coverage !== 'True' && r.subsidiary_low_coverage !== '1',
      label: 'Lobbying per billion of revenue against tax-haven subsidiaries per billion', xTitle: 'Lobbying per $1bn revenue', yTitle: 'Haven subsidiaries per $1bn' });
    scatter('chart-sc-contracts', { xf: 'us_lobby_usd', yf: 'us_contracts_avg_usd', xlog: true, ylog: true, xfmt: money, yfmt: money,
      label: 'US lobbying against federal contracts', xTitle: 'US lobbying per year', yTitle: 'Federal contracts per year' });
    scatter('chart-sc-pac', { xf: 'pac_2020_2024_usd', yf: 'pac_share_rep', xlog: true, xfmt: money, yfmt: pct0, yDomain: [0, 1], ref: 0.5,
      filter: r => n(r.pac_2020_2024_usd) > 0,
      label: 'PAC giving against Republican share', xTitle: 'PAC giving 2020–2024', yTitle: 'Share to Republicans' });
    drawCorr();
  }
  function drawCorr() {
    const tb = document.querySelector('#table-corr tbody'); tb.replaceChildren();
    const d = dash.data('corr'); if (d.status !== 'ok') return;
    d.data.forEach(r => {
      const tr = document.createElement('tr'); tr.dataset.key = `${r.x},${r.y}`; tr.style.cursor = 'default';
      const pill = (rho, p) => {
        const td = document.createElement('td');
        if (rho === '' || rho == null) { td.textContent = '—'; return td; }
        const s = document.createElement('span'); s.className = 'ci-pill' + (+p < 0.01 ? ' ci-sig' : +p < 0.05 ? ' ci-weak' : '');
        s.textContent = d3.format('+.2f')(+rho); td.appendChild(s); return td;
      };
      const td0 = document.createElement('td'); td0.textContent = r.test + (r.note ? ` (${r.note})` : '');
      const td1 = document.createElement('td'); td1.textContent = r.n;
      const tp = document.createElement('td'); tp.textContent = r.p_value === '' ? '—' : d3.format('.3f')(+r.p_value);
      const tps = document.createElement('td'); tps.textContent = r.within_sector_p === '' ? '—' : d3.format('.3f')(+r.within_sector_p);
      tr.append(td0, td1, pill(r.spearman_rho, r.p_value), tp, pill(r.within_sector_rho, r.within_sector_p), tps);
      tb.appendChild(tr);
    });
  }

  // ---- Companies
  const sectorSel = document.getElementById('ci-sector');
  const regionSel = document.getElementById('ci-region');
  const search = document.getElementById('ci-search');
  [sectorSel, regionSel].forEach(s => s.addEventListener('change', drawCompanies));
  search.addEventListener('input', drawCompanies);
  document.querySelectorAll('#table-companies th button').forEach(b => b.addEventListener('click', () => {
    const k = b.dataset.sort; sortDir = sortKey === k ? -sortDir : (k === 'name' || k === 'sector' ? 1 : -1); sortKey = k; drawCompanies();
  }));
  document.querySelector('#table-companies tbody').addEventListener('click', ev => {
    const tr = ev.target.closest('tr[data-key]'); if (!tr) return; selected = tr.dataset.key; drawCompanies(); drawDetail();
    document.getElementById('company-detail').scrollIntoView({ block: 'nearest' });
  });

  function drawCompanies() {
    const tb = document.querySelector('#table-companies tbody');
    const d = dash.data('profile'); tb.replaceChildren(); if (d.status !== 'ok') return;
    if (sectorSel.options.length === 1) Array.from(new Set(d.data.map(r => r.sector))).sort().forEach(s => sectorSel.add(new Option(s, s)));
    const q = search.value.trim().toLowerCase();
    const rows = d.data.filter(r => (!q || r.name.toLowerCase().includes(q)) && (!sectorSel.value || r.sector === sectorSel.value) && (!regionSel.value || r.hq_region === regionSel.value))
      .sort((a, b) => {
        const va = sortKey === 'name' || sortKey === 'sector' ? a[sortKey] : (n(a[sortKey]) ?? -Infinity);
        const vb = sortKey === 'name' || sortKey === 'sector' ? b[sortKey] : (n(b[sortKey]) ?? -Infinity);
        return (va < vb ? -1 : va > vb ? 1 : 0) * sortDir;
      });
    document.querySelectorAll('#table-companies th').forEach(th => th.setAttribute('aria-sort', th.dataset.field === sortKey ? (sortDir > 0 ? 'ascending' : 'descending') : 'none'));
    rows.forEach(r => {
      const tr = document.createElement('tr'); tr.dataset.key = r.company_id; tr.setAttribute('aria-selected', String(r.company_id === selected));
      const cells = [r.name, r.sector, money(n(r.us_lobby_usd)), money(n(r.eu_lobby_usd)), money(n(r.de_lobby_usd)), money(n(r.fr_lobby_usd)),
        n(r.ec_meetings) == null ? '—' : int(n(r.ec_meetings)), money(n(r.pac_2020_2024_usd)), n(r.book_etr) == null ? '—' : pct1(n(r.book_etr)),
        n(r.n_havens_ex_home) == null ? '—' : int(n(r.n_havens_ex_home)), money(n(r.us_contracts_avg_usd))];
      cells.forEach((c, i) => { const td = document.createElement('td'); td.textContent = c; if (i === 1) td.className = 'ci-left'; tr.appendChild(td); });
      tb.appendChild(tr);
    });
  }

  const KV = [
    ['hq_country', 'Headquarters', v => v], ['sector', 'Sector', v => v], ['revenue_avg_usd', 'Revenue per year', v => money(+v)],
    ['us_lobby_usd', 'US lobbying per year', v => money(+v)], ['eu_lobby_usd', 'EU lobbying per year', v => money(+v)],
    ['de_lobby_usd', 'German lobbying per year', v => money(+v)], ['fr_lobby_usd', 'French lobbying per year', v => money(+v)],
    ['ec_meetings', 'Commission meetings 2019–24', v => int(+v)], ['eu_lobbyists_fte', 'EU lobbyists (FTE)', v => d3.format('.1f')(+v)],
    ['pac_2020_2024_usd', 'PAC giving 2020–24', v => money(+v)], ['pac_share_rep', 'PAC share to Republicans', v => pct0(+v)],
    ['book_etr', 'Book tax rate 2019–24', v => pct1(+v)], ['cash_etr', 'Cash tax rate 2019–24', v => pct1(+v)],
    ['statutory_rate', 'Home statutory rate', v => pct1(+v)], ['n_subsidiaries_with_lei', 'Subsidiaries with an LEI', v => int(+v)],
    ['n_havens_ex_home', 'In tax havens (abroad)', v => int(+v)], ['top_havens', 'Main havens', v => v.replace(/;/g, ', ')],
    ['us_contracts_avg_usd', 'US federal contracts per year', v => money(+v)],
  ];
  function drawDetail() {
    const d = dash.data('profile'); const kv = document.getElementById('detail-kv'); kv.replaceChildren();
    const el = document.getElementById('chart-detail'); clear(el);
    legend('legend-detail', [['US lobbying', C()[0], true], ['EU lobbying', C()[1], true]]);
    if (d.status !== 'ok') return;
    if (!selected) selected = (d.data.slice().sort((a, b) => (n(b.us_lobby_usd) || 0) - (n(a.us_lobby_usd) || 0))[0] || {}).company_id;
    const r = d.data.find(x => x.company_id === selected); if (!r) return;
    document.getElementById('detail-title').textContent = r.name;
    document.getElementById('detail-sub').textContent = `${r.sector} · ${r.hq_country} · yearly averages 2019–2024 unless noted`;
    KV.forEach(([f, label, fmt]) => {
      if (r[f] === '' || r[f] == null) return;
      const dt = document.createElement('dt'); dt.textContent = label;
      const dd = document.createElement('dd'); dd.textContent = fmt(r[f]);
      dd.dataset.source = 'profile'; dd.dataset.field = f; dd.dataset.where = 'company_id=' + encodeURIComponent(r.company_id);
      kv.append(dt, dd);
    });
    const p = dash.data('panel'); if (fail(el, p.status)) return;
    const rows = p.data.filter(x => x.company_id === selected && +x.year >= 2015 && +x.year <= 2025).map(x => ({ y: +x.year, us: n(x.us_lobby_usd), eu: n(x.eu_lobby_usd) })).sort((a, b) => a.y - b.y);
    const W = el.clientWidth || 300, H = 180, m = { top: 8, right: 10, bottom: 20, left: 48 };
    const x = d3.scaleLinear([2015, 2025], [m.left, W - m.right]);
    const y = d3.scaleLinear([0, d3.max(rows, q => Math.max(q.us || 0, q.eu || 0)) || 1], [H - m.bottom, m.top]).nice();
    const svg = svgIn(el, W, H, `US and EU lobbying over time for ${r.name}`);
    grid(svg, y, m.left, W - m.right, y.ticks(3), money);
    [2015, 2020, 2025].forEach(yr => svg.append('text').attr('x', x(yr)).attr('y', H - 4).attr('text-anchor', 'middle').text(yr));
    [['us', C()[0]], ['eu', C()[1]]].forEach(([k, col]) => {
      const pts = rows.filter(q => q[k] != null);
      svg.append('path').attr('d', d3.line().x(q => x(q.y)).y(q => y(q[k]))(pts)).attr('fill', 'none').attr('stroke', col).attr('stroke-width', 2).attr('stroke-linejoin', 'round');
    });
    const t = tip(el);
    svg.append('rect').attr('x', m.left).attr('y', m.top).attr('width', W - m.left - m.right).attr('height', H - m.top - m.bottom).attr('fill', 'transparent')
      .on('mousemove', ev => { const [px, py] = d3.pointer(ev); const yr = Math.round(x.invert(px)); const q = rows.find(z => z.y === yr) || {};
        showTip(el, t, String(yr), [['US', money(q.us), C()[0]], ['EU', money(q.eu), C()[1]]], x(yr) * ratio(el, W), py * ratio(el, W)); })
      .on('mouseleave', () => { t.hidden = true; });
  }

  // ---- Method
  function drawCoverage() {
    const tb = document.querySelector('#table-coverage tbody'); tb.replaceChildren();
    const d = dash.data('coverage'); if (d.status !== 'ok') return;
    d.data.forEach(r => {
      const tr = document.createElement('tr'); tr.dataset.key = r.source_id; tr.style.cursor = 'default';
      [r.title, r.origin, r.companies, r.first_year || 'current', r.last_year || '—'].forEach((c, i) => {
        const td = document.createElement('td'); td.textContent = c; if (i === 1) td.className = 'ci-left'; tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
  }

  // ---- tabs
  function show(p) {
    page = PAGES.includes(p) ? p : 'overview';
    PAGES.forEach(id => {
      document.getElementById(id).hidden = id !== page;
      document.getElementById('tab-' + id).setAttribute('aria-selected', String(id === page));
    });
    dash.setLink(page);
    draw();
  }
  document.querySelectorAll('.ci-tabs button').forEach(b => b.addEventListener('click', () => show(b.dataset.page)));

  function draw() {
    fillMarks();
    if (page === 'overview') drawReach();
    if (page === 'venues') { drawShare(); drawSectors(); drawMovers(); }
    if (page === 'outcomes') drawOutcomes();
    if (page === 'companies') { drawCompanies(); drawDetail(); }
    if (page === 'method') drawCoverage();
  }
  dash.onData(draw);
  const start = location.hash.slice(1);
  if (PAGES.includes(start)) show(start);
})();

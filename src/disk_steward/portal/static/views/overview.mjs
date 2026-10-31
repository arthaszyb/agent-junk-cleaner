// Overview: the number that can be cleaned right now (and the button for it), what the last scan found, where the
// space goes and why part of it is not offered. Every figure is a link to exactly the items behind it.
import { api, ApiError } from '../api.mjs';
import { diskRing } from '../charts.mjs';
import { delegate, h, replace } from '../dom.mjs';
import { browseHref, fmtAgo, fmtBytes, fmtCount, fmtTime, pluralCount } from '../format.mjs';
import { t } from '../i18n.mjs';
import { icon } from '../icons.mjs';
import { create as createNative } from './native.mjs';
import { quickPlan } from '../planflow.mjs';
import { diskLevel } from '../scale.mjs';
import { on, setJobFromResponse, state } from '../state.mjs';
import { emptyState, groupBadge, loading, notesBlock, pickTab, reportError, notesChanged, subTabs, tabPanel, tierBadge } from '../ui.mjs';

const REASONS = ['credentials', 'databases', 'config', 'user_content', 'current_version', 'tainted', 'unknown', 'other'];
const TIERS = ['junk', 'review', 'protected'];
const TOP_APPS = 10;
const DISMISSED = 'disk-steward-dismissed';
const AGE_DEBOUNCE_MS = 350;
const TABS = [['space', 'ov_space'], ['why', 'ov_why'], ['groups', 'ov_groups'], ['changes', 'ov_changes'], ['tools', 'ov_tools']];

export function create() {
  let root;
  let data = null;      // {ov, notes, quick, plans} or null before the first scan
  let loaded = false;
  let reloading = false;
  let scanSlot = null;
  let heroSlot = null;
  let days = null;      // the age the hero is judged at; null until the server says what the default is
  let ageTimer = null;
  const nativePart = createNative();
  let nativeMounted = false;
  let nativeSlot = null;
  let wantApps = false;
  let wantHero = false;  // opened from a link that names the hero: bring it into view once
  let apps = null;      // null: not asked; 'loading'; or the app_sizes() result
  let tab = null;       // the sub-tab under the hero and the tiles
  const disposers = [];

  // data ----------------------------------------------------------------------------------------------------

  async function loadQuick() {
    const q = await api.get('/api/quick', { days });
    days = q.days;
    return q;
  }

  async function load() {
    if (reloading) return;
    reloading = true;
    try {
      let next = null;   // the page keeps showing (and acting on) the old figures until the new ones are all in
      if (state.scan) {   // without a scan the endpoints answer 409; do not ask
        try {
          const [ov, notes, plans] = await Promise.all([api.get('/api/overview'), api.get('/api/spec/notes'), api.get('/api/plans')]);
          if (days === null) days = ov.offered.min_age_days;
          next = { ov, notes, plans, quick: await loadQuick() };
        } catch (e) {
          if (!(e instanceof ApiError && e.code === 'NO_SCAN')) throw e;   // the scan vanished since the state was read
        }
      }
      data = next;
      loaded = true;
      render();
    } finally {
      reloading = false;
    }
  }

  const here = (key) => data.ov.filters.rows[key];
  const link = (filter, ...children) => h('a', { class: 'figure', href: browseHref(filter) }, ...children);

  // scan and banners ----------------------------------------------------------------------------------------

  function scanBox() {
    const job = state.job;
    const scanning = job && job.kind === 'scan' && job.status === 'running';
    if (scanning) {
      const p = job.scan;
      const text = !p ? t('portal.scan.starting')
        : p.phase === 'last_used' ? t('portal.scan.reading_dates')
          : pluralCount('portal.scan.progress', p.files, { root: p.root, size: fmtBytes(p.alloc) });
      return h('div', { class: 'scanbox', role: 'status' },
        h('span', { text, 'data-testid': 'scan-progress' }),
        h('button', { class: 'btn', data: { action: 'cancel-scan' }, text: t('portal.common.cancel') }));
    }
    const busy = job && (job.status === 'running' || job.status === 'paused');
    return h('div', { class: 'scanbox' },
      h('button', { class: 'btn', data: { action: 'scan' }, 'data-testid': 'scan-button', disabled: busy,
        text: t(data ? 'portal.overview.scan_again' : 'portal.overview.scan_now') }),
      busy ? h('span', { class: 'muted', text: t('portal.overview.busy') }) : null);
  }

  function lastScan() {
    if (!data) return null;
    const { scan } = data.ov;
    return h('p', { class: 'muted', 'data-testid': 'last-scan' },
      t('portal.overview.last_scan', { id: scan.id, when: fmtTime(scan.finished_at), ago: fmtAgo((data.ov.generated_at - scan.finished_at) / 86400) }),
      ' ', t('portal.overview.areas', { areas: scan.domains.map((d) => t(`domain.${d}`)).join(', ') || '-' }));
  }

  function dismissed() {
    try {
      return localStorage.getItem(DISMISSED);
    } catch (e) {
      return null;   // storage is blocked: the banner comes back on every visit, which is harmless
    }
  }

  // The result of the latest run that has one, until the user closes the banner (once per plan).
  function lastCleanup() {
    const found = data.plans.finished.find((p) => p.result);
    if (!found || dismissed() === found.id) return null;
    return h('div', { class: 'banner banner-ok', 'data-testid': 'last-cleanup' },
      h('span', { text: t('portal.banner.last_cleanup', { freed: fmtBytes(found.result.freed), planned: fmtBytes(found.totals.alloc) }) }), ' ',
      h('a', { href: `#/plan/${found.id}`, text: t('portal.banner.details') }), ' ',
      h('button', { class: 'btn small', data: { action: 'dismiss-banner', plan: found.id }, text: t('portal.common.dismiss') }));
  }

  function banners() {
    const out = [];
    const rec = state.recovery;
    if (rec && rec.entries && rec.entries.length) {
      out.push(h('div', { class: 'banner banner-warn' }, pluralCount('portal.overview.recovered', rec.entries.length)));
    }
    if (!data) return out;
    const cleanup = lastCleanup();
    if (cleanup) out.push(cleanup);
    if (data.ov.scan.rules_changed) out.push(h('div', { class: 'banner banner-warn' }, t('cli.report.rules_changed')));
    if (data.notes.notes && notesChanged(data.notes.notes)) {
      out.push(h('div', { class: 'banner banner-info', 'data-testid': 'notes-banner' },
        h('strong', { text: t('portal.notes.title') }), notesBlock(data.notes.notes, data.notes.labels),
        h('a', { href: '#/browse', class: 'btn small' }, t('portal.notes.review'))));
    }
    for (const text of data.ov.cloud_notes) out.push(h('div', { class: 'banner banner-warn', text }));
    return out;
  }

  // the hero ------------------------------------------------------------------------------------------------

  function heroGap() {
    const q = data.quick;
    const recent = (q.summary.scope_by_state || {}).too_recent;
    const review = data.ov.not_included.review;
    const parts = [];
    if (recent && recent.count) {
      parts.push(h('a', { href: browseHref({ tier: 'junk', state: 'too_recent', days: q.days }), 'data-testid': 'hero-recent',
        text: t('portal.hero.more_recent', { size: fmtBytes(recent.alloc), days: q.days }) }));
    }
    if (review.items) {
      parts.push(h('a', { href: browseHref(here('review')), 'data-testid': 'hero-review', text: t('portal.hero.more_review', { size: fmtBytes(review.alloc) }) }));
    }
    return parts.flatMap((p, i) => (i ? [' · ', p] : [p]));
  }

  function ring() {
    const v = data.ov.volume;
    if (v.total === null || v.used === null) return null;
    const level = diskLevel(v.used, v.total);
    return [diskRing({ used: v.used, total: v.total, value: fmtBytes(v.free), caption: t('portal.shell.of_total', { total: fmtBytes(v.total) }),
      ariaLabel: t('cli.report.volume_line', { total: fmtBytes(v.total), used: fmtBytes(v.used), free: fmtBytes(v.free) }) }),
    level === 'ok' ? null : h('span', { class: 'level', 'data-level': level }, icon('alert', 13), t(level === 'crit' ? 'portal.shell.disk_crit' : 'portal.shell.disk_warn'))];
  }

  function hero() {
    const q = data.quick;
    const s = q.summary;
    const none = s.count === 0;
    return h('section', { class: 'card hero', 'data-testid': 'hero' },
      h('div', { class: 'hero-left' },
        h('div', { class: 'hero-main' },
          h('h2', { text: t('portal.hero.title') }),
          h('div', { class: 'hero-number', 'data-testid': 'hero-size', text: fmtBytes(s.alloc) }),
          h('div', { class: 'hero-items', 'data-testid': 'hero-items', text: pluralCount('portal.hero.items', s.count) }),
          h('label', { class: 'inline hero-age' }, h('span', { text: t('portal.hero.age_before') }),
            h('input', { type: 'number', min: '0', step: 'any', value: q.days, class: 'days', data: { input: 'days' },
              'data-testid': 'hero-days', 'aria-label': t('portal.hero.age_label') }),
            h('span', { text: t('portal.hero.age_after') }))),
        h('div', { class: 'hero-actions' },
          h('button', { class: 'btn primary solid big', disabled: none, data: { action: 'review-clean' }, 'data-testid': 'hero-button' },
            icon('cleanup', 22), t('portal.hero.button', { size: fmtBytes(s.alloc) })),
          none ? h('p', { class: 'muted', 'data-testid': 'hero-none', text: t('portal.hero.nothing') }) : null,
          h('p', { class: 'hero-gap muted', 'data-testid': 'hero-gap' }, heroGap()))),
      h('div', { class: 'hero-side' }, ring()));
  }

  // The age input keeps its place while the figures around it change.
  function updateHero() {
    if (!heroSlot) return;
    const input = heroSlot.querySelector('input');
    const typing = document.activeElement === input;
    const value = input.value;
    replace(heroSlot, hero());
    if (typing) {
      const again = heroSlot.querySelector('input');
      again.value = value;
      again.focus({ preventScroll: true });
    }
  }

  const reviewAndClean = () => quickPlan(() => data.quick, async () => {
    data.quick = await loadQuick();
    updateHero();
    return { count: data.quick.summary.count, alloc: data.quick.summary.alloc };
  });

  // numbers that are links -----------------------------------------------------------------------------------

  function tiers() {
    const { by_tier: byTier } = data.ov.totals;
    return h('div', { class: 'tiles' }, TIERS.map((tier) => {
      const v = byTier[tier];
      return h('a', { class: 'tile', href: browseHref(data.ov.filters.tier[tier]), 'data-testid': `tier-${tier}` },
        h('div', { class: 'tile-head' }, tierBadge(tier)),
        h('div', { class: 'tile-size', text: fmtBytes(v.alloc) }),
        tier === 'protected' ? null : h('div', { class: 'muted', text: t('portal.overview.reclaimable', { size: fmtBytes(v.reclaim) }) }),
        h('div', { class: 'muted', text: t('portal.overview.groups_items', { groups: fmtCount(v.groups), items: fmtCount(v.items) }) }));
    }));
  }

  function volume() {
    const v = data.ov.volume;
    if (v.total === null || v.used === null) return h('section', { class: 'card' }, h('h2', { text: t('portal.overview.volume') }), appsBox());
    const seg = (cls, bytes, label) => {
      const el = h('span', { class: `seg ${cls}`, title: `${label}: ${fmtBytes(bytes)}` });
      el.style.width = `${Math.max(0, (bytes / v.total) * 100)}%`;   // CSSOM: allowed by the CSP, unlike a style attribute
      return el;
    };
    const unattributed = v.unattributed === null ? 0 : v.unattributed;
    return h('section', { class: 'card', 'data-testid': 'volume' },
      h('h2', { text: t('portal.overview.volume') }),
      h('div', { class: 'bar', role: 'img', 'aria-label': t('cli.report.volume_line', { total: fmtBytes(v.total), used: fmtBytes(v.used), free: fmtBytes(v.free) }) },
        v.attributed === null ? null : seg('seg-scanned', v.attributed, t('portal.overview.legend_scanned')),
        seg('seg-other', unattributed, t('portal.overview.legend_other')),
        seg('seg-free', v.free, t('portal.overview.legend_free'))),
      h('p', { text: t('cli.report.volume_line', { total: fmtBytes(v.total), used: fmtBytes(v.used), free: fmtBytes(v.free) }) }),
      v.attributed === null ? null : h('p', { text: t('cli.report.volume_attributed', {
        attributed: fmtBytes(v.attributed), unattributed: fmtBytes(unattributed) }) }),
      v.snapshots && v.snapshots.length ? h('p', { class: 'muted', text: pluralCount('portal.warning.snapshots', v.snapshots.length) }) : null,
      appsBox());
  }

  function appsBox() {
    if (apps === null) {
      return h('div', { class: 'apps' }, h('button', { class: 'btn', data: { action: 'measure-apps' }, 'data-testid': 'measure-apps',
        text: t('portal.overview.measure_apps') }), ' ', h('span', { class: 'muted', text: t('portal.overview.measure_hint') }));
    }
    if (apps === 'loading') return h('p', { class: 'apps', role: 'status', text: t('cli.apps.measuring') });
    const sized = apps.entries.filter((e) => 'bytes' in e);
    const failed = apps.entries.filter((e) => 'error' in e);
    const shown = sized.slice(0, TOP_APPS);
    return h('div', { class: 'apps', 'data-testid': 'apps' }, h('h3', { text: t('cli.apps.title') }),
      sized.length ? h('p', { text: pluralCount('portal.overview.apps_measured', sized.length, { when: fmtTime(apps.measured_at), shown: shown.length, total: fmtBytes(apps.total) }) })
        : (failed.length ? null : h('p', { class: 'muted', text: t('cli.apps.none') })),
      shown.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', { class: 'num', text: t('cli.col.size') }), h('th', { text: t('cli.col.path') }))),
        h('tbody', {}, shown.map((e) => h('tr', {}, h('td', { class: 'num', text: fmtBytes(e.bytes) }), h('td', { class: 'path', text: e.path })))))) : null,
      failed.length ? h('p', { class: 'muted', text: pluralCount('portal.overview.apps_errors', failed.length,
        { details: failed.slice(0, TOP_APPS).map((e) => `${e.path}: ${e.error}`).join('; ') }) }) : null,
      h('p', { class: 'muted', text: t('cli.apps.note') }));
  }

  function domains() {
    const rows = Object.entries(data.ov.totals.by_domain).sort().map(([d, v]) => {
      const href = browseHref({ domain: d });
      const cell = (text, cls) => h('td', { class: cls }, h('a', { class: 'figure', href, tabindex: cls ? '-1' : null, text }));
      return h('tr', {}, cell(t(`domain.${d}`)), cell(fmtCount(v.groups), 'num'), cell(fmtCount(v.items), 'num'),
        cell(fmtBytes(v.alloc), 'num'), cell(fmtBytes(v.reclaim), 'num'));
    });
    return h('section', { class: 'card' }, h('h2', { text: t('cli.report.by_domain') }),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', { text: t('cli.col.domain') }), h('th', { class: 'num', text: t('cli.col.groups') }),
          h('th', { class: 'num', text: t('cli.col.items') }), h('th', { class: 'num', text: t('cli.col.size') }),
          h('th', { class: 'num', text: t('cli.col.reclaim') }))),
        h('tbody', {}, rows))));
  }

  function whySpace() {
    const { offered, not_included: ni } = data.ov;
    const age = offered.min_age_days;
    const row = (label, bucket, filter, testid, sub = false) => {
      const href = browseHref(filter);
      return h('tr', { class: sub ? 'sub' : null, 'data-testid': testid },
        h('td', {}, h('a', { class: 'figure', href, text: label })),
        h('td', { class: 'num' }, h('a', { class: 'figure', href, tabindex: '-1', text: fmtCount(bucket.items) })),
        h('td', { class: 'num' }, h('a', { class: 'figure', href, tabindex: '-1', text: fmtBytes(bucket.alloc) })));
    };
    const rows = [row(age ? t('portal.why.offered', { days: age }) : t('portal.why.offered_any'), offered, here('offered'), 'row-offered')];
    if (age) rows.push(row(t('portal.why.too_recent', { days: age }), ni.too_recent, here('too_recent'), 'row-too_recent'));
    rows.push(row(t('portal.why.floor_held'), ni.floor_held, here('floor_held'), 'row-floor_held'),
      row(t('portal.why.review'), ni.review, here('review'), 'row-review'),
      row(t('portal.why.report_only'), ni.report_only, here('report_only'), 'row-report_only'),
      row(t('portal.why.other'), ni.other, here('other'), 'row-other'),
      row(t('portal.why.protected'), ni.protected, here('protected'), 'row-protected'));
    for (const reason of REASONS) {
      const slot = ni.protected.by_reason[reason];
      if (slot) rows.push(row(t(`cli.reason.${reason}`), slot, data.ov.filters.reasons[reason], `row-reason-${reason}`, true));
    }
    const problems = Object.entries(ni.errors).filter(([code]) => code !== 'skipped_privacy');
    return h('section', { class: 'card', 'data-testid': 'why' },
      h('h2', { text: t('cli.why.title') }),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', { text: t('cli.col.what') }), h('th', { class: 'num', text: t('cli.col.items') }),
          h('th', { class: 'num', text: t('cli.col.size') }))),
        h('tbody', {}, rows))),
      ni.privacy_skipped ? h('p', { class: 'muted', text: pluralCount('portal.why.privacy', ni.privacy_skipped) }) : null,
      problems.length ? h('div', {}, h('h3', { text: t('portal.why.problems') }),
        h('ul', { class: 'plain' }, problems.map(([code, n]) => h('li', {},
          h('strong', { text: `${t(`cli.scan_error.${code}`)}: ${fmtCount(n)}` }), ' ', h('span', { class: 'muted', text: t(`scan.error.${code}`) }))))) : null);
  }

  function topGroups() {
    const { growth, top_groups: top } = data.ov;
    const has = growth.previous_scan_id !== null;
    const rows = top.map((g) => {
      const href = browseHref({ group: g.key });
      const cell = (child, cls) => h('td', { class: cls }, child);
      const num = (text) => h('a', { class: 'figure', href, tabindex: '-1', text });
      return h('tr', { 'data-testid': 'top-group', 'data-key': g.key },
        cell(h('a', { class: 'figure', href, text: g.label })),
        cell(groupBadge(g)),
        cell(num(fmtBytes(g.alloc)), 'num'), cell(num(fmtCount(g.items)), 'num'),
        has ? cell(num(g.new ? t('cli.growth.new') : fmtBytes(g.delta, true)), 'num') : null);
    });
    return h('section', { class: 'card' }, h('h2', { text: t('cli.report.top_groups', { count: top.length }) }),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', { text: t('cli.col.group') }), h('th', { text: t('cli.col.tier') }),
          h('th', { class: 'num', text: t('cli.col.size') }), h('th', { class: 'num', text: t('cli.col.items') }),
          has ? h('th', { class: 'num', text: t('cli.col.change') }) : null)),
        h('tbody', {}, rows))));
  }

  function growth() {
    const g = data.ov.growth;
    if (g.previous_scan_id === null) {
      return h('section', { class: 'card' }, h('h2', { text: t('portal.overview.growth') }), h('p', { class: 'muted', text: t('cli.growth.none') }));
    }
    const rows = g.groups.map((r) => h('tr', {},
      h('td', { class: 'num', text: fmtBytes(r.delta, true) }), h('td', { class: 'num', text: fmtBytes(r.alloc) }),
      h('td', {}, h('a', { class: 'figure', href: browseHref({ group: r.key }), text: r.label + (r.new ? ` ${t('cli.growth.new_tag')}` : '') }))));
    return h('section', { class: 'card', 'data-testid': 'growth' }, h('h2', { text: t('portal.overview.growth') }),
      h('p', { text: t('cli.growth.title', { previous: g.previous_scan_id, when: fmtTime(g.previous_finished_at), current: data.ov.scan.id }) }),
      g.alloc_delta === null ? null : h('p', { text: t('cli.growth.total', { delta: fmtBytes(g.alloc_delta, true) }) }),
      g.groups.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', { class: 'num', text: t('cli.col.change') }), h('th', { class: 'num', text: t('cli.col.size') }),
          h('th', { text: t('cli.col.group') }))),
        h('tbody', {}, rows))) : h('p', { class: 'muted', text: t('cli.growth.unchanged') }));
  }

  // page ----------------------------------------------------------------------------------------------------

  function renderScan() {
    if (scanSlot) replace(scanSlot, scanBox());
  }

  function render() {
    if (!loaded) return;
    scanSlot = h('div', { class: 'scan-slot' });
    heroSlot = data ? h('div', { class: 'hero-slot' }, hero()) : null;
    const tabs = TABS.map(([key, label]) => ({ key, label: t(`portal.tab.${label}`) }));
    tab = pickTab(tabs, tab);
    nativeSlot = tab === 'tools' ? h('div', { class: 'native-slot' }) : null;
    const part = () => ({ space: () => [volume(), domains()], why: whySpace, groups: topGroups, changes: growth, tools: () => nativeSlot }[tab]());
    replace(root,
      h('div', { class: 'page-head' }, h('h1', { text: t('portal.nav.overview') }), data ? scanSlot : null),
      lastScan(), ...banners(),
      data ? [heroSlot, tiers(), subTabs(tabs, tab, (key) => { tab = key; render(); }), tabPanel(tab, part())]
        : emptyState({ iconName: 'scan', title: t('portal.overview.no_scan_title'), body: t('portal.overview.no_scan_body'), action: scanSlot }));
    renderScan();
    if (data && nativeSlot) {   // the tools that clean themselves: drawn again into the new page, loaded once
      if (nativeMounted) nativePart.attach(nativeSlot);
      else {
        nativeMounted = true;
        nativePart.mount(nativeSlot).catch(reportError);
      }
    }
    if (wantApps && data) {   // #/overview?apps=1: from Insights, where "not scanned" points at the applications
      wantApps = false;
      const box = root.querySelector('.apps');
      box.scrollIntoView({ block: 'center' });
      const button = box.querySelector('[data-testid="measure-apps"]');
      if (button) button.focus({ preventScroll: true });
    }
    if (wantHero && heroSlot) {   // #/overview?quick=1 is where the "safe to clean" figures of other pages lead
      wantHero = false;
      heroSlot.scrollIntoView({ block: 'start' });
      heroSlot.querySelector('[data-testid="hero-button"]').focus({ preventScroll: true });
    }
  }

  async function setDays(text) {
    const value = Number(text);
    if (text === '' || !Number.isFinite(value) || value < 0) return;   // half-typed: wait for a number
    days = value;
    data.quick = await loadQuick();
    updateHero();
  }

  return {
    async mount(el, route) {
      root = el;
      wantHero = route.query.quick === '1';
      wantApps = route.query.apps === '1';
      tab = wantApps ? 'space' : route.query.tab || null;   // the applications panel is part of Disk & areas
      replace(root, loading());
      delegate(root, 'click', 'action', {
        scan: async () => {
          const r = await api.post('/api/scan', {});
          setJobFromResponse(r.job);
        },
        'cancel-scan': async () => { setJobFromResponse((await api.post('/api/job/cancel', {})).job); },
        'review-clean': reviewAndClean,
        'dismiss-banner': (el2) => {
          try {
            localStorage.setItem(DISMISSED, el2.dataset.plan);
          } catch (e) {
            // storage is blocked: the banner just closes for this visit
          }
          el2.closest('.banner').remove();
        },
        'measure-apps': async () => {
          apps = 'loading';
          render();
          try {
            apps = await api.get('/api/apps');
          } catch (e) {
            apps = null;
            render();
            throw e;
          }
          render();
        },
      });
      delegate(root, 'input', 'input', {
        days: (input) => {
          clearTimeout(ageTimer);
          ageTimer = setTimeout(() => setDays(input.value).catch(reportError), AGE_DEBOUNCE_MS);
        },
      });
      disposers.push(() => clearTimeout(ageTimer));
      disposers.push(on('job', ({ job, before }) => {
        const wasScanning = before && before.kind === 'scan' && before.status === 'running';
        if (wasScanning && job.status !== 'running') load().catch(reportError);
        else renderScan();
      }));
      disposers.push(on('events', (events) => {
        if (events.some((e) => e.type === 'scan.done')) load().catch(reportError);
      }));
      await load();
    },
    unmount() {
      disposers.forEach((f) => f());
      if (nativeMounted) nativePart.unmount();
    },
  };
}

/**
 * Runtime evidence probe.
 *
 * Runs inside the page after it has settled and returns *names only*: global
 * variable names, storage keys, custom element tags, attribute names. It never
 * reads a value out of storage or serialises an object, so nothing personal
 * can leave the page through this function.
 *
 * Every section is wrapped individually: one broken getter on a hostile site
 * must not cost us the other twenty layers of evidence.
 */
async () => {
  const out = { probe_errors: [] };

  const uniq = (values, limit) => {
    const seen = [];
    const set = new Set();
    for (const v of values || []) {
      if (v === undefined || v === null) continue;
      const s = String(v);
      if (!s || set.has(s)) continue;
      set.add(s);
      seen.push(s);
      if (seen.length >= limit) break;
    }
    return seen.sort();
  };

  const section = async (name, fn) => {
    try {
      out[name] = await fn();
    } catch (e) {
      out.probe_errors.push(name + ': ' + ((e && e.message) || 'unknown'));
    }
  };

  // --- Global JavaScript objects -------------------------------------------
  // The single strongest runtime signal. A vendor's global existing is proof the
  // SDK actually executed, which is exactly what a static HTML scan cannot see.
  await section('window_keys', () => {
    const keys = [];
    for (const key in window) keys.push(key);
    try {
      Object.getOwnPropertyNames(window).forEach((k) => keys.push(k));
    } catch (e) { /* cross-origin hardened window */ }
    return uniq(keys, 1200);
  });

  // Nested namespaces hide a lot of vendors: `window.dataLayer` is Google,
  // but `window.google_tag_manager` holds the container IDs, and objects like
  // `window.adobe`, `window.Kakao` expose sub-keys worth naming.
  await section('nested_keys', () => {
    const interesting = [
      'google_tag_manager', 'adobe', 'Kakao', 'naver', 'wcs', 'Insider',
      '_INS', 'appboy', 'braze', 'analytics', 'Optanon', 'OneTrust',
      'dataLayer', 'digitalData', 'utag', 'tealium', 'amplitude', 'mixpanel',
      'clevertap', 'moe', 'Moengage', 'hackle', 'airbridge', 'abr', 'dfinery',
      'AF', 'appier', 'DY', 'dyApi', 'exponea', 'bloomreach', 'Emarsys',
      'ChannelIO', 'zE', 'Intercom', 'segment', 'snowplow', 'clarity',
    ];
    const result = {};
    for (const name of interesting) {
      let value;
      try { value = window[name]; } catch (e) { continue; }
      if (value === undefined || value === null) continue;
      if (typeof value === 'function') { result[name] = ['<function>']; continue; }
      if (Array.isArray(value)) { result[name] = ['<array:' + value.length + '>']; continue; }
      if (typeof value !== 'object') { result[name] = ['<' + typeof value + '>']; continue; }
      let keys = [];
      try { keys = Object.keys(value); } catch (e) { keys = []; }
      result[name] = uniq(keys, 60);
    }
    return result;
  });

  // --- Storage: key names only ---------------------------------------------
  await section('cookie_names', () => {
    return uniq(
      (document.cookie || '').split(';').map((c) => c.split('=')[0].trim()).filter(Boolean),
      300
    );
  });

  await section('local_storage_keys', () => {
    const keys = [];
    for (let i = 0; i < localStorage.length; i++) keys.push(localStorage.key(i));
    return uniq(keys, 300);
  });

  await section('session_storage_keys', () => {
    const keys = [];
    for (let i = 0; i < sessionStorage.length; i++) keys.push(sessionStorage.key(i));
    return uniq(keys, 300);
  });

  // Layers most technology scanners never look at. Several engagement SDKs
  // recommendation engines create named databases and caches.
  await section('indexeddb_names', async () => {
    if (!indexedDB || !indexedDB.databases) return [];
    const dbs = await indexedDB.databases();
    return uniq(dbs.map((d) => d && d.name), 100);
  });

  await section('cache_names', async () => {
    if (typeof caches === 'undefined') return [];
    return uniq(await caches.keys(), 100);
  });

  await section('service_workers', async () => {
    if (!navigator.serviceWorker || !navigator.serviceWorker.getRegistrations) return [];
    const regs = await navigator.serviceWorker.getRegistrations();
    const urls = [];
    for (const reg of regs) {
      const worker = reg.active || reg.installing || reg.waiting;
      if (worker && worker.scriptURL) urls.push(worker.scriptURL.split('?')[0]);
      if (reg.scope) urls.push('scope:' + reg.scope);
    }
    return uniq(urls, 40);
  });

  // --- DOM shape -----------------------------------------------------------
  await section('script_srcs', () => {
    const srcs = Array.from(document.querySelectorAll('script[src]')).map((s) => s.src);
    return uniq(srcs, 400);
  });

  await section('link_hrefs', () => {
    const links = Array.from(document.querySelectorAll('link[href]'))
      .filter((l) => !/^stylesheet$/i.test(l.rel || '') || /\/\//.test(l.href))
      .map((l) => l.href);
    return uniq(links, 200);
  });

  await section('anchor_paths', () => {
    // Same-host link paths, for `radar suggest-urls`: the product page a
    // vendor only loads on is usually one click from the home page.
    const paths = [];
    for (const a of document.querySelectorAll('a[href]')) {
      try {
        const u = new URL(a.href, location.href);
        if (u.host === location.host && u.pathname.length > 1) paths.push(u.pathname);
      } catch (e) { /* malformed href */ }
    }
    return uniq(paths, 80);
  });

  await section('iframe_srcs', () => {
    return uniq(Array.from(document.querySelectorAll('iframe[src]')).map((f) => f.src), 100);
  });

  await section('meta_tags', () => {
    const metas = [];
    for (const m of document.querySelectorAll('meta')) {
      const name = m.getAttribute('name') || m.getAttribute('property') || m.getAttribute('http-equiv');
      if (!name) continue;
      // Only a short allowlist carries a value; everything else is name-only.
      const keepValue = /^(generator|application-name|framework|csrf-param|shopify-|wix-|next-head)/i.test(name);
      metas.push(keepValue ? name + '=' + String(m.getAttribute('content') || '').slice(0, 120) : name);
    }
    return uniq(metas, 200);
  });

  // Custom elements are a surprisingly clean vendor fingerprint: chat widgets,
  // review platforms and recommendation engines all inject their own tags.
  await section('custom_elements', () => {
    const tags = [];
    for (const el of document.querySelectorAll('*')) {
      const tag = el.tagName.toLowerCase();
      if (tag.includes('-')) tags.push(tag);
    }
    return uniq(tags, 120);
  });

  // Vendor SDKs mark up the page with their own data attributes
  // (`data-hackle-key`, `data-ch-testid`, `data-dy-...`).
  await section('data_attributes', () => {
    const names = [];
    let count = 0;
    for (const el of document.querySelectorAll('*')) {
      if (++count > 4000) break;
      for (const attr of el.attributes) {
        if (attr.name.startsWith('data-') || attr.name.includes(':')) names.push(attr.name);
      }
    }
    return uniq(names, 300);
  });

  await section('meta_csp', () => {
    const el = document.querySelector('meta[http-equiv="Content-Security-Policy" i]');
    return el ? String(el.getAttribute('content') || '').slice(0, 8000) : '';
  });

  // --- Inline script contents ----------------------------------------------
  // We keep identifier-shaped tokens rather than raw source. Hydration payloads
  // (`__NEXT_DATA__` and friends) routinely contain personal data in their
  // string values; tokenising drops the values and keeps the vendor names.
  await section('inline_script_tokens', () => {
    const STOP = new Set(['function', 'return', 'window', 'document', 'length',
      'push', 'this', 'null', 'true', 'false', 'undefined', 'const', 'var',
      'let', 'else', 'catch', 'typeof', 'prototype', 'value', 'string',
      'object', 'number', 'default', 'export', 'import', 'class', 'style',
      'width', 'height', 'display', 'color', 'script', 'https', 'http',
      'querySelector', 'getElementById', 'addEventListener', 'createElement',
      'appendChild', 'innerHTML', 'setAttribute', 'parentNode', 'forEach']);
    const tokens = [];
    const re = /[A-Za-z_$][A-Za-z0-9_$]{3,49}/g;
    let budget = 600000;
    for (const s of document.querySelectorAll('script:not([src])')) {
      const text = s.textContent || '';
      if (!text) continue;
      const slice = text.slice(0, Math.max(0, Math.min(text.length, budget)));
      budget -= slice.length;
      let m;
      while ((m = re.exec(slice)) !== null) {
        const t = m[0];
        if (!STOP.has(t)) tokens.push(t);
      }
      if (budget <= 0) break;
    }
    return uniq(tokens, 2500);
  });

  // Raw inline source, capped, used only for host extraction on the Python side.
  await section('inline_script_sample', () => {
    let buffer = '';
    for (const s of document.querySelectorAll('script:not([src])')) {
      buffer += (s.textContent || '').slice(0, 40000) + '\n';
      if (buffer.length > 400000) break;
    }
    return buffer.slice(0, 400000);
  });

  // --- Resources the request listener may have missed ----------------------
  // Performance entries include resources fetched before our listener attached,
  // plus `initiatorType`, which tells us whether a beacon fired.
  await section('performance_resources', () => {
    if (!performance || !performance.getEntriesByType) return [];
    return performance.getEntriesByType('resource')
      .slice(0, 800)
      .map((e) => e.initiatorType + ' ' + String(e.name).split('?')[0]);
  });

  await section('page_info', () => ({
    title: String(document.title || '').slice(0, 200),
    lang: document.documentElement.getAttribute('lang') || '',
    visible_text_head: String((document.body && document.body.innerText) || '').slice(0, 1500),
    html_length: (document.documentElement.outerHTML || '').length,
    frame_count: window.frames ? window.frames.length : 0,
  }));

  return out;
}

// Shared test UI for the user-facing screens (/, /operator, /output/hyeol, /output/maek, /mapping, …).
// server.py serves this file at /testui.js and prepends `const RIPPLES_NAV = [[path, label], ...]` (NAV_LINKS).
//
// Entry states, the same on every screen:
//   ?test   test UI: "‹ 뒤로" + menu (screen name, mock/connection status, UI 숨김, 전체 화면, links to other screens).
//           "UI 숨김" hides the page's test UI and the menu but keeps "‹ 뒤로" and "UI 표시".
//   ?clean  exhibition clean output: every UI layer (this one included) is hidden, only the visual output stays.
//   (none)  the page's existing behavior, unchanged.
// "‹ 뒤로": leave fullscreen first, then history.back() when we came from this server, otherwise go to /progress.
(() => {
  const FALLBACK = '/progress';
  const query = new URLSearchParams(location.search);
  const root = document.documentElement;
  const clean = query.has('clean');
  const test = query.has('test') && !clean;
  const nav = typeof RIPPLES_NAV !== 'undefined' ? RIPPLES_NAV : [[FALLBACK, '진행상황']];
  const path = location.pathname.replace(/\/+$/, '') || '/';
  const screenName = (nav.find(([u]) => u === path) || [path, path])[1];

  const css = document.createElement('style');
  css.textContent = `
    html.ripples-clean #ripples-ui, html.ripples-clean footer, html.ripples-clean #diag, html.ripples-clean #empty,
    html.ripples-clean #bar, html.ripples-clean #edit { display: none !important; }
    html.ripples-clean, html.ripples-clean body { cursor: none; }
    html.ripples-hide footer, html.ripples-hide #diag, html.ripples-hide #bar, html.ripples-hide #edit { display: none !important; }
    html.ripples-test #diag { top: calc(env(safe-area-inset-top, 0px) + 60px) !important; left: env(safe-area-inset-left, 0px) !important; }

    #ripples-ui { position: fixed; z-index: 2147483000; top: calc(env(safe-area-inset-top, 0px) + 8px);
      right: calc(env(safe-area-inset-right, 0px) + 8px); font: 15px/1.4 -apple-system, system-ui, sans-serif; color: #fff; }
    #ripples-ui .ru-bar { display: flex; gap: 8px; justify-content: flex-end; }
    #ripples-ui button, #ripples-ui a { -webkit-tap-highlight-color: transparent; touch-action: manipulation; }
    #ripples-ui button:focus-visible, #ripples-ui a:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
    #ripples-ui .ru-btn { min-width: 44px; min-height: 44px; padding: 0 12px; display: inline-flex; align-items: center;
      justify-content: center; gap: 6px; background: rgba(0,0,0,.8); color: #fff; border: 1px solid #666;
      border-radius: 6px; font: 600 15px/1 -apple-system, system-ui, sans-serif; cursor: pointer; }
    #ripples-ui .ru-btn:hover { border-color: #aaa; }
    #ripples-ui .ru-menu-btn svg { width: 20px; height: 20px; }
    /* The panel never runs past the visible screen: only the panel scrolls (fitPanel() sets the exact height from the
       visual viewport, which on iPhone Safari excludes the browser bars; 100vh there is the larger bars-hidden height). */
    #ripples-ui .ru-panel { display: none; margin-top: 8px; width: min(320px, calc(100vw - 16px - env(safe-area-inset-left, 0px) - env(safe-area-inset-right, 0px)));
      max-height: calc(100vh - 80px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));
      max-height: calc(100dvh - 80px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)); overflow-y: auto;
      overscroll-behavior: contain; -webkit-overflow-scrolling: touch;
      background: #0c0c0c; border: 1px solid #444; border-radius: 6px; padding: 12px; }
    #ripples-ui.ru-open .ru-panel { display: block; }
    #ripples-ui .ru-name { font-weight: 700; font-size: 18px; overflow-wrap: anywhere; }
    #ripples-ui .ru-state { color: #ccc; margin: 2px 0 12px; overflow-wrap: anywhere; }
    #ripples-ui .ru-state.bad { color: #ff8a7a; }
    #ripples-ui .ru-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    #ripples-ui .ru-actions .ru-btn { width: 100%; }
    #ripples-ui .ru-actions .ru-btn:disabled { opacity: .5; cursor: default; }
    #ripples-ui .ru-why { color: #ccc; font-size: 14px; margin: 6px 0 0; }
    #ripples-ui ul { list-style: none; margin: 12px 0 0; padding: 12px 0 0; border-top: 1px solid #333; }
    #ripples-ui li a { display: flex; justify-content: space-between; align-items: center; gap: 10px; min-height: 48px;
      padding: 8px 4px; color: #fff; text-decoration: underline; text-underline-offset: 3px; }
    #ripples-ui li a[aria-current="page"] { text-decoration: none; font-weight: 700; }
    #ripples-ui li a[aria-current="page"]::after { content: "현재 화면"; font-weight: 400; font-size: 14px; color: #ccc; }
    #ripples-ui .ru-show { display: none; }
    /* Docked: a page that provides #ripples-nav-slot (mapping) gets the buttons in its own navigation area,
       in normal flow, so they never sit over the page's work area. The open menu pushes content down. */
    #ripples-ui.docked { position: static; }
    body.split #ripples-ui.docked .ru-panel { max-height: 45vh; }
    /* PC (2026-10-20): the docked menu opens as a drop-down over the page (under the buttons) instead of growing the
       navigation area, which pushed the preview / work area down in the side-by-side PC layouts. */
    @media (min-width: 1024px) {
      #ripples-ui.docked { position: relative; }
      #ripples-ui.docked .ru-panel { position: absolute; right: 0; top: 100%; }
    }
    html.ripples-hide #ripples-ui .ru-menu-btn, html.ripples-hide #ripples-ui .ru-panel { display: none !important; }
    html.ripples-hide #ripples-ui .ru-show { display: inline-flex; }
  `;
  document.head.append(css);
  if (clean) root.classList.add('ripples-clean');

  let fallbackTimer = null;
  function leave() {
    let sameOrigin = false;
    try { sameOrigin = Boolean(document.referrer) && new URL(document.referrer).origin === location.origin; } catch (_) {}
    if (sameOrigin && window.history.length > 1) {
      window.history.back();
      // A tab opened with window.open() has a referrer but nothing to go back to; fall back if we are still here.
      fallbackTimer = setTimeout(() => { location.href = FALLBACK; }, 1500);
    } else {
      location.href = FALLBACK;
    }
  }
  function goBack() {
    const fullscreen = document.fullscreenElement || document.webkitFullscreenElement;
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (fullscreen && exit) Promise.resolve(exit.call(document)).catch(() => {}).then(leave);
    else leave();
  }
  window.addEventListener('pagehide', () => clearTimeout(fallbackTimer));
  window.ripplesUI = {test, clean, setHidden() {}, goBack};
  if (!test) return;
  root.classList.add('ripples-test');

  const canFullscreen = Boolean(document.fullscreenEnabled || document.webkitFullscreenEnabled);
  const isFullscreen = () => Boolean(document.fullscreenElement || document.webkitFullscreenElement);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const href = u => (u === FALLBACK ? u : u + '?test');

  const ui = document.createElement('div');
  ui.id = 'ripples-ui';
  ui.innerHTML = `
    <div class="ru-bar">
      <button type="button" class="ru-btn ru-back" aria-label="뒤로 (기록이 없으면 진행상황으로)">‹ 뒤로</button>
      <button type="button" class="ru-btn ru-show">UI 보이기</button>
      <button type="button" class="ru-btn ru-menu-btn" aria-expanded="false" aria-controls="ripples-panel">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
        <span class="ru-menu-label">메뉴</span>
      </button>
    </div>
    <nav class="ru-panel" id="ripples-panel" aria-label="테스트 화면">
      <div class="ru-name">${esc(screenName)}</div>
      <p class="ru-state" aria-live="polite">서버 상태 확인 중…</p>
      <div class="ru-actions">
        <button type="button" class="ru-btn ru-hide">UI 숨기기</button>
        <button type="button" class="ru-btn ru-fs"${canFullscreen ? '' : ' disabled'}>전체 화면</button>
      </div>
      ${canFullscreen ? '' : '<p class="ru-why">이 브라우저는 페이지 전체 화면을 지원하지 않습니다 (iPhone Safari 등).</p>'}
      <ul>${nav.map(([u, label]) => `<li><a href="${esc(href(u))}"${u === path ? ' aria-current="page"' : ''}>${esc(label)}</a></li>`).join('')}</ul>
    </nav>`;
  const slot = document.getElementById('ripples-nav-slot');
  if (slot) { ui.classList.add('docked'); slot.append(ui); } else document.body.append(ui);
  const $ = sel => ui.querySelector(sel);
  const menuButton = $('.ru-menu-btn');

  // The open state class is "ru-open", not "open": pages have their own .open rules (operator's button grid turned the
  // whole menu into a two-column grid on phones and pushed ‹ 뒤로 off screen).
  const panel = $('.ru-panel');
  const safeProbe = document.createElement('div');
  safeProbe.style.cssText = 'position:fixed;left:0;bottom:0;width:0;height:0;visibility:hidden;padding-bottom:env(safe-area-inset-bottom, 0px)';
  document.body.append(safeProbe);
  function fitPanel() {
    if (!ui.classList.contains('ru-open')) return;
    const vv = window.visualViewport;
    const bottom = (vv ? vv.offsetTop + vv.height : window.innerHeight) - (parseFloat(getComputedStyle(safeProbe).paddingBottom) || 0) - 8;
    let room = bottom - panel.getBoundingClientRect().top;
    // phones: the docked menu grows the mapping navigation area, so it keeps the work area visible (45 %); on PC it is a
    // drop-down over the page (CSS above) and may use the height left
    const pc = window.matchMedia && window.matchMedia('(min-width: 1024px)').matches;
    if (document.body.classList.contains('split') && !pc) room = Math.min(room, (vv ? vv.height : window.innerHeight) * 0.45);
    panel.style.maxHeight = Math.max(120, Math.floor(room)) + 'px';
  }
  function setOpen(open, focusButton) {
    ui.classList.toggle('ru-open', open);
    menuButton.setAttribute('aria-expanded', String(open));
    ui.querySelector('.ru-menu-label').textContent = open ? '닫기' : '메뉴';
    if (open) { panel.scrollTop = 0; fitPanel(); }
    if (focusButton) menuButton.focus();
  }
  window.addEventListener('resize', fitPanel);
  if (window.visualViewport) { window.visualViewport.addEventListener('resize', fitPanel); window.visualViewport.addEventListener('scroll', fitPanel); }
  // Pages with their own hide (mapping's E key) call setHidden(…, true) and listen for 'ripples:hide'.
  function setHidden(hidden, silent) {
    root.classList.toggle('ripples-hide', hidden);
    if (hidden) setOpen(false);
    if (!silent) document.dispatchEvent(new CustomEvent('ripples:hide', {detail: hidden}));
  }
  function toggleFullscreen() {
    if (isFullscreen()) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      Promise.resolve(exit && exit.call(document)).catch(() => {});
    } else if (canFullscreen) {
      const request = root.requestFullscreen || root.webkitRequestFullscreen;
      Promise.resolve(request && request.call(root)).catch(() => {});
    }
  }
  function syncFullscreenLabel() { if (canFullscreen) $('.ru-fs').textContent = isFullscreen() ? '전체 화면 끝내기' : '전체 화면'; }

  menuButton.addEventListener('click', () => setOpen(!ui.classList.contains('ru-open')));
  $('.ru-back').addEventListener('click', goBack);
  $('.ru-show').addEventListener('click', () => setHidden(false));
  $('.ru-hide').addEventListener('click', () => setHidden(true));
  $('.ru-fs').addEventListener('click', () => { toggleFullscreen(); setOpen(false); });
  $('.ru-panel').addEventListener('click', e => { if (e.target.closest('a')) setOpen(false); });
  // Keep taps on this UI from reaching page handlers (corner drag, double-click fullscreen, kiosk wake).
  for (const type of ['pointerdown', 'mousedown', 'touchstart', 'dblclick', 'click']) ui.addEventListener(type, e => e.stopPropagation());
  // Floating menu closes on an outside tap. A docked menu is part of the page flow: closing it on a corner drag would
  // resize the work area under the finger, so it closes only with its button, Escape or a link.
  document.addEventListener('pointerdown', e => { if (ui.classList.contains('ru-open') && !ui.classList.contains('docked') && !ui.contains(e.target)) setOpen(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && ui.classList.contains('ru-open')) setOpen(false, true); }, true);
  document.addEventListener('fullscreenchange', syncFullscreenLabel);
  document.addEventListener('webkitfullscreenchange', syncFullscreenLabel);

  // Status: one request in flight, retry once per 2 s.
  let inflight = false;
  async function poll() {
    if (inflight) return;
    inflight = true;
    const state = $('.ru-state');
    try {
      const signal = AbortSignal.timeout ? AbortSignal.timeout(2000) : undefined;
      const d = await fetch('/inputs', {cache: 'no-store', signal}).then(r => r.json());
      state.textContent = `${d.mock ? 'mock 모드' : '실제 장치 모드'} · ToF ${d.tof.connected ? '연결됨' : '끊김'}`;
      state.className = 'ru-state' + (d.tof.connected ? '' : ' bad');
    } catch (_) {
      state.textContent = '서버 연결 끊김 · 2초마다 다시 연결 시도';
      state.className = 'ru-state bad';
    } finally {
      inflight = false;
    }
  }
  poll();
  setInterval(poll, 2000);
  Object.assign(window.ripplesUI, {setHidden, setOpen});

})();


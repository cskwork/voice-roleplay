// voice-roleplay landing page. No dependencies. Everything here is progressive: without JS the page shows
// English, all feature panels stacked, and the video as a poster with a download link.
(function () {
  'use strict';
  var doc = document.documentElement;
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function lang() { return doc.getAttribute('data-lang') === 'ko' ? 'ko' : 'en'; }

  // ---------------------------------------------------------------- language toggle
  var titles = {
    en: document.title,
    ko: 'voice-roleplay (말하기 연습): 내 Mac에서만 도는 영어 말하기 연습'
  };
  function applyLang(l) {
    doc.setAttribute('data-lang', l);
    doc.lang = l;
    document.title = titles[l];
    // Swap alt text and labels that live in attributes.
    document.querySelectorAll('[data-alt-ko]').forEach(function (el) {
      if (!el.hasAttribute('data-alt-en')) el.setAttribute('data-alt-en', el.getAttribute('alt'));
      el.setAttribute('alt', el.getAttribute(l === 'ko' ? 'data-alt-ko' : 'data-alt-en'));
    });
    document.querySelectorAll('[data-label-ko]').forEach(function (el) {
      if (!el.hasAttribute('data-label-en')) el.setAttribute('data-label-en', el.getAttribute('aria-label'));
      el.setAttribute('aria-label', el.getAttribute(l === 'ko' ? 'data-label-ko' : 'data-label-en'));
    });
    syncVideoLabel();
  }
  var toggle = document.getElementById('lang-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var next = lang() === 'ko' ? 'en' : 'ko';
      try { localStorage.setItem('vr-lang', next); } catch (e) {}
      applyLang(next);
    });
  }

  // ---------------------------------------------------------------- video
  var video = document.getElementById('launch');
  var frame = video && video.closest('.video-frame');
  var btnSound = document.getElementById('v-sound');
  var btnToggle = document.getElementById('v-toggle');

  function syncVideoLabel() {
    if (!btnToggle || !video) return;
    var paused = video.paused;
    var key = 'data-label-' + (paused ? 'play' : 'pause') + (lang() === 'ko' ? '-ko' : '');
    btnToggle.setAttribute('aria-label', btnToggle.getAttribute(key));
    if (frame) frame.classList.toggle('is-paused', paused);
  }
  function play() {
    video.preload = 'auto';
    var p = video.play();
    if (p && p.catch) p.catch(function () { syncVideoLabel(); });
  }
  if (video) {
    video.addEventListener('play', syncVideoLabel);
    video.addEventListener('pause', syncVideoLabel);
    var saveData = navigator.connection && navigator.connection.saveData;
    if (!reduceMotion.matches && !saveData) {
      // Start after the page has loaded so the poster, not the MP4, is the first paint.
      if (document.readyState === 'complete') play();
      else window.addEventListener('load', play, { once: true });
    }
    syncVideoLabel();
    btnSound.addEventListener('click', function () {
      video.currentTime = 0;
      video.muted = false;
      video.loop = false;
      play();
      btnSound.hidden = true;
    });
    video.addEventListener('ended', function () {
      video.muted = true;
      video.loop = true;
      btnSound.hidden = false;
      syncVideoLabel();
    });
    btnToggle.addEventListener('click', function () {
      if (video.paused) play(); else video.pause();
    });
  }

  // ---------------------------------------------------------------- copy buttons
  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var root = btn.closest('[data-copy-root]');
      var src = root.querySelector('[data-copy-text]');
      var cmds = src.querySelectorAll('.c-cmd');
      var text = cmds.length
        ? Array.prototype.map.call(cmds, function (c) { return c.textContent; }).join('\n')
        : src.textContent;
      var done = btn.querySelector('.copy-done');
      function say(msg) {
        done.textContent = msg;
        clearTimeout(btn._t);
        btn._t = setTimeout(function () { done.textContent = ''; }, 1800);
      }
      if (!navigator.clipboard) { say(lang() === 'ko' ? '복사할 수 없어요' : 'Copy not available'); return; }
      navigator.clipboard.writeText(text).then(
        function () { say(lang() === 'ko' ? '복사했어요' : 'Copied'); },
        function () { say(lang() === 'ko' ? '복사하지 못했어요' : 'Could not copy'); }
      );
    });
  });

  // ---------------------------------------------------------------- feature tabs (WAI-ARIA tabs, automatic activation)
  document.querySelectorAll('[data-tabs]').forEach(function (root) {
    var tabs = Array.prototype.slice.call(root.querySelectorAll('[role="tab"]'));
    var panels = tabs.map(function (t) { return document.getElementById(t.getAttribute('aria-controls')); });
    function select(i, focus) {
      tabs.forEach(function (t, j) {
        var on = i === j;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        panels[j].hidden = !on;
        panels[j].classList.remove('is-entering');
      });
      if (!reduceMotion.matches) {
        void panels[i].offsetWidth;
        panels[i].classList.add('is-entering');
      }
      if (focus) tabs[i].focus();
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(i, false); });
      t.addEventListener('keydown', function (e) {
        var n = tabs.length, k = e.key, next = null;
        if (k === 'ArrowRight' || k === 'ArrowDown') next = (i + 1) % n;
        else if (k === 'ArrowLeft' || k === 'ArrowUp') next = (i - 1 + n) % n;
        else if (k === 'Home') next = 0;
        else if (k === 'End') next = n - 1;
        if (next !== null) { e.preventDefault(); select(next, true); }
      });
    });
    panels.forEach(function (p, j) { p.hidden = j !== 0; });
  });

  // ---------------------------------------------------------------- hint ladder in the top bar: which rung are we on?
  var rungNav = document.getElementById('rungs');
  var rungs = Array.prototype.slice.call(document.querySelectorAll('[data-rung]'));
  if (rungNav && rungs.length) {
    var links = rungNav.querySelectorAll('[data-rung-link]');
    var ticking = false;
    function update() {
      ticking = false;
      var line = window.innerHeight * 0.45;
      var current = rungs[0].getAttribute('data-rung');
      rungs.forEach(function (r) { if (r.getBoundingClientRect().top <= line) current = r.getAttribute('data-rung'); });
      links.forEach(function (a) {
        if (a.getAttribute('data-rung-link') === current) a.setAttribute('aria-current', 'step');
        else a.removeAttribute('aria-current');
      });
    }
    window.addEventListener('scroll', function () {
      if (!ticking) { ticking = true; requestAnimationFrame(update); }
    }, { passive: true });
    window.addEventListener('resize', update);
    update();
  }

  // ---------------------------------------------------------------- GitHub stars (optional; hidden on any failure)
  var counts = document.querySelectorAll('[data-star-count]');
  if (counts.length && window.fetch) {
    fetch('https://api.github.com/repos/cskwork/voice-roleplay', { headers: { Accept: 'application/vnd.github+json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || typeof d.stargazers_count !== 'number') return;
        var n = d.stargazers_count;
        var label = n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k' : String(n);
        counts.forEach(function (el) {
          el.textContent = label;
          el.hidden = false;
        });
      })
      .catch(function () {});
  }

  applyLang(lang());
})();

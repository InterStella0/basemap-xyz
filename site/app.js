(function () {
  'use strict';

  const TILE_URL = '${BASEMAP_PUBLIC_URL}/tiles/countries/{z}/{x}/{y}.png';
  const ATTRIBUTION =
    '&copy; <a href="https://queeniemella.cc">queeniemella</a> | ' +
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

  document.querySelectorAll('.copy').forEach(function (button) {
    button.addEventListener('click', function () {
      const source = document.getElementById(button.dataset.copyTarget);
      if (!source) return;

      write(source.textContent).then(function (ok) {
        button.textContent = ok ? 'Copied' : 'Press Ctrl+C';
        button.dataset.copied = String(ok);
        setTimeout(function () {
          button.textContent = 'Copy';
          delete button.dataset.copied;
        }, 1600);
      });
    });
  });

  function write(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return false; }
      );
    }
    // fallback
    try {
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.setAttribute('readonly', '');
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(scratch);
      return Promise.resolve(ok);
    } catch (err) {
      return Promise.resolve(false);
    }
  }

  const tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));

  function select(tab) {
    tabs.forEach(function (other) {
      const selected = other === tab;
      other.setAttribute('aria-selected', String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute('aria-controls')).hidden = !selected;
    });
  }

  tabs.forEach(function (tab, index) {
    tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
    tab.addEventListener('click', function () { select(tab); });
    tab.addEventListener('keydown', function (event) {
      const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
      if (!step) return;
      event.preventDefault();
      const next = tabs[(index + step + tabs.length) % tabs.length];
      select(next);
      next.focus();
    });
  });

  if (typeof L === 'undefined') return;

  const map = L.map('map', {
    center: [48.8566, 2.3522],
    zoom: 3,
    minZoom: 1,
    maxZoom: 20,
    // Scrolling the page must not zoom the map out from under the reader; drag still works.
    scrollWheelZoom: false,
    // Leaflet's own zoom control defaults to the top left, where the nav already is.
    zoomControl: false
  });

  L.control.zoom({ position: 'bottomright' }).addTo(map);
  map.attributionControl.setPrefix('');

  var tiles = L.tileLayer(TILE_URL, {
    maxZoom: 20,
    attribution: ATTRIBUTION,
    crossOrigin: true
  }).addTo(map);

  let warned = false;
  tiles.on('tileerror', function () {
    if (warned) return;
    warned = true;
    console.warn('[dark-basemap] some tiles failed to load');
  });
})();

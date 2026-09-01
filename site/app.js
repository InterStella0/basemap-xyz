(function () {
  'use strict';

  // nginx/docker-entrypoint.sh envsubst's BASEMAP_PUBLIC_URL into this file at container start,
  // exactly as it does for the snippets in index.html — so the page always quotes the host it is
  // actually being served from. TILE_PATH is what the picker rewrites around.
  const TILE_BASE = '${BASEMAP_PUBLIC_URL}';
  const TILE_PATH = '/tiles/';
  const DEFAULT_LAYER = 'countries';

  function tileUrl(layer) {
    return TILE_BASE + TILE_PATH + layer + '/{z}/{x}/{y}.png';
  }

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

  // --- the layer picker ---------------------------------------------------------------------
  // Every snippet on the page hardcodes DEFAULT_LAYER, so the first thing we do is turn each of
  // them back into a template. Re-rendering from that template is what keeps a snippet correct
  // however many times the picker is clicked; patching the live text in place would compound.
  const snippets = Array.prototype.slice
    .call(document.querySelectorAll('.urlbox code, .code code'))
    .filter(function (el) { return el.textContent.indexOf(TILE_PATH + DEFAULT_LAYER + '/') !== -1; })
    .map(function (el) {
      return { el: el, template: el.textContent.split(TILE_PATH + DEFAULT_LAYER + '/') };
    });

  const pickers = Array.prototype.slice.call(document.querySelectorAll('[data-layer-picker]'));
  const listeners = [];
  let current = DEFAULT_LAYER;

  function selectLayer(name) {
    current = name;
    snippets.forEach(function (s) {
      s.el.textContent = s.template.join(TILE_PATH + name + '/');
    });
    pickers.forEach(function (picker) {
      picker.querySelectorAll('button').forEach(function (button) {
        button.setAttribute('aria-pressed', String(button.dataset.layer === name));
      });
    });
    listeners.forEach(function (fn) { fn(name); });
  }

  pickers.forEach(function (picker) {
    picker.addEventListener('click', function (event) {
      const button = event.target.closest('button[data-layer]');
      if (button && button.dataset.layer !== current) selectLayer(button.dataset.layer);
    });
  });

  if (typeof L === 'undefined') return;

  function makeLayer(name) {
    const layer = L.tileLayer(tileUrl(name), {
      maxZoom: 20,
      attribution: ATTRIBUTION,
      crossOrigin: true
    });
    let warned = false;
    layer.on('tileerror', function () {
      if (warned) return;
      warned = true;
      console.warn('[dark-basemap] some tiles failed to load');
    });
    return layer;
  }

  // Swapping the tile source without touching centre, zoom or attribution.
  function follow(map) {
    let tiles = makeLayer(current).addTo(map);
    listeners.push(function (name) {
      const next = makeLayer(name).addTo(map);
      map.removeLayer(tiles);
      tiles = next;
    });
  }

  // --- hero backdrop ------------------------------------------------------------------------
  const hero = document.getElementById('map');
  if (hero) {
    const map = L.map(hero, {
      center: [48.8566, 2.3522],
      zoom: 3,
      minZoom: 1,
      maxZoom: 20,
      // It sits behind the headline, so it is decoration: no scroll capture, no keyboard trap.
      scrollWheelZoom: false,
      keyboard: false,
      // Leaflet's own zoom control defaults to the top left, where the nav already is.
      zoomControl: false
    });
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    map.attributionControl.setPrefix('');
    follow(map);
  }

  // --- the map you can actually use ---------------------------------------------------------
  const demoEl = document.getElementById('demo-map');
  if (demoEl) {
    const map = L.map(demoEl, {
      center: [48.8566, 2.3522],
      zoom: 5,
      minZoom: 1,
      maxZoom: 20,
      // Off until the reader deliberately clicks in, so scrolling past the section still scrolls
      // the page. Leaving the wheel armed makes a full-width map a scroll trap.
      scrollWheelZoom: false,
      zoomControl: true
    });
    map.attributionControl.setPrefix('');
    follow(map);

    const hint = L.DomUtil.create('div', 'map-hint', demoEl);
    hint.textContent = 'Click the map to zoom with the wheel';
    function arm() {
      map.scrollWheelZoom.enable();
      hint.hidden = true;
    }
    function disarm() {
      map.scrollWheelZoom.disable();
      hint.hidden = false;
    }
    map.on('click', arm);
    // focusin rather than focus: the event has to bubble up from whichever control was tabbed to.
    demoEl.addEventListener('focusin', arm);
    demoEl.addEventListener('mouseleave', disarm);
    demoEl.addEventListener('focusout', function (event) {
      if (!demoEl.contains(event.relatedTarget)) disarm();
    });

    // The whole point of the zoom-switched layer is invisible unless you can see the zoom you are
    // at, so show it. DETAIL_FROM_ZOOM mirrors TILE_LAYER_ROUTES in .env — if the route moves, so
    // does this number.
    const DETAIL_FROM_ZOOM = 4;
    const readout = document.getElementById('demo-zoom');
    function report() {
      if (!readout) return;
      const z = map.getZoom();
      const detail = current === 'countries' && z >= DETAIL_FROM_ZOOM;
      readout.textContent = 'z' + z + ' \u00b7 ' + (detail ? 'OpenStreetMap detail' : 'Natural Earth');
    }
    map.on('zoomend', report);
    listeners.push(report);
    report();
  }
})();

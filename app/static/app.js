// Script size control, screen wake lock, ready heartbeat, live timer, and real-time state sync.
(function () {
  var root = document.documentElement;
  var fs = parseFloat(localStorage.getItem('sf_fs') || '1.15');
  root.style.setProperty('--fs', fs);
  window.sfFont = function (d) {
    fs = Math.min(2.2, Math.max(0.9, fs + d));
    root.style.setProperty('--fs', fs);
    try { localStorage.setItem('sf_fs', fs); } catch (e) {}
  };

  var page = document.getElementById('assign');
  if (!page) return;
  var id = page.dataset.id, csrf = page.dataset.csrf;
  try { localStorage.setItem('sf_script_' + id, document.getElementById('scriptbox').innerHTML); } catch (e) {}

  // Screen WakeLock
  var lock = null;
  async function wake() {
    try { if (navigator.wakeLock) lock = await navigator.wakeLock.request('screen'); } catch (e) {}
  }
  wake();
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') wake();
  });

  // Ready & Session Logic
  var ready = false, startBtn = document.getElementById('startbtn'), readyBtn = document.getElementById('readybtn');
  function post(act) {
    var fd = new FormData();
    fd.append('csrf', csrf);
    return fetch('/a/' + id + '/' + act, { method: 'POST', body: fd, credentials: 'same-origin' });
  }

  function setReady(v) {
    ready = v;
    if (!readyBtn) return;
    readyBtn.textContent = v ? 'Ready ✓ (Tap to undo)' : "I'm ready";
    readyBtn.classList.toggle('pri', !v);
    post(v ? 'ready' : 'unready');
  }

  if (readyBtn) readyBtn.addEventListener('click', function () { setReady(!ready); });

  // Elapsed recording timer
  var startedAt = parseFloat(page.dataset.started || '0');
  var timerEl = document.getElementById('rec-timer');
  function updateTimer() {
    if (!timerEl || !startedAt) return;
    var nowSec = Date.now() / 1000;
    var elSec = Math.max(0, Math.floor(nowSec - startedAt));
    var mins = Math.floor(elSec / 60);
    var secs = elSec % 60;
    timerEl.textContent = (mins < 10 ? '0' : '') + mins + ':' + (secs < 10 ? '0' : '') + secs;
  }
  if (timerEl) {
    updateTimer();
    setInterval(updateTimer, 1000);
  }

  // Real-time polling (every 3 seconds)
  function poll() {
    fetch('/a/' + id + '/state', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        // Status transition (e.g. ASSIGNED -> IN_SESSION, or IN_SESSION -> CONFIRMED)
        if (s.status !== page.dataset.status) {
          location.reload();
          return;
        }

        // Role switch sync (if partner swapped roles)
        if (s.me_role && page.dataset.merole && s.me_role !== page.dataset.merole) {
          location.reload();
          return;
        }

        // Partner readiness
        var p = document.getElementById('pready');
        var ptxt = document.getElementById('ptxt');
        if (p && ptxt) {
          p.className = 'dot' + (s.partner_ready ? ' on' : '');
          ptxt.textContent = s.partner_ready ? '✅ Partner is ready to record!' : '⏳ Waiting for partner to be ready...';
        }
        if (startBtn) {
          startBtn.disabled = !(s.partner_ready && s.me_ready);
        }

        // Live recording state
        if (s.started_at && !startedAt) {
          startedAt = s.started_at;
        }

        // Partner completed status
        var pdone = document.getElementById('partner-done-status');
        if (pdone) {
          pdone.textContent = s.partner_done ? '✅ Partner marked recording completed!' : '⏳ Partner is recording...';
        }
      })
      .catch(function () {});

    if (ready) post('ready');
  }

  setInterval(poll, 3000);
  poll();
})();

// Generic In-Tab Table Filter
window.sfFilterTable = function (inputId, tableId, countId, noMatchId) {
  var input = document.getElementById(inputId);
  if (!input) return;
  var q = (input.value || '').toLowerCase().trim();
  var table = document.getElementById(tableId);
  if (!table) return;

  var clearBtn = document.getElementById(inputId + '-clear');
  if (clearBtn) {
    clearBtn.style.display = q ? 'block' : 'none';
  }

  var tbody = table.querySelector('tbody');
  if (!tbody) return;
  var rows = tbody.querySelectorAll('tr:not(.no-filter)');
  var count = 0;
  var totalRows = 0;

  rows.forEach(function (r) {
    if (r.id === noMatchId) return;
    if (r.querySelector('td.mute[colspan]')) {
      r.style.display = q ? 'none' : '';
      return;
    }
    totalRows++;
    var text = r.innerText.toLowerCase();
    if (!q || text.indexOf(q) !== -1) {
      r.style.display = '';
      count++;
    } else {
      r.style.display = 'none';
    }
  });

  if (countId) {
    var cEl = document.getElementById(countId);
    if (cEl) {
      cEl.textContent = q ? count + ' of ' + totalRows : totalRows;
    }
  }

  if (noMatchId) {
    var nmEl = document.getElementById(noMatchId);
    if (!nmEl && count === 0 && totalRows > 0) {
      nmEl = document.createElement('tr');
      nmEl.id = noMatchId;
      nmEl.className = 'no-filter';
      nmEl.innerHTML = '<td colspan="15" style="text-align:center; padding:2rem; color:#94a3b8">🔍 No matching records found.</td>';
      tbody.appendChild(nmEl);
    }
    if (nmEl) {
      nmEl.style.display = (count === 0 && totalRows > 0) ? '' : 'none';
    }
  }
};

window.sfClearFilter = function (inputId, tableId, countId, noMatchId) {
  var input = document.getElementById(inputId);
  if (input) {
    input.value = '';
    window.sfFilterTable(inputId, tableId, countId, noMatchId);
    input.focus();
  }
};


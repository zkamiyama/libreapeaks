const $ = id => document.getElementById(id);

async function json(url, options = {}) {
  const response = await fetch(url, options);
  let body = {};
  try { body = await response.json(); } catch (_error) { /* ignore */ }
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function showLaunch(show) {
  $('launchPanel').classList.toggle('hidden', !show);
}

function showConfig(show) {
  $('configPanel').classList.toggle('hidden', !show);
}

function setProgress(session) {
  $('launchProgress').value = Math.max(0, Math.min(100, Number(session.progress) || 0));
  $('launchStatus').textContent = session.error
    ? `Preparation failed: ${session.error}`
    : session.status || (session.ready ? 'Ready' : 'Waiting for audio');
}

function updateConfigVisibility() {
  const policy = $('cachePolicy').value;
  const central = policy === 'reaper-central';
  const follow = policy === 'reaper-config';
  $('centralDirectoryRow').hidden = !central;
  $('reaperIniRow').hidden = !follow;
  $('autoReaperIniRow').hidden = !follow;
  const verifyAvailable = central || follow;
  $('verifyReaperRow').hidden = !verifyAvailable;
  $('reaperExecutableRow').hidden = !verifyAvailable || !$('verifyReaper').checked;
  $('reaperIni').disabled = follow && $('autoReaperIni').checked;
}

function populateConfig(payload) {
  const cache = payload.cache || {};
  $('cachePolicy').value = cache.policy || 'sidecar';
  $('centralDirectory').value = cache.cache_directory || '';
  $('reaperIni').value = cache.reaper_ini || '';
  $('autoReaperIni').checked = Boolean(cache.auto_reaper_ini);
  $('configPeakRate').value = cache.peak_rate == null ? '0' : String(cache.peak_rate);
  $('verifyReaper').checked = Boolean(cache.verify_with_reaper);
  $('reaperExecutable').value = cache.reaper_executable || '';
  $('configPath').textContent = payload.config_path ? `Saved in ${payload.config_path}` : '';
  $('configStatus').textContent = '';
  updateConfigVisibility();
}

async function loadConfig() {
  const payload = await json('/api/config');
  populateConfig(payload);
  return payload;
}

function configPayload() {
  const peakRate = Number.parseInt($('configPeakRate').value || '0', 10);
  return {
    version: 1,
    cache: {
      policy: $('cachePolicy').value,
      cache_directory: $('centralDirectory').value.trim(),
      reaper_ini: $('reaperIni').value.trim(),
      auto_reaper_ini: $('autoReaperIni').checked,
      verify_with_reaper: $('verifyReaper').checked,
      reaper_executable: $('reaperExecutable').value.trim(),
      peak_rate: Number.isFinite(peakRate) && peakRate > 0 ? peakRate : null,
    },
  };
}

async function saveConfig() {
  $('configStatus').textContent = 'Saving…';
  const payload = await json('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(configPayload()),
  });
  populateConfig(payload);
  $('launchStatus').textContent = 'Cache settings saved; they apply to the next opened audio file.';
  showConfig(false);
}

function installConfigHandlers() {
  const open = async () => {
    try {
      await loadConfig();
      showConfig(true);
    } catch (error) {
      showConfig(true);
      $('configStatus').textContent = String(error);
    }
  };
  $('cacheSettingsButton').addEventListener('click', open);
  $('launchSettingsButton').addEventListener('click', open);
  $('configCancelButton').addEventListener('click', () => showConfig(false));
  $('configSaveButton').addEventListener('click', () => {
    saveConfig().catch(error => { $('configStatus').textContent = String(error); });
  });
  $('cachePolicy').addEventListener('change', updateConfigVisibility);
  $('autoReaperIni').addEventListener('change', updateConfigVisibility);
  $('verifyReaper').addEventListener('change', updateConfigVisibility);
}

async function startUi() {
  await import('/app.js');
  await import('/daw_app.mjs');
}

async function waitForRevision(targetRevision) {
  showLaunch(true);
  for (;;) {
    const session = await json('/api/session');
    setProgress(session);
    if (session.error && !session.building) throw new Error(session.error);
    if (!session.building && session.ready && Number(session.revision) >= targetRevision) {
      location.reload();
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
}

async function openFile(file) {
  if (!file) return;
  showLaunch(true);
  $('launchProgress').value = 0;
  $('launchStatus').textContent = `Uploading ${file.name}…`;
  const query = new URLSearchParams({name: file.name});
  const response = await json(`/api/open?${query}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/octet-stream'},
    body: file,
  });
  await waitForRevision(Number(response.target_revision));
}

function installOpenHandlers() {
  const input = $('audioFileInput');
  $('openAudioButton').addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    const file = input.files?.[0];
    input.value = '';
    openFile(file).catch(error => {
      showLaunch(true);
      $('launchStatus').textContent = String(error);
    });
  });

  const drop = $('launchDrop');
  const enter = event => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    showLaunch(true);
    drop.classList.add('dragging');
  };
  document.addEventListener('dragenter', enter);
  document.addEventListener('dragover', enter);
  document.addEventListener('dragleave', event => {
    if (!event.relatedTarget) drop.classList.remove('dragging');
  });
  document.addEventListener('drop', event => {
    event.preventDefault();
    drop.classList.remove('dragging');
    const file = [...(event.dataTransfer?.files || [])][0];
    openFile(file).catch(error => {
      showLaunch(true);
      $('launchStatus').textContent = String(error);
    });
  });
}

async function init() {
  installOpenHandlers();
  installConfigHandlers();
  await loadConfig();
  const session = await json('/api/session');
  setProgress(session);
  if (!session.ready) {
    showLaunch(true);
    return;
  }
  showLaunch(Boolean(session.building));
  await startUi();
}

init().catch(error => {
  showLaunch(true);
  $('launchStatus').textContent = String(error);
});

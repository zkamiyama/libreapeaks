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

function setProgress(session) {
  $('launchProgress').value = Math.max(0, Math.min(100, Number(session.progress) || 0));
  $('launchStatus').textContent = session.error
    ? `Preparation failed: ${session.error}`
    : session.status || (session.ready ? 'Ready' : 'Waiting for audio');
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

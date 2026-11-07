import { Terminal } from '@xterm/xterm';
import { startDesk } from './desk';

type Check = { name: string; command: string; expected: string; weight: number; timeout: number };
type Learning = { enabled?: boolean; rounds?: number; inputs?: string; checks?: Check[] };
type ScoreReport = { status?: string; baseline?: number; candidate?: number; delta?: number | null; retained?: number; adopted?: boolean; total_trials?: number; trial_count?: number; selected?: string; workspace?: string; fallbacks?: string[] };
type Trial = { label: string; score: number | null; status: string; workspace?: string; duration?: number; checks: { name: string; passed: boolean; command_id: string; weight: number; output: string; exit_code: number | null }[] };
type Task = { learning?: Learning; learning_report?: ScoreReport; id: string; title: string; goal: string; steps: string[]; skills: string[]; status: string; created: string; result: string };
type Skill = { id: string; name: string; description: string; body: string; enabled: boolean };
type Memory = { id: string; title: string; content: string; kind: string; evidence: string[]; created: string; uses: number };
type Command = { id: string; command: string; started: string; status: string; exit_code: number | null; cwd: string; output?: string };
type Message = { id: number; role: string; content: string; created: string };
type AppState = {
  browser: { running: boolean; url: string; title: string; updated?: number; tabs: { index: number; url: string; active: boolean }[] };
  agent: { trial?: string; phase: string; active: string | null; paused: boolean; manual: boolean; auto_queue: boolean };
  tasks: Task[]; skills: Skill[]; memories: Memory[]; commands: Command[]; messages: Message[];
  events: { id: number; kind: string; text: string; created: string }[];
  model: { provider: string; base_url: string; model: string; has_key: boolean };
  terminal: { cwd: string; alive: boolean; ready: boolean; cols: number; rows: number; session_id: string };
  counts: { tasks: number; commands: number; memories: number; messages: number };
  runtime: string;
};
type TerminalEvent = { type: string; data?: string; cols?: number; rows?: number; session_id?: string };

const icons: Record<string, string> = {
  browser: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  desk: '<rect x="3" y="3" width="18" height="13" rx="2"/><path d="M8 21h8M12 16v5M6 7l3 3-3 3m6 0h4"/>',
  tasks: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2M9 9h6M9 13h6M9 17h4"/>',
  skills: '<path d="M9 3H4v6a3 3 0 1 1 0 6v5h6a3 3 0 1 0 5 0h5v-6a3 3 0 1 1 0-5V3h-6a3 3 0 1 0-5 0Z"/>',
  memory: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0v-7"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  settings: '<path d="m9 3 6 0 1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1Z"/><circle cx="12" cy="12" r="3"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 1 1 5 2c-2 1-2 2-2 3m0 3h.01"/>',
  plug: '<path d="M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0Zm6 10v4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  terminal: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4m7 0h5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  play: '<path d="m8 4 12 8-12 8Z"/>',
  code: '<path d="m7 7-5 5 5 5M17 7l5 5-5 5M14 4l-4 16"/>',
  note: '<path d="M14 2H5v20h14V7ZM14 2v6h5M8 12h8M8 16h6"/>',
  chat: '<path d="M21 11a9 9 0 0 1-13 8l-5 2 1-6A9 9 0 1 1 21 11Z"/><path d="M8 10h8m-8 4h5"/>',
  arrow: '<path d="M12 20V4m-6 6 6-6 6 6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
};
const icon = (name: string): string => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] ?? icons.terminal}</svg>`;
function fillIcons(): void { document.querySelectorAll<HTMLElement>('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon ?? 'terminal'); }); }
function el<T extends HTMLElement = HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing UI element: ${id}`);
  return node as T;
}
function esc(value: unknown): string { return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c); }
function prettyTime(value: string): string { return new Date(value).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
function text(id: string, value: string): void { el(id).textContent = value; }
let toastTimer = 0;
function toast(message: string): void { text('toast', message); el('toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = window.setTimeout(() => el('toast').classList.remove('visible'), 5500); if (el<HTMLDialogElement>('modal').open) { let status = document.getElementById('dialog-status'); if (!status) { status = document.createElement('p'); status.id = 'dialog-status'; status.className = 'form-status'; status.setAttribute('role', 'status'); el('modal-body').append(status); } status.textContent = message; } }
function caught(error: unknown): void { toast(error instanceof Error ? error.message : String(error)); }
function bind(id: string, fn: () => void | Promise<void>): void { el(id).addEventListener('click', () => { Promise.resolve().then(fn).catch(caught); }); }
function submit(id: string, fn: () => Promise<void>): void { el<HTMLFormElement>(id).addEventListener('submit', event => { event.preventDefault(); void fn().catch(caught); }); }
function field(id: string): string { return el<HTMLInputElement>(id).value.trim(); }
function readSaved<T>(key: string, fallback: T): T { try { return JSON.parse(localStorage.getItem(key) ?? 'null') as T ?? fallback; } catch { return fallback; } }

let state: AppState | null = null;
let connected = false;
let socket: WebSocket | null = null;
let expanded = false;
let currentView = 'workspace';
let activityTab = 'commands';
let messageStamp = '';
let refreshBusy = false;
let reconnectTimer = 0;
let pairingToken = sessionStorage.getItem('terminal-token') ?? '';
let endpoint = localStorage.getItem('terminal-endpoint') ?? (['8765', '4173'].includes(location.port) ? location.origin : 'http://127.0.0.1:8765');
let drafts = readSaved<Task[]>('terminal-drafts', []);

async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (!pairingToken) throw new Error('Connect your Linux runtime first.');
  const response = await fetch(endpoint + '/api' + path, {
    method, headers: { Authorization: `Bearer ${pairingToken}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(210000),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: `Runtime returned HTTP ${response.status}` })) as { detail?: unknown };
    throw new Error(typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail));
  }
  return await response.json() as T;
}

const term = new Terminal({ cols: 100, rows: 28, fontSize: 12, lineHeight: 1.03, fontFamily: 'Consolas, "Liberation Mono", monospace', cursorBlink: true,
  scrollback: 10000, theme: { background: '#0b100b', foreground: '#c5d7b0', cursor: '#d3e9a3', selectionBackground: '#415633', green: '#acd38e', brightGreen: '#d3e9a3' } });
term.open(el('terminal'));
term.onData(data => { if (state?.agent.manual && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });

function terminalScale(): void {
  const host = expanded ? el('terminal-window') : el('terminal-anchor');
  const width = expanded ? Math.min(host.clientWidth - 30, 1300) : host.clientWidth;
  const height = expanded ? host.clientHeight - 120 : host.clientHeight;
  const scale = Math.min(width / 780, height / 390);
  el('terminal-scale').style.transform = `scale(${scale})`;
  if (expanded) {
    el('terminal-scale').style.width = '780px';
    el('terminal-scale').style.height = `${390 * scale}px`;
    el('terminal-scale').style.transformOrigin = 'top center';
  } else {
    el('terminal-scale').style.height = '390px';
    el('terminal-scale').style.transformOrigin = '0 0';
  }
}
new ResizeObserver(terminalScale).observe(el('scene'));
window.addEventListener('resize', terminalScale);
function expand(value: boolean): void { expanded = value; el('terminal-window').classList.toggle('expanded', value); el('terminal-scrim').hidden = !value; terminalScale(); if (value && state?.agent.manual) term.focus(); }
bind('expand-terminal', () => expand(true)); bind('collapse-terminal', () => expand(false)); bind('terminal-scrim', () => expand(false));
document.addEventListener('keydown', event => { if (event.key === 'Escape' && expanded) expand(false); });

function connectSocket(): void {
  clearTimeout(reconnectTimer);
  if (socket) { socket.onclose = null; socket.close(); }
  const url = new URL(endpoint); url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'; url.pathname = '/ws/terminal'; url.search = '';
  const ws = new WebSocket(url);
  socket = ws;
  ws.onopen = () => ws.send(JSON.stringify({ token: pairingToken }));
  ws.onmessage = event => {
    const msg = JSON.parse(String(event.data)) as TerminalEvent;
    if (msg.type === 'reset') { term.reset(); }
    if (msg.type === 'output' && msg.data) { term.write(Uint8Array.from(atob(msg.data), char => char.charCodeAt(0))); }
    if (msg.type === 'resize' && msg.cols && msg.rows) { term.resize(msg.cols, msg.rows); }
    if (msg.type === 'synced') { el('terminal-offline').hidden = true; }
    if (msg.type === 'shell_exit') { toast('The Linux shell exited. Restart the runtime for a new session.'); }
    if (msg.type === 'resync') { ws.close(); }
  };
  ws.onclose = () => { if (socket !== ws) return; el('terminal-offline').hidden = false; if (connected) reconnectTimer = window.setTimeout(connectSocket, 2000); };
}

async function refresh(): Promise<void> {
  if (refreshBusy || !pairingToken) return;
  refreshBusy = true;
  try {
    state = await api<AppState>('/state');
    const fresh = !connected;
    connected = true;
    if (fresh) connectSocket();
    render();
  } catch { if (connected) { connected = false; el('terminal-offline').hidden = false; render(); } }
  finally { refreshBusy = false; }
}

function empty(title: string, body: string, symbol = 'tasks'): string { return `<div class="empty-state">${icon(symbol)}<h2>${esc(title)}</h2><p>${esc(body)}</p></div>`; }
function changeView(view: string): void {
  if (!['workspace', 'tasks', 'browser', 'skills', 'memory', 'activity'].includes(view)) return;
  currentView = view;
  document.querySelectorAll<HTMLElement>('.view').forEach(v => v.classList.toggle('active', v.id === `view-${view}`));
  document.querySelectorAll<HTMLElement>('.nav[data-view]').forEach(v => v.classList.toggle('active', v.dataset.view === view));
  text('breadcrumb', ({ workspace: 'Overview', browser: 'Browser', tasks: 'Task queue', skills: 'Skills', memory: 'Memory', activity: 'Recordings' } as Record<string, string>)[view]);
  history.replaceState(null, '', `#${view}`); renderLists();
}
document.querySelectorAll<HTMLElement>('[data-view]').forEach(button => button.addEventListener('click', () => changeView(button.dataset.view ?? 'workspace')));

function render(): void {
  const phase = connected ? state?.agent.phase ?? 'idle' : 'offline';
  const queued = (state?.tasks.filter(t => t.status === 'queued').length ?? 0) + drafts.length;
  text('queue-count', String(queued)); text('task-stat', String(queued)); text('memory-stat', String(state?.counts.memories ?? 0));
  text('skills-stat', state ? String(state.skills.filter(s => s.enabled).length) : '—');
  text('connection-text', connected ? 'Runtime connected' : 'Connect runtime');
  el('connect-open').querySelector('.status-dot')?.classList.toggle('on', connected);
  const description = !connected ? 'Connect a runtime to bring your workspace to life.' : state?.agent.manual ? 'You have the keyboard. The agent is paused.' : state?.agent.paused ? 'Paused. The current command may still be finishing.' : ({ idle: 'At the desk, ready for your next task.', thinking: 'Planning the next step with your selected model.', working: 'Running a command in the live terminal.', learning: 'Harness² is improving the procedure using recorded evidence.' } as Record<string, string>)[phase] ?? phase;
  const label = !connected ? 'Offline' : state?.agent.manual ? 'Manual control' : state?.agent.paused ? 'Paused' : phase.charAt(0).toUpperCase() + phase.slice(1);
  text('operator-status', label); text('operator-description', description + (state?.agent.trial ? ' · ' + state.agent.trial : '')); text('scene-state', connected ? description : 'A quiet moment before the next idea.');
  text('agent-badge', connected ? label : 'Waiting for connection');
  text('manual-toggle', state?.agent.manual ? 'Return control' : 'Take control'); text('manual-expanded', state?.agent.manual ? 'Return control' : 'Take control');
  el('pause-agent').innerHTML = icon(state?.agent.paused ? 'play' : 'pause');
  text('terminal-cwd', state?.terminal.cwd ?? 'No runtime connected');
  text('chat-model', connected && state?.model.model ? state.model.model : connected ? 'Choose a model in settings' : 'No model connected');
  text('shell-label', connected ? (state?.runtime === 'docker' ? 'Linux · Docker' : 'Linux · process') : 'Linux terminal');
  text('memory-footer', connected ? 'Persistent memory · recording to disk' : state ? 'Disconnected · showing last received state' : 'Memory is saved by your connected runtime');
  if (state) {
    const stamp = state.messages.map(m => m.id).join(',');
    if (stamp !== messageStamp) {
      messageStamp = stamp;
      if (state.messages.length) {
        const messages = el('messages'); const bottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
        messages.innerHTML = state.messages.map(m => `<div class="message ${esc(m.role)}"><div class="message-meta"><span>${m.role === 'agent' ? 'OPERATOR' : m.role === 'user' ? 'YOU' : 'RUNTIME'}</span><span>${esc(prettyTime(m.created))}</span></div><p>${esc(m.content)}</p></div>`).join('');
        if (bottom) messages.scrollTop = messages.scrollHeight;
      }
    }
  }
  renderLists();
  renderBrowser();
}

function allTasks(): Task[] { return [...drafts, ...(state?.tasks ?? [])]; }
function renderLists(): void {
  if (currentView === 'tasks') {
    el('task-list').innerHTML = allTasks().length ? allTasks().map(t => `<article class="record-card"><div class="card-meta"><span>${esc(prettyTime(t.created))}</span><span class="badge ${t.status === 'completed' ? 'success' : t.status === 'failed' ? 'failed' : ''}">${esc(t.status.toUpperCase())}</span></div><h3>${esc(t.title)}</h3>${t.steps.length ? `<ol class="task-steps">${t.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}${t.result ? `<p>${esc(t.result)}</p>` : ''}${scoreCard(t)}<div class="card-actions"><button class="text-button" data-learning="${esc(t.id)}">Learning & score ↗</button>${['draft', 'queued', 'failed', 'cancelled', 'blocked', 'interrupted'].includes(t.status) ? `<button class="secondary" data-run-task="${esc(t.id)}">${t.status === 'draft' ? 'Connect & run' : t.status === 'queued' ? 'Run task' : 'Retry task'}</button>` : ''}${['draft', 'queued'].includes(t.status) ? `<button class="text-button" data-cancel-task="${esc(t.id)}">Remove</button>` : ''}<span class="subtle-label">${t.skills.length} SKILLS SELECTED</span></div></article>`).join('') : empty('A clear desk. A fresh queue.', 'Create a task with a goal and optional steps. Your operator runs one task at a time.');
  }
  if (currentView === 'skills') {
    el('skill-list').innerHTML = state?.skills.length ? state.skills.map(s => `<article class="record-card"><div class="card-meta">${icon('skills')}<span class="badge">${s.enabled ? 'ENABLED' : 'DISABLED'}</span></div><h3>${esc(s.name)}</h3><p>${esc(s.description)}</p><div class="card-actions"><button class="text-button" data-edit-skill="${esc(s.id)}">Edit instructions ↗</button><button class="text-button" data-toggle-skill="${esc(s.id)}">${s.enabled ? 'Disable' : 'Enable'}</button></div></article>`).join('') : empty('Bring your skills to the desk.', 'Connect your runtime to load its installed skills, or prepare a new Markdown skill to import.', 'skills');
  }
  if (currentView === 'memory') {
    const search = field('memory-search').toLowerCase();
    const memories = (state?.memories ?? []).filter(m => `${m.title} ${m.content}`.toLowerCase().includes(search));
    el('memory-list').innerHTML = memories.length ? memories.map(m => `<article class="record-card"><div class="card-meta"><span>${esc(m.kind.toUpperCase())} · RECALLED ${m.uses} TIMES</span><span>${esc(prettyTime(m.created))}</span></div><h3>${esc(m.title)}</h3><p>${esc(m.content)}</p><div class="card-actions">${m.evidence.map((id, index) => `<button class="text-button" data-command="${esc(id)}">Evidence ${index + 1} ↗</button>`).join('')}</div></article>`).join('') : empty(search ? 'No matching memories.' : 'Experience starts here.', search ? 'Try a different word.' : 'Successful procedures will appear with their command evidence. You can also add notes and preferences.', 'memory');
  }
  if (currentView === 'activity') {
    if (activityTab === 'commands') el('activity-list').innerHTML = state?.commands.length ? state.commands.map(c => `<article class="record-card"><div class="card-meta"><span>${esc(prettyTime(c.started))}</span><span class="badge ${c.exit_code === 0 ? 'success' : c.exit_code !== null ? 'failed' : ''}">${c.exit_code === null ? esc(c.status) : 'EXIT ' + c.exit_code}</span></div><h3><code>${esc(c.command)}</code></h3><div class="card-actions"><button class="text-button" data-command="${esc(c.id)}">Read actual output ↗</button><span class="subtle-label">${esc(c.cwd)}</span></div></article>`).join('') : empty('An honest record of the work.', 'Commands appear here once they run. Raw terminal recordings are included in memory exports.', 'activity');
    if (activityTab === 'events') el('activity-list').innerHTML = state?.events.length ? state.events.map(e => `<article class="record-card"><div class="card-meta"><span>${esc(e.kind.toUpperCase())}</span><span>${esc(prettyTime(e.created))}</span></div><p>${esc(e.text)}</p></article>`).join('') : empty('No events yet.', 'Runtime connections, skill changes and memory updates are recorded here.', 'activity');
    if (activityTab === 'apps') el('activity-list').innerHTML = `<div class="info-strip">These launchers run the displayed command in the same live terminal. They do not require a model.</div>` + [
      ['System information', 'uname -a; printf "\\nAvailable tools:\\n"; command -v python3 node npm git curl'],
      ['Inspect workspace', 'pwd; ls -lah'], ['Python', 'python3 --version'], ['Node.js', 'node --version; npm --version'],
      ['Own Chromium browser', 'terminal-browser start'], ['Git', 'git --version'], ['Harness² self-check', 'harness2-demo --output harness2-self-check --k 1'],
      ['Serve workspace on port 8081', 'python3 -m http.server 8081 --bind 0.0.0.0 > server.log 2>&1 &'],
    ].map(([name, command]) => `<article class="record-card"><h3>${name}</h3><code>${esc(command)}</code><div class="card-actions"><button class="secondary" data-launch-command="${esc(command)}">Run in terminal</button></div></article>`).join('');
  }
}

function modal(title: string, body: string, eyebrow = 'WORKSPACE'): void { text('modal-title', title); text('modal-eyebrow', eyebrow); el('modal-body').innerHTML = body; fillIcons(); if (!el<HTMLDialogElement>('modal').open) el<HTMLDialogElement>('modal').showModal(); }
function closeModal(): void { el<HTMLDialogElement>('modal').close(); }
bind('close-modal', closeModal);
el<HTMLDialogElement>('modal').addEventListener('click', event => { if (event.target === el('modal')) { const rect = el('modal').getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal(); } });

function settings(): void {
  modal('Connect your workspace', `<p>The interface connects to your own Linux runtime. The terminal and agent run there; your browser is the window into it.</p><form id="connect-form"><label class="field"><span>Runtime URL</span><input id="runtime-url" type="url" required value="${esc(endpoint)}"></label><label class="field"><span>Pairing token</span><input id="runtime-token" type="password" autocomplete="off" required placeholder="Paste the token from your runtime"><small>Stored for this browser session only. Obtain it with docker compose exec terminal cat /data/token.</small></label><div class="button-row"><button class="primary" type="submit">Connect runtime</button><button class="secondary" type="button" id="disconnect">Disconnect</button></div><div class="form-status" id="connection-result"></div></form><form id="model-form"><label class="field"><span>Model provider</span><select id="model-provider"><option value="ollama">Ollama · local model</option><option value="compatible">OpenAI-compatible endpoint</option></select></label><label class="field"><span>Model API base URL</span><input id="model-url" type="url" required value="${esc(state?.model.base_url ?? 'http://host.docker.internal:11434')}"><small>For a compatible API, include its base path, for example /v1.</small></label><div class="form-grid"><label class="field"><span>Model name</span><input id="model-name" list="model-options" value="${esc(state?.model.model ?? '')}" placeholder="Choose an installed model"><datalist id="model-options"></datalist></label><label class="field"><span>API key (if required)</span><input id="model-key" type="password" autocomplete="off" placeholder="${state?.model.has_key ? 'Saved · blank keeps existing key' : 'Not needed for local Ollama'}"></label></div><div class="form-actions"><button class="secondary" type="button" id="test-model">Save & discover models</button><button class="primary" type="submit">Save model</button></div><div class="form-status" id="model-result"></div></form>`, 'RUNTIME SETTINGS');
  el<HTMLInputElement>('runtime-token').value = pairingToken;
  el<HTMLSelectElement>('model-provider').value = state?.model.provider ?? 'ollama';
  submit('connect-form', async () => {
    endpoint = field('runtime-url').replace(/\/$/, '');
    const url = new URL(endpoint); if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Use an HTTP or HTTPS runtime URL.');
    pairingToken = field('runtime-token'); text('connection-result', 'Connecting…');
    try {
      state = await api<AppState>('/state'); connected = true;
      sessionStorage.setItem('terminal-token', pairingToken); localStorage.setItem('terminal-endpoint', endpoint);
      connectSocket(); render(); text('connection-result', 'Connected. Your terminal stream is live.');
      el<HTMLInputElement>('model-url').value = state.model.base_url; el<HTMLInputElement>('model-name').value = state.model.model; el<HTMLSelectElement>('model-provider').value = state.model.provider;
    } catch (error) { connected = false; text('connection-result', 'Could not connect. Check the URL and token. From the hosted UI, your browser may require local-network permission; you can also open http://localhost:8765 directly.'); render(); throw error; }
  });
  bind('disconnect', () => { connected = false; pairingToken = ''; sessionStorage.removeItem('terminal-token'); clearTimeout(reconnectTimer); if (socket) { socket.onclose = null; socket.close(); socket = null; } el('terminal-offline').hidden = false; render(); text('connection-result', 'Browser disconnected. The runtime and queued work remain on your machine.'); });
  async function saveModel(): Promise<void> { await api('/model', 'POST', { provider: field('model-provider'), base_url: field('model-url'), model: field('model-name'), api_key: field('model-key') || null }); await refresh(); }
  submit('model-form', async () => { await saveModel(); text('model-result', 'Model settings saved. Run a task when you are ready.'); });
  bind('test-model', async () => { text('model-result', 'Checking model endpoint…'); await saveModel(); const response = await api<{ models: string[] }>('/models'); el('model-options').innerHTML = response.models.map(name => `<option value="${esc(name)}"></option>`).join(''); text('model-result', response.models.length ? `Connected. Available models: ${response.models.join(', ')}` : 'Connected, but no models were returned. Enter the exact model name.'); if (!field('model-name') && response.models.length) el<HTMLInputElement>('model-name').value = response.models[0]; });
}
bind('settings-open', settings); bind('connect-open', settings);

function guide(): void {
  modal('Your operator, in a few steps.', `<p>terminal.. uses a real Linux container and the Harness² persistent library. There is no simulated shell behind the desk.</p><ol class="guide-steps"><li><strong>Start the runtime</strong><p>Install Docker Desktop on Windows or macOS, or Docker Engine with Compose on Linux. Extract the supplied source bundle and run <code>start.bat</code> on Windows or <code>./start.sh</code> on Linux/macOS.</p></li><li><strong>Open your local workspace</strong><p>Open <a class="inline-link" href="http://localhost:8765" target="_blank" rel="noopener">http://localhost:8765</a>. Copy the pairing token printed by the launcher into Runtime settings.</p></li><li><strong>Choose the model</strong><p>Connect a model you already run in Ollama or configure a compatible API. Model access is separate from Harness². Discover models, choose one, then save.</p></li><li><strong>Give it a task</strong><p>Select skills and add any steps. Commands, results and conversations persist in the runtime. Learned procedures link back to successful command evidence.</p></li></ol><div class="info-strip">The live adapter uses Harness²'s component tree and experience library. Enable Repeated learning in a task to run real Harness² sequential recursion: four executions per round, in fresh workspace folders and browser profiles. Add verification commands to measure a score from 0 to 100. The final procedure is retained only if it beats the baseline. External services remain shared, so use repeatable tasks. A Docker container shares its host's Linux kernel; it is not a full virtual machine.</div><div class="form-actions"><button class="primary" id="guide-settings">Open runtime settings</button></div>`, 'GETTING STARTED');
  bind('guide-settings', settings);
}
bind('guide-open', guide); bind('guide-inline', guide);

function scoreCard(task: Task): string {
  const r = task.learning_report;
  const mode = task.learning?.enabled ? `REPEATED LEARNING · ${4 * (task.learning.rounds ?? 1)} TRIALS` : 'RUN ONCE · RECORDING ON';
  if (r?.retained === undefined) return `<div class="learning-summary"><span>${mode}</span><strong>${r?.status === 'running' ? 'Measuring…' : 'Not measured'}</strong></div>`;
  return `<div class="learning-summary"><span>${mode}</span><div class="score-line"><strong>${r.retained}<small>/100</small></strong>${r.delta !== null && r.delta !== undefined ? `<span class="score-delta ${r.delta < 0 ? 'negative' : ''}">${r.delta > 0 ? '+' : ''}${r.delta} points</span>` : ''}<span>${r.adopted ? 'Improved procedure retained' : task.learning?.enabled ? 'Baseline retained' : 'Verified checks'}</span></div></div>`;
}
function learningFields(config: Learning = {}): string {
  return `<section class="learning-fields"><label class="learning-toggle"><span><strong>Repeated learning</strong><small>Run, inspect, improve, compare.</small></span><input id="learning-enabled" type="checkbox" role="switch" ${config.enabled ? 'checked' : ''}></label><div class="form-grid"><label class="field"><span>Learning rounds</span><select id="learning-rounds"><option value="1" ${config.rounds === 1 ? 'selected' : ''}>1 round · 4 executions</option><option value="2" ${config.rounds === 2 ? 'selected' : ''}>2 rounds · 8 executions</option><option value="3" ${config.rounds === 3 ? 'selected' : ''}>3 rounds · 12 executions</option></select></label><label class="field"><span>Input folder · optional</span><input id="learning-inputs" value="${esc(config.inputs ?? '')}" placeholder="project-folder"><small>Inside /workspace. Blank starts each trial empty.</small></label></div><p id="learning-description" class="form-status"></p><label class="field"><span>Verification checks</span><small>Score = passed check weights ÷ all weights × 100. A check needs exit 0 and any required output text. Add checks even with learning off to measure a single run.</small></label><div id="verification-checks">${(config.checks ?? []).map(checkFields).join('')}</div><button id="add-check" class="text-button" type="button">+ Add verification check</button></section>`;
}
function checkFields(check: Check): string {
  return `<div class="verification-check"><div class="check-heading"><input data-check="name" value="${esc(check.name)}" placeholder="What does this verify?" aria-label="Check name" required><button type="button" class="text-button" data-remove-check aria-label="Remove check">×</button></div><label class="field"><span>Command</span><textarea data-check="command" rows="2" required placeholder="test -f output.txt" aria-label="Verification command">${esc(check.command)}</textarea></label><div class="form-grid"><label class="field"><span>Required output · optional</span><input data-check="expected" value="${esc(check.expected)}"></label><label class="field"><span>Weight</span><input data-check="weight" type="number" min="1" max="100" value="${check.weight}"></label></div></div>`;
}
function bindLearning(): void {
  function update(): void {
    const on = el<HTMLInputElement>('learning-enabled').checked;
    el<HTMLSelectElement>('learning-rounds').disabled = !on; el<HTMLInputElement>('learning-inputs').disabled = !on;
    text('learning-description', on ? 'Fresh folder and browser profile per trial. External apps remain shared: use repeatable work. A better measured procedure is retained; ties and regressions keep the baseline.' : 'Run once. Recording and memory stay on. Enable repeated learning to compare procedures.');
  }
  el('learning-enabled').addEventListener('change', update); update();
  bind('add-check', () => { el('verification-checks').insertAdjacentHTML('beforeend', checkFields({ name: '', command: '', expected: '', weight: 1, timeout: 30 })); });
  el('verification-checks').addEventListener('click', event => { (event.target as Element).closest('[data-remove-check]')?.closest('.verification-check')?.remove(); });
}
function readLearning(): Learning {
  const checks: Check[] = Array.from(el('verification-checks').querySelectorAll('.verification-check')).map((row, index) => {
    const value = (name: string): string => row.querySelector<HTMLInputElement>(`[data-check="${name}"]`)?.value.trim() ?? '';
    if (!value('command')) throw new Error('Every verification check needs a command.');
    return { name: value('name') || `Check ${index + 1}`, command: value('command'), expected: value('expected'), weight: Number(value('weight')) || 1, timeout: 30 };
  });
  const enabled = el<HTMLInputElement>('learning-enabled').checked;
  if (enabled && !checks.length) throw new Error('Add at least one verification check for repeated learning.');
  return { enabled, rounds: Number(field('learning-rounds')), inputs: field('learning-inputs'), checks };
}
async function learningEditor(id: string): Promise<void> {
  const task = allTasks().find(t => t.id === id); if (!task) return;
  const details = id.startsWith('draft-') ? { trials: [] as Trial[] } : await api<{ trials: Trial[] }>(`/tasks/${id}/learning`);
  const report = task.learning_report;
  modal('Learning & measured change', `<p>${esc(task.title)}</p>${scoreCard(task)}${report?.baseline !== undefined ? `<div class="score-comparison"><span>Baseline <strong>${report.baseline}/100</strong></span><span>Final candidate <strong>${report.candidate ?? '—'}/100</strong></span><span>Retained <strong>${report.retained}/100</strong></span></div><p>Change compares the final composed candidate with the highest baseline result. It is an observed task result, not a general ability rating.</p>` : ''}${report?.fallbacks?.length ? `<div class="info-strip">Some improvement proposals could not be applied: ${esc(report.fallbacks.join(', '))}. Their trials used the prior instructions.</div>` : ''}${details.trials.map(t => `<details class="trial-detail"><summary>${esc(t.label)} · ${t.score ?? '—'}/100 · ${esc(t.status)}</summary><p>${esc(t.workspace ?? '')}</p>${t.checks.map(c => `<div class="check-result"><span class="badge ${c.passed ? 'success' : 'failed'}">${c.passed ? 'PASS' : 'FAIL'}</span> ${esc(c.name)} · weight ${c.weight}<button class="text-button" data-command="${esc(c.command_id)}">Evidence ↗</button></div>`).join('')}</details>`).join('')}<form id="learning-form">${learningFields(task.learning)}<div class="form-actions"><button class="primary" type="submit" ${task.status === 'running' ? 'disabled' : ''}>Save learning requirements</button></div></form>`, 'MEASURE → COMPARE → RETAIN');
  bindLearning();
  submit('learning-form', async () => { const config = readLearning(); if (id.startsWith('draft-')) { task.learning = config; localStorage.setItem('terminal-drafts', JSON.stringify(drafts)); } else await api(`/tasks/${id}/learning`, 'POST', config); closeModal(); await refresh(); renderLists(); toast('Learning requirements saved.'); });
}

function taskBuilder(goal = '', selected: string[] = []): void {
  modal('Assemble a task', `<form id="task-form"><label class="field"><span>What should your operator achieve?</span><textarea id="task-goal" rows="4" required placeholder="Describe the outcome, any files to use, and how to check it…">${esc(goal)}</textarea></label><label class="field"><span>Steps <span class="dim">/ optional, executed in order</span></span></label><div id="step-list"></div><button class="text-button" type="button" id="add-step">+ Add a step</button><label class="field"><span>Skills for this task</span></label><div class="check-row">${(state?.skills ?? []).filter(s => s.enabled).map(s => `<label class="check-pill"><input type="checkbox" value="${esc(s.id)}" name="task-skill" ${selected.includes(s.id) ? 'checked' : ''}>${esc(s.name)}</label>`).join('') || '<p class="muted">Connect a runtime to choose its installed skills.</p>'}</div>${learningFields()}<div class="form-actions"><button class="secondary" type="submit">${connected ? 'Add to queue' : 'Save task draft'}</button><button class="primary" type="button" id="run-task-now">Run now</button></div><div class="form-status">${connected ? 'Task history and command recordings are stored by your runtime.' : 'Drafts stay in this browser until you connect and run them.'}</div></form>`, 'TASK BUILDER');
  bind('add-step', () => { const row = document.createElement('div'); row.className = 'step-row'; row.innerHTML = `<span>↳</span><input placeholder="Describe the next step" aria-label="Task step"><button type="button" aria-label="Remove step">×</button>`; row.querySelector('button')?.addEventListener('click', () => row.remove()); el('step-list').append(row); row.querySelector('input')?.focus(); });
  bindLearning();
  const create = async (run: boolean): Promise<void> => {
    const taskGoal = field('task-goal'); if (!taskGoal) throw new Error('Add a goal for your task.');
    const steps = Array.from(el('step-list').querySelectorAll<HTMLInputElement>('input')).map(input => input.value.trim()).filter(Boolean);
    const skills = Array.from(el('modal-body').querySelectorAll<HTMLInputElement>('input[name="task-skill"]:checked')).map(input => input.value);
    const learning = readLearning();
    if (!connected) {
      const ident = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
      drafts.unshift({ id: 'draft-' + ident, title: taskGoal.split('\n')[0].slice(0, 90), goal: taskGoal, steps, skills, learning, status: 'draft', created: new Date().toISOString(), result: '' });
      localStorage.setItem('terminal-drafts', JSON.stringify(drafts)); closeModal(); render(); changeView('tasks'); toast('Task draft saved in this browser. Connect your runtime to run it.'); if (run) settings(); return;
    }
    const task = await api<Task>('/tasks', 'POST', { goal: taskGoal, steps, skills, learning }); closeModal();
    if (run) await api(`/tasks/${task.id}/run`, 'POST', { auto_queue: false });
    await refresh(); changeView(run ? 'workspace' : 'tasks'); toast(run ? 'Task started.' : 'Task added to queue.');
  };
  submit('task-form', () => create(false)); bind('run-task-now', () => create(true));
}
bind('new-task-top', () => taskBuilder()); bind('new-task-queue', () => taskBuilder());
const presets: Record<string, [string, string[]]> = {
  inspect: ['Inspect this Linux environment. Report the operating system, current directory, and available Python, Node.js, Git and curl versions. Make no file changes.', ['linux-basics']],
  build: ['Create a small, polished HTML page in a new workspace folder. Serve it on port 8081 and verify the HTTP response with curl. Tell me the address and file location.', ['web-workbench']],
  notes: ['Inspect the files in my workspace and propose a sensible organisation plan. Do not move or delete anything yet. Save the plan as a text file and verify it exists.', ['linux-basics']],
};
document.querySelectorAll<HTMLElement>('[data-preset]').forEach(button => button.addEventListener('click', () => { const preset = presets[button.dataset.preset ?? 'inspect']; taskBuilder(...preset); }));

async function runTask(id: string, autoQueue = false): Promise<void> {
  if (!connected) { settings(); throw new Error('Connect the runtime before running this task.'); }
  const draft = drafts.find(t => t.id === id);
  if (draft) { const created = await api<Task>('/tasks', 'POST', { goal: draft.goal, steps: draft.steps, skills: draft.skills, learning: draft.learning ?? {} }); drafts = drafts.filter(t => t.id !== id); localStorage.setItem('terminal-drafts', JSON.stringify(drafts)); id = created.id; }
  await api(`/tasks/${id}/run`, 'POST', { auto_queue: autoQueue }); await refresh(); changeView('workspace');
}
bind('run-queue', async () => { const next = [...allTasks()].reverse().find(t => ['queued', 'draft'].includes(t.status)); if (!next) throw new Error('The queue is empty.'); await runTask(next.id, true); });
submit('chat-form', async () => {
  const goal = field('chat-input'); if (!goal) return;
  if (!connected) { taskBuilder(goal); return; }
  const task = await api<Task>('/tasks', 'POST', { goal, steps: [], skills: state?.skills.filter(s => s.enabled).map(s => s.id) ?? [] });
  el<HTMLTextAreaElement>('chat-input').value = '';
  if (!state?.agent.active && !state?.agent.manual) { await api(`/tasks/${task.id}/run`, 'POST', { auto_queue: true }); } else toast('Message added as a task in the queue.');
  await refresh(); el('messages').scrollTop = el('messages').scrollHeight;
});
el('chat-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); el<HTMLFormElement>('chat-form').requestSubmit(); } });
bind('pause-agent', async () => { await api('/control', 'POST', { action: state?.agent.paused ? 'resume' : 'pause' }); await refresh(); });
bind('stop-agent', async () => { await api('/control', 'POST', { action: 'stop' }); await refresh(); });
async function manual(): Promise<void> { await api('/control', 'POST', { action: state?.agent.manual ? 'release' : 'takeover' }); await refresh(); if (state?.agent.manual) { expand(true); term.focus(); } }
bind('manual-toggle', manual); bind('manual-expanded', manual);

function skillEditor(skill?: Skill): void {
  modal(skill ? 'Edit skill' : 'Add a skill', `<form id="skill-form"><div class="form-grid"><label class="field"><span>Name</span><input id="skill-name" required value="${esc(skill?.name ?? '')}"></label><label class="field"><span>ID</span><input id="skill-id" required pattern="[a-z0-9][a-z0-9-]{0,59}" value="${esc(skill?.id ?? '')}" ${skill ? 'readonly' : ''} placeholder="my-skill"></label></div><label class="field"><span>Description</span><input id="skill-description" required value="${esc(skill?.description ?? '')}"></label><label class="field"><span>Instructions · Markdown</span><textarea id="skill-body" rows="12" required placeholder="Explain when to use the skill, which commands to run, and how to verify the result.">${esc(skill?.body ?? '')}</textarea></label><div class="form-actions"><button class="primary" type="submit">Save skill</button></div></form>`, 'MODULAR SKILLS');
  submit('skill-form', async () => { await api('/skills', 'POST', { id: field('skill-id'), name: field('skill-name'), description: field('skill-description'), body: field('skill-body'), enabled: skill?.enabled ?? true }); closeModal(); await refresh(); toast('Skill saved. Select it when assembling a task.'); });
}
bind('new-skill', () => skillEditor()); bind('import-skill', () => el<HTMLInputElement>('skill-file').click());
el<HTMLInputElement>('skill-file').addEventListener('change', () => { void (async () => { const file = el<HTMLInputElement>('skill-file').files?.[0]; if (!file) return; if (file.size > 50000) throw new Error('Skill files must be under 50 KB.'); const body = await file.text(); const id = file.name.replace(/\.md$/i, '').toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/^-+/, '').slice(0, 60) || 'custom-skill'; skillEditor({ id, name: file.name.replace(/\.md$/i, ''), description: 'Imported Markdown skill', body, enabled: true }); el<HTMLInputElement>('skill-file').value = ''; })().catch(caught); });
bind('new-memory', () => {
  modal('Remember something', `<form id="memory-form"><label class="field"><span>Title</span><input id="memory-title" required></label><label class="field"><span>Type</span><select id="memory-kind"><option value="note">Note</option><option value="preference">Preference · recalled across tasks</option></select></label><label class="field"><span>What should your operator remember?</span><textarea id="memory-content" rows="6" required></textarea></label><div class="form-actions"><button class="primary" type="submit">Save memory</button></div></form>`, 'PERSISTENT MEMORY');
  submit('memory-form', async () => { await api('/memories', 'POST', { title: field('memory-title'), content: field('memory-content'), kind: field('memory-kind') }); closeModal(); await refresh(); toast('Memory saved.'); });
});
el('memory-search').addEventListener('input', renderLists);

async function exportMemory(): Promise<void> {
  if (!connected) throw new Error('Connect the runtime to export its memory.');
  const response = await fetch(endpoint + '/api/export', { headers: { Authorization: `Bearer ${pairingToken}` } });
  if (!response.ok) throw new Error('The memory export could not be created.');
  const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a'); link.href = url; link.download = 'terminal-memory.zip'; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 10000); toast('Memory export downloaded.');
}
bind('export-memory', exportMemory); bind('export-recordings', exportMemory);
for (const tab of ['commands', 'events', 'apps']) bind(`${tab}-tab`, () => { activityTab = tab; document.querySelectorAll('.tab').forEach(button => button.classList.toggle('active', button.id === `${tab}-tab`)); renderLists(); });

document.addEventListener('click', event => {
  const target = (event.target as Element).closest<HTMLElement>('[data-learning],[data-run-task],[data-cancel-task],[data-edit-skill],[data-toggle-skill],[data-command],[data-launch-command]');
  if (!target) return;
  void (async () => {
    if (target.dataset.learning) await learningEditor(target.dataset.learning);
    if (target.dataset.runTask) await runTask(target.dataset.runTask);
    if (target.dataset.cancelTask) {
      const id = target.dataset.cancelTask;
      if (id.startsWith('draft-')) { drafts = drafts.filter(t => t.id !== id); localStorage.setItem('terminal-drafts', JSON.stringify(drafts)); render(); }
      else { await api(`/tasks/${id}/cancel`, 'POST'); await refresh(); }
    }
    if (target.dataset.editSkill) { const skill = state?.skills.find(s => s.id === target.dataset.editSkill); if (skill) skillEditor(skill); }
    if (target.dataset.toggleSkill) { const skill = state?.skills.find(s => s.id === target.dataset.toggleSkill); if (skill) { await api('/skills', 'POST', { ...skill, enabled: !skill.enabled }); await refresh(); } }
    if (target.dataset.command) { const command = await api<Command>(`/commands/${target.dataset.command}`); modal('Command recording', `<div class="card-meta"><span>${esc(prettyTime(command.started))}</span><span class="badge">${command.exit_code === null ? esc(command.status) : 'EXIT ' + command.exit_code}</span></div><pre>${esc(command.command)}</pre><p>Actual terminal output</p><pre>${esc(command.output ?? '')}</pre><div class="info-strip">Working directory: ${esc(command.cwd)}</div>`, 'EXECUTION EVIDENCE'); }
    if (target.dataset.launchCommand) { changeView('workspace'); expand(true); await api('/command', 'POST', { command: target.dataset.launchCommand }); await refresh(); }
  })().catch(caught);
});

let browserFrameUrl = '';
let browserFrameBusy = false;
let browserFrameStamp = 0;
function renderBrowser(): void {
  const browser = state?.browser;
  const running = connected && browser?.running;
  text('browser-status', !connected ? 'Connect your runtime' : running ? browser.title || browser.url : 'Browser is stopped');
  el('browser-empty').hidden = !!running;
  el('browser-frame').hidden = !running;
  if (document.activeElement !== el('browser-url') && browser?.url && browser.url !== 'about:blank') el<HTMLInputElement>('browser-url').value = browser.url;
  if (document.activeElement !== el('browser-tabs')) el('browser-tabs').innerHTML = browser?.tabs.length ? browser.tabs.map(tab => `<option value="${tab.index}" ${tab.active ? 'selected' : ''}>${tab.index + 1} · ${esc(tab.url)}</option>`).join('') : '<option>No tabs</option>';
  if (running && currentView === 'browser' && browser.updated !== browserFrameStamp && !browserFrameBusy) void fetchBrowserFrame();
}
async function fetchBrowserFrame(): Promise<void> {
  browserFrameBusy = true;
  try {
    const response = await fetch(endpoint + '/api/browser/frame', { headers: { Authorization: `Bearer ${pairingToken}` }, cache: 'no-store' });
    if (!response.ok) return;
    const url = URL.createObjectURL(await response.blob());
    el<HTMLImageElement>('browser-frame').src = url;
    if (browserFrameUrl) URL.revokeObjectURL(browserFrameUrl);
    browserFrameUrl = url; browserFrameStamp = state?.browser.updated ?? 0;
  } finally { browserFrameBusy = false; }
}
async function browserAction(action: string, args: string[] = []): Promise<void> {
  if (!connected) { settings(); throw new Error('Connect your Linux runtime to use its browser.'); }
  await api('/browser/action', 'POST', { action, args }); await refresh(); browserFrameStamp = 0; renderBrowser();
}
bind('browser-start', () => browserAction('start')); bind('browser-stop', () => browserAction('stop'));
bind('browser-back', () => browserAction('back')); bind('browser-reload', () => browserAction('reload'));
bind('browser-new', () => browserAction('new', ['about:blank']));
bind('browser-up', () => browserAction('scroll', ['-600'])); bind('browser-down', () => browserAction('scroll', ['600']));
bind('browser-press', () => browserAction('press', [field('browser-key')]));
submit('browser-address', () => browserAction('open', [field('browser-url')]));
el('browser-tabs').addEventListener('change', () => { void browserAction('tab', [field('browser-tabs')]).catch(caught); });
bind('browser-expand', async () => { if (document.fullscreenElement) await document.exitFullscreen(); else await el('browser-panel').requestFullscreen(); });
el('browser-frame').addEventListener('click', event => { const rect = el('browser-frame').getBoundingClientRect(); void browserAction('tap', [String(Math.round((event.clientX - rect.left) * 1280 / rect.width)), String(Math.round((event.clientY - rect.top) * 800 / rect.height))]).catch(caught); });
bind('browser-type', () => {
  modal('Type into the focused field', `<p>Click a field in the browser first. This text becomes a recorded terminal command. For passwords, take terminal control and use <code>read -rs secret; terminal-browser type "$secret"; unset secret</code>.</p><form id="browser-type-form"><label class="field"><span>Text</span><textarea id="browser-text" rows="3" required></textarea></label><button class="primary" type="submit">Type in browser</button></form>`, 'BROWSER INPUT');
  submit('browser-type-form', async () => { const value = el<HTMLTextAreaElement>('browser-text').value; closeModal(); await browserAction('type', [value]); });
});

fillIcons(); render(); changeView(location.hash.slice(1) || 'workspace');
startDesk(el<HTMLCanvasElement>('desk-art'), () => connected ? state?.agent.phase ?? 'idle' : 'offline');
window.setInterval(() => { void refresh(); }, 1800);
if (pairingToken) void refresh();

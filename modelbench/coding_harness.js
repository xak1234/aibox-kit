const $ = (id) => {
    const node = document.getElementById(id);
    if (!node)
        throw new Error(`Missing interface element: ${id}`);
    return node;
};
const input = (id) => $(id);
const select = (id) => $(id);
const area = (id) => $(id);
const button = (id) => $(id);
const text = (id, value) => { $(id).textContent = String(value ?? ''); };
function node(tag, content = '', cls = '') {
    const element = document.createElement(tag);
    element.textContent = content;
    if (cls)
        element.className = cls;
    return element;
}
function cached(key, fallback) { try {
    return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback;
}
catch {
    return fallback;
} }
function remember(key, value) { try {
    localStorage.setItem(key, JSON.stringify(value));
}
catch { /* Editing and runs remain available if browser storage is full. */ } }
const authKey = new URLSearchParams(location.search).get('key') || '';
const headers = { 'Content-Type': 'application/json' };
if (authKey)
    headers['X-Key'] = authKey;
for (const id of ['benchLink', 'classicLink']) {
    const a = $(id);
    if (authKey)
        a.href += `?key=${encodeURIComponent(authKey)}`;
}
let status = null;
let activeWorkspace = cached('mh.workspace', '');
let selectedRun = '';
let run = null;
let runs = [];
let files = [];
let events = [];
let cursor = 0;
let filePath = '';
let fileSha = '';
let originalText = '';
let modelSignature = '';
let currentView = 'overview';
let pollBusy = false;
let statusTick = 0;
let lastRunVersion = 0;
const activeStates = new Set(['queued', 'planning', 'running', 'validating', 'reviewing', 'cancelling', 'applying']);
let savedTeam = cached('mh.team', { master: '', agents: [], parallel: '1' });
async function api(path, body) {
    const response = await fetch(`/api/harness/${path}`, { method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok || data.error && response.status !== 200)
        throw new Error(String(data.error || `HTTP ${response.status}`));
    return data;
}
function error(value) { const message = value instanceof Error ? value.message : String(value); $('error').hidden = false; text('error', message); const dialog = document.querySelector('dialog[open]'); if (dialog) {
    dialog.querySelector('.dialog-error')?.remove();
    dialog.append(node('p', message, 'dialog-error'));
} }
function clearError() { $('error').hidden = true; document.querySelectorAll('.dialog-error').forEach(e => e.remove()); }
async function guarded(fn) { try {
    clearError();
    await fn();
}
catch (exc) {
    error(exc);
} }
function isDirty() { return !area('editor').disabled && area('editor').value !== originalText; }
function updateDirty() { $('dirty').hidden = !isDirty(); button('saveFile').disabled = !isDirty() || input('staged').checked; lineNumbers(); }
function lineNumbers() { const value = area('editor').value; const count = value.split('\n').length; text('lineNumbers', Array.from({ length: count }, (_, i) => String(i + 1)).join('\n')); text('fileStats', `${count} lines · ${new TextEncoder().encode(value).length.toLocaleString()} bytes · UTF-8`); }
function showView(view) { currentView = view; for (const v of ['overview', 'code', 'diff']) {
    $(`view-${v}`).hidden = v !== view;
    document.querySelector(`[data-view="${v}"]`)?.setAttribute('aria-selected', String(v === view));
} if (view === 'diff' && selectedRun)
    void guarded(loadDiff); }
function showConsole(view) { for (const v of ['activity', 'checks', 'terminal']) {
    $(`console-${v}`).hidden = v !== view;
    document.querySelector(`[data-console="${v}"]`)?.setAttribute('aria-selected', String(v === view));
} }
async function confirmAction(title, message, yes = 'Continue') {
    const dialog = $('confirmDialog');
    text('confirmTitle', title);
    text('confirmText', message);
    text('confirmYes', yes);
    return new Promise(resolve => { let settled = false; const finish = (value) => { if (settled)
        return; settled = true; dialog.close(); resolve(value); }; button('confirmNo').onclick = () => finish(false); button('confirmYes').onclick = () => finish(true); dialog.oncancel = (event) => { event.preventDefault(); finish(false); }; dialog.showModal(); button('confirmNo').focus(); });
}
async function discardEditor() { return !isDirty() || confirmAction('Unsaved file changes', 'Discard the unsaved changes in the editor?', 'Discard changes'); }
function teamPreference() { savedTeam = { master: select('master').value, agents: [...document.querySelectorAll('.agent-check:checked')].map(e => Number(e.value)), parallel: select('parallel').value }; remember('mh.team', savedTeam); text('agentCount', `${savedTeam.agents.length} / 3`); updateStart(); }
function updateStart() { const busy = runs.some(r => r.workspace === activeWorkspace && activeStates.has(r.status)); button('startRun').disabled = !activeWorkspace || !savedTeam.master || !savedTeam.agents.length || busy || !status?.sandbox.ready; text('composerHint', busy ? 'A run is active for this project.' : activeWorkspace ? `${savedTeam.agents.length} agent${savedTeam.agents.length === 1 ? '' : 's'} · changes staged for review` : 'Select a project and configure your team.'); }
function renderModels(models) {
    const signature = JSON.stringify(models.map(m => [m.port, m.name, m.pid, m.tools]));
    if (signature === modelSignature)
        return;
    modelSignature = signature;
    const master = select('master');
    master.replaceChildren();
    for (const model of models) {
        const option = new Option(model.name, String(model.port));
        master.add(option);
    }
    if (!models.length)
        master.add(new Option('Load a generation model in the bench', ''));
    if (models.some(m => String(m.port) === savedTeam.master))
        master.value = savedTeam.master;
    $('agents').replaceChildren();
    const eligible = models.filter(m => m.tools);
    const choices = savedTeam.agents.length ? savedTeam.agents : [eligible[0]?.port];
    for (const model of eligible) {
        const label = node('label', '', 'agent-option');
        const check = node('input');
        check.type = 'checkbox';
        check.className = 'agent-check';
        check.value = String(model.port);
        check.checked = choices.includes(model.port);
        const detail = node('span');
        detail.append(node('strong', model.name), node('small', `:${model.port} · tools available${model.ctx ? ' · ' + Math.round(model.ctx / 1024) + 'k context' : ''}`));
        label.append(check, detail);
        check.onchange = () => { if (document.querySelectorAll('.agent-check:checked').length > 3) {
            check.checked = false;
            error(new Error('Choose up to three agents.'));
        } teamPreference(); };
        $('agents').append(label);
    }
    if (!eligible.length)
        $('agents').append(node('p', 'No tool-capable models are loaded. Open the bench to load one.', 'field-note'));
    select('parallel').value = savedTeam.parallel;
    teamPreference();
}
function renderWorkspaces(workspaces) {
    const picker = select('workspace');
    const signature = JSON.stringify(workspaces.map(w => [w.id, w.name]));
    if (picker.dataset.signature !== signature) {
        picker.dataset.signature = signature;
        picker.replaceChildren(new Option('Select a project', ''));
        for (const w of workspaces)
            picker.add(new Option(w.name, w.id));
    }
    if (!workspaces.some(w => w.id === activeWorkspace))
        activeWorkspace = workspaces[0]?.id || '';
    picker.value = activeWorkspace;
    const current = workspaces.find(w => w.id === activeWorkspace);
    text('projectName', current?.name || 'Your coding workspace');
    text('projectPath', current?.path || 'Choose a project to get started');
    $('projectPath').title = current?.path || '';
    text('host', current?.host || status?.host || 'Linux workspace');
    remember('mh.workspace', activeWorkspace);
    updateStart();
}
async function refreshStatus() { status = await api('status'); text('host', status.host); text('executionHost', status.host); text('sandboxStatus', status.sandbox.ready ? 'Container ready. Commands have no access to the host filesystem or network.' : status.sandbox.error || 'Container unavailable.'); renderModels(status.models); renderWorkspaces(status.workspaces); text('connection', ''); $('connection').append(node('i'), document.createTextNode('Connected')); $('connection').classList.remove('offline'); }
async function changeWorkspace() { if (!await discardEditor()) {
    select('workspace').value = activeWorkspace;
    return;
} remember(`mh.draft.${activeWorkspace}`, area('task').value); activeWorkspace = select('workspace').value; selectedRun = ''; run = null; filePath = ''; fileSha = ''; originalText = ''; area('editor').value = ''; area('editor').disabled = true; input('staged').checked = false; area('task').value = cached(`mh.draft.${activeWorkspace}`, ''); area('checks').value = cached(`mh.checks.${activeWorkspace}`, ''); renderWorkspaces(status?.workspaces || []); await Promise.all([loadFiles(), loadRuns()]); $('explorer').classList.remove('show'); }
async function loadFiles() { if (!activeWorkspace) {
    files = [];
    renderFiles();
    return;
} const query = `files?workspace=${encodeURIComponent(activeWorkspace)}${input('staged').checked && selectedRun ? '&run=' + selectedRun : ''}`; files = (await api(query)).files; renderFiles(); }
function renderFiles() { const list = $('files'); list.replaceChildren(); const needle = input('fileFilter').value.toLowerCase(); for (const file of files.filter(f => f.path.toLowerCase().includes(needle))) {
    const item = node('button', '', 'file-row' + (file.path === filePath ? ' selected' : ''));
    const ext = file.path.split('.').pop()?.toUpperCase().slice(0, 3) || '·';
    item.append(node('span', ext, 'file-ext'), node('span', file.path, 'file-label'));
    item.title = file.path;
    item.onclick = () => void guarded(() => openFile(file.path));
    list.append(item);
} if (!list.children.length)
    list.append(node('p', activeWorkspace ? 'No matching files. Create a file or describe a new project below.' : 'Project files appear here.', 'muted padded')); text('footerFiles', `${files.length} files`); }
async function openFile(path) { if (!await discardEditor())
    return; const data = await api(`file?workspace=${activeWorkspace}&path=${encodeURIComponent(path)}${input('staged').checked && selectedRun ? '&run=' + selectedRun : ''}`); filePath = path; fileSha = data.sha; originalText = data.content; area('editor').value = data.content; area('editor').disabled = false; area('editor').readOnly = input('staged').checked; text('editorPath', path); text('editorMode', input('staged').checked ? 'Staged · read only' : 'Project'); updateDirty(); renderFiles(); showView('code'); $('explorer').classList.remove('show'); }
async function saveFile() { if (!filePath || input('staged').checked)
    return; const result = await api('file', { workspace: activeWorkspace, path: filePath, content: area('editor').value, expected_sha: fileSha }); fileSha = result.sha; originalText = area('editor').value; updateDirty(); await loadFiles(); text('footerState', 'File saved'); }
async function loadRuns() { if (!activeWorkspace) {
    runs = [];
    renderRuns();
    renderRun();
    return;
} runs = (await api(`runs?workspace=${activeWorkspace}`)).runs; renderRuns(); if (!selectedRun && runs.length) {
    await chooseRun(runs[0].id, false);
} updateStart(); }
function renderRuns() { $('runs').replaceChildren(); text('runCount', runs.length); for (const item of runs) {
    const row = node('button', '', 'run-row' + (item.id === selectedRun ? ' selected' : ''));
    row.append(node('strong', item.task));
    const meta = node('small');
    meta.append(node('i', '', 'state-dot ' + item.status), document.createTextNode(item.status.replaceAll('_', ' ') + ' · ' + new Date(item.created * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })));
    row.append(meta);
    row.title = item.task;
    row.onclick = () => void guarded(() => chooseRun(item.id, true));
    $('runs').append(row);
} if (!runs.length)
    $('runs').append(node('p', 'Your runs are saved here.', 'muted padded')); }
async function chooseRun(ident, show = true) { selectedRun = ident; cursor = 0; events = []; lastRunVersion = 0; $('console-activity').replaceChildren(); run = await api(`run?id=${ident}`); renderRuns(); renderRun(); await loadEvents(); if (show)
    showView('overview'); if (input('staged').checked) {
    await loadFiles();
} button('exportRun').disabled = false; }
function renderRun() {
    $('welcome').hidden = !!run;
    $('runDetail').hidden = !run;
    if (!run)
        return;
    text('runId', `RUN ${run.id.slice(0, 8)} · ${new Date(run.created * 1000).toLocaleString()}`);
    const title = (run.plan?.summary || run.task.split(/[\n.!?]/)[0]).slice(0, 140);
    text('runTitle', title);
    text('runRequest', run.task);
    $('activeTeam').replaceChildren(node('span', `Master · ${run.master.name}`), ...run.agents.map((m, i) => node('span', `A${i + 1} · ${m.name}`)));
    text('runStatus', run.status.replaceAll('_', ' '));
    $('runStatus').className = 'badge ' + run.status;
    text('footerState', run.status.replaceAll('_', ' '));
    text('runSummary', run.error || '');
    const stages = ['planning', 'running', 'validating', 'reviewing', 'ready'];
    const labels = ['Plan', 'Implement', 'Validate', 'Review', 'Apply'];
    let phase = stages.indexOf(run.status);
    if (['applied', 'rolled_back'].includes(run.status))
        phase = 5;
    $('pipeline').replaceChildren(...labels.map((label, i) => node('span', label, i < phase ? 'passed' : i === phase ? 'current' : '')));
    $('taskBoard').replaceChildren();
    for (const task of run.tasks) {
        const card = node('article', '', 'task-card');
        const top = node('div', '', 'task-top');
        top.append(node('i', '', 'state-dot ' + task.status), node('strong', task.title), node('span', 'A' + (task.agent + 1), 'task-agent'));
        const instruction = node('details', '', 'task-instruction');
        instruction.append(node('summary', 'Instructions & acceptance criteria'), node('pre', task.instruction));
        card.append(top, instruction);
        const scopes = node('div', '', 'task-files');
        for (const path of task.files)
            scopes.append(node('span', path));
        scopes.append(node('span', task.status.replaceAll('_', ' ')));
        if (task.depends_on.length)
            scopes.append(node('span', 'after ' + task.depends_on.join(', ')));
        card.append(scopes);
        if (task.result || task.error) {
            const failure = task.status === 'completed' ? '' : task.error;
            const details = node('details');
            details.append(node('summary', failure ? 'Failure details' : 'Agent result'), node('pre', failure || task.result || ''));
            card.append(details);
        }
        $('taskBoard').append(card);
    }
    if (!run.tasks.length)
        $('taskBoard').append(node('p', 'The master is preparing tasks and acceptance checks.', 'field-note'));
    text('taskProgress', `${run.tasks.filter(t => t.status === 'completed').length} / ${run.tasks.length} complete`);
    $('review').hidden = !run.review;
    if (run.review) {
        $('review').className = 'review-card' + (run.review.approved ? '' : ' blocked');
        $('review').replaceChildren(node('h2', run.review.approved ? 'Ready for your review' : 'Master requested changes'), node('p', run.review.summary));
        for (const issue of run.review.issues || [])
            $('review').append(node('p', '• ' + issue));
    }
    $('cancelRun').hidden = !activeStates.has(run.status) || run.status === 'cancelling';
    $('resumeRun').hidden = !['failed', 'cancelled', 'interrupted', 'needs_validation', 'needs_changes'].includes(run.status);
    $('applyRun').hidden = !['ready', 'rolled_back'].includes(run.status);
    $('rollbackRun').hidden = run.status !== 'applied';
    text('diffCount', run.changed_files ?? 0);
    $('metrics').replaceChildren(...[`${run.usage.requests} model requests`, `${run.usage.prompt_tokens.toLocaleString()} input tokens`, `${run.usage.completion_tokens.toLocaleString()} output tokens`].map(t => node('span', t)));
    renderChecks();
}
function renderChecks() { $('console-checks').replaceChildren(); const checks = run?.checks || []; text('checksCount', checks.length); for (const check of checks) {
    const item = node('details', '', 'check');
    const title = node('summary');
    title.append(node('span', check.exit_code === 0 ? 'PASS' : 'FAIL', check.exit_code === 0 ? 'ok' : 'bad'), node('span', check.command));
    item.append(title, node('pre', `Exit ${check.exit_code} · ${check.seconds}s${check.timed_out ? ' · timed out' : ''}\n\n${check.output}${check.truncated ? '\n[Output truncated]' : ''}`));
    $('console-checks').append(item);
} if (!checks.length)
    $('console-checks').append(node('div', 'Real exit codes and command output appear here.', 'console-empty')); }
async function loadEvents() { if (!selectedRun)
    return; const result = await api(`events?id=${selectedRun}&since=${cursor}`); const panel = $('console-activity'); const stick = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 45; for (const event of result.events) {
    cursor = event.seq;
    events.push(event);
    const row = node('div', '', 'event' + (event.error || event.kind === 'task_failed' ? ' failed' : ''));
    row.append(node('time', new Date(event.ts * 1000).toLocaleTimeString()), node('span', event.tool || event.kind, 'event-kind'));
    const detail = node('div');
    const message = event.message || event.command || event.model || `${event.task || ''} ${event.tool || event.kind}`;
    if (event.result || event.args) {
        const info = node('details');
        info.append(node('summary', message), node('pre', JSON.stringify({ arguments: event.args, result: event.result }, null, 2)));
        detail.append(info);
    }
    else
        detail.append(node('span', message));
    row.append(detail);
    panel.append(row);
} while (panel.childElementCount > 500)
    panel.firstElementChild?.remove(); text('eventCount', events.length); if (stick)
    panel.scrollTop = panel.scrollHeight; }
async function loadDiff() { if (!selectedRun)
    return; const diff = await api(`diff?id=${selectedRun}`); text('diffCount', diff.files.length); $('diffFiles').replaceChildren(...diff.files.map(f => node('span', `${f.before ? f.after ? 'M' : 'D' : 'A'} ${f.path}`, 'changed-file'))); $('diff').replaceChildren(...(diff.text || 'No source changes yet.').split('\n').map(line => node('span', line, line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : line.startsWith('@@') ? 'hunk' : ''))); if (diff.truncated)
    $('diff').append(node('span', '[Diff exceeds the display limit; inspect the staged files.]', 'hunk')); }
async function startRun() { if (isDirty()) {
    error(new Error('Save the file in the editor before starting a run.'));
    return;
} if (!area('task').value.trim()) {
    area('task').focus();
    return;
} button('startRun').disabled = true; try {
    const result = await api('run', { workspace: activeWorkspace, task: area('task').value, master: Number(select('master').value), agents: savedTeam.agents, parallel: Number(select('parallel').value), checks: area('checks').value.split('\n').map(t => t.trim()).filter(Boolean) });
    await loadRuns();
    await chooseRun(result.id);
    showConsole('activity');
}
finally {
    updateStart();
} }
async function runAction(action) { if (!run)
    return; if (action === 'apply') {
    showView('diff');
    const diff = await api(`diff?id=${run.id}`);
    if (!await confirmAction('Apply reviewed changes', `Apply ${diff.files.length} changed file(s) to ${status?.workspaces.find(w => w.id === activeWorkspace)?.path}?\n\nThe harness checks for conflicting edits and saves rollback copies.`, 'Apply changes'))
        return;
} if (action === 'rollback' && !await confirmAction('Roll back this run', 'Restore the files changed by this run? Later edits to those files will be detected and will prevent rollback.', 'Roll back'))
    return; const body = { id: run.id, action }; if (action === 'resume')
    body.checks = area('checks').value.split('\n').map(t => t.trim()).filter(Boolean); run = await api('action', body); renderRun(); await Promise.all([loadRuns(), loadFiles()]); }
async function poll() { if (pollBusy || document.hidden)
    return; pollBusy = true; try {
    if (!status || ++statusTick >= 5) {
        statusTick = 0;
        await refreshStatus();
    }
    if (activeWorkspace)
        await loadRuns();
    if (selectedRun) {
        const fresh = await api(`run?id=${selectedRun}`);
        if (fresh.updated !== lastRunVersion) {
            lastRunVersion = fresh.updated;
            run = fresh;
            renderRun();
            if (currentView === 'diff')
                await loadDiff();
        }
        await loadEvents();
    }
}
catch (exc) {
    text('connection', '');
    $('connection').append(node('i'), document.createTextNode('Reconnecting'));
    $('connection').classList.add('offline');
    text('footerState', exc instanceof Error ? exc.message : 'Connection interrupted');
}
finally {
    pollBusy = false;
} }
document.documentElement.dataset.theme = cached('mh.theme', 'dark');
button('theme').onclick = () => { const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = next; remember('mh.theme', next); };
for (const element of document.querySelectorAll('[data-view]'))
    element.onclick = () => showView(element.dataset.view || 'overview');
for (const element of document.querySelectorAll('[data-console]'))
    element.onclick = () => showConsole(element.dataset.console || 'activity');
for (const element of document.querySelectorAll('[data-close]'))
    element.onclick = () => element.closest('dialog')?.close();
button('newWorkspace').onclick = () => $('projectDialog').showModal();
button('projectToggle').onclick = () => { $('explorer').classList.toggle('show'); $('teamPanel').classList.remove('show'); };
button('teamToggle').onclick = () => { $('teamPanel').classList.toggle('show'); $('explorer').classList.remove('show'); };
select('workspace').onchange = () => void guarded(changeWorkspace);
select('master').onchange = teamPreference;
select('parallel').onchange = teamPreference;
input('fileFilter').oninput = renderFiles;
button('refreshFiles').onclick = () => void guarded(loadFiles);
button('refreshDiff').onclick = () => void guarded(loadDiff);
input('staged').onchange = () => void guarded(async () => { if (!await discardEditor()) {
    input('staged').checked = !input('staged').checked;
    return;
} originalText = area('editor').value; await loadFiles(); if (filePath && files.some(f => f.path === filePath))
    await openFile(filePath);
else {
    filePath = '';
    area('editor').value = '';
    area('editor').disabled = true;
    originalText = '';
    updateDirty();
} });
area('editor').oninput = updateDirty;
area('editor').onscroll = () => { $('lineNumbers').scrollTop = area('editor').scrollTop; };
area('editor').onkeydown = event => { if (event.key === 'Tab' && !area('editor').readOnly) {
    event.preventDefault();
    const editor = area('editor');
    const start = editor.selectionStart;
    editor.setRangeText('    ', start, editor.selectionEnd, 'end');
    updateDirty();
} };
button('saveFile').onclick = () => void guarded(saveFile);
button('newFile').onclick = () => { if (!activeWorkspace) {
    error(new Error('Open a project first.'));
    return;
} $('fileDialog').showModal(); };
$('fileForm').onsubmit = event => { event.preventDefault(); void guarded(async () => { const path = input('newFilePath').value.trim(); if (files.some(f => f.path === path))
    throw new Error('That file already exists.'); await api('file', { workspace: activeWorkspace, path, content: '', expected_sha: '' }); $('fileDialog').close(); input('staged').checked = false; await loadFiles(); await openFile(path); }); };
$('projectForm').onsubmit = event => { event.preventDefault(); void guarded(async () => { button('createProject').disabled = true; try {
    const ws = await api('workspace', { name: input('projectInputName').value, path: input('projectInputPath').value });
    $('projectDialog').close();
    await refreshStatus();
    select('workspace').value = ws.id;
    await changeWorkspace();
}
finally {
    button('createProject').disabled = false;
} }); };
button('startRun').onclick = () => void guarded(startRun);
for (const [id, action] of [['cancelRun', 'cancel'], ['resumeRun', 'resume'], ['applyRun', 'apply'], ['rollbackRun', 'rollback']])
    button(id).onclick = () => void guarded(() => runAction(action));
for (const element of document.querySelectorAll('[data-prompt]'))
    element.onclick = () => { area('task').value = element.dataset.prompt || ''; area('task').focus(); };
area('task').oninput = () => remember(`mh.draft.${activeWorkspace}`, area('task').value);
area('checks').oninput = () => remember(`mh.checks.${activeWorkspace}`, area('checks').value);
button('consoleToggle').onclick = () => { document.querySelector('.console')?.classList.toggle('expanded'); };
$('terminalForm').onsubmit = event => { event.preventDefault(); void guarded(async () => { if (!activeWorkspace)
    throw new Error('Open a project first.'); button('terminalSend').disabled = true; text('terminalOutput', 'Running in the isolated project copy…'); try {
    const output = await api('terminal', { workspace: activeWorkspace, command: input('terminalCommand').value });
    text('terminalOutput', `${output.output}\nExit ${output.exit_code} · ${output.seconds}s${output.truncated ? ' · output truncated' : ''}`);
}
finally {
    button('terminalSend').disabled = false;
} }); };
button('exportRun').onclick = () => void guarded(async () => { if (!run)
    return; const id = run.id; button('exportRun').disabled = true; try {
    const snapshot = await api(`run?id=${id}`);
    const diff = await api(`diff?id=${id}`);
    const history = [];
    let since = 0;
    for (;;) {
        const batch = await api(`events?id=${id}&since=${since}`);
        if (!batch.events.length)
            break;
        history.push(...batch.events);
        since = batch.events[batch.events.length - 1].seq;
    }
    const blob = new Blob([JSON.stringify({ run: snapshot, diff, events: history }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = node('a');
    anchor.href = url;
    anchor.download = `modelbench-run-${id.slice(0, 8)}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}
finally {
    button('exportRun').disabled = false;
} });
document.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    if (!button('startRun').disabled)
        void guarded(startRun);
} if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && currentView === 'code') {
    event.preventDefault();
    if (!button('saveFile').disabled)
        void guarded(saveFile);
} });
window.addEventListener('beforeunload', event => { if (isDirty()) {
    event.preventDefault();
    event.returnValue = '';
} });
document.addEventListener('visibilitychange', () => { if (!document.hidden)
    void poll(); });
void guarded(async () => { await refreshStatus(); area('task').value = cached(`mh.draft.${activeWorkspace}`, ''); area('checks').value = cached(`mh.checks.${activeWorkspace}`, ''); await Promise.all([loadFiles(), loadRuns()]); });
setInterval(() => void poll(), 2500);
export {};

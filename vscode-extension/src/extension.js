'use strict';
const vscode = require('vscode');
const path = require('node:path');
const crypto = require('node:crypto');
const {inside, invoke} = require('./core');

function activate(context) {
  let root, panel, plan, generation = 0, options = {}, target, busy = false;
  const diagnostics = vscode.languages.createDiagnosticCollection('ProdXProj');
  const changed = new vscode.EventEmitter();
  const pending = new Map();
  const disposables = [diagnostics, changed];
  const errors = error => vscode.window.showErrorMessage(error.message);
  async function project() {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before using ProdXProj.');
    if (!root) {
      const folders = vscode.workspace.workspaceFolders || [];
      const folder = folders.length === 1 ? folders[0] : await vscode.window.showWorkspaceFolderPick();
      if (!folder || folder.uri.scheme !== 'file') throw new Error('Open a local project folder first.');
      root = folder.uri.fsPath;
    }
    return root;
  }
  async function backend(action, extra = {}) {
    return invoke(vscode.workspace.getConfiguration('prodxproj', vscode.Uri.file(await project())).get('pythonPath'),
      context.extensionPath, {root, action, target, options, ...extra}, vscode.workspace.isTrusted,
      progress => panel?.webview.postMessage({type: 'error', value: progress.message}));
  }
  const tree = {
    onDidChangeTreeData: changed.event,
    getTreeItem: item => item,
    getChildren: item => {
      if (item?.children) return item.children;
      if (item) return [];
      const entry = (label, children) => Object.assign(new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.Collapsed), {children});
      const leaf = label => new vscode.TreeItem(label);
      const open = new vscode.TreeItem('Open production dashboard');
      open.command = {command: 'prodxproj.dashboard', title: 'Dashboard'};
      if (!plan) return [open, leaf('Refresh to load project metadata')];
      return [open, entry(`${plan.project} · ${plan.version}`, [leaf(`Target: ${target || 'unselected'}`), leaf(`Startup: ${plan.startup}`)]),
        entry(`Variables (${plan.variables.length})`, plan.variables.map(x => leaf(`${x.name}: ${x.state} · ${x.source}`))),
        entry(`Shipping files (${plan.files.length})`, plan.files.map(x => leaf(`${x.package} → ${x.destination}`))),
        entry(`Versions (${plan.history.length})`, plan.history.map(x => leaf(`${x.version} · ${x.branch} · ${x.created}`))),
        entry('Checks', plan.issues.map(leaf))];
    }
  };
  disposables.push(vscode.window.registerTreeDataProvider('prodxproj.explorer', tree));
  async function refresh(reveal = false) {
    const id = ++generation;
    const result = await backend('plan', {reveal});
    if (id !== generation) return;
    plan = result;
    changed.fire();
    panel?.webview.postMessage({type: 'plan', value: plan, selectedTarget: target});
  }
  function dashboard() {
    if (panel) {panel.reveal(); return refresh().catch(errors);}
    panel = vscode.window.createWebviewPanel('prodxproj.dashboard', 'ProdXProj', vscode.ViewColumn.One, {
      enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(context.extensionUri, 'media')]
    });
    const nonce = crypto.randomBytes(24).toString('hex');
    const script = panel.webview.asWebviewUri(vscode.Uri.joinPath(context.extensionUri, 'media', 'dashboard.js'));
    const style = panel.webview.asWebviewUri(vscode.Uri.joinPath(context.extensionUri, 'media', 'dashboard.css'));
    panel.webview.html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${panel.webview.cspSource}; script-src 'nonce-${nonce}';">
      <link rel="stylesheet" href="${style}"><title>ProdXProj</title></head><body>
      <header><h1>ProdXProj</h1><p id="summary">Load a project to preview its production layout.</p></header>
      <nav aria-label="Project actions"><button data-action="refresh">Refresh</button><button data-action="config">Edit project settings</button>
      <button data-action="reveal">Reveal variable values</button><button data-action="test">Run local tests</button></nav>
      <section><button data-action="operation-status">Operation history</button><button data-action="operation-reconcile">Acknowledge inspected interrupted operation</button><p>Interrupted infrastructure operations block retries until their actual state has been inspected.</p></section>
      <section aria-label="Shipping options"><label>Target <select id="target"><option value="">Select target</option></select></label>
      <label><input type="checkbox" id="env">Include environment files</label><label><input type="checkbox" id="keys">Include SSH keys</label>
      <label><input type="checkbox" id="data">Include user data</label><label><input type="checkbox" id="secrets">Include credentials</label>
      <p>Inclusion controls preview the package. Credential attachments stay client-side; preview does not deploy.</p></section>
      <p id="status" role="status" aria-live="polite"></p><section><h2>Shipping pipeline</h2><div id="phases" class="pipeline"></div><ul id="issues"></ul></section>
      <section><h2>Variables</h2><div class="scroll"><table><thead><tr><th>Name</th><th>Value</th><th>Source</th><th>Behavior</th></tr></thead><tbody id="variables"></tbody></table></div></section>
      <section><h2>Project explorer · destination preview</h2><label>Filter files <input id="filter" type="search"></label><div class="scroll"><table><thead><tr><th>Source</th><th>Package</th><th>Server destination</th><th>Action / checks</th></tr></thead><tbody id="files"></tbody></table></div><p id="file-count"></p></section>
      <section><h2>Generated scripts and service files</h2><div id="generated"></div></section>
      <section><h2>Version topology</h2><p>Each release shows its parent links, branch, timestamp and changed paths.</p><div id="topology" class="scroll"></div><div id="history"></div></section>
      <section><h2>Local Docker emulation</h2><label>Replicas <input id="replicas" type="number" min="1" max="20" value="1"></label>
      <button data-action="emulate-plan">Preview</button><button data-action="emulate-up">Build and start</button><button data-action="emulate-status">Status</button><button data-action="emulate-test">Test replicas</button><button data-action="emulate-down">Stop</button></section>
      <section><h2>Remote setup and startup</h2><button data-action="remote-preview">Preview SSH setup</button><button data-action="remote-setup">Run SSH setup</button>
      <button data-action="service-status">Service status</button><button data-action="service-start">Start</button><button data-action="service-stop">Stop</button><button data-action="service-restart">Restart</button><button data-action="service-enable">Enable startup</button><button data-action="service-disable">Disable startup</button>
      <p>Uses the selected target and its configured SSH identity. Setup installs prerequisites; edit project settings for managed systemd/Compose service details.</p></section>
      <section><h2>Operation result / script effects</h2><pre id="result">No operation run.</pre></section>
      <script nonce="${nonce}" src="${script}"></script></body></html>`;
    panel.onDidDispose(() => {panel = undefined; generation++;});
    panel.webview.onDidReceiveMessage(async message => {
      try {
        if (!message || typeof message.action !== 'string') return;
        if (message.action === 'ready') return await refresh();
        if (message.action === 'options') {
          options = Object.fromEntries(['env', 'keys', 'data', 'secrets'].map(key => [key, message.options?.[key] === true]));
          target = typeof message.target === 'string' && plan?.targets.includes(message.target) ? message.target : undefined;
          return await refresh();
        }
        if (message.action === 'refresh' || message.action === 'reveal') return await refresh(message.action === 'reveal');
        if (message.action === 'config') return await vscode.window.showTextDocument(vscode.Uri.file(path.join(await project(), 'proxproject.json')));
        if (message.action === 'operation-reconcile') {
          const status = await backend('operation-status');
          if (!status.pending) throw new Error('No interrupted operation needs reconciliation.');
          const answer = await vscode.window.showWarningMessage(
            `Operation ${status.pending.operationId} has unknown completion state. Verify its local/remote effects before acknowledging; acknowledgement does not cancel or undo work.`,
            {modal: true}, 'I inspected the operation state');
          if (answer === 'I inspected the operation state') {
            const result = await backend('operation-reconcile', {pendingId: status.pending.operationId, inspected: true});
            panel?.webview.postMessage({type: 'result', value: result});
          }
          return;
        }
        if (message.action === 'open-file') {
          if (typeof message.path !== 'string' || !plan?.files.some(x => x.source === message.path && x.source !== 'Generated by ProdXProj')) return;
          return await vscode.window.showTextDocument(vscode.Uri.file(inside(await project(), message.path)));
        }
        const actions = ['operation-status', 'test', 'emulate-plan', 'emulate-up', 'emulate-status', 'emulate-test', 'emulate-down',
          'remote-preview', 'remote-setup', 'service-status', 'service-start', 'service-stop', 'service-restart', 'service-enable', 'service-disable'];
        if (!actions.includes(message.action) || busy) return;
        busy = true;
        panel?.webview.postMessage({type: 'busy', value: true});
        try {
          const result = await vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: `ProdXProj: ${message.action}`, cancellable: false},
            () => backend(message.action, message.action.startsWith('emulate-') ? {replicas: message.replicas} : {}));
          panel?.webview.postMessage({type: 'result', value: result});
        } finally {busy = false; panel?.webview.postMessage({type: 'busy', value: false});}
      } catch (error) {panel?.webview.postMessage({type: 'error', value: error.message});}
    });
  }
  async function check(document) {
    if (!vscode.workspace.isTrusted || document.uri.scheme !== 'file' || document.getText().length > 1024 * 1024) return;
    const version = document.version;
    const file = inside(await project(), document.uri.fsPath);
    const result = await backend('check', {path: path.relative(root, file), text: document.getText()});
    if (document.version !== version) return;
    diagnostics.set(document.uri, (result.diagnostics || []).map(x => {
      const line = Math.max(0, Math.min(document.lineCount - 1, (x.line || 1) - 1));
      return new vscode.Diagnostic(document.lineAt(line).range, x.message, vscode.DiagnosticSeverity.Error);
    }));
    panel?.webview.postMessage({type: 'result', value: result});
  }
  function command(name, handler) {
    disposables.push(vscode.commands.registerCommand(name, async () => {try {await handler();} catch (error) {errors(error);}}));
  }
  command('prodxproj.dashboard', async () => {await project(); dashboard();});
  command('prodxproj.refresh', () => refresh());
  command('prodxproj.checkScript', async () => {if (vscode.window.activeTextEditor) await check(vscode.window.activeTextEditor.document);});
  command('prodxproj.runScript', async () => {
    const document = vscode.window.activeTextEditor?.document;
    if (!document) throw new Error('Open a script first.');
    if (document.isDirty) throw new Error('Save the script before explicitly running it.');
    const file = inside(await project(), document.uri.fsPath);
    const result = await backend('run', {path: path.relative(root, file)});
    if (panel) panel.webview.postMessage({type: 'result', value: result});
    else vscode.window.showInformationMessage(`ProdXProj script finished with exit code ${result.returncode}.`);
  });
  command('prodxproj.selectPython', async () => {
    await project();
    const selection = await vscode.window.showOpenDialog({canSelectMany: false, openLabel: 'Select Python executable'});
    if (selection?.[0]) await vscode.workspace.getConfiguration('prodxproj', vscode.Uri.file(root)).update('pythonPath', selection[0].fsPath, vscode.ConfigurationTarget.Workspace);
  });
  disposables.push(vscode.workspace.onDidChangeTextDocument(event => {
    if (!['python', 'json', 'shellscript', 'javascript', 'powershell'].includes(event.document.languageId)) return;
    const key = event.document.uri.toString();
    clearTimeout(pending.get(key));
    pending.set(key, setTimeout(() => {pending.delete(key); check(event.document).catch(() => {});}, 500));
  }));
  disposables.push(vscode.workspace.onDidCloseTextDocument(document => {
    clearTimeout(pending.get(document.uri.toString())); pending.delete(document.uri.toString()); diagnostics.delete(document.uri);
  }));
  disposables.push(vscode.window.registerUriHandler({handleUri: async uri => {
    try {
      if (uri.path !== '/open') return;
      const requested = new URLSearchParams(uri.query).get('project');
      if (!requested) return;
      const folders = vscode.workspace.workspaceFolders || [];
      if (!folders.some(x => x.uri.scheme === 'file' && path.resolve(x.uri.fsPath) === path.resolve(requested))) {
        throw new Error('Open the requested project folder, then run ProdXProj: Dashboard.');
      }
      root = requested; target = undefined; options = {};
      await project(); dashboard();
    } catch (error) {errors(error);}
  }}));
  disposables.push({dispose() {for (const timer of pending.values()) clearTimeout(timer); panel?.dispose();}});
  context.subscriptions.push(...disposables);
}
module.exports = {activate};

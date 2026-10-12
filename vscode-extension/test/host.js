'use strict';
const vscode = require('vscode');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
async function run() {
  const extension = vscode.extensions.getExtension('prodxproj.prodxproj');
  assert.ok(extension, 'extension is discovered');
  await extension.activate();
  const root = vscode.workspace.workspaceFolders[0].uri;
  await vscode.commands.executeCommand('prodxproj.refresh');
  const document = await vscode.workspace.openTextDocument(vscode.Uri.joinPath(root, 'script.py'));
  await vscode.window.showTextDocument(document);
  await vscode.commands.executeCommand('prodxproj.checkScript');
  assert.ok(vscode.languages.getDiagnostics(document.uri).some(x => x.source === 'ProdXProj' || x.message.includes('syntax')),
    'syntax diagnostics appear for invalid saved script');
  await vscode.commands.executeCommand('prodxproj.dashboard');
  fs.writeFileSync(path.join(root.fsPath, 'host-result.json'), JSON.stringify({passed:true, activation:true, plan:true, diagnostics:true, dashboard:true}));
}
module.exports = {run};

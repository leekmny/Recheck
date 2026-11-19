'use strict';

// The selected TypeScript installation is an operator dependency. User source
// arrives on stdin and is parsed without resolving imports or executing it.
const fs = require('node:fs');
const path = require('node:path');
const [filename, maximum, parser] = process.argv.slice(2);
let ts, modulePath;
try {
  modulePath = require.resolve(parser || 'typescript');
  ts = require(modulePath);
  if (typeof ts.createSourceFile !== 'function' || typeof ts.version !== 'string') {
    throw new Error('Invalid TypeScript compiler API');
  }
} catch (error) {
  process.stderr.write('TypeScript parser unavailable. Set RECHECK_TYPESCRIPT_MODULE to a trusted TypeScript package directory.\n');
  process.exit(42);
}

const suffix = path.extname(filename).toLowerCase();
const scriptKind = {
  '.js': ts.ScriptKind.JS, '.mjs': ts.ScriptKind.JS, '.cjs': ts.ScriptKind.JS,
  '.jsx': ts.ScriptKind.JSX, '.ts': ts.ScriptKind.TS, '.mts': ts.ScriptKind.TS,
  '.cts': ts.ScriptKind.TS, '.tsx': ts.ScriptKind.TSX,
}[suffix];
const decoder = new TextDecoder('utf-8', {fatal: true});
const input = decoder.decode(fs.readFileSync(0));
const source = ts.createSourceFile(filename, input, ts.ScriptTarget.Latest, true, scriptKind);
const branches = new Set([
  ts.SyntaxKind.IfStatement, ts.SyntaxKind.ConditionalExpression, ts.SyntaxKind.SwitchStatement,
  ts.SyntaxKind.ForStatement, ts.SyntaxKind.ForInStatement, ts.SyntaxKind.ForOfStatement,
  ts.SyntaxKind.WhileStatement, ts.SyntaxKind.DoStatement, ts.SyntaxKind.CatchClause,
]);
const shortCircuit = new Set([
  ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken,
]);
const callKinds = new Set([
  ts.SyntaxKind.CallExpression, ts.SyntaxKind.NewExpression, ts.SyntaxKind.TaggedTemplateExpression,
]);

function isFunction(node) {
  return ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node)
    || ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node)
    || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node);
}

function isClass(node) {
  return ts.isClassDeclaration(node) || ts.isClassExpression(node);
}

function functionName(node) {
  if (node.name) return node.name.getText(source);
  if (ts.isConstructorDeclaration(node)) return 'constructor';
  if (node.parent && node.parent.initializer === node && node.parent.name) {
    return node.parent.name.getText(source);
  }
  return '<anonymous>';
}

function features(root) {
  const counts = Object.create(null);
  const pending = [root];
  let size = 0, branchCount = 0, calls = 0;
  while (pending.length) {
    const node = pending.pop();
    const tag = ts.SyntaxKind[node.kind];
    counts[tag] = (counts[tag] || 0) + 1;
    size++;
    if (branches.has(node.kind)
        || (ts.isBinaryExpression(node) && shortCircuit.has(node.operatorToken.kind))) branchCount++;
    if (callKinds.has(node.kind)) calls++;
    if (node === root || (!isFunction(node) && !isClass(node))) {
      ts.forEachChild(node, child => { pending.push(child); });
    }
  }
  return {tokens: counts, size, branches: branchCount, calls};
}

const functions = [];
let total = 0;
function visit(node, scope) {
  if (isFunction(node)) {
    const name = [...scope, functionName(node)].join('.');
    const position = source.getLineAndCharacterOfPosition(node.getStart(source));
    const line = position.line + 1;
    const column = position.character + 1;
    total++;
    if (functions.length < Number(maximum)) {
      const row = {id: `${filename}:${name}:${line}:${column}`, name, file: filename, line, column,
                   features: node.body ? features(node) : null};
      if (!node.body) row.unobserved_reason = 'Declaration has no observed source body';
      functions.push(row);
    }
    scope = [...scope, functionName(node)];
  } else if (isClass(node)) {
    scope = [...scope, node.name?.getText(source) ?? '<anonymous>'];
  }
  ts.forEachChild(node, child => { visit(child, scope); });
}

const diagnostics = source.parseDiagnostics.slice(0, 20).map(diagnostic => ({
  code: diagnostic.code, start: diagnostic.start,
  message: ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'),
}));
if (!diagnostics.length) visit(source, []);
process.stdout.write(JSON.stringify({version: ts.version, node_version: process.version,
  module: modulePath, total, functions, diagnostics}));

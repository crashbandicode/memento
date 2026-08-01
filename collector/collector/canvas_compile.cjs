"use strict";

// Deterministic TSX transpiler. The source is parsed and transformed but never
// imported or evaluated in this process.
const fs = require("node:fs");

if (process.argv.length !== 4) {
  process.stderr.write("compile_failed");
  process.exit(2);
}

const [typescriptPath, sourcePath] = process.argv.slice(2);
const ts = require(typescriptPath);
const source = fs.readFileSync(sourcePath, "utf8");
const file = ts.createSourceFile(
  "artifact.canvas.tsx",
  source,
  ts.ScriptTarget.ES2020,
  true,
  ts.ScriptKind.TSX,
);

let defaultExports = 0;
let invalid = "";
const forbiddenGlobals = new Set([
  "eval",
  "Function",
  "fetch",
  "XMLHttpRequest",
  "WebSocket",
  "EventSource",
  "Worker",
  "SharedWorker",
  "importScripts",
  "require",
  "setInterval",
  "setTimeout",
  "requestAnimationFrame",
  "queueMicrotask",
  "window",
  "document",
  "globalThis",
  "localStorage",
  "sessionStorage",
  "indexedDB",
  "parent",
  "top",
  "opener",
]);
const forbiddenElements = new Set(["script", "iframe", "object", "embed", "form"]);

function moduleName(node) {
  return node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)
    ? node.moduleSpecifier.text
    : "";
}

function walk(node) {
  if (invalid) return;
  if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
    if (moduleName(node) !== "cursor/canvas") invalid = "unsupported_import";
  }
  if (
    ts.isCallExpression(node)
    && node.expression.kind === ts.SyntaxKind.ImportKeyword
  ) {
    invalid = "dynamic_import";
  }
  if (
    ts.isExportAssignment(node)
    || (
      (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node))
      && node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword)
      && node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)
    )
  ) {
    defaultExports += 1;
  }
  if (
    ts.isWithStatement(node)
    || ts.isDebuggerStatement(node)
    || ts.isMetaProperty(node)
    || ts.isWhileStatement(node)
    || ts.isDoStatement(node)
    || ts.isForStatement(node)
    || ts.isForInStatement(node)
    || ts.isForOfStatement(node)
  ) {
    invalid = "forbidden_syntax";
  }
  if (
    ts.isCallExpression(node)
    && ts.isIdentifier(node.expression)
    && [
      "eval",
      "Function",
      "fetch",
      "setInterval",
      "setTimeout",
      "requestAnimationFrame",
      "queueMicrotask",
    ].includes(node.expression.text)
  ) {
    invalid = "forbidden_syntax";
  }
  if (ts.isIdentifier(node) && forbiddenGlobals.has(node.text)) {
    const parent = node.parent;
    const isPropertyName = (
      (ts.isPropertyAccessExpression(parent) && parent.name === node)
      || (ts.isPropertyAssignment(parent) && parent.name === node)
      || (ts.isMethodDeclaration(parent) && parent.name === node)
    );
    if (!isPropertyName) invalid = "forbidden_syntax";
  }
  if (
    (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
    && ts.isIdentifier(node.tagName)
    && forbiddenElements.has(node.tagName.text.toLowerCase())
  ) {
    invalid = "forbidden_syntax";
  }
  if (
    ts.isJsxAttribute(node)
    && node.name.text === "dangerouslySetInnerHTML"
  ) {
    invalid = "forbidden_syntax";
  }
  ts.forEachChild(node, walk);
}
walk(file);

if (invalid) {
  process.stderr.write(invalid);
  process.exit(3);
}
if (defaultExports !== 1) {
  process.stderr.write("missing_default_export");
  process.exit(4);
}

const removeImports = (context) => {
  const visit = (node) => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      return undefined;
    }
    return ts.visitEachChild(node, visit, context);
  };
  return (root) => ts.visitNode(root, visit);
};

const result = ts.transpileModule(source, {
  fileName: "artifact.canvas.tsx",
  reportDiagnostics: true,
  compilerOptions: {
    target: ts.ScriptTarget.ES2020,
    module: ts.ModuleKind.ES2020,
    jsx: ts.JsxEmit.React,
    importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    isolatedModules: true,
    removeComments: true,
    sourceMap: false,
    inlineSourceMap: false,
    noEmitHelpers: false,
  },
  transformers: { before: [removeImports] },
});

const diagnostics = (result.diagnostics || []).filter(
  (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
);
if (diagnostics.length > 0) {
  process.stderr.write("compile_diagnostic");
  process.exit(5);
}
if (!result.outputText || Buffer.byteLength(result.outputText, "utf8") > 500000) {
  process.stderr.write("compile_output");
  process.exit(6);
}
process.stdout.write(result.outputText);

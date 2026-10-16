import * as t from "@babel/types";

import {
  semanticStaticPropertyKey,
  semanticMemberWriteTargets,
  unwrapJavaScriptExpression,
} from "./javascriptAstValues.js";
import { SemanticPropertyPathCoverage } from "./javascriptSemanticPropertyPaths.js";
import { traverseJavaScriptAstSteps } from "./javascriptSemanticTraversal.js";

/** Match Electron's supported module entry points. */
export const ELECTRON_MODULE =
  /^electron(?:\/(?:common|main|renderer|utility))?$/u;

/**
 * Electron's CommonJS exports define no `default` member, so a leading
 * `default` segment can only denote the module object itself, as an ES module
 * default import (`import electron from "electron"`) binds it.
 */
export const electronExportPath = (
  importedPath: readonly string[],
): readonly string[] =>
  importedPath[0] === "default" ? importedPath.slice(1) : importedPath;

/** Static identity recovery cannot establish the absence of Electron usage. */
export const ELECTRON_IDENTITY_LIMITATION =
  "Electron API identity follows proven lexical module aliases. Reassigned bindings, dynamic selections, and visibly overwritten namespace members are unresolved; missing findings do not establish absence. Arbitrary call side effects and runtime registration are not evaluated.";

/** Preserve exact property segments instead of flattening literal dotted keys. */
export const electronMemberPath = (
  node: t.Node,
): {
  readonly root: t.Node;
  readonly members: readonly (string | null)[];
} => {
  let root = unwrapJavaScriptExpression(node).node;
  const members: (string | null)[] = [];
  while (t.isMemberExpression(root) || t.isOptionalMemberExpression(root)) {
    members.push(semanticStaticPropertyKey(root.property, root.computed));
    root = unwrapJavaScriptExpression(root.object).node;
  }
  return { root, members: members.toReversed() };
};

/** Index visible writes through proven Electron aliases, without executing them. */
export function* collectElectronMemberWritesSteps(
  program: t.Program,
  origin: (root: t.Node) => readonly string[] | undefined,
): Generator<void, SemanticPropertyPathCoverage> {
  const writes = new SemanticPropertyPathCoverage();
  yield* traverseJavaScriptAstSteps(program, {
    enter: (node) => {
      const target = t.isAssignmentExpression(node)
        ? node.left
        : t.isUpdateExpression(node)
          ? node.argument
          : t.isUnaryExpression(node, { operator: "delete" })
            ? node.argument
            : t.isForOfStatement(node) || t.isForInStatement(node)
              ? node.left
              : undefined;
      if (target === undefined) return;
      for (const member of semanticMemberWriteTargets(target)) {
        const { root, members } = electronMemberPath(member);
        const exported = origin(root);
        if (exported !== undefined) writes.retain([...exported, ...members]);
      }
    },
  });
  return writes;
}

/** Syntax-highlighted Python, rendered as React text nodes (no innerHTML). */
import { useMemo } from "react";
import { tokenizePython } from "../lib/highlight";

export function CodeBlock({ code }: { code: string }) {
  const tokens = useMemo(() => tokenizePython(code), [code]);
  return (
    <pre className="code-block">
      <code>
        {tokens.map((token, i) =>
          token.type === "plain" ? (
            token.text
          ) : (
            <span key={i} className={`tok-${token.type}`}>
              {token.text}
            </span>
          ),
        )}
      </code>
    </pre>
  );
}

/**
 * A tiny hand-rolled Python tokenizer for syntax highlighting. One regex pass;
 * tokens are rendered as React text nodes (no innerHTML), so agent code is
 * never interpreted as markup.
 */

export type TokenType = "kw" | "str" | "num" | "com" | "call" | "plain";

export interface Token {
  type: TokenType;
  text: string;
}

const KEYWORDS = new Set([
  "and", "as", "assert", "async", "await", "break", "class", "continue",
  "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
  "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass",
  "raise", "return", "try", "while", "with", "yield",
  "True", "False", "None", "match", "case", "self",
]);

// Ordered alternatives: (prefixed) strings, comments, numbers, identifiers,
// then a single-character catch-all (merged into runs afterwards).
const TOKEN_RE =
  /[rRbBfFuU]{0,2}(?:"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:\\.|[^"\\\n])*(?:"|$)|'(?:\\.|[^'\\\n])*(?:'|$))|#[^\n]*|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b|[A-Za-z_][A-Za-z0-9_]*|[\s\S]/g;

const STRING_START_RE = /^[rRbBfFuU]{0,2}["']/;

export function tokenizePython(code: string): Token[] {
  const tokens: Token[] = [];
  const push = (type: TokenType, text: string) => {
    const last = tokens[tokens.length - 1];
    if (type === "plain" && last?.type === "plain") last.text += text;
    else tokens.push({ type, text });
  };

  TOKEN_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = TOKEN_RE.exec(code)) !== null) {
    const text = match[0];
    const first = text.charAt(0);
    if (first === "#") {
      push("com", text);
    } else if (text.length > 1 && STRING_START_RE.test(text)) {
      push("str", text);
    } else if (/\d/.test(first)) {
      push("num", text);
    } else if (/[A-Za-z_]/.test(first) && text.length >= 1 && /^[A-Za-z_][A-Za-z0-9_]*$/.test(text)) {
      if (KEYWORDS.has(text)) {
        push("kw", text);
      } else if (code.charAt(match.index + text.length) === "(") {
        push("call", text);
      } else {
        push("plain", text);
      }
    } else {
      push("plain", text);
    }
  }
  return tokens;
}

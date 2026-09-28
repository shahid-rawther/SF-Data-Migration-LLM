import type { ReactNode } from 'react';

/**
 * Renders the Markdown the agents write (headings, lists, emphasis, code,
 * tables, links) as React elements. Nothing is injected as HTML, so text from
 * the model cannot add markup of its own.
 *
 * `_underscore_` emphasis is deliberately not supported: agent text is full of
 * field names like work_start, which it would mangle.
 */
export function AgentMarkdown({ text, compact = false }: { text: string; compact?: boolean }) {
  const blocks = parseBlocks(text || '');
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: compact ? 4 : 8 }}>
      {blocks.map((block, index) => renderBlock(block, index))}
    </div>
  );
}

/** Inline formatting only, for one-line text such as a list of evidence. */
export function AgentInline({ text }: { text: string }) {
  return <>{renderInline(text || '')}</>;
}

// ── blocks ────────────────────────────────────────────────────────────────────

type ListItem = { text: string; children: ListNode | null };
type ListNode = { ordered: boolean; start: number; items: ListItem[] };

type Block =
  | { type: 'heading'; level: number; text: string }
  | { type: 'paragraph'; lines: string[] }
  | { type: 'list'; list: ListNode }
  | { type: 'code'; text: string }
  | { type: 'quote'; lines: string[] }
  | { type: 'table'; header: string[]; rows: string[][] }
  | { type: 'rule' };

const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const FENCE = /^\s*```/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    if (FENCE.test(line)) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) body.push(lines[i++]);
      i += 1;
      blocks.push({ type: 'code', text: body.join('\n') });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, text: heading[2] });
      i += 1;
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ type: 'rule' });
      i += 1;
      continue;
    }

    if (line.includes('|') && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])) {
      const header = splitRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(splitRow(lines[i++]));
      blocks.push({ type: 'table', header, rows });
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quoted: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s*>\s?/, ''));
      blocks.push({ type: 'quote', lines: quoted });
      continue;
    }

    if (LIST_ITEM.test(line)) {
      const listLines: string[] = [];
      while (i < lines.length) {
        const current = lines[i];
        if (LIST_ITEM.test(current)) {
          listLines.push(current);
        } else if (current.trim() && /^\s{2,}/.test(current) && listLines.length) {
          // An indented line continues the item above it.
          listLines[listLines.length - 1] += ` ${current.trim()}`;
        } else if (!current.trim() && i + 1 < lines.length && LIST_ITEM.test(lines[i + 1])) {
          // A blank line between items keeps the list together.
        } else {
          break;
        }
        i += 1;
      }
      blocks.push({ type: 'list', list: parseList(listLines) });
      continue;
    }

    const paragraph: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !HEADING.test(lines[i]) &&
      !LIST_ITEM.test(lines[i]) &&
      !FENCE.test(lines[i]) &&
      !RULE.test(lines[i]) &&
      !/^\s*>/.test(lines[i])
    ) {
      paragraph.push(lines[i++].trim());
    }
    blocks.push({ type: 'paragraph', lines: paragraph });
  }
  return blocks;
}

/** Items indented deeper than their predecessor become that item's sub-list. */
function parseList(lines: string[]): ListNode {
  const parsed = lines.map((line) => {
    const match = LIST_ITEM.exec(line)!;
    return {
      indent: match[1].replace(/\t/g, '    ').length,
      ordered: /\d/.test(match[2]),
      start: parseInt(match[2], 10) || 1,
      text: match[3],
    };
  });

  const build = (from: number, to: number, indent: number): ListNode => {
    const node: ListNode = { ordered: parsed[from].ordered, start: parsed[from].start, items: [] };
    let index = from;
    while (index < to) {
      const item = parsed[index];
      let end = index + 1;
      while (end < to && parsed[end].indent > indent) end += 1;
      node.items.push({
        text: item.text,
        children: end > index + 1 ? build(index + 1, end, parsed[index + 1].indent) : null,
      });
      index = end;
    }
    return node;
  };

  return build(0, parsed.length, parsed[0]?.indent ?? 0);
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function renderBlock(block: Block, key: number): ReactNode {
  switch (block.type) {
    case 'heading':
      return (
        <div key={key} style={block.level <= 2 ? headingLarge : headingSmall}>
          {renderInline(block.text)}
        </div>
      );
    case 'paragraph':
      return (
        <p key={key} style={paragraph}>
          {block.lines.map((line, index) => (
            <span key={index}>
              {index > 0 && <br />}
              {renderInline(line)}
            </span>
          ))}
        </p>
      );
    case 'list':
      return renderList(block.list, key);
    case 'code':
      return (
        <pre key={key} style={codeBlock}>
          <code>{block.text}</code>
        </pre>
      );
    case 'quote':
      return (
        <div key={key} style={quote}>
          {block.lines.map((line, index) => (
            <span key={index}>
              {index > 0 && <br />}
              {renderInline(line)}
            </span>
          ))}
        </div>
      );
    case 'table':
      return (
        <div key={key} style={{ overflowX: 'auto' }}>
          <table style={table}>
            <thead>
              <tr>
                {block.header.map((cell, index) => (
                  <th key={index} style={th}>{renderInline(cell)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, index) => (
                    <td key={index} style={td}>{renderInline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case 'rule':
      return <hr key={key} style={rule} />;
  }
}

function renderList(list: ListNode, key: number): ReactNode {
  const items = list.items.map((item, index) => (
    <li key={index} style={{ margin: '2px 0' }}>
      {renderInline(item.text)}
      {item.children && renderList(item.children, index)}
    </li>
  ));
  return list.ordered ? (
    <ol key={key} start={list.start} style={listStyle}>{items}</ol>
  ) : (
    <ul key={key} style={listStyle}>{items}</ul>
  );
}

// ── inline ────────────────────────────────────────────────────────────────────

const INLINE =
  /(`+)([^`]+?)\1|\*\*([^*]+?)\*\*|__([^_]+?)__|~~([^~]+?)~~|\*([^*\s][^*]*?)\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index ?? 0;
    if (index > last) nodes.push(text.slice(last, index));
    const [, ticks, code, bold, boldUnderscore, strike, italic, label, href] = match;
    if (ticks) {
      nodes.push(<code key={key++} style={inlineCode}>{code}</code>);
    } else if (bold || boldUnderscore) {
      nodes.push(<strong key={key++}>{renderInline(bold || boldUnderscore)}</strong>);
    } else if (strike) {
      nodes.push(<s key={key++}>{renderInline(strike)}</s>);
    } else if (italic) {
      nodes.push(<em key={key++}>{renderInline(italic)}</em>);
    } else if (label) {
      nodes.push(
        /^https?:\/\//i.test(href) ? (
          <a key={key++} href={href} target="_blank" rel="noopener noreferrer" style={link}>
            {renderInline(label)}
          </a>
        ) : (
          <span key={key++}>{renderInline(label)}</span>
        ),
      );
    }
    last = index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// ── styles ────────────────────────────────────────────────────────────────────

const headingLarge: React.CSSProperties = {
  fontSize: 13.5,
  fontWeight: 700,
  color: '#171717',
  marginTop: 4,
};
const headingSmall: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 700,
  color: '#262626',
  marginTop: 2,
};
const paragraph: React.CSSProperties = { margin: 0, lineHeight: 1.55 };
const listStyle: React.CSSProperties = { margin: 0, paddingLeft: 20, lineHeight: 1.55 };
const inlineCode: React.CSSProperties = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  fontSize: '0.9em',
  background: 'rgba(0, 0, 0, 0.06)',
  padding: '1px 5px',
  borderRadius: 4,
  wordBreak: 'break-word',
};
const codeBlock: React.CSSProperties = {
  margin: 0,
  padding: '8px 10px',
  background: '#f5f5f5',
  border: '1px solid #e5e5e5',
  borderRadius: 6,
  fontSize: 12,
  lineHeight: 1.5,
  overflowX: 'auto',
  whiteSpace: 'pre',
};
const quote: React.CSSProperties = {
  borderLeft: '3px solid #d4d4d4',
  paddingLeft: 10,
  color: '#525252',
  lineHeight: 1.55,
};
const table: React.CSSProperties = { borderCollapse: 'collapse', fontSize: 12, width: '100%' };
const th: React.CSSProperties = {
  textAlign: 'left',
  padding: '4px 8px',
  borderBottom: '1px solid #d4d4d4',
  fontWeight: 700,
  whiteSpace: 'nowrap',
};
const td: React.CSSProperties = {
  padding: '4px 8px',
  borderBottom: '1px solid #f0f0f0',
  verticalAlign: 'top',
};
const rule: React.CSSProperties = { border: 'none', borderTop: '1px solid #e5e5e5', margin: '4px 0' };
const link: React.CSSProperties = { color: '#171717', textDecoration: 'underline' };

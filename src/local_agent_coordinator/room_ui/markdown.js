'use strict';

(() => {
  const t = (key, params = {}) => window.BeanestI18n ? window.BeanestI18n.t(key, params)
    : key.replace(/\{(p\d+)\}/g, (token, name) => Object.hasOwn(params, name) ? String(params[name]) : token);
  function element(tag, content = '', className = '') {
    const node = document.createElement(tag);
    node.textContent = content;
    if (className) node.className = className;
    return node;
  }

// Model text is never HTML. This deliberately small Markdown reader only
// creates known DOM elements. Local references require a host-owned resolver;
// remote images are never loaded by Markdown itself.
const readerLimits = { characters: 256 * 1024, lines: 8192, blocks: 3000, inline: 500 };

function appendMarkdownInline(parent, text, options = {}) {
  // Keep malformed, very long lines from turning token matching into costly
  // repeated scans; the rest remains visible as literal text.
  const scan = text.slice(0, 4096);
  const tokens = /!\[([^\]\n]*)\]\((<[^<>\n]+>|[^\s)]*)\)|\[([^\]\n]+)\]\((<[^<>\n]+>|[^\s)]*)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g;
  let offset = 0;
  let count = 0;
  for (const match of scan.matchAll(tokens)) {
    if (++count > readerLimits.inline) break;
    if (match.index > offset) parent.append(element('span', text.slice(offset, match.index)));
    const reference = markdownReference(match);
    const local = reference && isLocalReference(reference.href) && typeof options.localReference === 'function' ? options.localReference(reference) : null;
    if (local) parent.append(local);
    else if (match[1] !== undefined) parent.append(element('span', t('图片：{p0}（不自动加载）', { p0: match[1] || t('未命名') }), 'markdown-image-note'));
    else if (match[3] !== undefined) {
      const href = reference.href;
      if (/^https?:\/\/[^\s<>"'`\\]+$/i.test(href) && !/[\u0000-\u0020\u007f]/.test(href)) {
        const link = element('a', match[3]);
        link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer';
        parent.append(link);
      } else parent.append(element('span', match[0]));
    } else parent.append(element(match[5] !== undefined ? 'code' : match[6] !== undefined ? 'strong' : 'em', match[5] ?? match[6] ?? match[7]));
    offset = match.index + match[0].length;
  }
  if (offset < text.length) parent.append(element('span', text.slice(offset)));
}

function markdownReference(match) {
  if (match[1] === undefined && match[3] === undefined) return null;
  const destination = match[2] ?? match[4];
  const href = destination.startsWith('<') && destination.endsWith('>') ? destination.slice(1, -1) : destination;
  return { href, label: match[1] ?? match[3], image: match[1] !== undefined };
}
function isLocalReference(href) {
  return typeof href === 'string' && !!href && !/[\u0000-\u001f\u007f]/.test(href)
    && (/^file:\/\/\//i.test(href) || !/^[a-z][a-z0-9+.-]*:/i.test(href) && !href.startsWith('//') && !href.startsWith('#'));
}
function localReferences(content) {
  const pattern = /!?\[([^\]\n]*)\]\((<[^<>\n]+>|[^\s)]*)\)/g;
  const references = [];
  for (const match of content.slice(0, readerLimits.characters).matchAll(pattern)) {
    const href = match[2].startsWith('<') ? match[2].slice(1, -1) : match[2];
    if (isLocalReference(href)) references.push({ href, label: match[1], image: match[0].startsWith('!') });
    if (references.length >= readerLimits.inline) break;
  }
  return references;
}
function renderMarkdown(content, holder, options = {}) {
  const bounded = content.slice(0, readerLimits.characters);
  const allLines = bounded.replace(/\r\n?/g, '\n').split('\n');
  const lines = allLines.slice(0, readerLimits.lines);
  let index = 0;
  let blocks = 0;
  const tableCells = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim()).slice(0, 40);
  const tableRule = (line) => /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  const startsBlock = (line, next = '') => /^\s*(?:#{1,6}\s|```|~~~|[-*+]\s|\d+\.\s|>\s|(?:---+|\*\*\*+)\s*$)/.test(line) || (line.includes('|') && tableRule(next));
  while (index < lines.length && blocks++ < readerLimits.blocks) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const fence = line.match(/^\s*(`{3,}|~{3,})([^\s]*)\s*$/);
    if (fence) {
      index += 1;
      const code = [];
      while (index < lines.length && !new RegExp(`^\\s*${fence[1][0]}{${fence[1].length},}\\s*$`).test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      const pre = element('pre'); pre.append(element('code', code.join('\n'))); holder.append(pre); continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) { const node = element(`h${heading[1].length}`); appendMarkdownInline(node, heading[2], options); holder.append(node); index += 1; continue; }
    if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) { holder.append(element('hr')); index += 1; continue; }
    if (line.includes('|') && tableRule(lines[index + 1] || '')) {
      const wrap = element('div', '', 'markdown-table'); const table = element('table'); const head = element('thead'); const row = element('tr');
      for (const cell of tableCells(line)) { const node = element('th'); appendMarkdownInline(node, cell, options); row.append(node); }
      head.append(row); table.append(head); const body = element('tbody'); index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim() && blocks++ < readerLimits.blocks) {
        const tr = element('tr'); for (const cell of tableCells(lines[index++])) { const td = element('td'); appendMarkdownInline(td, cell, options); tr.append(td); } body.append(tr);
      }
      table.append(body); wrap.append(table); holder.append(wrap); continue;
    }
    const list = line.match(/^\s*([-*+]|\d+\.)\s+(.+)$/);
    if (list) {
      const ordered = /\d/.test(list[1]); const node = element(ordered ? 'ol' : 'ul');
      while (index < lines.length && blocks++ < readerLimits.blocks) {
        const item = lines[index].match(/^\s*([-*+]|\d+\.)\s+(.+)$/);
        if (!item || /\d/.test(item[1]) !== ordered) break;
        const li = element('li'); appendMarkdownInline(li, item[2], options); node.append(li); index += 1;
      }
      holder.append(node); continue;
    }
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) { const node = element('blockquote'); appendMarkdownInline(node, quote[1], options); holder.append(node); index += 1; continue; }
    const paragraph = [line]; index += 1;
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index], lines[index + 1])) paragraph.push(lines[index++]);
    const node = element('p'); appendMarkdownInline(node, paragraph.join('\n'), options); holder.append(node);
  }
  if (content.length > bounded.length || allLines.length > lines.length || index < lines.length) holder.append(element('p', t('这份内容较长，排版只显示前一部分；切换“查看源文”即可阅读完整内容。'), 'reader-limit'));
}

window.BeanestMarkdown = Object.freeze({ render: renderMarkdown, localReferences, limits: Object.freeze(readerLimits) });
})();

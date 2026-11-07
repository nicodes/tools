#!/usr/bin/env python3
"""Check metadata and basic accessible markup in an actual static-site export."""
import argparse
import gzip
import importlib.util
import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

_source_spec = importlib.util.spec_from_file_location('site_source_config', Path(__file__).with_name('source_config.py'))
_source = importlib.util.module_from_spec(_source_spec)
_source_spec.loader.exec_module(_source)


VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


@dataclass
class Node:
    tag: str
    attrs: dict
    parent: object = None
    children: list = field(default_factory=list)

    def text(self):
        if self.tag in {'script', 'style', 'template'} or self.attrs.get('aria-hidden') == 'true':
            return ''
        return ' '.join(child.text() if isinstance(child, Node) else child for child in self.children).strip()


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('document', {})
        self.current = self.root
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.current)
        self.current.children.append(node)
        self.nodes.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def findings(html):
    document = Document()
    document.feed(html)
    nodes = document.nodes
    errors = []
    ids = {n.attrs['id']: n for n in nodes if n.attrs.get('id')}

    def named(node, content=True):
        if node.attrs.get('aria-label', '').strip() or node.attrs.get('title', '').strip():
            return True
        references = node.attrs.get('aria-labelledby', '').split()
        if references and all(ref in ids for ref in references) and any(ids[ref].text() for ref in references):
            return True
        return content and bool(node.text())

    if not any(n.tag == 'html' and n.attrs.get('lang', '').strip() for n in nodes):
        errors.append('html requires a language')
    if not any(n.tag == 'title' and n.text() for n in nodes):
        errors.append('page requires a nonempty title')
    for name in ['description', 'viewport']:
        if not any(n.tag == 'meta' and n.attrs.get('name', '').lower() == name and n.attrs.get('content', '').strip() for n in nodes):
            errors.append('page requires ' + name + ' metadata')
    if sum(n.tag == 'main' or n.attrs.get('role') == 'main' for n in nodes) != 1:
        errors.append('page requires one main landmark')
    for node in nodes:
        if node.tag == 'img' and 'alt' not in node.attrs:
            errors.append('image requires alt text or explicit decorative alt')
        if node.tag in {'button', 'a'} and (node.tag != 'a' or 'href' in node.attrs) and not named(node):
            # An image's alternative text also names its enclosing link/control.
            pending = list(node.children)
            alternative = False
            while pending:
                child = pending.pop()
                if isinstance(child, Node):
                    alternative |= bool(child.tag == 'img' and child.attrs.get('alt', '').strip())
                    pending.extend(child.children)
            if not alternative:
                errors.append(node.tag + ' requires an accessible name')
        if node.tag in {'input', 'select', 'textarea'}:
            kind = node.attrs.get('type', '').lower()
            if node.tag == 'input' and kind in {'hidden', 'submit', 'reset', 'button', 'image'}:
                if kind == 'image' and not node.attrs.get('alt', '').strip():
                    errors.append('image input requires alt text')
                if kind == 'button' and not (node.attrs.get('value', '').strip() or named(node, content=False)):
                    errors.append('button input requires an accessible name')
                continue
            parent = node.parent
            wrapped = False
            while parent is not None:
                wrapped |= parent.tag == 'label' and bool(parent.text())
                parent = parent.parent
            labelled = any(n.tag == 'label' and n.attrs.get('for') == node.attrs.get('id') and n.attrs.get('for') and n.text() for n in nodes)
            if not (named(node, content=False) or wrapped or labelled):
                errors.append(node.tag + ' requires a label')
    return sorted(set(errors))


def check(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('site output must be a regular directory')
    files = sorted(root.rglob('*.html'))
    if not files:
        raise ValueError('site output contains no HTML pages')
    errors = []
    for path in files:
        if (path.is_symlink() or any(p.is_symlink() for p in path.parents)
                or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024):
            raise ValueError('HTML output must be bounded and regular')
        errors.extend(str(path.relative_to(root)) + ': ' + error for error in findings(path.read_text(encoding='utf-8')))
    return files, errors


def performance(root, budget):
    """Measure reproducible export bytes, independent of CDN compression or timing."""
    required = {'max_total_bytes', 'max_file_bytes', 'max_html_gzip_bytes', 'max_javascript_gzip_bytes'}
    if not isinstance(budget, dict) or set(budget) != required or any(type(value) is not int or not 0 < value <= 64 * 1024 * 1024 for value in budget.values()):
        raise ValueError('performance budget requires four positive bounded integer byte limits')
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('site output must be a regular directory')
    errors, total, javascript, count = [], 0, 0, 0
    largest_html = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('performance output must not contain symbolic links')
        if path.is_dir():
            continue
        count += 1
        if count > 10000 or not path.is_file():
            raise ValueError('performance output must contain bounded regular files')
        size = path.stat().st_size
        total += size
        name = str(path.relative_to(root))
        if size > budget['max_file_bytes']:
            errors.append(f'{name}: {size} bytes exceeds per-file budget {budget["max_file_bytes"]}')
        # Never load an unbounded file merely to produce a compressed-size report.
        if path.suffix.lower() in {'.html', '.js', '.mjs'}:
            if size > 8 * 1024 * 1024:
                raise ValueError('compressible website output must be bounded')
            compressed = len(gzip.compress(path.read_bytes(), mtime=0))
            if path.suffix.lower() == '.html':
                largest_html = max(largest_html, compressed)
                if compressed > budget['max_html_gzip_bytes']:
                    errors.append(f'{name}: {compressed} gzip bytes exceeds HTML budget {budget["max_html_gzip_bytes"]}')
            else:
                javascript += compressed
    if total > budget['max_total_bytes']:
        errors.append(f'export: {total} bytes exceeds total budget {budget["max_total_bytes"]}')
    if javascript > budget['max_javascript_gzip_bytes']:
        errors.append(f'JavaScript: {javascript} gzip bytes exceeds budget {budget["max_javascript_gzip_bytes"]}')
    return {'files': count, 'total_bytes': total, 'largest_html_gzip_bytes': largest_html, 'javascript_gzip_bytes': javascript}, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--budget', type=Path, help='product-owned TOML byte limits for the actual export')
    args = parser.parse_args()
    try:
        files, errors = check(args.directory)
        if args.budget:
            metrics, exceeded = performance(args.directory, _source.read(args.budget))
            errors.extend(exceeded)
            print('Export performance:', json.dumps(metrics, sort_keys=True))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    if errors:
        parser.exit(1, '\n'.join(errors) + '\n')
    print('Verified metadata and basic accessible markup for', len(files), 'HTML pages')


if __name__ == '__main__':
    main()

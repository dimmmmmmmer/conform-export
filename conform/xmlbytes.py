"""Parse byte locations and splice edits; the XML is never reserialized."""
from dataclasses import dataclass, field
import re
from xml.parsers import expat
from xml.sax.saxutils import escape


class Unsupported(ValueError):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict
    start: int
    body: int
    empty: bool
    stop: int = 0           # start of the closing tag (== body for <x/>)
    end: int = 0            # just past the closing tag
    text: str = ''
    children: list = field(default_factory=list)

    def all(self, tag):
        return [n for n in self.children if n.tag == tag]

    def one(self, tag):
        found = self.all(tag)
        if len(found) != 1:
            raise Unsupported('Expected one %s/%s, got %s' % (self.tag, tag, len(found)))
        return found[0]

    def value(self, tag):
        return self.one(tag).text

    def path(self, *tags):
        node = self
        for tag in tags:
            node = node.one(tag)
        return node


def parse(data):
    # Namespaces intentionally disabled: native Resolve XML includes ListMgt:: tags.
    # Names are inserted as UTF-8 bytes, so any other declared encoding would
    # silently turn them into mojibake.
    declared = re.match(rb'(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?encoding\s*=\s*["\']([^"\']+)', data)
    if (declared and declared.group(1).lower() not in (b'utf-8', b'utf8')) or data.startswith((b'\xff\xfe', b'\xfe\xff')):
        raise Unsupported('Only UTF-8 XML is supported')
    data.decode('utf-8')
    p = expat.ParserCreate()
    stack, roots = [], []

    def start(tag, attrs):
        a = p.CurrentByteIndex
        # Find the closing > outside quoted attribute values.
        i, quote = a, None
        while i < len(data):
            c = data[i]
            if quote:
                if c == quote:
                    quote = None
            elif c in (34, 39):
                quote = c
            elif c == 62:
                break
            i += 1
        node = Node(tag, attrs, a, i + 1, data[a:i].rstrip().endswith(b'/'))
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    def end(tag):
        node = stack.pop()
        node.stop = p.CurrentByteIndex if not node.empty else node.body
        node.end = data.index(b'>', node.stop) + 1 if not node.empty else node.body

    def chars(value):
        if stack:
            stack[-1].text += value

    def reject(*args):
        raise Unsupported('Entity declarations are not supported')

    p.StartElementHandler, p.EndElementHandler = start, end
    p.CharacterDataHandler = chars
    p.EntityDeclHandler = reject
    p.ExternalEntityRefHandler = reject
    p.Parse(data, True)
    if len(roots) != 1:
        raise Unsupported('Expected one XML root')
    return roots[0]


def text(value):
    value = str(value)
    if any(ord(c) < 32 and c not in '\t\n\r' for c in value):
        raise ValueError('Invalid XML control character in text')
    return escape(value).replace('\r', '&#13;').encode('utf-8')


def text_edit(node, value):
    if node.children or node.empty:
        raise Unsupported('Expected a text element: ' + node.tag)
    return node.body, node.stop, text(value)


def remove(node):
    return node.start, node.end, b''


def apply(data, edits):
    out, cursor = [], 0
    for a, b, value in sorted(edits):
        if not cursor <= a <= b <= len(data):
            raise Unsupported('Overlapping or invalid patch ranges')
        out.extend((data[cursor:a], value))
        cursor = b
    out.append(data[cursor:])
    result = b''.join(out)
    parse(result)
    return result

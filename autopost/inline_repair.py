"""발행 HTML의 텍스트 노드에 노출된 Markdown 링크만 보정한다."""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

from .content import repair_link_text


@dataclass(frozen=True)
class LinkRepair:
    html: str
    changes: int


class _LinkRepairParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.offsets = [0]
        for line in source.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.cursor = 0
        self.protected: list[str] = []
        self.edits: list[tuple[int, int, str, int]] = []

    def position(self) -> int:
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def flush(self, end: int) -> None:
        if not self.protected:
            fixed, count = repair_link_text(self.source[self.cursor:end])
            if count:
                self.edits.append((self.cursor, end, fixed, count))
        self.cursor = end

    def handle_starttag(self, tag: str, attrs) -> None:
        at = self.position()
        self.flush(at)
        self.cursor = at + len(self.get_starttag_text())
        if tag in ("pre", "code", "a", "script", "style", "textarea"):
            self.protected.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        at = self.position()
        self.flush(at)
        self.cursor = at + len(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        at = self.position()
        self.flush(at)
        self.cursor = self.source.index(">", at) + 1
        if tag in self.protected:
            self.protected.remove(tag)

    def handle_comment(self, data: str) -> None:
        at = self.position()
        self.flush(at)
        self.cursor = self.source.index("-->", at) + 3

    def handle_decl(self, decl: str) -> None:
        at = self.position()
        self.flush(at)
        self.cursor = self.source.index(">", at) + 1


def repair_html_links(source: str) -> LinkRepair:
    """태그·속성·주석·코드·기존 링크는 그대로 두며, 두 번 실행해도 같은 결과다."""
    parser = _LinkRepairParser(source)
    parser.feed(source)
    parser.close()
    parser.flush(len(source))
    fixed = source
    for start, end, replacement, _ in reversed(parser.edits):
        fixed = fixed[:start] + replacement + fixed[end:]
    return LinkRepair(fixed, sum(count for _, _, _, count in parser.edits))

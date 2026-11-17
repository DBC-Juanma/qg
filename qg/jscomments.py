"""Comments of TS/JS sources, by a lexer that skips strings, templates, regexes and JSX text."""

import bisect
import re
from dataclasses import dataclass, field

SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
NO_JSX = (".ts",)
PRAGMA = re.compile(
    r"(eslint-disable|eslint-enable|@ts-expect-error|@ts-ignore|@ts-nocheck|prettier-ignore"
    r"|istanbul ignore|c8 ignore|biome-ignore|@vite-ignore|webpackChunkName)"
)
REFERENCE = re.compile(r"///\s*<reference\b")
SPACE = re.compile(r"\s*")
WORD = re.compile(r"[A-Za-z_$#\u0080-￿][\w$\u0080-￿]*")
FLAGS = re.compile(r"[A-Za-z]*")
NUMBER = re.compile(r"\.?\d[\w.]*")
TAG_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
ATTRIBUTE = re.compile(r"[\w$.:-]+")
TEXT = re.compile(r"[^<{]*")
NOT_VALUE = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await extends".split()
)
MODIFIERS = r"((public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*"
CONTROL = r"(if|for|while|switch|catch|with|return|function)\b"
DECLARATION = re.compile(
    r"export\s"
    r"|(async\s+)?function\b"
    r"|(abstract\s+)?class\s"
    r"|(const|let|var)\s+[\w$]+[^=]*=.*(=>|\bfunction\b)"
    rf"|(?!{CONTROL}){MODIFIERS}\*?#?[\w$]+\s*(<[^>]*>)?\s*\([^)]*\)\s*(:\s*[^={{;]+)?\{{"
)


@dataclass
class _Lexer:
    """Walks `text` once, collecting the (start, end) offsets of its comments; JSX when `jsx`."""

    text: str
    jsx: bool
    pos: int = 0
    value: bool = False
    comments: list[tuple[int, int]] = field(default_factory=list)

    def run(self) -> list[tuple[int, int]]:
        """Returns the offsets of every comment, a line-1 shebang excluded."""
        if self.text.startswith("#!"):
            newline = self.text.find("\n")
            self.pos = len(self.text) if newline < 0 else newline
        self.code(nested=False)
        return self.comments

    def comment(self) -> bool:
        """Consumes a comment at `pos` and records it; tells whether there was one."""
        head = self.text[self.pos : self.pos + 2]
        if head == "//":
            end = self.text.find("\n", self.pos)
            end = len(self.text) if end < 0 else end
        elif head == "/*":
            end = self.text.find("*/", self.pos + 2)
            end = len(self.text) if end < 0 else end + 2
        else:
            return False
        self.comments.append((self.pos, end))
        self.pos = end
        return True

    def code(self, nested: bool) -> None:
        """Consumes code up to the `}` closing it when `nested`, else to the end of the text."""
        depth, text = 0, self.text
        self.value = False
        while self.pos < len(text):
            char = text[self.pos]
            if char.isspace():
                self.pos += 1
            elif self.comment():
                continue
            elif char in "'\"":
                self.string(char)
                self.value = True
            elif char == "`":
                self.template()
                self.value = True
            elif char == "/" and not self.value and self.regex():
                self.value = True
            elif char == "<" and self.jsx and not self.value and self.element():
                self.value = True
            elif char == "}" and depth == 0 and nested:
                self.pos += 1
                return
            elif word := WORD.match(text, self.pos):
                self.pos = word.end()
                self.value = word.group() not in NOT_VALUE
            elif number := NUMBER.match(text, self.pos):
                self.pos = number.end()
                self.value = True
            else:
                depth += {"{": 1, "}": -1}.get(char, 0)
                self.value = char in ")]}"
                self.pos += 1

    def string(self, quote: str) -> None:
        """Consumes a quoted string; an unescaped newline ends it."""
        self.pos += 1
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 2
                continue
            if char == "\n":
                return
            self.pos += 1
            if char == quote:
                return

    def template(self) -> None:
        """Consumes a template literal, its `${...}` substitutions lexed as code."""
        self.pos += 1
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 2
            elif char == "`":
                self.pos += 1
                return
            elif self.text.startswith("${", self.pos):
                self.pos += 2
                self.code(nested=True)
            else:
                self.pos += 1

    def regex(self) -> bool:
        """Consumes a regex literal at `pos`; tells whether one ends on its line."""
        start, inside = self.pos, False
        self.pos += 1
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\n":
                break
            if char == "\\":
                self.pos += 2
                continue
            self.pos += 1
            if char == "[":
                inside = True
            elif char == "]":
                inside = False
            elif char == "/" and not inside:
                self.pos = FLAGS.match(self.text, self.pos).end()
                return True
        self.pos = start
        return False

    def element(self) -> bool:
        """Consumes a JSX element at `pos`; restores `pos` and the comments when it is not one."""
        start, kept = self.pos, len(self.comments)
        if self.tag():
            return True
        self.pos = start
        del self.comments[kept:]
        return False

    def tag(self) -> bool:
        """Consumes an opening tag and, unless self-closing, its children and closing tag."""
        self.pos = SPACE.match(self.text, self.pos + 1).end()
        if self.text.startswith(">", self.pos):
            self.pos += 1
            return self.children()
        name = TAG_NAME.match(self.text, self.pos)
        if name is None:
            return False
        self.pos = name.end()
        while self.pos < len(self.text):
            self.pos = SPACE.match(self.text, self.pos).end()
            char = self.text[self.pos : self.pos + 1]
            if self.text.startswith("/>", self.pos):
                self.pos += 2
                return True
            if char == ">":
                self.pos += 1
                return self.children()
            if char == "{":
                self.pos += 1
                self.code(nested=True)
            elif char in ("'", '"'):
                end = self.text.find(char, self.pos + 1)
                if end < 0:
                    return False
                self.pos = end + 1
            elif char == "=":
                self.pos += 1
            elif attribute := ATTRIBUTE.match(self.text, self.pos):
                self.pos = attribute.end()
            elif not self.comment():
                return False
        return False

    def children(self) -> bool:
        """Consumes JSX children up to and including the closing tag; tells whether it closed."""
        while self.pos < len(self.text):
            self.pos = TEXT.match(self.text, self.pos).end()
            if self.text.startswith("{", self.pos):
                self.pos += 1
                self.code(nested=True)
            elif self.text.startswith("<", self.pos):
                after = SPACE.match(self.text, self.pos + 1).end()
                if self.text.startswith("/", after):
                    end = self.text.find(">", after)
                    if end < 0:
                        return False
                    self.pos = end + 1
                    return True
                if not self.tag():
                    return False
        return False


def _line_after(text: str, end: int) -> str:
    """Returns the first non-blank, non-decorator line of code from offset `end` on."""
    while end <= len(text):
        stop = text.find("\n", end)
        stop = len(text) if stop < 0 else stop
        line = text[end:stop].strip()
        if line and not line.startswith("@"):
            return line
        end = stop + 1
    return ""


def _described(text: str, start: int, end: int) -> bool:
    """Tells whether comment `start`..`end` is a lone one-line `/** */` above a declaration."""
    body = text[start:end]
    if "\n" in body or not body.startswith("/**") or not body.endswith("*/"):
        return False
    alone = not text[text.rfind("\n", 0, start) + 1 : start].strip()
    return alone and DECLARATION.match(_line_after(text, end)) is not None


def _pragma(body: str) -> bool:
    """Tells whether comment `body` is a tool pragma or a triple-slash reference."""
    if REFERENCE.match(body):
        return True
    inner = body[2:].removesuffix("*/").lstrip("/*! \t\r\n")
    return PRAGMA.match(inner) is not None


def comment_lines(text: str, name: str) -> list[int]:
    """Returns the lines of TS/JS `text` (path `name`) holding a comment that must not be added."""
    newlines = [index for index, char in enumerate(text) if char == "\n"]
    lexer = _Lexer(text, jsx=not name.endswith(NO_JSX))
    lines: set[int] = set()
    for start, end in lexer.run():
        if _pragma(text[start:end]) or _described(text, start, end):
            continue
        first = bisect.bisect_left(newlines, start) + 1
        last = bisect.bisect_left(newlines, max(start, end - 1)) + 1
        lines.update(range(first, last + 1))
    return sorted(lines)

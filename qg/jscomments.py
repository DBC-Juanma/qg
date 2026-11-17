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
NUMBER = re.compile(r"\.?\d[\w.]*")
QUOTED = {
    "'": re.compile(r"'(?:[^'\\\n]|\\[\s\S])*'?"),
    '"': re.compile(r'"(?:[^"\\\n]|\\[\s\S])*"?'),
}
REGEX = re.compile(r"/(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[A-Za-z]*")
TEMPLATE_TEXT = re.compile(r"[^`\\$]+")
TAG_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
ATTRIBUTE = re.compile(r"[\w$.:-]+")
TEXT = re.compile(r"[^<{]*")
NOT_VALUE = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await extends".split()
)
CODE, NESTED, TEMPLATE, TAG, CHILDREN = range(5)
SIGNATURE_LIMIT = 500
HEAD = re.compile(r"(export|declare)\s|(async\s+)?function\b|(abstract\s+)?class\s")
CONTROL = re.compile(
    r"(if|for|while|switch|catch|with|return|await|new|throw|typeof|delete|void|yield|super|this)\b"
)
BINDING = re.compile(r"(const|let|var)\s+[\w$]+\s*(:[^=]{0,200})?=\s*(async\s+)?")
MEMBER = re.compile(
    r"((public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*"
    r"\*?#?[\w$]+[?!]?\s*(<[^<>()]{0,200}>\s*)?"
)
GENERIC = re.compile(r"<[^<>()]{0,200}>\s*")
BARE_ARROW = re.compile(r"[\w$]+\s*=>")
PARENS = re.compile(r"[()]")


@dataclass
class _Attempt:
    """A JSX element tried at offset `start`, with the comment and frame counts to rewind to."""

    start: int
    comments: int
    frames: int


@dataclass
class _Lexer:
    """Walks `text` with an explicit stack of modes, collecting comment (start, end) offsets."""

    text: str
    jsx: bool
    pos: int = 0
    value: bool = False
    comments: list[tuple[int, int]] = field(default_factory=list)
    frames: list[list[int]] = field(default_factory=lambda: [[CODE, 0]])
    attempts: list[_Attempt] = field(default_factory=list)
    failed: set[int] = field(default_factory=set)
    budget: int = 0
    no_regex_until: int = -1

    def run(self) -> list[tuple[int, int]]:
        """Returns the offsets of every comment, a line-1 shebang excluded."""
        if self.text.startswith("#!"):
            newline = self.text.find("\n")
            self.pos = len(self.text) if newline < 0 else newline
        self.budget = 2 * len(self.text) + 4096
        step = {
            CODE: self.code,
            NESTED: self.code,
            TEMPLATE: self.template,
            TAG: self.tag,
            CHILDREN: self.children,
        }
        while True:
            if self.pos >= len(self.text):
                if not self.attempts:
                    return self.comments
                self.fail(everything=True)
                continue
            frame = self.frames[-1]
            step[frame[0]](frame)

    def push(self, kind: int, slot: int = 0) -> None:
        """Enters mode `kind`; code starts in operand position."""
        self.frames.append([kind, slot])
        self.value = False

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

    def code(self, frame: list[int]) -> None:
        """Consumes one token of code; a `}` at depth 0 of nested code returns to its parent."""
        text, char = self.text, self.text[self.pos]
        if char.isspace():
            self.pos += 1
        elif self.comment():
            return
        elif char in QUOTED:
            self.pos = QUOTED[char].match(text, self.pos).end()
            self.value = True
        elif char == "`":
            self.pos += 1
            self.push(TEMPLATE)
        elif char == "/" and not self.value and self.regex():
            self.value = True
        elif char == "<" and self.jsx and not self.value and self.opens():
            return
        elif char == "}" and frame[0] == NESTED and frame[1] == 0:
            self.pos += 1
            self.frames.pop()
        elif word := WORD.match(text, self.pos):
            self.pos = word.end()
            self.value = word.group() not in NOT_VALUE
        elif number := NUMBER.match(text, self.pos):
            self.pos = number.end()
            self.value = True
        else:
            frame[1] += {"{": 1, "}": -1}.get(char, 0)
            self.value = char in ")]}"
            self.pos += 1

    def regex(self) -> bool:
        """Consumes a regex literal at `pos`; tells whether one ends on its line."""
        if self.pos < self.no_regex_until:
            return False
        found = REGEX.match(self.text, self.pos)
        if found is None:
            newline = self.text.find("\n", self.pos)
            self.no_regex_until = len(self.text) if newline < 0 else newline
            return False
        self.pos = found.end()
        return True

    def template(self, frame: list[int]) -> None:
        """Consumes one piece of a template literal; `${` enters nested code."""
        text = self.text
        if text[self.pos] == "\\":
            self.pos += 2
        elif text[self.pos] == "`":
            self.pos += 1
            self.frames.pop()
            self.value = True
        elif text.startswith("${", self.pos):
            self.pos += 2
            self.push(NESTED)
        elif found := TEMPLATE_TEXT.match(text, self.pos):
            self.pos = found.end()
        else:
            self.pos += 1

    def opens(self) -> bool:
        """Tries a JSX element at `pos` unless that offset failed or the rewind budget is spent."""
        if self.pos in self.failed or self.budget <= 0:
            return False
        self.attempts.append(_Attempt(self.pos, len(self.comments), len(self.frames)))
        self.element(self.pos)
        return True

    def element(self, start: int) -> None:
        """Enters the opening tag or fragment of the element at `start`."""
        self.pos = SPACE.match(self.text, start + 1).end()
        if self.text.startswith(">", self.pos):
            self.pos += 1
            self.push(CHILDREN, start)
            return
        name = TAG_NAME.match(self.text, self.pos)
        if name is None:
            self.fail()
            return
        self.pos = name.end()
        self.push(TAG, start)

    def finish(self) -> None:
        """Leaves a closed element; back in code, its attempt succeeded."""
        self.frames.pop()
        if self.frames[-1][0] in (CODE, NESTED):
            self.attempts.pop()
            self.value = True

    def fail(self, everything: bool = False) -> None:
        """Rewinds the innermost JSX attempt (all at EOF), marking its element starts as failed."""
        index = 0 if everything else len(self.attempts) - 1
        attempt = self.attempts[index]
        self.failed.update(f[1] for f in self.frames[attempt.frames :] if f[0] in (TAG, CHILDREN))
        self.failed.update(a.start for a in self.attempts[index:])
        self.budget -= self.pos - attempt.start
        del self.attempts[index:]
        del self.frames[attempt.frames :]
        del self.comments[attempt.comments :]
        self.pos = attempt.start + 1
        self.value = False

    def tag(self, frame: list[int]) -> None:
        """Consumes one piece of an opening tag: an attribute, a `{...}`, `>` or `/>`."""
        text = self.text
        self.pos = SPACE.match(text, self.pos).end()
        if self.pos >= len(text):
            return
        char = text[self.pos]
        if text.startswith("/>", self.pos):
            self.pos += 2
            self.finish()
        elif char == ">":
            self.pos += 1
            frame[0] = CHILDREN
        elif char == "{":
            self.pos += 1
            self.push(NESTED)
        elif char in "'\"":
            end = text.find(char, self.pos + 1)
            if end < 0:
                self.fail()
            else:
                self.pos = end + 1
        elif char == "=":
            self.pos += 1
        elif attribute := ATTRIBUTE.match(text, self.pos):
            self.pos = attribute.end()
        elif not self.comment():
            self.fail()

    def children(self, frame: list[int]) -> None:
        """Consumes JSX text, then a `{...}`, a child element or the closing tag."""
        text = self.text
        self.pos = TEXT.match(text, self.pos).end()
        if self.pos >= len(text):
            return
        if text[self.pos] == "{":
            self.pos += 1
            self.push(NESTED)
            return
        after = SPACE.match(text, self.pos + 1).end()
        if not text.startswith("/", after):
            self.element(self.pos)
            return
        end = text.find(">", after)
        if end < 0:
            self.fail()
        else:
            self.pos = end + 1
            self.finish()


def _after_parens(code: str) -> str | None:
    """Returns what follows the parenthesis group `code` starts with; None without one."""
    if not code.startswith("("):
        return None
    depth = 0
    for paren in PARENS.finditer(code):
        depth += 1 if paren.group() == "(" else -1
        if depth == 0:
            return code[paren.end() :].lstrip()
    return None


def _function_value(code: str) -> bool:
    """Tells whether expression `code` starts with a function or an arrow function."""
    if code.startswith("function") or BARE_ARROW.match(code):
        return True
    generic = GENERIC.match(code)
    after = _after_parens(code[generic.end() :] if generic else code)
    return after is not None and after.startswith(("=>", ":"))


def _declaration(code: str) -> bool:
    """Tells whether `code` starts a function, method, class, arrow binding or exported name."""
    if HEAD.match(code):
        return True
    if CONTROL.match(code):
        return False
    binding = BINDING.match(code)
    if binding:
        return _function_value(code[binding.end() :])
    member = MEMBER.match(code)
    rest = code[member.end() :] if member else ""
    if rest.startswith("="):
        return _function_value(rest[1:].lstrip().removeprefix("async").lstrip())
    after = _after_parens(rest)
    return after is not None and after.startswith(("{", ":"))


def _code_after(text: str, end: int) -> str:
    """Returns the next non-decorator code from `end`, capped; empty when its line is too long."""
    while end <= len(text):
        stop = text.find("\n", end)
        stop = len(text) if stop < 0 else stop
        line = text[end:stop].strip()
        if line and not line.startswith("@"):
            if stop - end > SIGNATURE_LIMIT:
                return ""
            begin = end + len(text[end:stop]) - len(text[end:stop].lstrip())
            return text[begin : begin + SIGNATURE_LIMIT]
        end = stop + 1
    return ""


def _described(text: str, start: int, end: int) -> bool:
    """Tells whether comment `start`..`end` is a lone one-line `/** */` above a declaration."""
    body = text[start:end]
    if "\n" in body or not body.startswith("/**") or not body.endswith("*/"):
        return False
    alone = not text[text.rfind("\n", 0, start) + 1 : start].strip()
    return alone and _declaration(_code_after(text, end))


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

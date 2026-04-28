"""
Scans shared source files for $ markers, generates target-specific headers,
and writes cleaned copies of the shared sources into each target build folder.

Usage can be found at start of main()
"""

import argparse
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path


MARKER = "\n$"
MARKER_LEN = 1
DEFAULT_SOURCE_SUFFIXES = (".h", ".hh", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx")


@dataclass
class MarkerBlock:
    file: Path
    start: int
    end: int
    text: str
    replacement: str = ""


@dataclass
class PassDef:
    name: str
    block_keyword: str
    schema: list["SchemaPart"]
    init_vars: dict
    instance_ops: list[tuple[str, str]]


@dataclass
class SchemaPart:
    kind: str
    value: str = ""
    alternatives: list[list["SchemaPart"]] | None = None


def parse_pass_file(source: str) -> dict[str, str]:
    section_re = re.compile(
        r'^[ \t]*(pass|schema\s*\(\s*\)|schema|init\s*\(\s*\)|init|instance\s*\(\s*\)|instance)',
        re.MULTILINE,
    )

    positions = [(m.group(0).strip(), m.start()) for m in section_re.finditer(source)]
    sections: dict[str, str] = {}
    first_section_start = positions[0][1] if positions else len(source)
    raw_init = source[:first_section_start].strip()
    if raw_init:
        sections["python"] = raw_init

    for i, (name, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else len(source)
        body = source[start:end]

        key = re.match(r'\w+', name).group(0)
        if key == "pass":
            body = re.sub(r'^[ \t]*pass\s+\w+[ \t]*(?:\{[ \t]*\n?)?', '', body, count=1)
            raw_init = textwrap.dedent(body).strip()
            if raw_init:
                sections["python"] = raw_init
            continue

        body = re.sub(r'^[ \t]*' + re.escape(name) + r'[ \t]*(?:\{[ \t]*\n?)?', '', body, count=1)
        sections[key] = unwrap_section_body(body)

    return sections


def unwrap_section_body(body: str) -> str:
    lines = body.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if lines and lines[-1].strip() == "}":
        lines.pop()

    return "\n".join(lines)


def parse_schema_template(schema_body: str, pass_name: str, file: Path) -> list[SchemaPart]:
    source = textwrap.dedent(schema_body).strip()
    if not source:
        raise ValueError(f"$pass {pass_name} has an empty schema block in {file}")

    if re.search(r"<\s*[A-Za-z_]\w*\s*>", source):
        parts = parse_raw_schema_template(source)
    else:
        parts = parse_legacy_schema_template(source, pass_name, file)

    if not parts:
        raise ValueError(f"$pass {pass_name} has an empty schema block in {file}")

    compact = compact_schema_parts(parts, pass_name, file)
    first_literal = first_schema_literal(compact)
    if not first_literal:
        raise ValueError(f"$pass {pass_name} schema in {file} must begin with literal syntax")

    if not schema_starts_with_keyword(first_literal, pass_name):
        raise ValueError(f"$pass {pass_name} schema in {file} must begin with {pass_name!r}")

    return compact


def parse_raw_schema_template(source: str) -> list[SchemaPart]:
    wrapped = parse_wrapped_schema_literal(source)
    if wrapped is not None:
        source = wrapped

    parts, end = parse_raw_schema_parts(source, 0, False)
    if end != len(source):
        raise ValueError(f"Unexpected trailing schema syntax: {source[end:]!r}")
    return parts


def parse_wrapped_schema_literal(source: str) -> str | None:
    source = source.strip()
    if len(source) < 2 or source[0] not in "\"'" or source[-1] != source[0]:
        return None

    quote = source[0]
    value = []
    escaped = False
    for i in range(1, len(source) - 1):
        ch = source[i]
        if escaped:
            value.append(unescape_schema_char(ch))
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == quote:
            return None
        value.append(ch)

    if escaped:
        raise ValueError("Schema literal ends with a trailing escape")
    return "".join(value)


def parse_legacy_schema_template(source: str, pass_name: str, file: Path) -> list[SchemaPart]:
    parts, end = parse_legacy_schema_parts(source, 0, False, pass_name, file)
    if end != len(source):
        raise ValueError(f"Invalid schema syntax in $pass {pass_name} in {file}: {source[end:end+20]!r}")
    return parts


def parse_raw_schema_parts(source: str, start: int, in_branch: bool) -> tuple[list[SchemaPart], int]:
    parts = []
    literal = []
    i = start

    while i < len(source):
        ch = source[i]
        if ch == "]":
            if not in_branch:
                raise ValueError("Unexpected closing ] in schema")
            break
        if ch == "|" and in_branch:
            break
        if ch == "[":
            if literal:
                parts.append(SchemaPart("literal", "".join(literal)))
                literal = []
            branch, i = parse_raw_schema_branch(source, i + 1)
            parts.append(branch)
            continue
        if ch == "<":
            end = source.find(">", i + 1)
            if end == -1:
                raise ValueError(f"Unterminated schema capture in {source!r}")
            name = source[i + 1:end].strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                raise ValueError(f"Invalid schema capture name <{name}>")
            if literal:
                parts.append(SchemaPart("literal", "".join(literal)))
                literal = []
            parts.append(SchemaPart("capture", name))
            i = end + 1
            continue
        literal.append(ch)
        i += 1

    if literal:
        parts.append(SchemaPart("literal", "".join(literal)))
    return parts, i


def parse_raw_schema_branch(source: str, start: int) -> tuple[SchemaPart, int]:
    alternatives = []
    saw_separator = False
    i = start

    while True:
        parts, i = parse_raw_schema_parts(source, i, True)
        alternatives.append(parts)
        if i >= len(source):
            raise ValueError("Unterminated schema branch")
        if source[i] == "|":
            saw_separator = True
            i += 1
            continue
        if source[i] == "]":
            if not saw_separator:
                raise ValueError("Schema branch must contain '|'")
            return SchemaPart("branch", alternatives=alternatives), i + 1
        raise ValueError(f"Invalid schema branch syntax near {source[i:i+20]!r}")


def parse_legacy_schema_parts(
    source: str,
    start: int,
    in_branch: bool,
    pass_name: str,
    file: Path,
) -> tuple[list[SchemaPart], int]:
    parts = []
    i = start

    while i < len(source):
        ch = source[i]
        if ch == "]":
            if not in_branch:
                raise ValueError(f"Unexpected closing ] in $pass {pass_name} schema in {file}")
            break
        if ch == "|" and in_branch:
            break
        if ch == "[":
            branch, i = parse_legacy_schema_branch(source, i + 1, pass_name, file)
            parts.append(branch)
            continue
        if ch.isspace():
            while i < len(source) and source[i].isspace():
                i += 1
            parts.append(SchemaPart("literal", " "))
            continue
        if ch in "\"'":
            literal, i = parse_schema_string_literal(source, i)
            parts.append(SchemaPart("literal", literal))
            continue
        ident = re.match(r"[A-Za-z_]\w*", source[i:])
        if ident:
            parts.append(SchemaPart("capture", ident.group(0)))
            i += len(ident.group(0))
            continue
        raise ValueError(f"Invalid schema syntax in $pass {pass_name} in {file}: {source[i:i+20]!r}")

    return parts, i


def parse_legacy_schema_branch(source: str, start: int, pass_name: str, file: Path) -> tuple[SchemaPart, int]:
    alternatives = []
    saw_separator = False
    i = start

    while True:
        parts, i = parse_legacy_schema_parts(source, i, True, pass_name, file)
        alternatives.append(parts)
        if i >= len(source):
            raise ValueError(f"Unterminated schema branch in $pass {pass_name} in {file}")
        if source[i] == "|":
            saw_separator = True
            i += 1
            continue
        if source[i] == "]":
            if not saw_separator:
                raise ValueError(f"Schema branch in $pass {pass_name} in {file} must contain '|'")
            return SchemaPart("branch", alternatives=alternatives), i + 1
        raise ValueError(f"Invalid schema branch in $pass {pass_name} in {file}: {source[i:i+20]!r}")


def compact_schema_parts(parts: list[SchemaPart], pass_name: str, file: Path) -> list[SchemaPart]:
    compact: list[SchemaPart] = []
    previous_can_end_with_capture = False

    for part in parts:
        if part.kind == "branch":
            alternatives = [
                compact_schema_parts(alternative, pass_name, file)
                for alternative in part.alternatives or []
            ]
            if not any(alternatives):
                continue
            compact.append(SchemaPart("branch", alternatives=alternatives))
            previous_can_end_with_capture = any(schema_ends_with_capture(alternative) for alternative in alternatives)
            continue

        if part.kind == "literal":
            if compact and compact[-1].kind == "literal":
                compact[-1].value += part.value
            else:
                compact.append(SchemaPart("literal", part.value))
            previous_can_end_with_capture = False
            continue

        if previous_can_end_with_capture:
            raise ValueError(f"$pass {pass_name} schema in {file} has adjacent captures without literal syntax between them")
        compact.append(part)
        previous_can_end_with_capture = True

    return compact


def schema_ends_with_capture(parts: list[SchemaPart]) -> bool:
    for part in reversed(parts):
        if part.kind == "literal" and part.value:
            return False
        if part.kind == "capture":
            return True
        if part.kind == "branch":
            return any(schema_ends_with_capture(alternative) for alternative in part.alternatives or [])
    return False


def first_schema_literal(parts: list[SchemaPart]) -> str:
    for part in parts:
        if part.kind == "literal" and part.value.strip():
            return part.value
        if part.kind == "branch":
            for alternative in part.alternatives or []:
                literal = first_schema_literal(alternative)
                if literal:
                    return literal
    return ""


def parse_schema_string_literal(source: str, start: int) -> tuple[str, int]:
    quote = source[start]
    value = []
    i = start + 1
    escaped = False

    while i < len(source):
        ch = source[i]
        if escaped:
            value.append(unescape_schema_char(ch))
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch == quote:
            return "".join(value), i + 1
        value.append(ch)
        i += 1

    raise ValueError("Unterminated schema string literal")


def unescape_schema_char(ch: str) -> str:
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "\\": "\\",
        "\"": "\"",
        "'": "'",
    }
    return escapes.get(ch, ch)


def schema_starts_with_keyword(literal: str, keyword: str) -> bool:
    stripped = literal.lstrip()
    if not stripped.startswith(keyword):
        return False
    if len(stripped) == len(keyword):
        return True
    return not (stripped[len(keyword)].isalnum() or stripped[len(keyword)] == "_")


def run_init_python(source: str, file: Path) -> dict:
    source = textwrap.dedent(source).strip()
    scope = {"__builtins__": {}}
    try:
        exec(source, scope, scope)
    except Exception as exc:
        raise ValueError(f"Invalid raw python in $pass block in {file}: {exc}") from exc
    return {key: value for key, value in scope.items() if not key.startswith("__")}


def parse_instance_section(instance_body: str) -> list[tuple[str, str]]:
    lines = instance_body.splitlines()
    ops = []
    i = 0

    while i < len(lines):
        m = re.match(r'\s*(\w+)\s*\+=\s*(.*)', lines[i])
        if not m:
            i += 1
            continue

        var = m.group(1)
        rest = m.group(2).strip()

        if rest:
            ops.append((var, rest))
            i += 1
        else:
            block = []
            i += 1
            while i < len(lines) and lines[i].startswith(" "):
                block.append(lines[i])
                i += 1
            ops.append((var, "\n".join(block)))

    return ops


def pass_name(pass_text: str, file: Path) -> str:
    first_line = pass_text.lstrip().splitlines()[0].strip()
    m = re.match(r"pass\s+(\w+)\s*\{?\s*;?\s*$", first_line)
    if not m:
        raise ValueError(f"Expected $pass <name> in {file}")
    return m.group(1)


def unwrap_pass_block(pass_text: str) -> str:
    lines = pass_text.strip().splitlines()
    if lines and lines[0].strip().endswith("{"):
        lines[0] = lines[0].rstrip().removesuffix("{").rstrip()
    if lines and lines[-1].strip() in ("}", "};"):
        lines.pop()
    return "\n".join(lines)


def compile_pass(pass_text: str, file: Path) -> PassDef:
    pass_text = unwrap_pass_block(pass_text)
    name = pass_name(pass_text, file)
    sections = parse_pass_file(pass_text)
    missing = [name for name in ("schema", "instance") if name not in sections]
    if missing:
        raise ValueError(f"$pass {name} is missing section(s): {', '.join(missing)}")

    return PassDef(
        name=name,
        block_keyword=name,
        schema=parse_schema_template(sections["schema"], name, file),
        init_vars=run_init_python(sections.get("python", ""), file),
        instance_ops=parse_instance_section(sections["instance"]),
    )


def marker_positions(source: str) -> list[int]:
    return [m.start() + 1 for m in re.finditer(re.escape(MARKER), source)]


def block_end(text: str, start: int) -> int:
    depth = 0
    saw_open = False
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
            saw_open = True
        elif text[i] == "}":
            depth -= 1
            if saw_open and depth == 0:
                end = i + 1
                while end < len(text) and text[end].isspace() and text[end] != "\n":
                    end += 1
                if end < len(text) and text[end] == ";":
                    end += 1
                return end
    raise ValueError("Unclosed $ block")


def iter_source_files(shared_dir: Path, source_suffixes: tuple[str, ...]) -> list[Path]:
    suffixes = {suffix.lower() for suffix in source_suffixes}
    return [
        file
        for file in sorted(shared_dir.rglob("*"))
        if file.is_file() and file.suffix.lower() in suffixes
    ]


def discover_blocks(
    shared_dir: Path,
    source_suffixes: tuple[str, ...],
) -> tuple[list[MarkerBlock], dict[Path, list[MarkerBlock]]]:
    blocks = []
    strip_blocks: dict[Path, list[MarkerBlock]] = {}

    for file in iter_source_files(shared_dir, source_suffixes):
        source = file.read_text()
        positions = marker_positions(source)
        for index, start in enumerate(positions):
            end = block_end(source, start)
            text = source[start + MARKER_LEN:end]
            block = MarkerBlock(file=file, start=start, end=end, text=text)
            blocks.append(block)
            strip_blocks.setdefault(file, []).append(block)

    return blocks, strip_blocks


def parse_instance(block: MarkerBlock, pass_def: PassDef) -> dict[str, str]:
    return match_schema(block.text.strip(), pass_def.schema, block.file, pass_def.name)


def match_schema(source: str, schema: list[SchemaPart], file: Path, pass_name: str) -> dict[str, str]:
    result = match_schema_nodes(source, schema, 0, 0, {})
    if result is None:
        snippet = source[:40]
        raise ValueError(f"Syntax error in ${pass_name} block in {file}: could not match schema near {snippet!r}")

    pos, values = result
    trailing = source[pos:].strip()
    if trailing:
        raise ValueError(f"Syntax error in ${pass_name} block in {file}: unexpected trailing syntax {trailing!r}")
    return values


def match_schema_nodes(
    source: str,
    schema: list[SchemaPart],
    index: int,
    pos: int,
    values: dict[str, str],
) -> tuple[int, dict[str, str]] | None:
    if index >= len(schema):
        if source[pos:].strip():
            return None
        return len(source), values

    part = schema[index]
    if part.kind == "literal":
        end = match_schema_literal(source, pos, part.value)
        if end is None:
            return None
        return match_schema_nodes(source, schema, index + 1, end, values)

    if part.kind == "branch":
        for alternative in part.alternatives or []:
            matched = match_schema_nodes(source, alternative + schema[index + 1:], 0, pos, values.copy())
            if matched is not None:
                return matched
        return None

    if index == len(schema) - 1:
        captured = source[pos:].strip()
        if not captured:
            return None
        next_values = values.copy()
        next_values[part.value] = captured
        return len(source), next_values

    for capture_end in iter_capture_end_positions(source, pos):
        captured = source[pos:capture_end].strip()
        if not captured:
            continue
        next_values = values.copy()
        next_values[part.value] = captured
        matched = match_schema_nodes(source, schema, index + 1, capture_end, next_values)
        if matched is not None:
            return matched

    return None


def match_schema_literal(source: str, start: int, literal: str) -> int | None:
    i = start
    j = 0

    while j < len(literal):
        if literal[j].isspace():
            while j < len(literal) and literal[j].isspace():
                j += 1
            i = skip_c_whitespace(source, i)
            continue

        if i >= len(source) or source[i] != literal[j]:
            return None
        i += 1
        j += 1

    return i


def skip_c_whitespace(source: str, start: int) -> int:
    i = start

    while i < len(source):
        if source[i].isspace():
            i += 1
            continue

        if source.startswith("//", i):
            i += 2
            while i < len(source) and source[i] != "\n":
                i += 1
            continue

        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end == -1:
                raise ValueError("Unterminated block comment")
            i = end + 2
            continue

        break

    return i


def iter_capture_end_positions(source: str, start: int):
    yield start
    brace_depth = 0
    bracket_depth = 0
    paren_depth = 0
    in_string = False
    in_char = False
    escaped = False
    i = start

    while i < len(source):
        if not in_string and not in_char:
            if source.startswith("//", i):
                i += 2
                while i < len(source) and source[i] != "\n":
                    i += 1
                continue
            if source.startswith("/*", i):
                end = source.find("*/", i + 2)
                if end == -1:
                    raise ValueError("Unterminated block comment")
                i = end + 2
                if brace_depth == 0 and bracket_depth == 0 and paren_depth == 0:
                    yield i
                continue

        ch = source[i]

        if escaped:
            escaped = False
            i += 1
            continue

        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if in_char:
            if ch == "\\":
                escaped = True
            elif ch == "'":
                in_char = False
            i += 1
            continue

        if ch == '"':
            in_string = True
        elif ch == "'":
            in_char = True
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth -= 1
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1

        i += 1
        if not in_string and not in_char and brace_depth == 0 and bracket_depth == 0 and paren_depth == 0:
            yield i


def render_value(expr: str, fields: dict[str, str], counters: dict) -> str:
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"':
        return expr[1:-1]
    if expr in fields:
        return fields[expr]
    if expr in counters:
        return str(counters[expr])
    return ""


def render_expr(expr: str, fields: dict[str, str], counters: dict) -> str:
    expr = expr.strip()

    if expr.startswith("{"):
        end = matching_brace(expr, 0)
        if end == len(expr) - 1:
            return render_block(expr[1:end], fields, counters)
        return ""

    if expr.endswith("++"):
        var = expr[:-2].strip()
        val = counters.get(var, 0)
        counters[var] = val + 1
        return str(val)

    concat = render_implicit_concat(expr, fields, counters)
    if concat is not None:
        return concat

    parts = split_top_level(expr, "+")
    if len(parts) > 1:
        values = [render_concat_atom(part, fields, counters) for part in parts]
        if any(value is None for value in values):
            return ""
        return "".join(values)

    if expr in fields:
        return fields[expr]

    if expr in counters:
        return str(counters[expr])

    return render_value(expr, fields, counters)


def render_block(body: str, fields: dict[str, str], counters: dict) -> str:
    body = body.strip()
    condition = parse_prefix_condition(body)
    if condition is not None:
        f, op, cmp, true_body, false_body = condition
        cond = fields.get(f, "") == cmp
        if op == "!=":
            cond = not cond
        return render_block(true_body if cond else false_body, fields, counters)

    if not body.startswith("return"):
        raise ValueError(f"Expected return statement in expression block: {body!r}")

    expr = body[6:].strip()
    if expr.endswith(";"):
        expr = expr[:-1].strip()
    ternary = parse_ternary(expr)
    if ternary is not None:
        f, op, cmp, true_expr, false_expr = ternary
        cond = fields.get(f, "") == cmp
        if op == "!=":
            cond = not cond
        return render_expr(true_expr if cond else false_expr, fields, counters)
    return render_expr(expr, fields, counters)


def parse_ternary(expr: str) -> tuple[str, str, str, str, str] | None:
    m = re.match(r'(\w+)\s*(==|!=)\s*"([^"]*?)"\s*\?', expr)
    if not m:
        return None

    true_start = m.end()
    colon = find_top_level(expr, ":", true_start)
    if colon is None:
        return None

    return (
        m.group(1),
        m.group(2),
        m.group(3),
        expr[true_start:colon].strip(),
        expr[colon + 1:].strip(),
    )


def parse_prefix_condition(expr: str) -> tuple[str, str, str, str, str] | None:
    m = re.match(r'if\s+(\w+)\s*(==|!=)\s*"([^"]*?)"\s*\{', expr)
    if not m:
        return None

    true_start = m.end()
    true_end = matching_brace(expr, true_start - 1)
    if true_end is None:
        return None

    rest = expr[true_end + 1:].lstrip()
    if not rest.startswith("else"):
        return None
    rest = rest[4:].lstrip()
    if not rest.startswith("{"):
        return None

    false_start_in_rest = 1
    false_end_in_rest = matching_brace(rest, 0)
    if false_end_in_rest is None:
        return None

    if rest[false_end_in_rest + 1:].strip():
        return None

    return (
        m.group(1),
        m.group(2),
        m.group(3),
        expr[true_start:true_end].strip(),
        rest[false_start_in_rest:false_end_in_rest].strip(),
    )


def matching_brace(expr: str, open_index: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False

    for i in range(open_index, len(expr)):
        ch = expr[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i

    return None


def find_top_level(expr: str, needle: str, start: int = 0) -> int | None:
    brace_depth = 0
    paren_depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(expr)):
        ch = expr[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == needle and brace_depth == 0 and paren_depth == 0:
            return i

    return None


def render_implicit_concat(expr: str, fields: dict[str, str], counters: dict) -> str | None:
    pieces = []
    i = 0
    saw_token = False

    while i < len(expr):
        if expr[i].isspace():
            i += 1
            continue

        if expr[i] == '"':
            i += 1
            escaped = False
            value = []
            while i < len(expr):
                ch = expr[i]
                if escaped:
                    value.append("\\" + ch)
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    break
                else:
                    value.append(ch)
                i += 1
            if i >= len(expr) or expr[i] != '"':
                return None
            pieces.append("".join(value))
            i += 1
            saw_token = True
            continue

        m = re.match(r'[A-Za-z_]\w*(?:\+\+)?', expr[i:])
        if not m:
            return None

        token = m.group(0)
        if i + len(token) + 1 < len(expr) and expr[i + len(token):i + len(token) + 2] == "++":
            token += "++"
        if token.endswith("++"):
            pieces.append(render_expr(token, fields, counters))
        else:
            value = render_variable(token, fields, counters)
            if value is None:
                return None
            pieces.append(value)
        i += len(token)
        saw_token = True

    if not saw_token or len(pieces) < 2:
        return None

    return "".join(pieces)


def render_variable(token: str, fields: dict[str, str], counters: dict) -> str | None:
    if token in {"if", "else", "return"}:
        return None
    if token in fields:
        return fields[token]
    if token in counters:
        return str(counters[token])
    return None


def render_concat_atom(expr: str, fields: dict[str, str], counters: dict) -> str | None:
    expr = expr.strip()
    if expr.endswith("++"):
        return render_expr(expr, fields, counters)
    if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"':
        return render_value(expr, fields, counters)
    return render_variable(expr, fields, counters)


def split_top_level(expr: str, separator: str) -> list[str]:
    parts = []
    start = 0
    in_string = False
    escape = False

    for i, ch in enumerate(expr):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string and ch == separator:
            parts.append(expr[start:i].strip())
            start = i + 1

    if not parts:
        return [expr]

    parts.append(expr[start:].strip())
    return parts


def render_template(template: str, fields: dict[str, str], counters: dict) -> str:
    rendered = render_expr(template, fields, counters)
    if rendered:
        return rendered
    raise ValueError(f"Invalid instance expression: {template!r}")


def format_cpp_like(source: str) -> str:
    lines = []
    indent = 0

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue

        closes = line.count("}")
        opens = line.count("{")
        leading_closes = len(re.match(r"^}*", line).group(0))
        if leading_closes:
            indent = max(indent - leading_closes, 0)

        lines.append("  " * indent + line)

        trailing_closes = closes - leading_closes
        indent = max(indent + opens - trailing_closes, 0)

    return "\n".join(lines).rstrip() + "\n"


def render_fragments(pass_def: PassDef, instances: list[dict[str, str]]) -> dict[str, str]:
    import copy

    state = copy.deepcopy(pass_def.init_vars)
    counters = {k: v for k, v in state.items() if isinstance(v, int)}
    accs = {k: v for k, v in state.items() if isinstance(v, list)}

    for fields in instances:
        for var, tmpl in pass_def.instance_ops:
            rendered = render_template(tmpl, fields, counters)
            if var in accs:
                accs[var].append(rendered)
            else:
                counters[var] = rendered

    fragments = {}
    for key, value in accs.items():
        fragments[key] = format_cpp_like("\n".join(value))
    return fragments


def strip_marker_blocks(source: str, blocks: list[MarkerBlock]) -> str:
    parts = []
    cursor = 0
    for block in sorted(blocks, key=lambda item: item.start):
        parts.append(source[cursor:block.start])
        parts.append(block.replacement)
        cursor = block.end
    parts.append(source[cursor:])
    source = "".join(parts)
    source = re.sub(r"\n{3,}", "\n\n", source)
    return source.strip() + "\n"


def write_generated_sources(
    shared_dir: Path,
    strip_map: dict[Path, list[MarkerBlock]],
    output_root: Path,
    source_suffixes: tuple[str, ...],
) -> None:
    for file in iter_source_files(shared_dir, source_suffixes):
        rel = file.relative_to(shared_dir.parent)
        out_path = output_root / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        source = file.read_text()
        if file in strip_map:
            out = strip_marker_blocks(source, strip_map.get(file, []))
        else:
            out = source
        out_path.write_text(out)
        print(f"Written: {out_path}")


def write_syntax_hints(out_path: Path, pass_names: list[str]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pass_macros = "\n".join(f"#define ${name} struct" for name in sorted(pass_names))
    out_path.write_text(f"""#pragma once

// Editor-only helper for source files that contain $ transpiler markers.
// GCC and Clang accept '$' in identifiers, so these macros make marker
// lines look C++-ish to editors while generated files strip them out.
#define $pass struct
{pass_macros}
#define schema() void schema()
#define instance() void instance()

""")
    print(f"Written syntax hints: {out_path}")


def resolve_output_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def parse_target_specs(target_specs: list[str], output_root: Path) -> dict[str, Path]:
    targets: dict[str, Path] = {}
    for spec in target_specs:
        if "=" not in spec:
            raise ValueError(f"Invalid target spec {spec!r}; expected <tag>=<path>")
        tag, value = spec.split("=", 1)
        tag = tag.strip()
        value = value.strip()
        if not tag or not value:
            raise ValueError(f"Invalid target spec {spec!r}; expected <tag>=<path>")
        if tag in targets:
            raise ValueError(f"Duplicate target tag {tag!r}")
        targets[tag] = resolve_output_path(output_root, value)
    return targets


def parse_args(argv: list[str]) -> argparse.Namespace:
    if argv and not argv[0].startswith("-"):
        if len(argv) < 4:
            raise ValueError(
                "Legacy usage: metacodegen.py <shared_dir> <gen_py_dir> <output_root> tag=path ..."
            )
        return argparse.Namespace(
            shared_dir=Path(argv[0]),
            output_root=Path(argv[2]),
            shared_output_root=None,
            syntax_hints=None,
            no_syntax_hints=False,
            source_suffixes=list(DEFAULT_SOURCE_SUFFIXES),
            targets=argv[3:],
        )

    parser = argparse.ArgumentParser(
        description=(
            "Scan shared source files for $ markers, generate target-specific headers, "
            "and emit stripped shared sources for integration into build outputs."
        )
    )
    parser.add_argument("--shared-dir", required=True, type=Path, help="Source directory that contains shared files")
    parser.add_argument(
        "--output-root",
        required=True,
        type=Path,
        help="Base directory used to resolve relative output paths",
    )
    parser.add_argument(
        "--shared-output-root",
        type=Path,
        default=None,
        help="Optional extra directory for stripped shared sources in addition to per-target outputs",
    )
    parser.add_argument(
        "--target",
        dest="targets",
        action="append",
        default=[],
        help="Target output spec in the form <tag>=<stamp-or-output-path>",
    )
    parser.add_argument(
        "--syntax-hints",
        type=Path,
        default=None,
        help="Custom path for syntax_hints.h; defaults to <output-root>/syntax_hints.h",
    )
    parser.add_argument(
        "--no-syntax-hints",
        action="store_true",
        help="Skip writing syntax_hints.h",
    )
    parser.add_argument(
        "--source-suffix",
        dest="source_suffixes",
        action="append",
        default=list(DEFAULT_SOURCE_SUFFIXES),
        help="File suffix to scan and rewrite; can be provided multiple times",
    )
    args = parser.parse_args(argv)
    if not args.targets:
        parser.error("at least one --target <tag>=<path> is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    shared_dir = args.shared_dir
    output_root = args.output_root
    out_map = parse_target_specs(args.targets, output_root)
    source_suffixes = tuple(dict.fromkeys(args.source_suffixes))

    blocks, strip_map = discover_blocks(shared_dir, source_suffixes)
    pass_blocks = [block for block in blocks if block.text.lstrip().startswith("pass")]
    if not pass_blocks:
        raise ValueError(f"No $pass block found under {shared_dir}")

    pass_defs: dict[str, PassDef] = {}
    for block in pass_blocks:
        pass_def = compile_pass(block.text, block.file)
        if pass_def.name in pass_defs:
            raise ValueError(f"Duplicate $pass {pass_def.name}")
        pass_defs[pass_def.name] = pass_def
        block.replacement = ""

    instances_by_pass = {name: [] for name in pass_defs}

    for block in blocks:
        stripped = block.text.lstrip()
        if stripped.startswith("pass"):
            continue

        marker_name = stripped.split(None, 1)[0]
        pass_def = pass_defs.get(marker_name)
        if pass_def is None:
            raise ValueError(f"Unknown ${marker_name} block in {block.file}")
        instances_by_pass[marker_name].append(parse_instance(block, pass_def))

    for tag, stamp_path in out_map.items():
        target_root = stamp_path.parent
        target_root.mkdir(parents=True, exist_ok=True)
        for name, pass_def in pass_defs.items():
            fragments = render_fragments(pass_def, instances_by_pass[name])
            for fragment_name, content in fragments.items():
                out_path = target_root / f"{fragment_name}.h"
                out_path.write_text(content)
                print(f"Written {tag}: {out_path}")
        write_generated_sources(shared_dir, strip_map, target_root, source_suffixes)
        stamp_path.write_text("# Generated by codegen\n")
        print(f"Written {tag}: {stamp_path}")

    if args.shared_output_root is not None:
        write_generated_sources(shared_dir, strip_map, args.shared_output_root, source_suffixes)
    if not args.no_syntax_hints:
        syntax_hints_path = args.syntax_hints or (output_root / "syntax_hints.h")
        write_syntax_hints(syntax_hints_path, list(pass_defs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

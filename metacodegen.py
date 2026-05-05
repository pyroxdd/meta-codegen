"""
Scans shared source files for $ markers, generates shared headers once,
and writes cleaned copies of the shared sources into a shared build folder.
"""

import argparse
import ast
import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
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
    name: str | None
    block_keyword: str
    schema: list["SchemaPart"]
    init_vars: dict
    output_params: list[str]
    instance_targets: list[str]
    instance_ops: list["InstanceOp"]
    is_helper: bool = False
    local_helper_defs: dict[str, "PassDef"] = field(default_factory=dict)


@dataclass
class InstanceOp:
    kind: str
    target: str | None = None
    template: str | None = None
    helper_name: str | None = None
    input_expr: str | None = None
    output_targets: list[str] | None = None
    alias_name: str | None = None
    source_target: str | None = None
    condition_field: str | None = None
    condition_op: str | None = None
    condition_value: str | None = None
    true_ops: list["InstanceOp"] | None = None
    false_ops: list["InstanceOp"] | None = None


@dataclass
class SchemaPart:
    kind: str
    value: str = ""
    alternatives: list[list["SchemaPart"]] | None = None
    capture_name: str | None = None


class SymbolicExpr:
    def __init__(self, expr: str):
        self.expr = expr

    def __str__(self) -> str:
        return self.expr

    def __repr__(self) -> str:
        return self.expr

    def _coerce(self, other) -> str:
        if isinstance(other, SymbolicExpr):
            return other.expr
        return str(other)

    def __add__(self, other):
        return SymbolicExpr(f"({self.expr} + {self._coerce(other)})")

    def __radd__(self, other):
        return SymbolicExpr(f"({self._coerce(other)} + {self.expr})")

    def __sub__(self, other):
        return SymbolicExpr(f"({self.expr} - {self._coerce(other)})")

    def __rsub__(self, other):
        return SymbolicExpr(f"({self._coerce(other)} - {self.expr})")


def parse_pass_file(source: str) -> dict[str, str]:
    legacy_section_match = re.search(r'^[ \t]*(schema|instance)\s*\(\s*\)', source, re.MULTILINE)
    if legacy_section_match is not None:
        section_name = legacy_section_match.group(1)
        raise ValueError(
            f"Deprecated {section_name}() section syntax is no longer supported; "
            f"use `{section_name} {{ ... }}` or the compact `$pass {{ schema }} {{ instance }}` form instead"
        )

    section_re = re.compile(
        r'^[ \t]*(pass|schema|init\s*\(\s*\)|init|instance)',
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
            body = re.sub(r'^[ \t]*pass(?:[ \t]+\w+(?:\([^)]*\))?)?[ \t]*(?:\{[ \t]*\n?)?', '', body, count=1)
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
            i = end + 1
            if i < len(source) and source[i] == "[":
                branch, i = parse_raw_schema_branch(source, i + 1, capture_name=name)
                parts.append(branch)
                continue
            parts.append(SchemaPart("capture", name))
            continue
        literal.append(ch)
        i += 1

    if literal:
        parts.append(SchemaPart("literal", "".join(literal)))
    return parts, i


def parse_raw_schema_branch(source: str, start: int, capture_name: str | None = None) -> tuple[SchemaPart, int]:
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
            return SchemaPart("branch", alternatives=alternatives, capture_name=capture_name), i + 1
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
            name = ident.group(0)
            i += len(name)
            if i < len(source) and source[i] == "[":
                branch, i = parse_legacy_schema_branch(source, i + 1, pass_name, file, capture_name=name)
                parts.append(branch)
                continue
            parts.append(SchemaPart("capture", name))
            continue
        raise ValueError(f"Invalid schema syntax in $pass {pass_name} in {file}: {source[i:i+20]!r}")

    return parts, i


def parse_legacy_schema_branch(
    source: str,
    start: int,
    pass_name: str,
    file: Path,
    capture_name: str | None = None,
) -> tuple[SchemaPart, int]:
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
            return SchemaPart("branch", alternatives=alternatives, capture_name=capture_name), i + 1
        raise ValueError(f"Invalid schema branch in $pass {pass_name} in {file}: {source[i:i+20]!r}")


def compact_schema_parts(parts: list[SchemaPart], pass_name: str, file: Path) -> list[SchemaPart]:
    compact: list[SchemaPart] = []

    for part in parts:
        if part.kind == "branch":
            alternatives = [
                compact_schema_parts(alternative, pass_name, file)
                for alternative in part.alternatives or []
            ]
            if not any(alternatives):
                continue
            compact.append(SchemaPart("branch", alternatives=alternatives, capture_name=part.capture_name))
            continue

        if part.kind == "literal":
            if compact and compact[-1].kind == "literal":
                compact[-1].value += part.value
            else:
                compact.append(SchemaPart("literal", part.value))
            continue

        compact.append(part)

    return compact


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
    safe_builtins = {
        "__import__": __import__,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "range": range,
        "set": set,
        "str": str,
        "tuple": tuple,
        "zip": zip,
    }
    scope = {"__builtins__": safe_builtins}
    import_paths = []
    for candidate in (file.parent.resolve(), Path.cwd().resolve()):
        if candidate not in import_paths:
            import_paths.append(candidate)
    previous_sys_path = list(sys.path)
    for path in reversed(import_paths):
        sys.path.insert(0, str(path))
    try:
        exec(source, scope, scope)
    except Exception as exc:
        raise ValueError(f"Invalid raw python in $pass block in {file}: {exc}") from exc
    finally:
        sys.path[:] = previous_sys_path
    return {key: value for key, value in scope.items() if not key.startswith("__")}


def parse_instance_statement(text: str, block_lines: list[str] | None = None) -> InstanceOp:
    decl_match = re.match(r'\s*var\s+([A-Za-z_]\w*)\s*$', text)
    if decl_match:
        return InstanceOp(kind="var", alias_name=decl_match.group(1))

    assign_match = re.match(r'\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*$', text)
    if assign_match:
        return InstanceOp(
            kind="assign",
            alias_name=assign_match.group(1),
            source_target=assign_match.group(2),
        )

    emit_match = re.match(r'\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\+=\s*(.*)', text)
    if emit_match:
        target = emit_match.group(1)
        rest = emit_match.group(2).strip()
        if rest:
            return InstanceOp(kind="emit", target=target, template=rest)
        if block_lines is None:
            raise ValueError(f"Expected indented block after emit target {target!r}")
        return InstanceOp(kind="emit", target=target, template="\n".join(block_lines))

    call_match = re.match(r'\s*([A-Za-z_]\w*)\s*\[(.+)\]\s*\((.*)\)\s*$', text)
    if call_match:
        helper_name = call_match.group(1)
        input_expr = call_match.group(2).strip()
        output_targets = [part.strip() for part in split_top_level(call_match.group(3), ",") if part.strip()]
        return InstanceOp(
            kind="call",
            helper_name=helper_name,
            input_expr=input_expr,
            output_targets=output_targets,
        )

    raise ValueError(f"Unsupported instance statement: {text.strip()!r}")


def parse_instance_section(instance_body: str) -> list[InstanceOp]:
    lines = instance_body.splitlines()

    def parse_if_statement(line_text: str, line_index: int) -> tuple[InstanceOp, int]:
        stripped = line_text.strip()
        if_match = re.match(r'\s*if\s+(\w+)\s*(==|!=)\s*"([^"]*?)"\s*(.*)$', line_text)
        if if_match is None:
            raise ValueError(f"Unsupported if statement: {stripped!r}")

        field_name = if_match.group(1)
        op = if_match.group(2)
        cmp_value = if_match.group(3)
        rest = if_match.group(4).strip()

        if rest == "{":
            true_ops, next_i = parse_ops(line_index + 1, stop_on_else=True)
        elif rest:
            true_ops = [parse_instance_statement(rest)]
            next_i = line_index + 1
        else:
            raise ValueError(f"Unsupported if statement: {stripped!r}")

        false_ops: list[InstanceOp] = []
        if next_i < len(lines):
            else_stripped = lines[next_i].strip()
            else_match = re.match(r'^else\s*(.*)$', else_stripped)
            if else_match:
                else_rest = else_match.group(1).strip()
                if else_rest == "{":
                    false_ops, next_i = parse_ops(next_i + 1)
                elif else_rest.startswith("if "):
                    nested_if, next_i = parse_if_statement(else_rest, next_i)
                    false_ops = [nested_if]
                elif else_rest:
                    false_ops = [parse_instance_statement(else_rest)]
                    next_i += 1
                else:
                    raise ValueError(f"Unsupported else statement: {else_stripped!r}")

        return InstanceOp(
            kind="if",
            condition_field=field_name,
            condition_op=op,
            condition_value=cmp_value,
            true_ops=true_ops,
            false_ops=false_ops,
        ), next_i

    def parse_ops(start: int, stop_on_else: bool = False) -> tuple[list[InstanceOp], int]:
        ops: list[InstanceOp] = []
        i = start

        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue
            if stripped == "}":
                return ops, i + 1
            if stop_on_else and (stripped == "else" or stripped.startswith("else ") or stripped.startswith("else{")):
                return ops, i

            if re.match(r'\s*if\s+', lines[i]):
                if_op, next_i = parse_if_statement(lines[i], i)
                ops.append(if_op)
                i = next_i
                continue

            if stripped.startswith("else"):
                raise ValueError("Unexpected else without matching if")

            emit_match = re.match(r'\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\+=\s*(.*)', lines[i])
            if emit_match and not emit_match.group(2).strip():
                block = []
                i += 1
                while i < len(lines) and lines[i].startswith(" "):
                    block.append(lines[i])
                    i += 1
                ops.append(parse_instance_statement(lines[i - len(block) - 1], block))
                continue

            ops.append(parse_instance_statement(lines[i]))
            i += 1

        return ops, i

    ops, end = parse_ops(0)
    if end != len(lines):
        trailing = next((line.strip() for line in lines[end:] if line.strip()), "")
        if trailing:
            raise ValueError(f"Unsupported trailing instance content: {trailing!r}")
    return ops


def iter_instance_ops(ops: list[InstanceOp]):
    for op in ops:
        yield op
        if op.kind == "if":
            yield from iter_instance_ops(op.true_ops or [])
            yield from iter_instance_ops(op.false_ops or [])


def declared_instance_aliases(ops: list[InstanceOp]) -> set[str]:
    return {
        op.alias_name
        for op in iter_instance_ops(ops)
        if op.kind in {"var", "assign"} and op.alias_name is not None
    }


def parse_named_block_header(header: str, file: Path, keyword: str) -> tuple[str | None, list[str]]:
    m = re.match(rf"{re.escape(keyword)}(?:[ \t]+(\w+)(?:\(([^)]*)\))?)?\s*\{{?\s*;?\s*$", header)
    if not m:
        raise ValueError(f"Expected {keyword} or {keyword} <name>(...) in {file}")
    name = m.group(1)
    output_params = []
    if m.group(2):
        output_params = [part.strip() for part in m.group(2).split(",") if part.strip()]
    return name, output_params


def parse_pass_header(header: str, file: Path) -> tuple[list[str], bool]:
    m = re.match(r"pass(?:[ \t]+(\w+)(?:\(([^)]*)\))?)?\s*\{?\s*;?\s*$", header)
    if not m:
        raise ValueError(f"Expected pass in {file}")
    if m.group(1) is not None:
        raise ValueError(f"Top-level pass in {file} cannot be named; use nested rule <name>(...) for helpers")
    output_params = []
    if m.group(2):
        output_params = [part.strip() for part in m.group(2).split(",") if part.strip()]
    return output_params, m.group(2) is not None


def unwrap_pass_block(pass_text: str) -> str:
    lines = pass_text.strip().splitlines()
    if lines and lines[0].strip().endswith("{"):
        lines[0] = lines[0].rstrip().removesuffix("{").rstrip()
    if lines and lines[-1].strip() in ("}", "};"):
        lines.pop()
    return "\n".join(lines)


def find_top_level_named_blocks(source: str, keyword: str) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    for match in re.finditer(rf"(?m)^[ \t]*{re.escape(keyword)}\b", source):
        start = match.start()
        if positions and start < positions[-1][1]:
            continue
        positions.append((start, block_end(source, start)))
    return positions


def extract_top_level_named_blocks(source: str, keyword: str) -> tuple[str, list[str]]:
    blocks = find_top_level_named_blocks(source, keyword)
    if not blocks:
        return source, []

    parts = []
    extracted = []
    cursor = 0
    for start, end in blocks:
        parts.append(source[cursor:start])
        extracted.append(source[start:end])
        cursor = end
    parts.append(source[cursor:])
    return "".join(parts), extracted


def parse_compact_pass_sections(pass_text: str, file: Path) -> tuple[str, str]:
    stripped = pass_text.strip()
    header_match = re.match(r"pass\s*\{", stripped)
    if header_match is None:
        raise ValueError(f"Expected compact pass syntax in {file}")

    parse_pass_header(stripped[:header_match.end() - 1].strip(), file)
    schema_open = stripped.find("{", header_match.start(), header_match.end())
    schema_close = matching_brace(stripped, schema_open)
    if schema_close is None:
        raise ValueError(f"Compact pass in {file} has an unterminated schema block")
    schema_body = stripped[schema_open + 1:schema_close].strip()

    instance_open = skip_c_whitespace(stripped, schema_close + 1)
    if instance_open >= len(stripped) or stripped[instance_open] != "{":
        raise ValueError(f"Compact pass in {file} is missing instance block")
    instance_close = matching_brace(stripped, instance_open)
    if instance_close is None:
        raise ValueError(f"Compact pass in {file} has an unterminated instance block")
    instance_body = stripped[instance_open + 1:instance_close].strip()

    trailing = stripped[instance_close + 1:].strip()
    if trailing not in ("", ";"):
        raise ValueError(f"Unexpected trailing pass syntax in {file}: {trailing!r}")

    return schema_body, instance_body


def normalize_compact_python(python_text: str) -> str:
    lines = python_text.splitlines()
    if not lines:
        return ""

    normalized = [lines[0].lstrip()]
    indents = [
        len(line) - len(line.lstrip())
        for line in lines[1:]
        if line.strip()
    ]
    trim = min(indents) if indents else 0
    for line in lines[1:]:
        if trim and len(line) >= trim:
            normalized.append(line[trim:])
        else:
            normalized.append(line)
    return "\n".join(normalized).strip()


def split_compact_schema_block(block_body: str, pass_name: str | None, file: Path) -> tuple[str, str]:
    lines = block_body.splitlines()
    if not lines:
        raise ValueError(f"Compact pass in {file} has an empty schema block")

    candidates = []
    for split_index in range(len(lines) + 1):
        python_text = "\n".join(lines[:split_index]).strip()
        schema_text = "\n".join(lines[split_index:]).strip()
        if not schema_text:
            continue

        try:
            parse_schema_template(schema_text, pass_name, file)
        except ValueError:
            continue

        if python_text:
            try:
                compile(normalize_compact_python(python_text), str(file), "exec")
            except SyntaxError:
                continue

        candidates.append((normalize_compact_python(python_text), schema_text))

    if not candidates:
        raise ValueError(f"Compact pass in {file} does not contain a valid schema block")

    return candidates[-1]


def compile_rule(rule_text: str, file: Path) -> PassDef:
    stripped_rule = rule_text.strip()
    compact_match = re.match(r"rule(?:[ \t]+\w+(?:\([^)]*\))?)?\s*\{", stripped_rule)
    if compact_match is not None and "schema" not in stripped_rule and "instance" not in stripped_rule:
        name, output_params = parse_named_block_header(stripped_rule[:compact_match.end() - 1].strip(), file, "rule")
        schema_open = stripped_rule.find("{", compact_match.start(), compact_match.end())
        schema_close = matching_brace(stripped_rule, schema_open)
        if schema_close is None:
            raise ValueError(f"Compact rule in {file} has an unterminated schema block")
        first_block_body = stripped_rule[schema_open + 1:schema_close].strip()

        instance_open = skip_c_whitespace(stripped_rule, schema_close + 1)
        if instance_open >= len(stripped_rule) or stripped_rule[instance_open] != "{":
            raise ValueError(f"Compact rule in {file} is missing instance block")
        instance_close = matching_brace(stripped_rule, instance_open)
        if instance_close is None:
            raise ValueError(f"Compact rule in {file} has an unterminated instance block")
        instance_body = stripped_rule[instance_open + 1:instance_close].strip()

        trailing = stripped_rule[instance_close + 1:].strip()
        if trailing not in ("", ";"):
            raise ValueError(f"Unexpected trailing rule syntax in {file}: {trailing!r}")
        raw_python, schema_body = split_compact_schema_block(first_block_body, name, file)
        sections = {"python": raw_python, "schema": schema_body, "instance": instance_body}
    else:
        unwrapped = unwrap_pass_block(re.sub(r"^\s*rule\b", "pass", stripped_rule, count=1))
        lines = unwrapped.lstrip().splitlines()
        first_line = lines[0].strip()
        name, output_params = parse_named_block_header(first_line.replace("pass", "rule", 1), file, "rule")
        rebuilt_rule_text = first_line
        body_text = "\n".join(lines[1:])
        if body_text:
            rebuilt_rule_text += "\n" + body_text
        sections = parse_pass_file(rebuilt_rule_text)
        missing = [section_name for section_name in ("schema", "instance") if section_name not in sections]
        if missing:
            raise ValueError(f"rule {name or '<unnamed>'} is missing section(s): {', '.join(missing)}")
        raw_python = sections.get("python", "")

    if name is None:
        raise ValueError(f"rule in {file} must declare a name")

    instance_ops = parse_instance_section(sections["instance"])
    declared_aliases = declared_instance_aliases(instance_ops)
    if not output_params:
        raise ValueError(f"rule {name} must declare at least one output parameter; implicit return output is not supported")
    invalid_targets = sorted({
        op.target for op in iter_instance_ops(instance_ops)
        if op.kind == "emit" and op.target not in output_params and op.target not in declared_aliases
    })
    if invalid_targets:
        raise ValueError(f"rule {name} may only write to declared outputs {output_params}, found: {', '.join(invalid_targets)}")
    for op in iter_instance_ops(instance_ops):
        if op.kind == "assign":
            if op.source_target is None:
                continue
            if op.source_target not in output_params and op.source_target not in declared_aliases:
                raise ValueError(
                    f"rule {name} may only bind variables to declared outputs {output_params} or other variables, found: {op.source_target}"
                )

    return PassDef(
        name=name,
        block_keyword=name,
        schema=parse_schema_template(sections["schema"], name, file),
        init_vars=run_init_python(raw_python, file),
        output_params=output_params,
        instance_targets=[],
        instance_ops=instance_ops,
        is_helper=True,
    )


def compile_pass(pass_text: str, file: Path) -> PassDef:
    stripped_pass = pass_text.strip()
    compact_match = re.match(r"pass\s*\{", stripped_pass)
    local_helper_defs: dict[str, PassDef] = {}
    if compact_match is not None and "schema" not in stripped_pass and "instance" not in stripped_pass:
        first_block_body, instance_body = parse_compact_pass_sections(stripped_pass, file)
        output_params, _ = parse_pass_header(stripped_pass[:compact_match.end() - 1].strip(), file)
        raw_python, schema_body = split_compact_schema_block(first_block_body, None, file)
        sections = {"python": raw_python, "schema": schema_body, "instance": instance_body}
    else:
        pass_text = unwrap_pass_block(pass_text)
        lines = pass_text.lstrip().splitlines()
        first_line = lines[0].strip()
        output_params, has_outputs = parse_pass_header(first_line, file)
        if has_outputs:
            raise ValueError(f"Top-level pass in {file} cannot declare outputs; use nested rule <name>(...) for helpers")
        body_text = "\n".join(lines[1:])
        body_text, rule_texts = extract_top_level_named_blocks(body_text, "rule")
        for rule_text in rule_texts:
            rule_def = compile_rule(rule_text, file)
            if rule_def.name in local_helper_defs:
                raise ValueError(f"Duplicate rule {rule_def.name} in {file}")
            local_helper_defs[rule_def.name] = rule_def
        rebuilt_pass_text = first_line
        if body_text:
            rebuilt_pass_text += "\n" + body_text
        sections = parse_pass_file(rebuilt_pass_text)
        missing = [section_name for section_name in ("schema", "instance") if section_name not in sections]
        if missing:
            raise ValueError(f"Top-level $pass in {file} is missing section(s): {', '.join(missing)}")
        raw_python = sections.get("python", "")

    instance_ops = parse_instance_section(sections["instance"])
    declared_aliases = declared_instance_aliases(instance_ops)
    invalid_emit_targets = sorted({
        op.target for op in iter_instance_ops(instance_ops)
        if op.kind == "emit" and op.target is not None and not target_is_allowed(op.target, declared_aliases)
    })
    if invalid_emit_targets:
        raise ValueError(
            f"Top-level $pass in {file} must write to outputs using 'out.<name> += ...' or a declared variable, found: {', '.join(invalid_emit_targets)}"
        )
    invalid_assignments = []
    for op in iter_instance_ops(instance_ops):
        if op.kind != "assign":
            continue
        if op.source_target is None:
            continue
        if not target_is_allowed(op.source_target, declared_aliases):
            invalid_assignments.append(op.source_target)
    if invalid_assignments:
        raise ValueError(
            f"Top-level $pass in {file} may only bind variables to 'out.<name>' sinks or other declared variables, found: {', '.join(invalid_assignments)}"
        )
    invalid_call_targets = sorted({
        target
        for op in iter_instance_ops(instance_ops)
        if op.kind == "call" and op.output_targets is not None
        for target in op.output_targets
        if not target_is_allowed(target, declared_aliases)
    })
    if invalid_call_targets:
        raise ValueError(
            f"Top-level $pass in {file} must pass outputs as 'out.<name>' or declared variables when calling named rules, found: {', '.join(invalid_call_targets)}"
        )
    return PassDef(
        name=None,
        block_keyword="__top_level__",
        schema=parse_schema_template(sections["schema"], None, file),
        init_vars=run_init_python(raw_python, file),
        output_params=output_params,
        instance_targets=list(dict.fromkeys(
            normalize_output_target(op.target)
            for op in iter_instance_ops(instance_ops)
            if op.kind == "emit" and op.target is not None and op.target not in declared_aliases
        )),
        instance_ops=instance_ops,
        is_helper=False,
        local_helper_defs=local_helper_defs,
    )


def marker_positions(source: str) -> list[int]:
    return [m.start() + 1 for m in re.finditer(re.escape(MARKER), source)]


def block_end(text: str, start: int) -> int:
    depth = 0
    saw_open = False
    in_string = False
    string_quote = ""
    escape = False
    in_line_comment = False
    in_block_comment = False

    for i in range(start, len(text)):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue

        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == string_quote:
                in_string = False
                string_quote = ""
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue

        if ch in ("'", '"'):
            in_string = True
            string_quote = ch
            escape = False
            continue

        if ch == "{":
            depth += 1
            saw_open = True
        elif ch == "}":
            depth -= 1
            if saw_open and depth == 0:
                end = i + 1
                while True:
                    probe = end
                    while probe < len(text) and text[probe].isspace():
                        probe += 1
                    if probe < len(text) and text[probe] == "{":
                        sibling_end = block_end(text, probe)
                        end = sibling_end
                        continue
                    break
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


def identify_pass(block: MarkerBlock, pass_defs: dict[str, PassDef]) -> tuple[str, dict[str, str]]:
    matches: list[tuple[str, dict[str, str]]] = []
    for name, pass_def in pass_defs.items():
        try:
            values = parse_instance(block, pass_def)
        except ValueError:
            continue
        matches.append((name, values))

    if not matches:
        snippet = block.text.strip()[:40]
        raise ValueError(f"Unknown $ block in {block.file}: could not match schema near {snippet!r}")

    if len(matches) > 1:
        names = ", ".join(name for name, _ in matches)
        snippet = block.text.strip()[:40]
        raise ValueError(f"Ambiguous $ block in {block.file}: matched [{names}] near {snippet!r}")

    return matches[0]


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
    allow_trailing: bool = False,
) -> tuple[int, dict[str, str]] | None:
    if index >= len(schema):
        if not allow_trailing and source[pos:].strip():
            return None
        return pos, values

    part = schema[index]
    if part.kind == "literal":
        end = match_schema_literal(source, pos, part.value)
        if end is None:
            return None
        return match_schema_nodes(source, schema, index + 1, end, values, allow_trailing)

    if part.kind == "branch":
        for alternative in part.alternatives or []:
            matched_alternative = match_schema_nodes(source, alternative, 0, pos, values.copy(), allow_trailing=True)
            if matched_alternative is None:
                continue
            alternative_end, alternative_values = matched_alternative
            if part.capture_name:
                alternative_values[part.capture_name] = source[pos:alternative_end].strip()
            matched = match_schema_nodes(source, schema, index + 1, alternative_end, alternative_values, allow_trailing)
            if matched is not None:
                return matched
        return None

    for capture_end in iter_capture_end_positions(source, pos):
        captured = source[pos:capture_end].strip()
        next_values = values.copy()
        next_values[part.value] = captured
        matched = match_schema_nodes(source, schema, index + 1, capture_end, next_values, allow_trailing)
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


def render_value(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str:
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] in "\"'" and expr[-1] == expr[0]:
        try:
            return str(ast.literal_eval(expr))
        except Exception:
            return expr[1:-1]
    if expr in fields:
        return fields[expr]
    if expr in counters:
        return str(counters[expr])
    return ""


def default_pass_output_name(pass_def: PassDef, fallback: str) -> str:
    if pass_def.name:
        return pass_def.name
    if pass_def.instance_targets:
        tokenized = [target.split("_") for target in pass_def.instance_targets]
        prefix: list[str] = []
        for parts in zip(*tokenized):
            if len(set(parts)) != 1:
                break
            prefix.append(parts[0])
        if prefix:
            return "_".join(prefix)
    literal = first_schema_literal(pass_def.schema).strip()
    if literal:
        keyword = re.sub(r"<[^>]*>", " ", literal)
        keyword = re.sub(r'["\'{}\[\]();,]+', " ", keyword)
        keyword = re.sub(r"\s+", "_", keyword.strip())
        keyword = keyword.strip("_")
        if keyword:
            return keyword
    return fallback.replace(":", "_")


def sanitize_path_token(path: Path | str) -> str:
    text = str(path)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower() or "pass"


def normalize_output_target(target: str) -> str:
    return target[4:] if target.startswith("out.") else target


def collect_declared_output_targets(instance_ops: list[InstanceOp]) -> list[str]:
    targets: list[str] = []
    for op in iter_instance_ops(instance_ops):
        if op.kind == "emit" and op.target is not None and op.target.startswith("out."):
            targets.append(normalize_output_target(op.target))
        elif op.kind == "assign" and op.source_target is not None and op.source_target.startswith("out."):
            targets.append(normalize_output_target(op.source_target))
        elif op.kind == "call" and op.output_targets is not None:
            for target in op.output_targets:
                if target.startswith("out."):
                    targets.append(normalize_output_target(target))
    return list(dict.fromkeys(targets))


def target_is_allowed(target: str, declared_aliases: set[str]) -> bool:
    return target.startswith("out.") or target in declared_aliases


def resolve_output_sink(
    target: str,
    local_output_bindings: dict[str, list[str]],
    global_accs: dict[str, list[str]],
) -> list[str]:
    if target in local_output_bindings:
        return local_output_bindings[target]
    if target.startswith("out."):
        normalized = normalize_output_target(target)
        return global_accs.setdefault(normalized, [])
    raise ValueError(
        f"Unknown output target {target!r}; use a declared named-pass output parameter or an 'out.<name>' global output"
    )


def render_python_expr(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str | None:
    def post_inc(name: str):
        value = counters.get(name, 0)
        counters[name] = value + 1
        return value

    translated = re.sub(r"\b([A-Za-z_]\w*)\+\+", lambda m: f'__post_inc__("{m.group(1)}")', expr)
    scope = {}
    scope.update(counters)
    scope.update(fields)
    scope.update(helper_functions)
    scope["__post_inc__"] = post_inc
    try:
        value = eval(translated, {"__builtins__": {}}, scope)
    except Exception:
        return None
    if value is None:
        return ""
    return str(value)


def render_expr(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str:
    expr = expr.strip()

    if expr.startswith("{"):
        end = matching_brace(expr, 0)
        if end == len(expr) - 1:
            return render_block(expr[1:end], fields, counters, helper_functions)
        return ""

    if expr.endswith("++"):
        var = expr[:-2].strip()
        val = counters.get(var, 0)
        counters[var] = val + 1
        return str(val)

    concat = render_implicit_concat(expr, fields, counters, helper_functions)
    if concat is not None:
        return concat

    python_value = render_python_expr(expr, fields, counters, helper_functions)
    if python_value is not None:
        return python_value

    parts = split_top_level(expr, "+")
    if len(parts) > 1:
        values = [render_concat_atom(part, fields, counters, helper_functions) for part in parts]
        if any(value is None for value in values):
            return ""
        return "".join(values)

    if expr in fields:
        return fields[expr]

    if expr in counters:
        return str(counters[expr])

    return render_value(expr, fields, counters, helper_functions)


def render_block(body: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str:
    body = body.strip()
    condition = parse_prefix_condition(body)
    if condition is not None:
        f, op, cmp, true_body, false_body = condition
        cond = fields.get(f, "") == cmp
        if op == "!=":
            cond = not cond
        return render_block(true_body if cond else false_body, fields, counters, helper_functions)

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
        return render_expr(true_expr if cond else false_expr, fields, counters, helper_functions)
    return render_expr(expr, fields, counters, helper_functions)


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


def render_implicit_concat(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str | None:
    pieces = []
    i = 0
    saw_token = False

    while i < len(expr):
        if expr[i].isspace():
            i += 1
            continue

        if expr[i] == '"':
            literal_start = i
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
            literal = expr[literal_start:i + 1]
            try:
                pieces.append(str(ast.literal_eval(literal)))
            except Exception:
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
            pieces.append(render_expr(token, fields, counters, helper_functions))
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


def render_concat_atom(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str | None:
    expr = expr.strip()
    if expr.endswith("++"):
        return render_expr(expr, fields, counters, helper_functions)
    if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"':
        return render_value(expr, fields, counters, helper_functions)
    value = render_variable(expr, fields, counters)
    if value is not None:
        return value
    return render_python_expr(expr, fields, counters, helper_functions)


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


def render_template(template: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str:
    rendered = render_expr(template, fields, counters, helper_functions)
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


def execute_instance_ops(
    pass_def: PassDef,
    fields: dict[str, str],
    counters: dict,
    helper_defs: dict[str, PassDef],
    local_output_bindings: dict[str, list[str]],
    global_accs: dict[str, list[str]],
) -> None:
    helper_functions: dict[str, object] = {}
    for op in pass_def.instance_ops:
        if op.kind == "var":
            continue

        if op.kind == "assign":
            if op.alias_name is None or op.source_target is None:
                continue
            local_output_bindings[op.alias_name] = resolve_output_sink(op.source_target, local_output_bindings, global_accs)
            continue

        if op.kind == "emit":
            if op.target is None or op.template is None:
                continue
            rendered = render_template(op.template, fields, counters, helper_functions)
            sink = resolve_output_sink(op.target, local_output_bindings, global_accs)
            sink.append(rendered)
            continue

        if op.kind == "call":
            if op.helper_name is None or op.input_expr is None or op.output_targets is None:
                continue
            helper_def = helper_defs.get(op.helper_name)
            if helper_def is None:
                raise ValueError(f"Unknown helper pass {op.helper_name!r}")
            if len(op.output_targets) != len(helper_def.output_params):
                raise ValueError(
                    f"Helper pass {op.helper_name} expects {len(helper_def.output_params)} outputs, got {len(op.output_targets)}"
                )
            input_text = render_template(op.input_expr, fields, counters, helper_functions)
            bound_outputs = {
                param: resolve_output_sink(target, local_output_bindings, global_accs)
                for param, target in zip(helper_def.output_params, op.output_targets)
            }
            execute_named_pass(helper_def, input_text, fields, counters, helper_defs, bound_outputs, global_accs)
            continue

        if op.kind == "if":
            if op.condition_field is None or op.condition_op is None or op.condition_value is None:
                continue
            matches = fields.get(op.condition_field, "") == op.condition_value
            if op.condition_op == "!=":
                matches = not matches
            branch_ops = op.true_ops if matches else op.false_ops
            if branch_ops:
                nested_pass = PassDef(
                    name=pass_def.name,
                    block_keyword=pass_def.block_keyword,
                    schema=pass_def.schema,
                    init_vars=pass_def.init_vars,
                    output_params=pass_def.output_params,
                    instance_targets=pass_def.instance_targets,
                    instance_ops=branch_ops,
                    is_helper=pass_def.is_helper,
                    local_helper_defs=pass_def.local_helper_defs,
                )
                execute_instance_ops(nested_pass, fields, counters, helper_defs, local_output_bindings, global_accs)
            continue

        raise ValueError(f"Unsupported instance op kind {op.kind!r}")


def execute_named_pass(
    pass_def: PassDef,
    input_text: str,
    outer_fields: dict[str, str],
    outer_counters: dict,
    helper_defs: dict[str, PassDef],
    output_bindings: dict[str, list[str]],
    global_accs: dict[str, list[str]],
) -> None:
    import copy

    state = {}
    for key, value in pass_def.init_vars.items():
        if isinstance(value, list):
            continue
        if isinstance(value, (dict, set, tuple)):
            state[key] = copy.deepcopy(value)
        else:
            state[key] = value

    pos = skip_c_whitespace(input_text, 0)
    local_index = 0

    while pos < len(input_text):
        matched = match_schema_nodes(input_text, pass_def.schema, 0, pos, outer_fields.copy(), allow_trailing=True)
        if matched is None:
            snippet = input_text[pos:pos + 40]
            raise ValueError(f"Helper pass {pass_def.name} could not match near {snippet!r}")

        end, fields = matched
        if end <= pos:
            raise ValueError(f"Helper pass {pass_def.name} made no progress")

        counters = copy.deepcopy(state)
        counters.update(outer_counters)
        counters["index"] = local_index
        execute_instance_ops(pass_def, fields, counters, helper_defs, output_bindings, global_accs)

        pos = skip_c_whitespace(input_text, end)
        local_index += 1


def render_fragments(
    pass_def: PassDef,
    instances: list[dict[str, str]],
    helper_defs: dict[str, PassDef],
    index_base_expr: str | None = None,
) -> dict[str, str]:
    import copy

    state = {}
    for key, value in pass_def.init_vars.items():
        if isinstance(value, list):
            continue
        if isinstance(value, (dict, set, tuple)):
            state[key] = copy.deepcopy(value)
        else:
            state[key] = value
    counters = state
    accs = {key: [] for key in pass_def.instance_targets}
    for key, value in pass_def.init_vars.items():
        if isinstance(value, list):
            accs.setdefault(key, []).extend(copy.deepcopy(value))

    for index, fields in enumerate(instances):
        counters["local_index"] = index
        if index_base_expr is None:
            counters["index"] = index
        elif index == 0:
            counters["index"] = SymbolicExpr(index_base_expr)
        else:
            counters["index"] = SymbolicExpr(f"({index_base_expr} + {index})")
        execute_instance_ops(pass_def, fields, counters, helper_defs, {}, accs)

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
        if write_text_if_changed(out_path, out):
            print(f"Written: {out_path}")


def write_text_if_changed(out_path: Path, content: str) -> bool:
    if out_path.exists():
        existing = out_path.read_text()
        if existing == content:
            return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content)
    return True


def delete_file_if_exists(path: Path) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False


def compile_pass_inventory(
    passes_dir: Path,
    generated_header_root: str,
    generated_header_prefix: str,
    source_suffixes: tuple[str, ...],
) -> list[dict]:
    blocks, _ = discover_blocks(passes_dir, source_suffixes)
    pass_blocks = [block for block in blocks if block.text.lstrip().startswith("pass")]
    if not pass_blocks:
        raise ValueError(f"No $pass block found under {passes_dir}")

    pass_blocks_by_file: dict[Path, list[MarkerBlock]] = {}
    for block in pass_blocks:
        pass_blocks_by_file.setdefault(block.file, []).append(block)
    duplicate_files = [file for file, file_blocks in pass_blocks_by_file.items() if len(file_blocks) > 1]
    if duplicate_files:
        duplicate_list = ", ".join(str(file.relative_to(passes_dir)) for file in sorted(duplicate_files))
        raise ValueError(f"Only one $pass block is allowed per file; split these files: {duplicate_list}")

    inventory: list[dict] = []
    for block in pass_blocks:
        pass_def = compile_pass(block.text, block.file)
        rel_file = block.file.relative_to(passes_dir)
        pass_name = sanitize_path_token(rel_file.stem)
        pass_id = pass_name
        outputs = []
        for fragment_name in collect_declared_output_targets(pass_def.instance_ops):
            rel_output = Path(f"{generated_header_prefix}{fragment_name}.h")
            if generated_header_root:
                rel_output = Path(generated_header_root) / rel_output
            outputs.append(rel_output.as_posix())

        inventory.append({
            "id": pass_id,
            "defined_in": rel_file.as_posix(),
            "source_file": str(block.file),
            "block_index_in_file": 0,
            "folder": pass_name,
            "outputs": outputs,
            "pass_text": block.text.strip(),
            "rule_count": len(pass_def.local_helper_defs),
        })

    return inventory


def write_pass_descriptor(out_path: Path, entry: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing_counts: dict[str, int] = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            raw_counts = existing.get("counts", {})
            if isinstance(raw_counts, dict):
                existing_counts = {str(key): int(value) for key, value in raw_counts.items()}
        except Exception:
            existing_counts = {}
    descriptor = {
        "id": entry["id"],
        "defined_in": entry["defined_in"],
        "source_file": entry["source_file"],
        "block_index_in_file": entry["block_index_in_file"],
        "folder": entry["folder"],
        "outputs": entry["outputs"],
        "pass_text": entry["pass_text"],
        "rule_count": entry["rule_count"],
        "counts": existing_counts,
    }
    if write_text_if_changed(out_path, json.dumps(descriptor, indent=2) + "\n"):
        print(f"Written: {out_path}")


def remove_stale_pass_artifacts(build_root: Path, active_ids: set[str]) -> None:
    build_root.mkdir(parents=True, exist_ok=True)
    active_names = {f"pass_{pass_id}.json" for pass_id in active_ids}
    for path in build_root.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith("pass_") and path.suffix == ".py":
            path.unlink()
            print(f"Removed legacy pass artifact: {path}")
            continue
        if path.name.startswith("pass_") and path.suffix == ".json" and path.name not in active_names:
            path.unlink()
            print(f"Removed stale pass artifact: {path}")


def discover_blocks_in_file(file: Path) -> tuple[list[MarkerBlock], list[MarkerBlock], str]:
    source = file.read_text()
    blocks: list[MarkerBlock] = []
    positions = marker_positions(source)
    for start in positions:
        end = block_end(source, start)
        text = source[start + MARKER_LEN:end]
        blocks.append(MarkerBlock(file=file, start=start, end=end, text=text))
    return blocks, blocks.copy(), source


def iter_pass_descriptor_paths(build_root: Path) -> list[Path]:
    return sorted(build_root.glob("pass_*.json"))


def load_pass_defs_from_build_root(build_root: Path) -> list[tuple[dict, PassDef]]:
    entries = []
    for descriptor_path in iter_pass_descriptor_paths(build_root):
        entries.append(json.loads(descriptor_path.read_text()))
    loaded: list[tuple[dict, PassDef]] = []
    for entry in entries:
        pass_text = entry.get("pass_text")
        source_file = entry.get("source_file")
        if not pass_text:
            raise ValueError(f"Pass descriptor {entry.get('id', '<unknown>')} does not define pass_text")
        pass_def = compile_pass(pass_text, Path(source_file))
        loaded.append((entry, pass_def))
    return loaded


def load_pass_entry(build_root: Path, pass_id: str) -> dict:
    descriptor_path = build_root / f"pass_{pass_id}.json"
    if not descriptor_path.exists():
        raise ValueError(f"Unknown pass id {pass_id!r} in {build_root}")
    return json.loads(descriptor_path.read_text())


def aggregate_output_path(output_root: Path, rel_output: str) -> Path:
    return output_root / Path(rel_output)


def output_subdir_rel_path(rel_output: str) -> Path:
    output_path = Path(rel_output)
    return output_path.parent / output_path.stem


def pass_header_rel_output_path(entry: dict, rel_output: str) -> Path:
    return output_subdir_rel_path(rel_output) / f"{entry['id']}.h"


def fragment_header_rel_output_path(entry: dict, rel_output: str, rel_file: Path) -> Path:
    return output_subdir_rel_path(rel_output) / entry["id"] / f"{sanitize_path_token(rel_file)}.h"


def read_instance_count(entry: dict, rel_file: Path) -> int:
    raw_counts = entry.get("counts", {})
    if not isinstance(raw_counts, dict):
        return 0
    return int(raw_counts.get(rel_file.as_posix(), 0))


def compute_index_base(
    entry: dict,
    rel_file: Path,
    rel_source_files: list[Path],
) -> int:
    total = 0
    for candidate in rel_source_files:
        if candidate == rel_file:
            break
        total += read_instance_count(entry, candidate)
    return total


def update_pass_count(build_root: Path, pass_id: str, rel_file: Path, count: int) -> None:
    descriptor_path = build_root / f"pass_{pass_id}.json"
    data = json.loads(descriptor_path.read_text())
    counts = data.setdefault("counts", {})
    rel_key = rel_file.as_posix()
    if count == 0:
        counts.pop(rel_key, None)
    else:
        counts[rel_key] = count
    if write_text_if_changed(descriptor_path, json.dumps(data, indent=2) + "\n"):
        print(f"Written: {descriptor_path}")


def write_pass_file_shards(
    entry: dict,
    pass_def: PassDef,
    rel_file: Path,
    instances: list[dict[str, str]],
    output_root: Path,
    build_root: Path,
    rel_source_files: list[Path],
) -> list[Path]:
    fragment_names = collect_declared_output_targets(pass_def.instance_ops)
    written_paths: list[Path] = []
    outputs = entry.get("outputs", [])
    if len(outputs) != len(fragment_names):
        raise ValueError(
            f"Manifest outputs for pass {entry['id']} do not match fragment count: {len(outputs)} vs {len(fragment_names)}"
        )

    index_base = compute_index_base(entry, rel_file, rel_source_files)
    rendered_fragments = render_fragments(
        pass_def,
        instances,
        pass_def.local_helper_defs,
        str(index_base),
    )

    for rel_output, fragment_name in zip(outputs, fragment_names):
        out_path = output_root / fragment_header_rel_output_path(entry, rel_output, rel_file)
        content = rendered_fragments.get(fragment_name, "").rstrip()
        if content:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            content += "\n"
            if write_text_if_changed(out_path, content):
                print(f"Written: {out_path}")
            written_paths.append(out_path)
        else:
            if delete_file_if_exists(out_path):
                print(f"Removed empty fragment: {out_path}")

    update_pass_count(build_root, entry["id"], rel_file, len(instances))

    return written_paths


def write_public_output_headers(entries: list[dict], output_root: Path) -> list[Path]:
    written_paths: list[Path] = []
    outputs_to_entries: dict[str, list[dict]] = {}
    for entry in entries:
        for rel_output in entry.get("outputs", []):
            outputs_to_entries.setdefault(rel_output, []).append(entry)

    for rel_output, output_entries in outputs_to_entries.items():
        out_path = aggregate_output_path(output_root, rel_output)
        lines = ["#pragma once", ""]
        for entry in output_entries:
            lines.append(f'#include "{pass_header_rel_output_path(entry, rel_output).as_posix()}"')
        content = "\n".join(lines).rstrip() + "\n"
        if write_text_if_changed(out_path, content):
            print(f"Written: {out_path}")
        written_paths.append(out_path)
    return written_paths


def write_pass_aggregate_headers(
    entries: list[dict],
    shared_dir: Path,
    output_root: Path,
    source_suffixes: tuple[str, ...],
) -> list[Path]:
    rel_source_files = [
        file.relative_to(shared_dir)
        for file in iter_source_files(shared_dir, source_suffixes)
    ]
    written_paths: list[Path] = []
    for entry in entries:
        for rel_output in entry.get("outputs", []):
            pass_out_path = output_root / pass_header_rel_output_path(entry, rel_output)
            pass_lines = ["#pragma once", ""]
            for rel_file in rel_source_files:
                fragment_path = output_root / fragment_header_rel_output_path(entry, rel_output, rel_file)
                if fragment_path.exists():
                    pass_lines.append(f'#include "{fragment_header_rel_output_path(entry, rel_output, rel_file).as_posix()}"')
            pass_content = "\n".join(pass_lines).rstrip() + "\n"
            if write_text_if_changed(pass_out_path, pass_content):
                print(f"Written: {pass_out_path}")
            written_paths.append(pass_out_path)

    return written_paths


def write_syntax_hints(out_path: Path, pass_names: list[str]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pass_macros = "\n".join(f"#define ${name} struct" for name in sorted(pass_names))
    content = f"""#pragma once

// Editor-only helper for source files that contain $ transpiler markers.
// GCC and Clang accept '$' in identifiers, so these macros make marker
// lines look C++-ish to editors while generated files strip them out.
#define $pass struct
{pass_macros}

"""
    if write_text_if_changed(out_path, content):
        print(f"Written syntax hints: {out_path}")


def resolve_output_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def parse_compile_passes_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile pass definitions into per-pass descriptors."
    )
    parser.add_argument("--passes-dir", required=True, type=Path, help="Directory that contains pass source files")
    parser.add_argument("--build-root", required=True, type=Path, help="Directory where generated pass descriptors are written")
    parser.add_argument("--shared-dir", type=Path, default=None, help="Optional shared source root used to emit aggregate headers")
    parser.add_argument("--output-root", type=Path, default=None, help="Output root for aggregate generated headers when --shared-dir is set")
    parser.add_argument(
        "--generated-header-prefix",
        default="",
        help="Optional prefix added to generated fragment header filenames",
    )
    parser.add_argument(
        "--generated-header-root",
        default="",
        help="Optional directory prefix for generated fragment headers, e.g. g",
    )
    parser.add_argument(
        "--source-suffix",
        dest="source_suffixes",
        action="append",
        default=list(DEFAULT_SOURCE_SUFFIXES),
        help="File suffix to scan; can be provided multiple times",
    )
    return parser.parse_args(argv)


def parse_process_file_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process one source file using compiled pass descriptors and emit per-pass shard headers."
    )
    parser.add_argument("--build-root", required=True, type=Path, help="Directory that contains generated pass descriptors")
    parser.add_argument("--input", required=True, type=Path, help="Source file to process")
    parser.add_argument("--shared-root", required=True, type=Path, help="Root directory of shared source files")
    parser.add_argument("--shared-output-root", required=True, type=Path, help="Output root for stripped shared files")
    return parser.parse_args(argv)


def parse_assemble_pass_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rewrite aggregate generated headers for one pass."
    )
    parser.add_argument("--build-root", required=True, type=Path, help="Directory that contains generated pass descriptors")
    parser.add_argument("--pass-id", required=True, help="Pass id to assemble")
    parser.add_argument("--shared-root", required=True, type=Path, help="Root directory of shared source files")
    parser.add_argument("--output-root", required=True, type=Path, help="Root directory for assembled generated headers")
    parser.add_argument(
        "--source-suffix",
        dest="source_suffixes",
        action="append",
        default=list(DEFAULT_SOURCE_SUFFIXES),
        help="File suffix to scan; can be provided multiple times",
    )
    return parser.parse_args(argv)


def parse_args(argv: list[str]) -> argparse.Namespace:
    if argv and argv[0] == "compile-passes":
        args = parse_compile_passes_args(argv[1:])
        args.command = "compile-passes"
        return args
    if argv and argv[0] == "process-file":
        args = parse_process_file_args(argv[1:])
        args.command = "process-file"
        return args
    if argv and argv[0] == "assemble-pass":
        args = parse_assemble_pass_args(argv[1:])
        args.command = "assemble-pass"
        return args
    if argv and not argv[0].startswith("-"):
        if len(argv) < 3:
            raise ValueError(
                "Legacy usage: metacodegen.py <shared_dir> <gen_py_dir> <output_root>"
            )
        args = argparse.Namespace(
            shared_dir=Path(argv[0]),
            output_root=Path(argv[2]),
            shared_output_root=None,
            stamp=Path(argv[2]) / "content.stamp",
            generated_header_prefix="",
            generated_header_root="",
            syntax_hints=None,
            no_syntax_hints=False,
            source_suffixes=list(DEFAULT_SOURCE_SUFFIXES),
        )
        args.command = "generate-all"
        return args

    parser = argparse.ArgumentParser(
        description=(
            "Scan shared source files for $ markers, generate shared headers once, "
            "and emit stripped shared sources for integration into a shared build output."
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
        help="Optional separate directory for stripped shared sources; defaults to --output-root",
    )
    parser.add_argument(
        "--stamp",
        type=Path,
        default=None,
        help="Optional stamp file written after generation; defaults to <output-root>/content.stamp",
    )
    parser.add_argument(
        "--generated-header-prefix",
        default="",
        help="Optional prefix added to generated fragment header filenames such as prefab.h -> <prefix>prefab.h",
    )
    parser.add_argument(
        "--generated-header-root",
        default="",
        help="Optional directory under the output root where generated fragment headers are organized by pass, e.g. g/tile/textures.h",
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
    args.command = "generate-all"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "compile-passes":
        source_suffixes = tuple(dict.fromkeys(args.source_suffixes))
        print(f"[codegen] Compile passes: {args.passes_dir}")
        entries = compile_pass_inventory(
            args.passes_dir,
            args.generated_header_root,
            args.generated_header_prefix,
            source_suffixes,
        )
        remove_stale_pass_artifacts(args.build_root.resolve(), {entry["id"] for entry in entries})
        for entry in entries:
            descriptor_path = args.build_root / f"pass_{entry['id']}.json"
            write_pass_descriptor(descriptor_path, entry)
        if args.shared_dir is not None and args.output_root is not None:
            write_public_output_headers(entries, args.output_root.resolve())
        print(f"[codegen] Compiled {len(entries)} passes")
        return 0
    if args.command == "process-file":
        shared_root = args.shared_root.resolve()
        input_file = args.input.resolve()
        build_root = args.build_root.resolve()
        rel_source_files = [
            file.relative_to(shared_root)
            for file in iter_source_files(shared_root, tuple(DEFAULT_SOURCE_SUFFIXES))
        ]
        rel_from_shared_parent = input_file.relative_to(shared_root.parent)
        rel_from_shared_root = input_file.relative_to(shared_root)
        print(f"[codegen] Process file: {rel_from_shared_root.as_posix()}")
        loaded_passes = load_pass_defs_from_build_root(build_root)
        blocks, strip_blocks, source = discover_blocks_in_file(input_file)

        pass_defs = {entry["id"]: pass_def for entry, pass_def in loaded_passes}
        instances_by_pass = {entry["id"]: [] for entry, _ in loaded_passes}

        for block in blocks:
            stripped = block.text.lstrip()
            if stripped.startswith("pass"):
                block.replacement = ""
                continue

            pass_id, values = identify_pass(block, pass_defs)
            instances_by_pass[pass_id].append(values)

        stripped_out = strip_marker_blocks(source, strip_blocks)
        shared_out_path = args.shared_output_root / rel_from_shared_parent
        shared_out_path.parent.mkdir(parents=True, exist_ok=True)
        if write_text_if_changed(shared_out_path, stripped_out):
            print(f"Written: {shared_out_path}")

        for entry, pass_def in loaded_passes:
            instances = instances_by_pass[entry["id"]]
            write_pass_file_shards(
                entry,
                pass_def,
                rel_from_shared_root,
                instances,
                args.shared_output_root.resolve(),
                build_root,
                rel_source_files,
            )

        matched_pass_count = sum(1 for instances in instances_by_pass.values() if instances)
        total_instances = sum(len(instances) for instances in instances_by_pass.values())
        print(f"[codegen] Matched {total_instances} instances across {matched_pass_count} passes")
        return 0
    if args.command == "assemble-pass":
        entry = load_pass_entry(args.build_root.resolve(), args.pass_id)
        print(f"[codegen] Assemble pass: {args.pass_id}")
        source_suffixes = tuple(dict.fromkeys(args.source_suffixes))
        write_pass_aggregate_headers(
            [entry],
            args.shared_root.resolve(),
            args.output_root.resolve(),
            source_suffixes,
        )
        return 0

    shared_dir = args.shared_dir
    output_root = args.output_root
    shared_output_root = args.shared_output_root or output_root
    stamp_path = args.stamp or (output_root / "content.stamp")
    source_suffixes = tuple(dict.fromkeys(args.source_suffixes))

    print(f"[codegen] Scan: {shared_dir}")
    blocks, strip_map = discover_blocks(shared_dir, source_suffixes)
    print(f"[codegen] Found {len(blocks)} marker blocks in {len(strip_map)} files")
    pass_blocks = [block for block in blocks if block.text.lstrip().startswith("pass")]
    if not pass_blocks:
        raise ValueError(f"No $pass block found under {shared_dir}")

    print(f"[codegen] Compile: {len(pass_blocks)} pass blocks")
    pass_defs: dict[str, PassDef] = {}
    for block in pass_blocks:
        pass_def = compile_pass(block.text, block.file)
        key = f"__top_level__:{len(pass_defs)}"
        pass_defs[key] = pass_def
        block.replacement = ""

    local_helper_count = sum(len(pass_def.local_helper_defs) for pass_def in pass_defs.values())
    instances_by_pass = {name: [] for name in pass_defs}
    print(f"[codegen] Match: {len(pass_defs)} passes, {local_helper_count} rules")

    for block in blocks:
        stripped = block.text.lstrip()
        if stripped.startswith("pass"):
            continue

        pass_name, values = identify_pass(block, pass_defs)
        instances_by_pass[pass_name].append(values)

    output_root.mkdir(parents=True, exist_ok=True)
    total_instances = sum(len(instances) for instances in instances_by_pass.values())
    print(f"[codegen] Emit: {total_instances} instances into {len(pass_defs)} output groups")
    for name, pass_def in pass_defs.items():
        fragments = render_fragments(pass_def, instances_by_pass[name], pass_def.local_helper_defs)
        folder_name = default_pass_output_name(pass_def, name)
        for fragment_name, content in fragments.items():
            if args.generated_header_root:
                out_path = output_root / args.generated_header_root / folder_name / f"{args.generated_header_prefix}{fragment_name}.h"
            else:
                out_path = output_root / f"{args.generated_header_prefix}{fragment_name}.h"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content)
            print(f"Written: {out_path}")

    print(f"[codegen] Rewrite: stripped shared sources -> {shared_output_root}")
    write_generated_sources(shared_dir, strip_map, shared_output_root, source_suffixes)
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text("# Generated by codegen\n")
    print(f"Written: {stamp_path}")
    if not args.no_syntax_hints:
        print("[codegen] Emit: syntax hints")
        syntax_hints_path = args.syntax_hints or (output_root / "syntax_hints.h")
        write_syntax_hints(syntax_hints_path, [pass_def.name for pass_def in pass_defs.values() if pass_def.name])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

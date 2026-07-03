"""
Scans shared source files for $ markers, generates shared headers once,
and writes cleaned copies of the shared sources into a shared build folder.

Pass authoring note for future developers:
- Do not use Python f-strings or rf-strings inside `src/shared/passes/*`.
- Pass files have their own templating/concatenation style such as
  `"prefix "value" suffix"` and helper-function string concatenation.
- Keeping pass source in that dedicated style avoids mixing two interpolation
  systems and keeps pass syntax readable and predictable.
"""

import argparse
import ast
import hashlib
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
    callable_name: str | None
    block_keyword: str
    schema: list["SchemaPart"]
    init_vars: dict
    output_params: list[str]
    instance_targets: list[str]
    instance_ops: list["InstanceOp"]
    is_helper: bool = False
    local_helper_defs: dict[str, "PassDef"] = field(default_factory=dict)
    local_func_defs: dict[str, "FuncDef"] = field(default_factory=dict)


@dataclass
class FuncDef:
    name: str
    params: list[str]
    instance_ops: list["InstanceOp"]


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
    condition_value_is_ref: bool = False
    true_ops: list["InstanceOp"] | None = None
    false_ops: list["InstanceOp"] | None = None


@dataclass
class SchemaPart:
    kind: str
    value: str = ""
    alternatives: list[list["SchemaPart"]] | None = None
    alternative_labels: list[str | None] | None = None
    capture_name: str | None = None


TRUTHY_DEFINE_VALUES = {"1", "true", "yes", "on"}
FALSY_DEFINE_VALUES = {"", "0", "false", "no", "off"}


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


def parse_define_args(values: list[str]) -> dict[str, str]:
    defines: dict[str, str] = {}
    for raw_value in values:
        text = raw_value.strip()
        if not text:
            continue
        if "=" in text:
            name, value = text.split("=", 1)
        else:
            name, value = text, "1"
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise ValueError(f"Invalid define name: {raw_value!r}")
        defines[name] = value.strip()
    return defines


def serialize_defines(defines: dict[str, str]) -> list[str]:
    return [f"{name}={value}" for name, value in sorted(defines.items())]


def is_truthy_define_value(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in TRUTHY_DEFINE_VALUES:
        return True
    if normalized in FALSY_DEFINE_VALUES:
        return False
    return True


def eval_preprocessor_expr(expr: str, defines: dict[str, str], file: Path, line_no: int) -> bool:
    expr = expr.strip()
    if not expr:
        raise ValueError(f"Empty #if expression in {file}:{line_no}")

    defined_match = re.fullmatch(r"(!?)defined\s*\(\s*([A-Za-z_]\w*)\s*\)", expr)
    if defined_match is not None:
        negate, name = defined_match.groups()
        result = name in defines
        return not result if negate else result

    ident_match = re.fullmatch(r"(!?)([A-Za-z_]\w*)", expr)
    if ident_match is not None:
        negate, name = ident_match.groups()
        result = is_truthy_define_value(defines.get(name))
        return not result if negate else result

    literal_match = re.fullmatch(r"(!?)(0|1|true|false)", expr, re.IGNORECASE)
    if literal_match is not None:
        negate, literal = literal_match.groups()
        result = literal.lower() in {"1", "true"}
        return not result if negate else result

    raise ValueError(f"Unsupported preprocessor expression {expr!r} in {file}:{line_no}")


def preprocess_pass_text(source: str, defines: dict[str, str], file: Path) -> str:
    if "#" not in source:
        return source

    directive_re = re.compile(r"^([ \t]*)#(ifdef|ifndef|if|elifdef|elifndef|elif|else|endif)\b(.*)$")
    lines = source.splitlines(keepends=True)
    output: list[str] = []
    stack: list[dict[str, bool]] = []

    for line_index, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("##"):
            if all(frame["active"] for frame in stack):
                output.append(line)
            continue

        match = directive_re.match(line)
        if match is None:
            if all(frame["active"] for frame in stack):
                output.append(line)
            else:
                newline = "\n" if line.endswith("\n") else ""
                output.append(newline)
            continue

        _, directive, remainder = match.groups()
        remainder = remainder.strip()

        if directive in {"ifdef", "ifndef", "if"}:
            if directive == "ifdef":
                if not re.fullmatch(r"[A-Za-z_]\w*", remainder):
                    raise ValueError(f"Invalid #ifdef name {remainder!r} in {file}:{line_index}")
                condition = remainder in defines
            elif directive == "ifndef":
                if not re.fullmatch(r"[A-Za-z_]\w*", remainder):
                    raise ValueError(f"Invalid #ifndef name {remainder!r} in {file}:{line_index}")
                condition = remainder not in defines
            else:
                condition = eval_preprocessor_expr(remainder, defines, file, line_index)

            parent_active = all(frame["active"] for frame in stack)
            stack.append({
                "parent_active": parent_active,
                "active": parent_active and condition,
                "branch_taken": condition,
            })
            output.append("\n" if line.endswith("\n") else "")
            continue

        if not stack:
            raise ValueError(f"Unexpected #{directive} without matching #if in {file}:{line_index}")

        frame = stack[-1]
        parent_active = frame["parent_active"]

        if directive in {"elifdef", "elifndef", "elif"}:
            if frame["branch_taken"]:
                frame["active"] = False
            else:
                if directive == "elifdef":
                    if not re.fullmatch(r"[A-Za-z_]\w*", remainder):
                        raise ValueError(f"Invalid #elifdef name {remainder!r} in {file}:{line_index}")
                    condition = remainder in defines
                elif directive == "elifndef":
                    if not re.fullmatch(r"[A-Za-z_]\w*", remainder):
                        raise ValueError(f"Invalid #elifndef name {remainder!r} in {file}:{line_index}")
                    condition = remainder not in defines
                else:
                    condition = eval_preprocessor_expr(remainder, defines, file, line_index)
                frame["active"] = parent_active and condition
                frame["branch_taken"] = condition
            output.append("\n" if line.endswith("\n") else "")
            continue

        if directive == "else":
            if remainder:
                raise ValueError(f"#else does not accept a condition in {file}:{line_index}")
            frame["active"] = parent_active and not frame["branch_taken"]
            frame["branch_taken"] = True
            output.append("\n" if line.endswith("\n") else "")
            continue

        if directive == "endif":
            if remainder:
                raise ValueError(f"#endif does not accept trailing tokens in {file}:{line_index}")
            stack.pop()
            output.append("\n" if line.endswith("\n") else "")
            continue

    if stack:
        raise ValueError(f"Unterminated preprocessor block in {file}")

    return "".join(output)


def parse_pass_file(source: str) -> dict[str, str]:
    legacy_section_match = re.search(r'^[ \t]*(schema|instance)\s*\(\s*\)', source, re.MULTILINE)
    if legacy_section_match is not None:
        section_name = legacy_section_match.group(1)
        raise ValueError(
            f"Deprecated {section_name}() section syntax is no longer supported; "
            f"use `{section_name} {{ ... }}` inside the surrounding pass or local pass block instead"
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


def parse_schema_template(
    schema_body: str,
    pass_name: str,
    file: Path,
    init_vars: dict[str, object] | None = None,
) -> list[SchemaPart]:
    source = textwrap.dedent(schema_body).strip()
    if not source:
        raise ValueError(f"$pass {pass_name} has an empty schema block in {file}")

    wrapped = parse_wrapped_schema_literal(source)
    if wrapped is not None:
        source = wrapped

    parts = parse_legacy_schema_template(source, pass_name, file, init_vars)

    if not parts:
        raise ValueError(f"$pass {pass_name} has an empty schema block in {file}")

    compact = compact_schema_parts(parts, pass_name, file)
    return compact


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


def parse_legacy_schema_template(
    source: str,
    pass_name: str,
    file: Path,
    init_vars: dict[str, object] | None = None,
) -> list[SchemaPart]:
    parts, end = parse_legacy_schema_parts(source, 0, False, pass_name, file, init_vars)
    if end != len(source):
        raise ValueError(f"Invalid schema syntax in $pass {pass_name} in {file}: {source[end:end+20]!r}")
    return parts


def schema_literal_var_value(name: str, init_vars: dict[str, object] | None) -> str | None:
    if not init_vars or name not in init_vars:
        return None
    value = init_vars[name]
    if isinstance(value, str):
        return value
    return None


def parse_legacy_schema_parts(
    source: str,
    start: int,
    in_branch: bool,
    pass_name: str,
    file: Path,
    init_vars: dict[str, object] | None = None,
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
            branch, i = parse_legacy_schema_branch(source, i + 1, pass_name, file, init_vars)
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
            if name == "eof":
                parts.append(SchemaPart("eof"))
                continue
            if i < len(source) and source[i] == "[":
                branch, i = parse_legacy_schema_branch(source, i + 1, pass_name, file, init_vars)
                if branch.alternative_labels and all(label is not None for label in branch.alternative_labels):
                    branch.capture_name = name
                    parts.append(branch)
                else:
                    parts.append(SchemaPart("capture", name))
                    parts.append(branch)
                continue
            literal_value = schema_literal_var_value(name, init_vars)
            if literal_value is not None:
                parts.append(SchemaPart("literal", literal_value))
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
    init_vars: dict[str, object] | None = None,
    capture_name: str | None = None,
) -> tuple[SchemaPart, int]:
    alternatives = []
    alternative_labels = []
    saw_separator = False
    i = start

    while True:
        label = None
        label_match = re.match(r'([A-Za-z_]\w*)@', source[i:])
        if label_match is not None:
            label = label_match.group(1)
            i += len(label_match.group(0))
        parts, i = parse_legacy_schema_parts(source, i, True, pass_name, file, init_vars)
        alternatives.append(parts)
        alternative_labels.append(label)
        if i >= len(source):
            raise ValueError(f"Unterminated schema branch in $pass {pass_name} in {file}")
        if source[i] == "|":
            saw_separator = True
            i += 1
            continue
        if source[i] == "]":
            if not saw_separator:
                raise ValueError(f"Schema branch in $pass {pass_name} in {file} must contain '|'")
            if capture_name is not None:
                missing_labels = [index for index, value in enumerate(alternative_labels) if value is None]
                if missing_labels:
                    raise ValueError(
                        f"Captured schema branch {capture_name!r} in $pass {pass_name} in {file} requires labels for every alternative"
                    )
                seen_labels: set[str] = set()
                for label_value in alternative_labels:
                    if label_value in seen_labels:
                        raise ValueError(
                            f"Captured schema branch {capture_name!r} in $pass {pass_name} in {file} has duplicate label {label_value!r}"
                        )
                    seen_labels.add(label_value)
            return SchemaPart("branch", alternatives=alternatives, alternative_labels=alternative_labels, capture_name=capture_name), i + 1
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
            compact.append(SchemaPart("branch", alternatives=alternatives, alternative_labels=part.alternative_labels, capture_name=part.capture_name))
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
    literal_assignments: dict[str, object] = {}
    if source:
        try:
            parsed = ast.parse(source, filename=str(file))
        except SyntaxError as exc:
            raise ValueError(f"Invalid raw python in $pass block in {file}: {exc}") from exc

        allowed_python_chunks: list[str] = []
        for node in parsed.body:
            node_text = ast.get_source_segment(source, node)
            if node_text is None:
                lines = source.splitlines()
                start_line = max(getattr(node, "lineno", 1) - 1, 0)
                end_line = getattr(node, "end_lineno", getattr(node, "lineno", 1))
                node_text = "\n".join(lines[start_line:end_line])

            if isinstance(node, (ast.Import, ast.ImportFrom)):
                allowed_python_chunks.append(node_text)
                continue

            if isinstance(node, ast.Assign):
                if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                    raise ValueError(
                        f"Only simple literal assignments are allowed in raw python sections of $pass blocks now; "
                        f"found {node_text!r} in {file}:{getattr(node, 'lineno', 1)}"
                    )
                try:
                    literal_assignments[node.targets[0].id] = ast.literal_eval(node.value)
                except Exception as exc:
                    raise ValueError(
                        f"Only literal assignments are allowed in raw python sections of $pass blocks now; "
                        f"found {node_text!r} in {file}:{getattr(node, 'lineno', 1)}: {exc}"
                    ) from exc
                allowed_python_chunks.append(node_text)
                continue

            raise ValueError(
                f"Only import statements and literal assignments are allowed in raw python sections of $pass blocks now; "
                f"found {node_text!r} in {file}:{getattr(node, 'lineno', 1)}"
            )

        source = "\n".join(allowed_python_chunks)

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
    scope.update(literal_assignments)
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

    invoke_match = re.match(r'\s*([A-Za-z_]\w*)\s*\[(.+)\]\s*$', text)
    if invoke_match:
        return InstanceOp(
            kind="invoke",
            helper_name=invoke_match.group(1),
            input_expr=invoke_match.group(2).strip(),
        )

    raise ValueError(f"Unsupported instance statement: {text.strip()!r}")


def parse_instance_section(instance_body: str) -> list[InstanceOp]:
    lines = instance_body.splitlines()

    def parse_if_statement(line_text: str, line_index: int) -> tuple[InstanceOp, int]:
        stripped = line_text.strip()
        if_match = re.match(r'\s*if\s+(\w+)\s*(==|!=)\s*(?:"([^"]*?)"|([A-Za-z_]\w*))\s*(.*)$', line_text)
        if if_match is None:
            raise ValueError(f"Unsupported if statement: {stripped!r}")

        field_name = if_match.group(1)
        op = if_match.group(2)
        cmp_value = if_match.group(3) if if_match.group(3) is not None else if_match.group(4)
        cmp_is_ref = if_match.group(3) is None and if_match.group(4) is not None
        rest = if_match.group(5).strip()

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
            condition_value_is_ref=cmp_is_ref,
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
        trailing = next((line.strip() for line in lines[end:] if line.strip() and line.strip() != "}"), "")
        if trailing:
            raise ValueError(f"Unsupported trailing instance content: {trailing!r}")
    return ops


def iter_instance_ops(ops: list[InstanceOp]):
    for op in ops:
        yield op
        if op.kind == "if":
            yield from iter_instance_ops(op.true_ops or [])
            yield from iter_instance_ops(op.false_ops or [])


def pass_uses_global_pass_instances(pass_def: PassDef) -> bool:
    return any(
        op.kind == "call" and
        op.input_expr is not None and
        op.input_expr.startswith("@pass:")
        for op in iter_instance_ops(pass_def.instance_ops)
    )


def pass_calls_itself(pass_def: PassDef) -> bool:
    if not pass_def.is_helper or pass_def.name is None:
        return False
    return any(
        op.kind == "call" and op.helper_name == pass_def.name
        for op in iter_instance_ops(pass_def.instance_ops)
    )


def declared_instance_aliases(ops: list[InstanceOp]) -> set[str]:
    return {
        op.alias_name
        for op in iter_instance_ops(ops)
        if op.kind in {"var", "assign"} and op.alias_name is not None
    }


def parse_named_block_header(header: str, file: Path, keyword: str) -> tuple[str | None, list[str]]:
    keyword_pattern = re.sub(r"\\ ", r"[ \t]+", re.escape(keyword))
    m = re.match(rf"{keyword_pattern}(?:[ \t]+(\w+)(?:\(([^)]*)\))?)?\s*\{{?\s*;?\s*$", header)
    if not m:
        raise ValueError(f"Expected {keyword} or {keyword} <name>(...) in {file}")
    name = m.group(1)
    output_params = []
    if m.group(2):
        output_params = [part.strip() for part in m.group(2).split(",") if part.strip()]
    return name, output_params


def normalize_local_pass_header(text: str) -> str:
    return re.sub(r"^\s*local[ \t]+pass\b", "pass", text, count=1)


def parse_pass_header(header: str, file: Path) -> tuple[str | None, list[str], bool]:
    m = re.match(r"pass(?:[ \t]+(\w+)(?:\(([^)]*)\))?)?\s*\{?\s*;?\s*$", header)
    if not m:
        raise ValueError(f"Expected pass in {file}")
    name = m.group(1)
    output_params = []
    if m.group(2):
        output_params = [part.strip() for part in m.group(2).split(",") if part.strip()]
    return name, output_params, m.group(2) is not None


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


def parse_legacy_two_block_sections(block_text: str, keyword: str) -> tuple[str, str] | None:
    stripped = block_text.strip()
    header_match = re.match(rf"{re.escape(keyword)}(?:[ \t]+\w+(?:\([^)]*\))?)?\s*\{{", stripped)
    if header_match is None:
        return None

    first_open = stripped.find("{", header_match.start(), header_match.end())
    first_close = matching_brace(stripped, first_open)
    if first_close is None:
        return None

    second_open = skip_c_whitespace(stripped, first_close + 1)
    if second_open >= len(stripped) or stripped[second_open] != "{":
        return None

    second_close = matching_brace(stripped, second_open)
    if second_close is None:
        return None

    trailing = stripped[second_close + 1:].strip()
    if trailing not in ("", ";"):
        return None

    return (
        stripped[first_open + 1:first_close].strip(),
        stripped[second_open + 1:second_close].strip(),
    )


def compile_local_pass(local_pass_text: str, file: Path, inherited_init_vars: dict | None = None) -> PassDef:
    stripped_local_pass = local_pass_text.strip()
    legacy_sections = parse_legacy_two_block_sections(stripped_local_pass, "local pass")
    if legacy_sections is not None:
        raise ValueError(
            f"Legacy two-block local pass syntax is no longer supported in {file}; "
            f"use `local pass <name>(...) {{ ... schema {{ ... }} instance {{ ... }} }}` instead"
        )

    unwrapped = unwrap_pass_block(normalize_local_pass_header(stripped_local_pass))
    lines = unwrapped.lstrip().splitlines()
    first_line = lines[0].strip()
    name, output_params = parse_named_block_header(first_line.replace("pass", "local pass", 1), file, "local pass")
    rebuilt_local_pass_text = first_line
    body_text = "\n".join(lines[1:])
    if body_text:
        rebuilt_local_pass_text += "\n" + body_text
    sections = parse_pass_file(rebuilt_local_pass_text)
    missing = [section_name for section_name in ("schema", "instance") if section_name not in sections]
    if missing:
        raise ValueError(f"local pass {name or '<unnamed>'} is missing section(s): {', '.join(missing)}")
    raw_python = sections.get("python", "")

    if name is None:
        raise ValueError(f"local pass in {file} must declare a name")

    instance_ops = parse_instance_section(sections["instance"])
    declared_aliases = declared_instance_aliases(instance_ops)
    if not output_params:
        raise ValueError(f"local pass {name} must declare at least one output parameter; implicit return output is not supported")
    invalid_targets = sorted({
        op.target for op in iter_instance_ops(instance_ops)
        if op.kind == "emit" and op.target not in output_params and op.target not in declared_aliases
    })
    if invalid_targets:
        raise ValueError(f"local pass {name} may only write to declared outputs {output_params}, found: {', '.join(invalid_targets)}")
    for op in iter_instance_ops(instance_ops):
        if op.kind == "assign":
            if op.source_target is None:
                continue
            if op.source_target not in output_params and op.source_target not in declared_aliases:
                raise ValueError(
                    f"local pass {name} may only bind variables to declared outputs {output_params} or other variables, found: {op.source_target}"
                )

    init_vars = dict(inherited_init_vars or {})
    init_vars.update(run_init_python(raw_python, file))
    schema = parse_schema_template(sections["schema"], name, file, init_vars)
    for label in collect_schema_branch_labels(schema):
        init_vars.setdefault(label, label)

    return PassDef(
        name=name,
        callable_name=None,
        block_keyword=name,
        schema=schema,
        init_vars=init_vars,
        output_params=output_params,
        instance_targets=[],
        instance_ops=instance_ops,
        is_helper=True,
    )


def compile_func(func_text: str, file: Path) -> FuncDef:
    stripped_func = func_text.strip()
    unwrapped = unwrap_pass_block(re.sub(r"^\s*func\b", "pass", stripped_func, count=1))
    lines = unwrapped.lstrip().splitlines()
    first_line = lines[0].strip()
    name, params = parse_named_block_header(first_line.replace("pass", "func", 1), file, "func")
    if name is None:
        raise ValueError(f"func in {file} must declare a name")

    body_text = "\n".join(lines[1:])
    instance_ops = parse_instance_section(body_text)
    declared_aliases = declared_instance_aliases(instance_ops)
    invalid_emit_targets = sorted({
        op.target for op in iter_instance_ops(instance_ops)
        if op.kind == "emit" and op.target is not None and op.target not in declared_aliases and op.target != "out"
    })
    if invalid_emit_targets:
        raise ValueError(
            f"func {name} in {file} may only write to local variables or 'out', found: {', '.join(invalid_emit_targets)}"
        )
    return FuncDef(
        name=name,
        params=params,
        instance_ops=instance_ops,
    )


def compile_pass(pass_text: str, file: Path) -> PassDef:
    stripped_pass = pass_text.strip()
    local_helper_defs: dict[str, PassDef] = {}
    local_func_defs: dict[str, FuncDef] = {}
    legacy_sections = parse_legacy_two_block_sections(stripped_pass, "pass")
    if legacy_sections is not None:
        raise ValueError(
            f"Legacy two-block $pass syntax is no longer supported in {file}; "
            f"use `$pass {{ ... schema {{ ... }} instance {{ ... }} }}` instead"
        )

    pass_text = unwrap_pass_block(pass_text)
    lines = pass_text.lstrip().splitlines()
    first_line = lines[0].strip()
    top_level_name, output_params, has_outputs = parse_pass_header(first_line, file)
    if has_outputs:
        raise ValueError(f"Top-level pass in {file} cannot declare outputs; use nested local pass <name>(...) for helpers")
    body_text = "\n".join(lines[1:])
    body_text, func_texts = extract_top_level_named_blocks(body_text, "func")
    for func_text in func_texts:
        func_def = compile_func(func_text, file)
        if func_def.name in local_func_defs:
            raise ValueError(f"Duplicate func {func_def.name} in {file}")
        local_func_defs[func_def.name] = func_def
    body_text, local_pass_texts = extract_top_level_named_blocks(body_text, "local pass")
    rebuilt_pass_text = first_line
    if body_text:
        rebuilt_pass_text += "\n" + body_text
    sections = parse_pass_file(rebuilt_pass_text)
    missing = [section_name for section_name in ("schema", "instance") if section_name not in sections]
    if missing:
        raise ValueError(f"Top-level $pass in {file} is missing section(s): {', '.join(missing)}")
    raw_python = sections.get("python", "")
    init_vars = run_init_python(raw_python, file)

    for local_pass_text in local_pass_texts:
        local_pass_def = compile_local_pass(local_pass_text, file, init_vars)
        if local_pass_def.name in local_helper_defs:
            raise ValueError(f"Duplicate local pass {local_pass_def.name} in {file}")
        local_helper_defs[local_pass_def.name] = local_pass_def

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
            f"Top-level $pass in {file} must pass outputs as 'out.<name>' or declared variables when calling local passes, found: {', '.join(invalid_call_targets)}"
        )
    schema = parse_schema_template(sections["schema"], None, file, init_vars)
    enum_labels = collect_schema_branch_labels(schema)
    for helper_def in local_helper_defs.values():
        enum_labels.update(collect_schema_branch_labels(helper_def.schema))
        helper_def.local_func_defs = local_func_defs
    for label in enum_labels:
        init_vars.setdefault(label, label)
        for helper_def in local_helper_defs.values():
            helper_def.init_vars.setdefault(label, label)

    return PassDef(
        name=None,
        callable_name=top_level_name,
        block_keyword="__top_level__",
        schema=schema,
        init_vars=init_vars,
        output_params=output_params,
        instance_targets=list(dict.fromkeys(
            output_target_key(op.target)
            for op in iter_instance_ops(instance_ops)
            if op.kind == "emit" and op.target is not None and op.target not in declared_aliases and parse_output_target(op.target) is not None
        )),
        instance_ops=instance_ops,
        is_helper=False,
        local_helper_defs=local_helper_defs,
        local_func_defs=local_func_defs,
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


def parse_builtin_wrap_block(block: MarkerBlock) -> list[MarkerBlock] | None:
    text = block.text.strip()
    if not text.startswith("wrap"):
        return None

    i = len("wrap")
    if i >= len(text) or not text[i].isspace():
        return None
    i = skip_c_whitespace(text, i)
    if i >= len(text) or text[i] not in "\"'":
        raise ValueError(f'Invalid $wrap block in {block.file}: expected quoted prefix after "wrap"')

    prefix, i = parse_schema_string_literal(text, i)
    i = skip_c_whitespace(text, i)
    if i >= len(text) or text[i] != "{":
        raise ValueError(f"Invalid $wrap block in {block.file}: expected '{{' after wrap prefix")

    body_end = matching_brace(text, i)
    if body_end is None:
        raise ValueError(f"Invalid $wrap block in {block.file}: unterminated wrapper body")

    trailing = text[body_end + 1:].strip()
    if trailing:
        raise ValueError(f"Invalid $wrap block in {block.file}: unexpected trailing syntax {trailing!r}")

    body = text[i + 1:body_end]
    synthetic_blocks: list[MarkerBlock] = []
    body_index = 0

    while True:
        body_index = skip_c_whitespace(body, body_index)
        if body_index >= len(body):
            break

        name_match = re.match(r"[A-Za-z_]\w*", body[body_index:])
        if name_match is None:
            snippet = body[body_index:body_index + 40]
            raise ValueError(f"Invalid $wrap entry in {block.file}: expected entry name near {snippet!r}")

        local_name = name_match.group(0)
        body_index += len(local_name)
        body_index = skip_c_whitespace(body, body_index)
        if body_index >= len(body) or body[body_index] != "{":
            raise ValueError(f"Invalid $wrap entry {local_name!r} in {block.file}: expected '{{' after entry name")

        entry_end = matching_brace(body, body_index)
        if entry_end is None:
            raise ValueError(f"Invalid $wrap entry {local_name!r} in {block.file}: unterminated entry body")

        entry_body = body[body_index + 1:entry_end].strip()
        synthetic_text = f"{prefix}{local_name} {{\n{entry_body}\n}}"
        synthetic_blocks.append(MarkerBlock(
            file=block.file,
            start=block.start,
            end=block.end,
            text=synthetic_text,
        ))

        body_index = entry_end + 1
        while body_index < len(body) and body[body_index].isspace():
            body_index += 1
        if body_index < len(body) and body[body_index] == ";":
            body_index += 1

    return synthetic_blocks


def expand_builtin_blocks(blocks: list[MarkerBlock]) -> list[MarkerBlock]:
    expanded: list[MarkerBlock] = []
    for block in blocks:
        synthetic_blocks = parse_builtin_wrap_block(block)
        if synthetic_blocks is None:
            expanded.append(block)
        else:
            expanded.extend(synthetic_blocks)
    return expanded


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
    raw_blocks = []
    strip_blocks: dict[Path, list[MarkerBlock]] = {}

    for file in iter_source_files(shared_dir, source_suffixes):
        source = file.read_text()
        positions = marker_positions(source)
        for index, start in enumerate(positions):
            end = block_end(source, start)
            text = source[start + MARKER_LEN:end]
            block = MarkerBlock(file=file, start=start, end=end, text=text)
            raw_blocks.append(block)
            strip_blocks.setdefault(file, []).append(block)

    return expand_builtin_blocks(raw_blocks), strip_blocks


def collect_instances_by_pass(
    shared_dir: Path,
    pass_defs: dict[str, PassDef],
    source_suffixes: tuple[str, ...],
) -> dict[str, list[dict[str, str]]]:
    instances_by_pass = {name: [] for name in pass_defs}
    blocks, _ = discover_blocks(shared_dir, source_suffixes)
    for block in blocks:
        stripped = block.text.lstrip()
        if stripped.startswith("pass"):
            continue
        pass_id, values = identify_pass(block, pass_defs)
        instances_by_pass[pass_id].append(values)
    return instances_by_pass


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
    return apply_missing_schema_captures(values, schema)


def collect_schema_capture_names(schema: list[SchemaPart]) -> set[str]:
    names: set[str] = set()
    for part in schema:
        if part.kind == "capture" and part.value:
            names.add(part.value)
        elif part.kind == "branch":
            if part.capture_name:
                names.add(part.capture_name)
            for alternative in part.alternatives or []:
                names.update(collect_schema_capture_names(alternative))
    return names


def apply_missing_schema_captures(values: dict[str, str], schema: list[SchemaPart]) -> dict[str, str]:
    completed = values.copy()
    for capture_name in collect_schema_capture_names(schema):
        completed.setdefault(capture_name, "")
    return completed


def collect_schema_branch_labels(schema: list[SchemaPart]) -> set[str]:
    labels: set[str] = set()
    for part in schema:
        if part.kind != "branch":
            continue
        for label in part.alternative_labels or []:
            if label is not None:
                labels.add(label)
        for alternative in part.alternatives or []:
            labels.update(collect_schema_branch_labels(alternative))
    return labels


def match_schema_nodes(
    source: str,
    schema: list[SchemaPart],
    index: int,
    pos: int,
    values: dict[str, str],
    allow_trailing: bool = False,
) -> tuple[int, dict[str, str]] | None:
    if allow_trailing:
        best_match: tuple[int, dict[str, str]] | None = None
        for matched in iter_schema_matches(source, schema, index, pos, values, allow_trailing):
            if best_match is None or matched[0] > best_match[0]:
                best_match = matched
        return best_match
    for matched in iter_schema_matches(source, schema, index, pos, values, allow_trailing):
        return matched
    return None


def iter_schema_matches(
    source: str,
    schema: list[SchemaPart],
    index: int,
    pos: int,
    values: dict[str, str],
    allow_trailing: bool = False,
):
    if index >= len(schema):
        if not allow_trailing and source[pos:].strip():
            return
        yield pos, values
        return

    part = schema[index]
    if part.kind == "literal":
        end = match_schema_literal(source, pos, part.value)
        if end is None:
            return
        yield from iter_schema_matches(source, schema, index + 1, end, values, allow_trailing)
        return

    if part.kind == "branch":
        for alt_index, alternative in enumerate(part.alternatives or []):
            for alternative_end, alternative_values in iter_schema_matches(
                source,
                alternative,
                0,
                pos,
                values.copy(),
                allow_trailing=True,
            ):
                next_values = alternative_values
                if part.capture_name:
                    if not part.alternative_labels or part.alternative_labels[alt_index] is None:
                        continue
                    next_values = alternative_values.copy()
                    next_values[part.capture_name] = part.alternative_labels[alt_index]
                yield from iter_schema_matches(source, schema, index + 1, alternative_end, next_values, allow_trailing)
        return

    if part.kind == "eof":
        end = skip_c_whitespace(source, pos)
        if end != len(source):
            return
        yield from iter_schema_matches(source, schema, index + 1, end, values, allow_trailing)
        return

    capture_positions = iter_capture_end_positions(source, pos)

    for capture_end in capture_positions:
        captured = source[pos:capture_end].strip()
        next_values = values.copy()
        next_values[part.value] = captured
        yield from iter_schema_matches(source, schema, index + 1, capture_end, next_values, allow_trailing)


def match_schema_literal(source: str, start: int, literal: str) -> int | None:
    i = start
    j = 0
    literal_is_space_only = literal != "" and all(ch == " " for ch in literal)

    while j < len(literal):
        if literal[j] == " ":
            while j < len(literal) and literal[j] == " ":
                j += 1
            next_i = skip_c_whitespace(source, i)
            if literal_is_space_only and next_i == i:
                return None
            i = next_i
            continue

        if literal[j] in "\n\r\t":
            if i >= len(source) or source[i] != literal[j]:
                return None
            i += 1
            j += 1
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


def match_c_keyword(source: str, start: int, keyword: str) -> bool:
    end = start + len(keyword)
    if not source.startswith(keyword, start):
        return False
    if start > 0 and (source[start - 1].isalnum() or source[start - 1] == "_"):
        return False
    if end < len(source) and (source[end].isalnum() or source[end] == "_"):
        return False
    return True


def top_level_item_end(source: str, start: int) -> int:
    brace_depth = 0
    bracket_depth = 0
    paren_depth = 0
    in_string = False
    in_char = False
    escaped = False
    saw_top_level_block = False
    starts_with_do = source[skip_c_whitespace(source, start):].startswith("do")
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
            i += 1
            continue
        if ch == "'":
            in_char = True
            i += 1
            continue

        if ch == "{":
            brace_depth += 1
            if brace_depth == 1 and bracket_depth == 0 and paren_depth == 0:
                saw_top_level_block = True
            i += 1
            continue
        if ch == "}":
            brace_depth -= 1
            i += 1
            if brace_depth == 0 and bracket_depth == 0 and paren_depth == 0:
                probe = skip_c_whitespace(source, i)
                if probe < len(source) and source[probe] == "{":
                    i = probe
                    continue
                if match_c_keyword(source, probe, "else") or match_c_keyword(source, probe, "catch") or match_c_keyword(source, probe, "finally"):
                    i = probe
                    continue
                if starts_with_do and match_c_keyword(source, probe, "while"):
                    i = probe
                    continue
                if probe < len(source) and source[probe] == ";":
                    return probe + 1
                return i
            continue
        if ch == "[":
            bracket_depth += 1
            i += 1
            continue
        if ch == "]":
            bracket_depth -= 1
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            paren_depth -= 1
            i += 1
            continue

        if brace_depth == 0 and bracket_depth == 0 and paren_depth == 0:
            if ch == ";":
                return i + 1
            if saw_top_level_block and not ch.isspace():
                return i

        i += 1

    return i


def iter_top_level_items(source: str):
    pos = skip_c_whitespace(source, 0)
    while pos < len(source):
        end = top_level_item_end(source, pos)
        if end <= pos:
            raise ValueError("Top-level item parser made no progress")
        yield source[pos:end].strip()
        pos = skip_c_whitespace(source, end)


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
        tokenized = [output_target_display_name(target).split("_") for target in pass_def.instance_targets]
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


def stable_json_dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def parse_output_target(target: str) -> tuple[str, str] | None:
    if target.startswith("out."):
        return ("header", target[4:])
    if target.startswith("source."):
        return ("source", target[7:])
    return None


def output_target_key(target: str) -> str:
    spec = parse_output_target(target)
    if spec is None:
        return target
    kind, name = spec
    return f"{kind}:{name}"


def output_target_display_name(target_or_key: str) -> str:
    spec = parse_output_target(target_or_key)
    if spec is not None:
        return spec[1]
    if ":" in target_or_key:
        return target_or_key.split(":", 1)[1]
    return target_or_key


def rel_output_path_for_target(
    target: str,
    generated_header_root: str,
    generated_header_prefix: str,
    generated_source_root: str,
    generated_source_prefix: str,
) -> Path:
    spec = parse_output_target(target)
    if spec is None:
        return Path(target)
    kind, name = spec
    if kind == "header":
        rel_output = Path(f"{generated_header_prefix}{name}.h")
        if generated_header_root:
            rel_output = Path(generated_header_root) / rel_output
        return rel_output
    rel_output = Path(f"{generated_source_prefix}{name}.cpp")
    if generated_source_root:
        rel_output = Path(generated_source_root) / rel_output
    return rel_output


def collect_declared_output_targets(instance_ops: list[InstanceOp]) -> list[str]:
    targets: list[str] = []
    for op in iter_instance_ops(instance_ops):
        if op.kind == "emit" and op.target is not None and parse_output_target(op.target) is not None:
            targets.append(op.target)
        elif op.kind == "assign" and op.source_target is not None and parse_output_target(op.source_target) is not None:
            targets.append(op.source_target)
        elif op.kind == "call" and op.output_targets is not None:
            for target in op.output_targets:
                if parse_output_target(target) is not None:
                    targets.append(target)
    return list(dict.fromkeys(targets))


def target_is_allowed(target: str, declared_aliases: set[str]) -> bool:
    return parse_output_target(target) is not None or target in declared_aliases


def resolve_output_sink(
    target: str,
    local_output_bindings: dict[str, list[str]],
    global_accs: dict[str, list[str]],
) -> list[str]:
    if target in local_output_bindings:
        return local_output_bindings[target]
    if parse_output_target(target) is not None:
        normalized = output_target_key(target)
        return global_accs.setdefault(normalized, [])
    raise ValueError(
        f"Unknown output target {target!r}; use a declared named-pass output parameter or a global output like 'out.<name>' or 'source.<name>'"
    )


def call_pass_func(
    func_def: FuncDef,
    arg_values: list[str],
    fields: dict[str, str],
    counters: dict,
    helper_functions: dict[str, object],
) -> str:
    if len(arg_values) != len(func_def.params):
        raise ValueError(f"func {func_def.name} expects {len(func_def.params)} args, got {len(arg_values)}")

    local_fields = fields.copy()
    for param, value in zip(func_def.params, arg_values):
        local_fields[param] = value

    local_func_defs = {
        name: value
        for name, value in helper_functions.items()
        if isinstance(value, FuncDef)
    }

    local_output_bindings = {"out": []}
    execute_instance_ops(
        PassDef(
            name=func_def.name,
            callable_name=None,
            block_keyword=func_def.name,
            schema=[],
            init_vars={},
            output_params=[],
            instance_targets=[],
            instance_ops=func_def.instance_ops,
            is_helper=True,
            local_func_defs=local_func_defs,
        ),
        local_fields,
        counters,
        {},
        local_output_bindings,
        {},
        None,
        None,
    )
    return "".join(local_output_bindings["out"])


def render_pass_func_expr(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str | None:
    m = re.fullmatch(r"([A-Za-z_]\w*)\((.*)\)", expr.strip())
    if m is None:
        return None
    func_name, args_text = m.groups()
    func_def = helper_functions.get(func_name)
    if not isinstance(func_def, FuncDef):
        return None
    arg_exprs = [part for part in split_top_level(args_text, ",")] if args_text.strip() else []
    arg_values = [render_expr(arg_expr, fields, counters, helper_functions) for arg_expr in arg_exprs]
    return call_pass_func(func_def, arg_values, fields, counters, helper_functions)


def render_python_expr(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str | None:
    def post_inc(name: str):
        value = counters.get(name, 0)
        counters[name] = value + 1
        return value

    def identifier_suffix(value) -> str:
        text = str(value)
        text = re.sub(r"[^A-Za-z0-9]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text or "0"

    translated = re.sub(r"\b([A-Za-z_]\w*)\+\+", lambda m: f'__post_inc__("{m.group(1)}")', expr)
    safe_builtins = {
        "abs": abs,
        "bool": bool,
        "float": float,
        "int": int,
        "len": len,
        "max": max,
        "min": min,
        "range": range,
        "str": str,
    }
    scope = {}
    scope.update(counters)
    scope.update(fields)
    for field_value in fields.values():
        if isinstance(field_value, str) and re.fullmatch(r"[A-Za-z_]\w*", field_value):
            scope.setdefault(field_value, field_value)
    scope.update(helper_functions)
    scope.update(safe_builtins)
    scope["identifier_suffix"] = identifier_suffix
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

    pass_func_value = render_pass_func_expr(expr, fields, counters, helper_functions)
    if pass_func_value is not None:
        return pass_func_value

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
        f, op, cmp, cmp_is_ref, true_body, false_body = condition
        cmp_value = resolve_condition_operand(cmp, cmp_is_ref, fields, counters, helper_functions)
        cond = fields.get(f, "") == cmp_value
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
        f, op, cmp, cmp_is_ref, true_expr, false_expr = ternary
        cmp_value = resolve_condition_operand(cmp, cmp_is_ref, fields, counters, helper_functions)
        cond = fields.get(f, "") == cmp_value
        if op == "!=":
            cond = not cond
        return render_expr(true_expr if cond else false_expr, fields, counters, helper_functions)
    return render_expr(expr, fields, counters, helper_functions)


def parse_ternary(expr: str) -> tuple[str, str, str, bool, str, str] | None:
    m = re.match(r'(\w+)\s*(==|!=)\s*(?:"([^"]*?)"|([A-Za-z_]\w*))\s*\?', expr)
    if not m:
        return None

    true_start = m.end()
    colon = find_top_level(expr, ":", true_start)
    if colon is None:
        return None

    return (
        m.group(1),
        m.group(2),
        m.group(3) if m.group(3) is not None else m.group(4),
        m.group(3) is None and m.group(4) is not None,
        expr[true_start:colon].strip(),
        expr[colon + 1:].strip(),
    )


def parse_prefix_condition(expr: str) -> tuple[str, str, str, bool, str, str] | None:
    m = re.match(r'if\s+(\w+)\s*(==|!=)\s*(?:"([^"]*?)"|([A-Za-z_]\w*))\s*\{', expr)
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
        m.group(3) if m.group(3) is not None else m.group(4),
        m.group(3) is None and m.group(4) is not None,
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
    def find_call_end(source: str, open_index: int) -> int | None:
        depth = 0
        in_string = False
        escaped = False

        for idx in range(open_index, len(source)):
            ch = source[idx]
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
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return idx + 1
        return None

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

        m = re.match(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\+\+)?', expr[i:])
        if not m:
            return None

        token = m.group(0)
        if i + len(token) + 1 < len(expr) and expr[i + len(token):i + len(token) + 2] == "++":
            token += "++"
        if token.endswith("++"):
            pieces.append(render_expr(token, fields, counters, helper_functions))
            i += len(token)
            saw_token = True
            continue

        token_end = i + len(token)
        if token_end < len(expr) and expr[token_end] == "(":
            call_end = find_call_end(expr, token_end)
            if call_end is None:
                return None
            value = render_pass_func_expr(expr[i:call_end], fields, counters, helper_functions)
            if value is None:
                value = render_python_expr(expr[i:call_end], fields, counters, helper_functions)
            if value is None:
                return None
            pieces.append(value)
            i = call_end
            saw_token = True
            continue

        if "." in token:
            value = render_python_expr(token, fields, counters, helper_functions)
            if value is None:
                return None
            pieces.append(value)
        else:
            value = render_variable(token, fields, counters, helper_functions)
            if value is None:
                return None
            pieces.append(value)
        i += len(token)
        saw_token = True

    if not saw_token or len(pieces) < 2:
        return None

    return "".join(pieces)


def render_variable(
    token: str,
    fields: dict[str, str],
    counters: dict,
    helper_functions: dict[str, object] | None = None,
) -> str | None:
    if token in {"if", "else", "return"}:
        return None
    if token in fields:
        return fields[token]
    if token in counters:
        return str(counters[token])
    if helper_functions and token in helper_functions:
        value = helper_functions[token]
        if isinstance(value, (str, int, float, bool, SymbolicExpr)):
            return str(value)
    return None


def resolve_condition_operand(
    token: str,
    is_ref: bool,
    fields: dict[str, str],
    counters: dict,
    helper_functions: dict[str, object],
) -> str:
    if not is_ref:
        return token
    value = render_variable(token, fields, counters, helper_functions)
    if value is None:
        raise ValueError(
            f"Unknown condition symbol {token!r}; quote string literals explicitly, for example \"{token}\""
        )
    return value


def render_concat_atom(expr: str, fields: dict[str, str], counters: dict, helper_functions: dict[str, object]) -> str | None:
    expr = expr.strip()
    if expr.endswith("++"):
        return render_expr(expr, fields, counters, helper_functions)
    if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"':
        return render_value(expr, fields, counters, helper_functions)
    value = render_variable(expr, fields, counters, helper_functions)
    if value is not None:
        return value
    pass_func_value = render_pass_func_expr(expr, fields, counters, helper_functions)
    if pass_func_value is not None:
        return pass_func_value
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
    if rendered is not None:
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
    global_pass_instances: dict[str, list[dict[str, str]]] | None = None,
    helper_call_stack: list[tuple[str, str]] | None = None,
    external_pass_defs: dict[str, PassDef] | None = None,
    external_pass_index_bases: dict[str, int] | None = None,
    external_pass_generated_counts: dict[str, int] | None = None,
) -> None:
    helper_functions: dict[str, object] = dict(pass_def.init_vars)
    helper_functions.update(pass_def.local_func_defs)

    def template_fields() -> dict[str, str]:
        scoped = fields.copy()
        for alias_name, sink in local_output_bindings.items():
            scoped[alias_name] = "".join(sink)
        return scoped

    for op in pass_def.instance_ops:
        if op.kind == "var":
            if op.alias_name is not None:
                local_output_bindings[op.alias_name] = []
            continue

        if op.kind == "assign":
            if op.alias_name is None or op.source_target is None:
                continue
            local_output_bindings[op.alias_name] = resolve_output_sink(op.source_target, local_output_bindings, global_accs)
            continue

        if op.kind == "emit":
            if op.target is None or op.template is None:
                continue
            rendered = render_template(op.template, template_fields(), counters, helper_functions)
            sink = resolve_output_sink(op.target, local_output_bindings, global_accs)
            sink.append(rendered)
            continue

        if op.kind == "call":
            if op.helper_name is None or op.input_expr is None or op.output_targets is None:
                continue
            helper_def = helper_defs.get(op.helper_name)
            if helper_def is None:
                raise ValueError(f"Unknown local pass {op.helper_name!r}")
            if len(op.output_targets) != len(helper_def.output_params):
                raise ValueError(
                    f"Local pass {op.helper_name} expects {len(helper_def.output_params)} outputs, got {len(op.output_targets)}"
                )
            bound_outputs = {
                param: resolve_output_sink(target, local_output_bindings, global_accs)
                for param, target in zip(helper_def.output_params, op.output_targets)
            }
            if op.input_expr.startswith("@pass:"):
                pass_id = op.input_expr[len("@pass:"):].strip()
                if global_pass_instances is None:
                    raise ValueError(f"Global pass instances are not available for local pass {op.helper_name}")
                source_instances = global_pass_instances.get(pass_id)
                if source_instances is None:
                    raise ValueError(f"Unknown global pass id {pass_id!r} for local pass {op.helper_name}")
                execute_pass_instance_helper(
                    helper_def,
                    source_instances,
                    fields,
                    counters,
                    helper_defs,
                    bound_outputs,
                    global_accs,
                    global_pass_instances,
                    helper_call_stack,
                    external_pass_defs,
                    external_pass_index_bases,
                    external_pass_generated_counts,
                )
            else:
                input_text = render_template(op.input_expr, template_fields(), counters, helper_functions)
                execute_named_pass(
                    helper_def,
                    input_text,
                    fields,
                    counters,
                    helper_defs,
                    bound_outputs,
                    global_accs,
                    global_pass_instances,
                    helper_call_stack,
                    external_pass_defs,
                    external_pass_index_bases,
                    external_pass_generated_counts,
                )
            continue

        if op.kind == "invoke":
            if op.helper_name is None or op.input_expr is None:
                continue
            if op.helper_name in helper_defs:
                raise ValueError(
                    f"Local pass {op.helper_name!r} requires explicit output bindings; use {op.helper_name}[...](...)"
                )
            if external_pass_defs is None:
                raise ValueError(f"External pass invocation is not available for {op.helper_name!r}")
            external_pass_def = external_pass_defs.get(op.helper_name)
            if external_pass_def is None:
                raise ValueError(f"Unknown named top-level pass {op.helper_name!r}")
            input_text = render_template(op.input_expr, template_fields(), counters, helper_functions)
            execute_named_pass(
                external_pass_def,
                input_text,
                fields,
                counters,
                helper_defs,
                {},
                global_accs,
                global_pass_instances,
                helper_call_stack,
                external_pass_defs,
                external_pass_index_bases,
                external_pass_generated_counts,
            )
            continue

        if op.kind == "if":
            if op.condition_field is None or op.condition_op is None or op.condition_value is None:
                continue
            field_value = fields.get(op.condition_field)
            if field_value is None and op.condition_field in local_output_bindings:
                field_value = "".join(local_output_bindings[op.condition_field])
            if field_value is None:
                field_value = ""
            condition_value = resolve_condition_operand(
                op.condition_value,
                op.condition_value_is_ref,
                fields,
                counters,
                helper_functions,
            )
            matches = field_value == condition_value
            if op.condition_op == "!=":
                matches = not matches
            branch_ops = op.true_ops if matches else op.false_ops
            if branch_ops:
                nested_pass = PassDef(
                    name=pass_def.name,
                    callable_name=pass_def.callable_name,
                    block_keyword=pass_def.block_keyword,
                    schema=pass_def.schema,
                    init_vars=pass_def.init_vars,
                    output_params=pass_def.output_params,
                    instance_targets=pass_def.instance_targets,
                    instance_ops=branch_ops,
                    is_helper=pass_def.is_helper,
                    local_helper_defs=pass_def.local_helper_defs,
                    local_func_defs=pass_def.local_func_defs,
                )
                execute_instance_ops(
                    nested_pass,
                    fields,
                    counters,
                    helper_defs,
                    local_output_bindings,
                    global_accs,
                    global_pass_instances,
                    helper_call_stack,
                    external_pass_defs,
                    external_pass_index_bases,
                    external_pass_generated_counts,
                )
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
    global_pass_instances: dict[str, list[dict[str, str]]] | None = None,
    helper_call_stack: list[tuple[str, str]] | None = None,
    external_pass_defs: dict[str, PassDef] | None = None,
    external_pass_index_bases: dict[str, int] | None = None,
    external_pass_generated_counts: dict[str, int] | None = None,
) -> None:
    import copy

    def clone_codegen_value(value):
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    def clone_codegen_state_dict(source: dict) -> dict:
        return {key: clone_codegen_value(value) for key, value in source.items()}

    if helper_call_stack is None:
        helper_call_stack = []

    scoped_helper_defs = dict(helper_defs)
    scoped_helper_defs.update(pass_def.local_helper_defs)

    helper_name = pass_def.name or pass_def.callable_name or "<unnamed>"
    call_signature = (helper_name, input_text)
    if call_signature in helper_call_stack:
        cycle_start = helper_call_stack.index(call_signature)
        cycle = helper_call_stack[cycle_start:] + [call_signature]
        cycle_lines = []
        for cycle_name, cycle_input in cycle:
            snippet = cycle_input[:80].replace("\n", "\\n").replace("\r", "\\r")
            cycle_lines.append(f"{cycle_name}({snippet!r})")
        raise ValueError(
            "Recursive local pass call made no progress:\n  " + "\n  ".join(cycle_lines)
        )

    helper_call_stack.append(call_signature)

    try:
        state = {}
        for key, value in pass_def.init_vars.items():
            if isinstance(value, list):
                continue
            if isinstance(value, (dict, set, tuple)):
                state[key] = clone_codegen_value(value)
            else:
                state[key] = value

        local_index = 0
        schema_capture_names = collect_schema_capture_names(pass_def.schema)

        if pass_calls_itself(pass_def):
            input_items = [input_text]
        else:
            input_items = list(iter_top_level_items(input_text))

        for item_text in input_items:
            inherited_fields = outer_fields.copy()
            for capture_name in schema_capture_names:
                inherited_fields.pop(capture_name, None)
            matched = match_schema_nodes(item_text, pass_def.schema, 0, 0, inherited_fields, allow_trailing=True)
            if matched is None:
                snippet = item_text[:40]
                raise ValueError(f"Local pass {pass_def.name} could not match near {snippet!r}")

            end, fields = matched
            fields = apply_missing_schema_captures(fields, pass_def.schema)
            if end <= 0:
                if skip_c_whitespace(item_text, 0) == len(item_text):
                    continue
                raise ValueError(f"Local pass {pass_def.name} made no progress")

            counters = clone_codegen_state_dict(state)
            counters.update(outer_counters)
            external_index_base = None
            if (
                not pass_def.is_helper and
                pass_def.callable_name is not None and
                external_pass_index_bases is not None
            ):
                external_index_base = external_pass_index_bases.get(pass_def.callable_name)
            generated_offset = 0
            if (
                not pass_def.is_helper and
                pass_def.callable_name is not None and
                external_pass_generated_counts is not None
            ):
                generated_offset = external_pass_generated_counts.get(pass_def.callable_name, 0)
                # Reserve the current generated slot before executing the item so
                # recursive named-pass calls allocate indices after this node
                # instead of colliding with it.
                external_pass_generated_counts[pass_def.callable_name] = generated_offset + 1

            if pass_def.is_helper or "index" not in outer_counters:
                counters["index"] = local_index
            elif external_index_base is not None:
                counters["index"] = external_index_base + generated_offset
            elif local_index == 0:
                counters["index"] = outer_counters["index"]
            else:
                counters["index"] = outer_counters["index"] + local_index
            execute_instance_ops(
                pass_def,
                fields,
                counters,
                scoped_helper_defs,
                output_bindings,
                global_accs,
                global_pass_instances,
                helper_call_stack,
                external_pass_defs,
                external_pass_index_bases,
                external_pass_generated_counts,
            )
            local_index += 1
    finally:
        helper_call_stack.pop()


def execute_pass_instance_helper(
    pass_def: PassDef,
    source_instances: list[dict[str, str]],
    outer_fields: dict[str, str],
    outer_counters: dict,
    helper_defs: dict[str, PassDef],
    output_bindings: dict[str, list[str]],
    global_accs: dict[str, list[str]],
    global_pass_instances: dict[str, list[dict[str, str]]] | None = None,
    helper_call_stack: list[tuple[str, str]] | None = None,
    external_pass_defs: dict[str, PassDef] | None = None,
    external_pass_index_bases: dict[str, int] | None = None,
    external_pass_generated_counts: dict[str, int] | None = None,
) -> None:
    import copy

    def clone_codegen_value(value):
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    def clone_codegen_state_dict(source: dict) -> dict:
        return {key: clone_codegen_value(value) for key, value in source.items()}

    state = {}
    for key, value in pass_def.init_vars.items():
        if isinstance(value, list):
            continue
        if isinstance(value, (dict, set, tuple)):
            state[key] = clone_codegen_value(value)
        else:
            state[key] = value

    for local_index, instance_fields in enumerate(source_instances):
        fields = outer_fields.copy()
        fields.update(instance_fields)
        counters = clone_codegen_state_dict(state)
        counters.update(outer_counters)
        counters["index"] = local_index
        execute_instance_ops(
            pass_def,
            fields,
            counters,
            helper_defs,
            output_bindings,
            global_accs,
            global_pass_instances,
            helper_call_stack,
            external_pass_defs,
            external_pass_index_bases,
            external_pass_generated_counts,
        )


def render_fragments(
    pass_def: PassDef,
    instances: list[dict[str, str]],
    helper_defs: dict[str, PassDef],
    index_base_expr: str | None = None,
    global_pass_instances: dict[str, list[dict[str, str]]] | None = None,
    external_pass_defs: dict[str, PassDef] | None = None,
    external_pass_index_bases: dict[str, int] | None = None,
    external_pass_generated_counts: dict[str, int] | None = None,
) -> dict[str, str]:
    import copy

    def clone_codegen_value(value):
        try:
            return copy.deepcopy(value)
        except Exception:
            return value
    
    def clone_codegen_state_dict(source: dict) -> dict:
        return {key: clone_codegen_value(value) for key, value in source.items()}

    state = {}
    for key, value in pass_def.init_vars.items():
        if isinstance(value, list):
            continue
        if isinstance(value, (dict, set, tuple)):
            state[key] = clone_codegen_value(value)
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
        execute_instance_ops(pass_def, fields, counters, helper_defs, {}, accs, global_pass_instances, None, external_pass_defs, external_pass_index_bases, external_pass_generated_counts)

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


def write_stamp_if_changed(stamp_path: Path | None, token: str) -> bool:
    if stamp_path is None:
        return False
    content = token.rstrip() + "\n"
    return write_text_if_changed(stamp_path, content)


def delete_file_if_exists(path: Path) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False


def compile_pass_inventory(
    passes_dir: Path,
    generated_header_root: str,
    generated_header_prefix: str,
    generated_source_root: str,
    generated_source_prefix: str,
    source_suffixes: tuple[str, ...],
    defines: dict[str, str],
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
    callable_names: dict[str, Path] = {}
    for block in pass_blocks:
        pass_def = compile_pass(preprocess_pass_text(block.text, defines, block.file), block.file)
        rel_file = block.file.relative_to(passes_dir)
        pass_name = sanitize_path_token(rel_file.stem)
        pass_id = pass_name
        outputs = []
        output_targets = collect_declared_output_targets(pass_def.instance_ops)
        for target in output_targets:
            rel_output = rel_output_path_for_target(
                target,
                generated_header_root,
                generated_header_prefix,
                generated_source_root,
                generated_source_prefix,
            )
            outputs.append(rel_output.as_posix())

        if pass_def.callable_name is not None:
            existing_file = callable_names.get(pass_def.callable_name)
            if existing_file is not None:
                raise ValueError(
                    f"Duplicate named top-level pass {pass_def.callable_name!r} in {existing_file} and {block.file}"
                )
            callable_names[pass_def.callable_name] = block.file

        inventory.append({
            "id": pass_id,
            "callable_name": pass_def.callable_name,
            "defined_in": rel_file.as_posix(),
            "source_file": str(block.file),
            "block_index_in_file": 0,
            "folder": pass_name,
            "outputs": outputs,
            "output_targets": output_targets,
            "pass_text": block.text.strip(),
            "defines": serialize_defines(defines),
            "local_pass_count": len(pass_def.local_helper_defs),
        })

    return inventory


def write_pass_descriptor(out_path: Path, entry: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "id": entry["id"],
        "callable_name": entry.get("callable_name"),
        "defined_in": entry["defined_in"],
        "source_file": entry["source_file"],
        "block_index_in_file": entry["block_index_in_file"],
        "folder": entry["folder"],
        "outputs": entry["outputs"],
        "output_targets": entry.get("output_targets", []),
        "pass_text": entry["pass_text"],
        "defines": entry.get("defines", []),
        "local_pass_count": entry["local_pass_count"],
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
    raw_blocks: list[MarkerBlock] = []
    positions = marker_positions(source)
    for start in positions:
        end = block_end(source, start)
        text = source[start + MARKER_LEN:end]
        raw_blocks.append(MarkerBlock(file=file, start=start, end=end, text=text))
    return expand_builtin_blocks(raw_blocks), raw_blocks.copy(), source


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
        defines = parse_define_args(entry.get("defines", []))
        preprocessed_pass_text = preprocess_pass_text(pass_text, defines, Path(source_file))
        pass_def = compile_pass(preprocessed_pass_text, Path(source_file))
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


def pass_count_rel_path(pass_id: str, rel_file: Path) -> Path:
    return Path("counts") / pass_id / f"{sanitize_path_token(rel_file)}.json"


def pass_count_path(build_root: Path, pass_id: str, rel_file: Path) -> Path:
    return build_root / pass_count_rel_path(pass_id, rel_file)


def process_state_rel_path(rel_file: Path) -> Path:
    return Path("process_state") / f"{sanitize_path_token(rel_file)}.json"


def process_state_path(build_root: Path, rel_file: Path) -> Path:
    return build_root / process_state_rel_path(rel_file)


def load_process_state(build_root: Path, rel_file: Path) -> dict | None:
    state_path = process_state_path(build_root, rel_file)
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return None


def write_process_state(build_root: Path, rel_file: Path, state: dict) -> None:
    state_path = process_state_path(build_root, rel_file)
    if write_text_if_changed(state_path, json.dumps(state, indent=2) + "\n"):
        print(f"Written: {state_path}")


def zero_count_map(pass_ids: list[str]) -> dict[str, int]:
    return {pass_id: 0 for pass_id in pass_ids}


def build_loaded_pass_descriptor_fingerprint(loaded_passes: list[tuple[dict, PassDef]]) -> str:
    descriptor_entries = []
    for entry, _ in loaded_passes:
        descriptor_entries.append({
            "id": entry["id"],
            "outputs": entry.get("outputs", []),
            "output_targets": entry.get("output_targets", []),
            "pass_text": entry.get("pass_text", ""),
            "defines": entry.get("defines", []),
            "local_pass_count": entry.get("local_pass_count", 0),
        })
    return hash_text(stable_json_dumps(descriptor_entries))


def build_process_state_fingerprint(
    rel_file: Path,
    source_text: str,
    loaded_pass_fingerprint: str,
    prefix_state_fingerprint: str,
) -> str:
    payload = {
        "file": rel_file.as_posix(),
        "source_sha1": hash_text(source_text),
        "passes_sha1": loaded_pass_fingerprint,
        "prefix_state_sha1": prefix_state_fingerprint,
    }
    return hash_text(stable_json_dumps(payload))


def build_next_prefix_state_fingerprint(prefix_state_fingerprint: str, local_count_map: dict[str, int]) -> str:
    payload = {
        "prefix_state_sha1": prefix_state_fingerprint,
        "local_counts": local_count_map,
    }
    return hash_text(stable_json_dumps(payload))


def build_count_fingerprint(count_map: dict[str, int]) -> str:
    normalized = {name: int(count_map.get(name, 0)) for name in sorted(count_map)}
    return hash_text(stable_json_dumps(normalized))


def process_file_expected_outputs(
    loaded_passes: list[tuple[dict, PassDef]],
    rel_file: Path,
    shared_out_path: Path,
    output_root: Path,
    build_root: Path,
) -> list[Path]:
    expected = [shared_out_path]
    for entry, _ in loaded_passes:
        for rel_output in entry.get("outputs", []):
            expected.append(output_root / fragment_header_rel_output_path(entry, rel_output, rel_file))
        expected.append(pass_count_path(build_root, entry["id"], rel_file))
    expected.append(process_state_path(build_root, rel_file))
    return expected


def read_instance_count(build_root: Path, pass_id: str, rel_file: Path) -> int:
    count_path = pass_count_path(build_root, pass_id, rel_file)
    if not count_path.exists():
        return 0
    try:
        data = json.loads(count_path.read_text())
        return int(data.get("count", 0))
    except Exception:
        return 0


def compute_index_base(
    entry: dict,
    build_root: Path,
    rel_file: Path,
    rel_source_files: list[Path],
) -> int:
    total = 0
    for candidate in rel_source_files:
        if candidate == rel_file:
            break
        total += read_instance_count(build_root, entry["id"], candidate)
    return total


def output_key_from_rel_output(rel_output: str) -> str:
    rel_path = Path(rel_output)
    if rel_path.suffix == ".cpp":
        return f"source:{rel_path.stem}"
    return f"header:{rel_path.stem}"


def build_output_owner_map(loaded_passes: list[tuple[dict, PassDef]]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for entry, _ in loaded_passes:
        output_targets = entry.get("output_targets", [])
        outputs = entry.get("outputs", [])
        if output_targets and len(output_targets) != len(outputs):
            raise ValueError(
                f"Pass descriptor {entry['id']!r} has mismatched outputs/output_targets lengths: {len(outputs)} vs {len(output_targets)}"
            )
        for index, rel_output in enumerate(outputs):
            output_key = (
                output_target_key(output_targets[index])
                if index < len(output_targets)
                else output_key_from_rel_output(rel_output)
            )
            existing_owner = owners.get(output_key)
            if existing_owner is not None and existing_owner != entry["id"]:
                raise ValueError(
                    f"Fragment output {output_key!r} is owned by multiple passes: {existing_owner!r} and {entry['id']!r}"
                )
            owners[output_key] = entry["id"]
    return owners


def update_pass_count(build_root: Path, pass_id: str, rel_file: Path, count: int) -> None:
    count_path = pass_count_path(build_root, pass_id, rel_file)
    data = {
        "pass_id": pass_id,
        "file": rel_file.as_posix(),
        "count": int(count),
    }
    if write_text_if_changed(count_path, json.dumps(data, indent=2) + "\n"):
        print(f"Written: {count_path}")


def write_pass_file_shards(
    entry: dict,
    pass_def: PassDef,
    rel_file: Path,
    instances: list[dict[str, str]],
    output_root: Path,
    build_root: Path,
    rel_source_files: list[Path],
    global_pass_instances: dict[str, list[dict[str, str]]] | None = None,
    external_pass_defs: dict[str, PassDef] | None = None,
    rendered_fragments: dict[str, str] | None = None,
    instance_count_override: int | None = None,
) -> list[Path]:
    output_targets = entry.get("output_targets", collect_declared_output_targets(pass_def.instance_ops))
    written_paths: list[Path] = []
    outputs = entry.get("outputs", [])
    if len(outputs) != len(output_targets):
        raise ValueError(
            f"Manifest outputs for pass {entry['id']} do not match output target count: {len(outputs)} vs {len(output_targets)}"
        )

    if rendered_fragments is None:
        index_base = compute_index_base(entry, build_root, rel_file, rel_source_files)
        rendered_fragments = render_fragments(
            pass_def,
            instances,
            pass_def.local_helper_defs,
            str(index_base),
            global_pass_instances,
            external_pass_defs,
        )

    for rel_output, target in zip(outputs, output_targets):
        out_path = output_root / fragment_header_rel_output_path(entry, rel_output, rel_file)
        content = rendered_fragments.get(output_target_key(target), "").rstrip()
        if content:
            content += "\n"
        else:
            # Keep declared fragment outputs on disk even when they are empty so
            # the build graph can treat them as satisfied byproducts.
            content = "#pragma once\n"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if write_text_if_changed(out_path, content):
            print(f"Written: {out_path}")
        written_paths.append(out_path)

    update_pass_count(build_root, entry["id"], rel_file, len(instances) if instance_count_override is None else instance_count_override)

    return written_paths


def write_public_output_headers(entries: list[dict], output_root: Path) -> list[Path]:
    written_paths: list[Path] = []
    outputs_to_entries: dict[str, list[dict]] = {}
    for entry in entries:
        for rel_output in entry.get("outputs", []):
            outputs_to_entries.setdefault(rel_output, []).append(entry)

    for rel_output, output_entries in outputs_to_entries.items():
        out_path = aggregate_output_path(output_root, rel_output)
        lines: list[str] = []
        if out_path.suffix == ".h":
            lines.extend(["#pragma once", ""])
        for entry in output_entries:
            lines.append(f'#include "{pass_header_rel_output_path(entry, rel_output).as_posix()}"')
        content = "\n".join(lines).rstrip()
        if content:
            content += "\n"
        elif out_path.suffix == ".h":
            content = "#pragma once\n"
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
#define $local struct
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
    parser.add_argument("--stamp", type=Path, default=None, help="Optional stamp file updated only when compiled pass outputs change")
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
        "--generated-source-prefix",
        default="",
        help="Optional prefix added to generated public source filenames; defaults to the header prefix",
    )
    parser.add_argument(
        "--generated-source-root",
        default="",
        help="Optional directory prefix for generated public source files, e.g. g; defaults to the header root",
    )
    parser.add_argument(
        "--source-suffix",
        dest="source_suffixes",
        action="append",
        default=list(DEFAULT_SOURCE_SUFFIXES),
        help="File suffix to scan; can be provided multiple times",
    )
    parser.add_argument(
        "--define",
        dest="defines",
        action="append",
        default=[],
        help="Boolean or valued define made available to pass preprocessing, e.g. NAME or NAME=1",
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
    parser.add_argument("--stamp", type=Path, default=None, help="Optional stamp file updated only when this file changes downstream-visible output")
    parser.add_argument(
        "--define",
        dest="defines",
        action="append",
        default=[],
        help="Accepted for CLI symmetry; pass descriptors remain the source of truth",
    )
    return parser.parse_args(argv)


def parse_assemble_pass_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rewrite aggregate generated headers for one pass."
    )
    parser.add_argument("--build-root", required=True, type=Path, help="Directory that contains generated pass descriptors")
    parser.add_argument("--pass-id", required=True, help="Pass id to assemble")
    parser.add_argument("--shared-root", required=True, type=Path, help="Root directory of shared source files")
    parser.add_argument("--output-root", required=True, type=Path, help="Root directory for assembled generated headers")
    parser.add_argument("--stamp", type=Path, default=None, help="Optional stamp file updated only when assembled outputs change")
    parser.add_argument(
        "--source-suffix",
        dest="source_suffixes",
        action="append",
        default=list(DEFAULT_SOURCE_SUFFIXES),
        help="File suffix to scan; can be provided multiple times",
    )
    parser.add_argument(
        "--define",
        dest="defines",
        action="append",
        default=[],
        help="Accepted for CLI symmetry; pass descriptors remain the source of truth",
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
            generated_source_prefix="",
            generated_source_root="",
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
        "--generated-source-prefix",
        default="",
        help="Optional prefix added to generated public source filenames; defaults to the header prefix",
    )
    parser.add_argument(
        "--generated-source-root",
        default="",
        help="Optional directory under the output root where generated public source files are organized; defaults to the header root",
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
    parser.add_argument(
        "--define",
        dest="defines",
        action="append",
        default=[],
        help="Boolean or valued define made available to pass preprocessing, e.g. NAME or NAME=1",
    )
    args = parser.parse_args(argv)
    args.command = "generate-all"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    define_map = parse_define_args(getattr(args, "defines", []))
    generated_source_root = getattr(args, "generated_source_root", "") or getattr(args, "generated_header_root", "")
    generated_source_prefix = getattr(args, "generated_source_prefix", "") or getattr(args, "generated_header_prefix", "")
    if args.command == "compile-passes":
        source_suffixes = tuple(dict.fromkeys(args.source_suffixes))
        entries = compile_pass_inventory(
            args.passes_dir,
            args.generated_header_root,
            args.generated_header_prefix,
            generated_source_root,
            generated_source_prefix,
            source_suffixes,
            define_map,
        )
        remove_stale_pass_artifacts(args.build_root.resolve(), {entry["id"] for entry in entries})
        for entry in entries:
            descriptor_path = args.build_root / f"pass_{entry['id']}.json"
            write_pass_descriptor(descriptor_path, entry)
        if args.shared_dir is not None and args.output_root is not None:
            write_public_output_headers(entries, args.output_root.resolve())
        if args.stamp is not None:
            write_stamp_if_changed(args.stamp.resolve(), hash_text(stable_json_dumps(entries)))
        return 0
    if args.command == "process-file":
        shared_root = args.shared_root.resolve()
        input_file = args.input.resolve()
        build_root = args.build_root.resolve()
        shared_output_root = args.shared_output_root.resolve()
        rel_source_files = [
            file.relative_to(shared_root)
            for file in iter_source_files(shared_root, tuple(DEFAULT_SOURCE_SUFFIXES))
        ]
        rel_from_shared_parent = input_file.relative_to(shared_root.parent)
        rel_from_shared_root = input_file.relative_to(shared_root)
        loaded_passes = load_pass_defs_from_build_root(build_root)
        blocks, strip_blocks, source = discover_blocks_in_file(input_file)

        pass_ids = [entry["id"] for entry, _ in loaded_passes]
        pass_defs = {entry["id"]: pass_def for entry, pass_def in loaded_passes}
        external_pass_defs = {
            pass_def.callable_name: pass_def
            for _, pass_def in loaded_passes
            if pass_def.callable_name is not None
        }
        instances_by_pass = {entry["id"]: [] for entry, _ in loaded_passes}

        for block in blocks:
            stripped = block.text.lstrip()
            if stripped.startswith("pass"):
                block.replacement = ""
                continue

            pass_id, values = identify_pass(block, pass_defs)
            instances_by_pass[pass_id].append(values)

        source_file_index = rel_source_files.index(rel_from_shared_root)
        prev_rel_file = rel_source_files[source_file_index - 1] if source_file_index > 0 else None
        prefix_count_by_pass_id = zero_count_map(pass_ids)
        prefix_state_fingerprint = build_count_fingerprint(prefix_count_by_pass_id)
        if prev_rel_file is not None:
            prev_state = load_process_state(build_root, prev_rel_file)
            if prev_state is not None:
                for pass_id in pass_ids:
                    prefix_count_by_pass_id[pass_id] = int(prev_state.get("cumulative_counts", {}).get(pass_id, 0))
                prefix_state_fingerprint = build_count_fingerprint(prefix_count_by_pass_id)
            else:
                for entry, _ in loaded_passes:
                    prefix_count_by_pass_id[entry["id"]] = compute_index_base(entry, build_root, rel_from_shared_root, rel_source_files)
                prefix_state_fingerprint = build_count_fingerprint(prefix_count_by_pass_id)

        loaded_pass_fingerprint = build_loaded_pass_descriptor_fingerprint(loaded_passes)
        current_input_fingerprint = build_process_state_fingerprint(
            rel_from_shared_root,
            source,
            loaded_pass_fingerprint,
            prefix_state_fingerprint,
        )
        shared_out_path = shared_output_root / rel_from_shared_parent
        current_state = load_process_state(build_root, rel_from_shared_root)
        uses_global_instances = any(
            pass_uses_global_pass_instances(pass_def)
            for _, pass_def in loaded_passes
        )
        if (
            not uses_global_instances and
            current_state is not None and
            current_state.get("input_fingerprint") == current_input_fingerprint and
            all(path.exists() for path in process_file_expected_outputs(
                loaded_passes,
                rel_from_shared_root,
                shared_out_path,
                shared_output_root,
                build_root,
            ))
        ):
            if args.stamp is not None:
                write_stamp_if_changed(args.stamp.resolve(), current_state.get("state_fingerprint", current_input_fingerprint))
            return 0

        external_pass_generated_counts: dict[str, int] = {}
        output_owner_by_fragment = build_output_owner_map(loaded_passes)
        global_instances_by_pass = None
        if uses_global_instances:
            global_instances_by_pass = collect_instances_by_pass(shared_root, pass_defs, tuple(DEFAULT_SOURCE_SUFFIXES))

        external_pass_index_bases = {
            pass_def.callable_name: (
                prefix_count_by_pass_id[entry["id"]] +
                len(instances_by_pass[entry["id"]])
            )
            for entry, pass_def in loaded_passes
            if pass_def.callable_name is not None
        }

        stripped_out = strip_marker_blocks(source, strip_blocks)
        shared_out_path.parent.mkdir(parents=True, exist_ok=True)
        if write_text_if_changed(shared_out_path, stripped_out):
            print(f"Written: {shared_out_path}")

        rendered_fragments_by_pass: dict[str, dict[str, str]] = {entry["id"]: {} for entry, _ in loaded_passes}
        for entry, pass_def in loaded_passes:
            index_base = prefix_count_by_pass_id[entry["id"]]
            rendered = render_fragments(
                pass_def,
                instances_by_pass[entry["id"]],
                pass_def.local_helper_defs,
                str(index_base),
                global_instances_by_pass,
                external_pass_defs,
                external_pass_index_bases,
                external_pass_generated_counts,
            )
            for fragment_name, content in rendered.items():
                owner_pass_id = output_owner_by_fragment.get(fragment_name)
                if owner_pass_id is None:
                    raise ValueError(
                        f"Fragment {fragment_name!r} produced while processing {entry['id']!r} has no owning pass descriptor"
                    )
                existing = rendered_fragments_by_pass[owner_pass_id].get(fragment_name, "")
                rendered_fragments_by_pass[owner_pass_id][fragment_name] = existing + content

        callable_name_to_pass_id = {
            pass_def.callable_name: entry["id"]
            for entry, pass_def in loaded_passes
            if pass_def.callable_name is not None
        }
        generated_count_by_pass_id: dict[str, int] = {}
        for callable_name, count in external_pass_generated_counts.items():
            pass_id = callable_name_to_pass_id.get(callable_name)
            if pass_id is None:
                continue
            generated_count_by_pass_id[pass_id] = generated_count_by_pass_id.get(pass_id, 0) + count

        for entry, pass_def in loaded_passes:
            write_pass_file_shards(
                entry,
                pass_def,
                rel_from_shared_root,
                instances_by_pass[entry["id"]],
                shared_output_root,
                build_root,
                rel_source_files,
                global_instances_by_pass,
                external_pass_defs,
                rendered_fragments_by_pass[entry["id"]],
                len(instances_by_pass[entry["id"]]) + generated_count_by_pass_id.get(entry["id"], 0),
            )

        local_count_by_pass_id = {
            entry["id"]: len(instances_by_pass[entry["id"]]) + generated_count_by_pass_id.get(entry["id"], 0)
            for entry, _ in loaded_passes
        }
        cumulative_count_by_pass_id = {
            pass_id: prefix_count_by_pass_id.get(pass_id, 0) + local_count_by_pass_id.get(pass_id, 0)
            for pass_id in pass_ids
        }
        state_fingerprint = build_count_fingerprint(cumulative_count_by_pass_id)
        write_process_state(build_root, rel_from_shared_root, {
            "file": rel_from_shared_root.as_posix(),
            "input_fingerprint": current_input_fingerprint,
            "prefix_state_fingerprint": prefix_state_fingerprint,
            "state_fingerprint": state_fingerprint,
            "local_counts": local_count_by_pass_id,
            "cumulative_counts": cumulative_count_by_pass_id,
        })
        if args.stamp is not None:
            write_stamp_if_changed(args.stamp.resolve(), state_fingerprint)

        matched_pass_count = sum(1 for instances in instances_by_pass.values() if instances)
        total_instances = sum(len(instances) for instances in instances_by_pass.values())
        return 0
    if args.command == "assemble-pass":
        entry = load_pass_entry(args.build_root.resolve(), args.pass_id)
        source_suffixes = tuple(dict.fromkeys(args.source_suffixes))
        written_paths = write_pass_aggregate_headers(
            [entry],
            args.shared_root.resolve(),
            args.output_root.resolve(),
            source_suffixes,
        )
        if args.stamp is not None:
            stamp_payload = []
            for path in written_paths:
                stamp_payload.append({
                    "path": path.as_posix(),
                    "content_sha1": hash_text(path.read_text()) if path.exists() else "",
                })
            write_stamp_if_changed(args.stamp.resolve(), hash_text(stable_json_dumps(stamp_payload)))
        return 0

    shared_dir = args.shared_dir
    output_root = args.output_root
    shared_output_root = args.shared_output_root or output_root
    stamp_path = args.stamp or (output_root / "content.stamp")
    source_suffixes = tuple(dict.fromkeys(args.source_suffixes))

    blocks, strip_map = discover_blocks(shared_dir, source_suffixes)
    pass_blocks = [block for block in blocks if block.text.lstrip().startswith("pass")]
    if not pass_blocks:
        raise ValueError(f"No $pass block found under {shared_dir}")

    pass_defs: dict[str, PassDef] = {}
    for block in pass_blocks:
        preprocessed_pass_text = preprocess_pass_text(block.text, define_map, block.file)
        pass_def = compile_pass(preprocessed_pass_text, block.file)
        key = f"__top_level__:{len(pass_defs)}"
        pass_defs[key] = pass_def
        block.replacement = ""

    local_helper_count = sum(len(pass_def.local_helper_defs) for pass_def in pass_defs.values())
    instances_by_pass = {name: [] for name in pass_defs}

    global_instances_by_pass = collect_instances_by_pass(shared_dir, pass_defs, source_suffixes)
    for pass_name, values in global_instances_by_pass.items():
        instances_by_pass[pass_name].extend(values)
    external_pass_defs = {
        pass_def.callable_name: pass_def
        for pass_def in pass_defs.values()
        if pass_def.callable_name is not None
    }
    external_pass_index_bases = {
        pass_def.callable_name: 0
        for pass_def in pass_defs.values()
        if pass_def.callable_name is not None
    }
    external_pass_generated_counts: dict[str, int] = {}

    output_root.mkdir(parents=True, exist_ok=True)
    total_instances = sum(len(instances) for instances in instances_by_pass.values())
    for name, pass_def in pass_defs.items():
        fragments = render_fragments(
            pass_def,
            instances_by_pass[name],
            pass_def.local_helper_defs,
            global_pass_instances=global_instances_by_pass,
            external_pass_defs=external_pass_defs,
            external_pass_index_bases=external_pass_index_bases,
            external_pass_generated_counts=external_pass_generated_counts,
        )
        for target in collect_declared_output_targets(pass_def.instance_ops):
            rel_output = rel_output_path_for_target(
                target,
                args.generated_header_root,
                args.generated_header_prefix,
                generated_source_root,
                generated_source_prefix,
            )
            out_path = output_root / rel_output
            content = fragments.get(output_target_key(target), "")
            if write_text_if_changed(out_path, content):
                print(f"Written: {out_path}")

    write_generated_sources(shared_dir, strip_map, shared_output_root, source_suffixes)
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text("# Generated by codegen\n")
    print(f"Written: {stamp_path}")
    if not args.no_syntax_hints:
        syntax_hints_path = args.syntax_hints or (output_root / "syntax_hints.h")
        write_syntax_hints(syntax_hints_path, [pass_def.name for pass_def in pass_defs.values() if pass_def.name])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

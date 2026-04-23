"""
aot_transpiler.py

Scans shared source files for $ markers, generates target-specific headers,
and writes cleaned copies of the shared sources into each target build folder.

Usage:
    python aot_transpiler.py <shared_dir> <gen_py_dir> <out_base_dir> <tag=file>...
"""

import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path


MARKER = "\n$"
MARKER_LEN = 1
SYNTAX_HINTS_INCLUDE = '#include "../../build/syntax_hints.h"'


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
    name_field: str
    fields: dict[str, str]
    init_vars: dict
    instance_ops: list[tuple[str, str]]


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


def parse_what_template(what_body: str) -> tuple[str, str, dict[str, str]]:
    lines = unwrap_schema_string(what_body).strip().splitlines()
    header = lines[0].strip()
    m = re.match(r'(\w+)\s+"(\w+)"\s*\{', header)
    if not m:
        raise ValueError(f"Invalid schema header: {header!r}")

    fields = {}
    for line in lines[1:]:
        line = line.strip()
        if not line or line in ("{", "}"):
            continue
        fm = re.match(r'(\w+)\s*=\s*"(\w+)"\s*;', line)
        if fm:
            fields[fm.group(1)] = fm.group(2)

    return m.group(1), m.group(2), fields


def unwrap_schema_string(schema_body: str) -> str:
    lines = []
    for raw_line in textwrap.dedent(schema_body).strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        lines.append(line)
    return "\n".join(lines)


def find_assignment_value(body: str, name: str) -> str:
    m = re.search(rf'\b{re.escape(name)}\b\s*=', body)
    if not m:
        return ""

    start = m.end()
    depth = 0
    for i in range(start, len(body)):
        ch = body[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
        elif ch == ";" and depth == 0:
            return normalize_assignment_value(body[start:i].strip())

    raise ValueError(f"Unterminated assignment for {name}")


def normalize_assignment_value(value: str) -> str:
    if not value.startswith("{") or not value.endswith("}"):
        return value

    depth = 0
    for i, ch in enumerate(value):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and i != len(value) - 1:
                return value

    return textwrap.dedent(value[1:-1]).strip() + "\n"


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

    block_keyword, name_field, fields = parse_what_template(sections["schema"])
    if block_keyword != name:
        raise ValueError(f"$pass {name} has schema block for {block_keyword}")

    return PassDef(
        name=name,
        block_keyword=block_keyword,
        name_field=name_field,
        fields=fields,
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


def discover_blocks(shared_dir: Path) -> tuple[list[MarkerBlock], dict[Path, list[MarkerBlock]]]:
    blocks = []
    strip_blocks: dict[Path, list[MarkerBlock]] = {}

    for file in sorted(shared_dir.rglob("*.h")):
        source = file.read_text()
        positions = marker_positions(source)
        for index, start in enumerate(positions):
            next_start = positions[index + 1] if index + 1 < len(positions) else len(source)
            rest = source[start + MARKER_LEN:].lstrip()
            end = block_end(source, start)

            text = source[start + MARKER_LEN:end]
            block = MarkerBlock(file=file, start=start, end=end, text=text)
            blocks.append(block)
            strip_blocks.setdefault(file, []).append(block)

    return blocks, strip_blocks


def parse_instance(block: MarkerBlock, pass_def: PassDef) -> dict[str, str]:
    pattern = re.compile(
        rf'^{pass_def.block_keyword}\s+(?P<{pass_def.name_field}>\w+)\s*{{(?P<__body>.*?)}}\s*;?\s*$',
        re.DOTALL,
    )
    m = pattern.match(block.text.strip())
    if not m:
        raise ValueError(f"Invalid ${pass_def.block_keyword} block in {block.file}")

    values = m.groupdict()
    body = values.pop("__body")
    for source_field, target_field in pass_def.fields.items():
        values[target_field] = find_assignment_value(body, source_field)
    return values


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


def render_line(template: str, fields: dict[str, str], counters: dict) -> str:
    return re.sub(r"\[(.*?)\]", lambda m: render_expr(m.group(1), fields, counters), template)


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


def strip_blocks(source: str, blocks: list[MarkerBlock]) -> str:
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


def ensure_syntax_hints_include(source: str) -> str:
    if SYNTAX_HINTS_INCLUDE in source:
        return source

    pragma = "#pragma once"
    if source.startswith(pragma):
        return source.replace(pragma, f"{pragma}\n\n{SYNTAX_HINTS_INCLUDE}", 1)

    return f"{SYNTAX_HINTS_INCLUDE}\n\n{source}"


def write_generated_sources(shared_dir: Path, strip_map: dict[Path, list[MarkerBlock]], out_map: dict[str, Path]) -> None:
    for tag, out_file in out_map.items():
        output_root = out_file.parent
        for file in sorted(shared_dir.rglob("*.h")):
            rel = file.relative_to(shared_dir.parent)
            out_path = output_root / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out = strip_blocks(file.read_text(), strip_map.get(file, []))
            out_path.write_text(ensure_syntax_hints_include(out))
            print(f"Written {tag}: {out_path}")


def write_syntax_hints(out_base: Path, pass_names: list[str]) -> Path:
    out_path = out_base / "syntax_hints.h"
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
    return out_path


def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: aot_transpiler.py <shared_dir> <gen_py_dir> <out_base_dir> tag=file ...")
        sys.exit(1)

    shared_dir = Path(sys.argv[1])
    gen_dir = Path(sys.argv[2])
    out_base = Path(sys.argv[3])
    out_map = {tag: out_base / fname for tag, fname in (arg.split("=", 1) for arg in sys.argv[4:])}

    gen_dir.mkdir(parents=True, exist_ok=True)
    gen_stamp = gen_dir / "content.py"

    blocks, strip_map = discover_blocks(shared_dir)
    pass_blocks = [block for block in blocks if block.text.lstrip().startswith("pass")]
    if not pass_blocks:
        raise ValueError(f"No $pass block found under {shared_dir}")

    pass_defs: dict[str, PassDef] = {}
    for block in pass_blocks:
        pass_def = compile_pass(block.text, block.file)
        if pass_def.name in pass_defs:
            raise ValueError(f"Duplicate $pass {pass_def.name}")
        pass_defs[pass_def.name] = pass_def
        block.replacement = f'#include "{pass_def.name}.h"'

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
        output_root = stamp_path.parent
        output_root.mkdir(parents=True, exist_ok=True)
        for name, pass_def in pass_defs.items():
            stale_wrapper = output_root / f"{pass_def.name}.h"
            if stale_wrapper.exists():
                stale_wrapper.unlink()
            fragments = render_fragments(pass_def, instances_by_pass[name])
            for fragment_name, content in fragments.items():
                out_path = output_root / f"{fragment_name}.h"
                out_path.write_text(content)
                print(f"Written {tag}: {out_path}")
        stamp_path.write_text("# Generated by aot_transpiler.py\n")
        print(f"Written {tag}: {stamp_path}")

    write_generated_sources(shared_dir, strip_map, out_map)
    write_syntax_hints(out_base, list(pass_defs))
    gen_stamp.write_text("# Generated by aot_transpiler.py\n")
    print(f"Written generator stamp: {gen_stamp}")


if __name__ == "__main__":
    main()

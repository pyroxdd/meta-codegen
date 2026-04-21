"""
aot_transpiler.py

Scans shared source files for @@ markers, generates target-specific headers,
and writes cleaned copies of the shared sources into each target build folder.

Usage:
    python aot_transpiler.py <shared_dir> <gen_py_dir> <out_base_dir> <tag=file>...
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path


MARKER = "\n@@"


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
    out_sections: dict[str, str]


def parse_pass_file(source: str) -> dict[str, str]:
    section_re = re.compile(
        r'^[ \t]*(pass|what|init|instance|out\s+\w+)\b',
        re.MULTILINE,
    )

    positions = [(m.group(0).strip(), m.start()) for m in section_re.finditer(source)]
    sections: dict[str, str] = {}

    for i, (name, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else len(source)
        body = source[start:end]

        key = name.split()[0]
        if key == "out":
            tag = name.split()[1]
            key = f"out:{tag}"

        body = re.sub(r'^[ \t]*' + re.escape(name) + r'[ \t]*\n?', '', body, count=1)
        sections[key] = body

    return sections


def parse_out_sections(sections: dict[str, str]) -> dict[str, str]:
    outs = {}
    for key, body in sections.items():
        if key.startswith("out:"):
            outs[key.split(":", 1)[1]] = body.strip()
    return outs


def parse_what_template(what_body: str) -> tuple[str, str, dict[str, str]]:
    lines = what_body.strip().splitlines()
    header = lines[0].strip()
    m = re.match(r'(\w+)\s+<<(\w+)>>\s*\{', header)
    if not m:
        raise ValueError(f"Invalid what header: {header!r}")

    fields = {}
    for line in lines[1:]:
        line = line.strip()
        if not line or line in ("{", "}"):
            continue
        fm = re.match(r'(\w+)\s*=\s*<<(\w+)>>\s*;', line)
        if fm:
            fields[fm.group(1)] = fm.group(2)

    return m.group(1), m.group(2), fields


def parse_init_section(init_body: str) -> dict:
    init_vars = {}
    for line in init_body.strip().splitlines():
        m = re.match(r'(\w+)\s*=\s*(.*)', line.strip())
        if not m:
            continue
        var, val = m.group(1), m.group(2).strip()
        if val == "[]":
            init_vars[var] = []
        elif val.isdigit():
            init_vars[var] = int(val)
        else:
            init_vars[var] = val
    return init_vars


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
    m = re.match(r"pass\s+(\w+)\s*$", first_line)
    if not m:
        raise ValueError(f"Expected @@pass <name> in {file}")
    return m.group(1)


def compile_pass(pass_text: str, file: Path) -> PassDef:
    name = pass_name(pass_text, file)
    sections = parse_pass_file(pass_text)
    missing = [name for name in ("what", "init", "instance") if name not in sections]
    if missing:
        raise ValueError(f"@@pass {name} is missing section(s): {', '.join(missing)}")

    block_keyword, name_field, fields = parse_what_template(sections["what"])
    if block_keyword != name:
        raise ValueError(f"@@pass {name} has what block for {block_keyword}")

    return PassDef(
        name=name,
        block_keyword=block_keyword,
        name_field=name_field,
        fields=fields,
        init_vars=parse_init_section(sections["init"]),
        instance_ops=parse_instance_section(sections["instance"]),
        out_sections=parse_out_sections(sections),
    )


def marker_positions(source: str) -> list[int]:
    return [m.start() + 1 for m in re.finditer(re.escape(MARKER), source)]


def instance_end(text: str, start: int) -> int:
    depth = 0
    saw_open = False
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
            saw_open = True
        elif text[i] == "}":
            depth -= 1
            if saw_open and depth == 0:
                return i + 1
    raise ValueError("Unclosed @@ instance block")


def discover_blocks(shared_dir: Path) -> tuple[list[MarkerBlock], dict[Path, list[MarkerBlock]]]:
    blocks = []
    strip_blocks: dict[Path, list[MarkerBlock]] = {}

    for file in sorted(shared_dir.rglob("*.h")):
        source = file.read_text()
        positions = marker_positions(source)
        for index, start in enumerate(positions):
            next_start = positions[index + 1] if index + 1 < len(positions) else len(source)
            rest = source[start + 2:].lstrip()
            if rest.startswith("pass"):
                end = next_start
            elif rest.startswith("end"):
                end = start + 2 + len(rest.splitlines()[0])
            else:
                end = instance_end(source, start)

            text = source[start + 2:end]
            block = MarkerBlock(file=file, start=start, end=end, text=text)
            blocks.append(block)
            strip_blocks.setdefault(file, []).append(block)

    return blocks, strip_blocks


def parse_instance(block: MarkerBlock, pass_def: PassDef) -> dict[str, str]:
    pattern = re.compile(
        rf'^{pass_def.block_keyword}\s+(?P<{pass_def.name_field}>\w+)\s*{{(?P<__body>.*?)}}\s*$',
        re.DOTALL,
    )
    m = pattern.match(block.text.strip())
    if not m:
        raise ValueError(f"Invalid @@{pass_def.block_keyword} block in {block.file}")

    values = m.groupdict()
    body = values.pop("__body")
    for source_field, target_field in pass_def.fields.items():
        field_match = re.search(rf'{source_field}\s*=\s*(.*?)\s*;', body, re.DOTALL)
        values[target_field] = field_match.group(1).strip() if field_match else ""
    return values


def render_expr(expr: str, fields: dict[str, str], counters: dict) -> str:
    expr = expr.strip()

    if expr.endswith("++"):
        var = expr[:-2].strip()
        val = counters.get(var, 0)
        counters[var] = val + 1
        return str(val)

    m = re.match(r'"([^"]*?)"\s+if\s+(\w+)\s*(==|!=)\s*"([^"]*?)"\s+else\s+"([^"]*?)"', expr)
    if m:
        t, f, op, cmp, e = m.groups()
        cond = fields.get(f, "") == cmp
        if op == "!=":
            cond = not cond
        return t if cond else e

    if expr in fields:
        return fields[expr]

    if expr in counters:
        return str(counters[expr])

    return ""


def render_line(template: str, fields: dict[str, str], counters: dict) -> str:
    return re.sub(r"<<(.*?)>>", lambda m: render_expr(m.group(1), fields, counters), template)


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


def render_outputs(pass_def: PassDef, instances: list[dict[str, str]]) -> dict[str, str]:
    import copy

    state = copy.deepcopy(pass_def.init_vars)
    counters = {k: v for k, v in state.items() if isinstance(v, int)}
    accs = {k: v for k, v in state.items() if isinstance(v, list)}

    for fields in instances:
        for var, tmpl in pass_def.instance_ops:
            rendered = render_line(tmpl, fields, counters)
            if var in accs:
                accs[var].append(rendered)
            else:
                counters[var] = rendered

    all_vars = {**counters, **accs}
    outputs = {}
    for tag, tmpl in pass_def.out_sections.items():
        out = tmpl
        for key, value in all_vars.items():
            if isinstance(value, list):
                value = "\n".join(value)
            out = out.replace(f"<<{key}>>", str(value))
        outputs[tag] = format_cpp_like(out)
    return outputs


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


def write_generated_sources(shared_dir: Path, strip_map: dict[Path, list[MarkerBlock]], out_map: dict[str, Path]) -> None:
    for tag, out_file in out_map.items():
        output_root = out_file.parent
        for file in sorted(shared_dir.rglob("*.h")):
            rel = file.relative_to(shared_dir.parent)
            out_path = output_root / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(strip_blocks(file.read_text(), strip_map.get(file, [])))
            print(f"Written {tag}: {out_path}")


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
        raise ValueError(f"No @@pass block found under {shared_dir}")

    pass_defs: dict[str, PassDef] = {}
    for block in pass_blocks:
        pass_def = compile_pass(block.text, block.file)
        if pass_def.name in pass_defs:
            raise ValueError(f"Duplicate @@pass {pass_def.name}")
        pass_defs[pass_def.name] = pass_def
        block.replacement = f'#include "{pass_def.name}.h"'

    instances_by_pass = {name: [] for name in pass_defs}

    for block in blocks:
        stripped = block.text.lstrip()
        if stripped.startswith("pass") or stripped.startswith("end"):
            continue

        marker_name = stripped.split(None, 1)[0]
        if marker_name == "end":
            continue
        pass_def = pass_defs.get(marker_name)
        if pass_def is None:
            raise ValueError(f"Unknown @@{marker_name} block in {block.file}")
        instances_by_pass[marker_name].append(parse_instance(block, pass_def))

    for name, pass_def in pass_defs.items():
        for tag, content in render_outputs(pass_def, instances_by_pass[name]).items():
            if tag not in out_map:
                raise ValueError(f"No output mapping provided for tag {tag!r}")
            out_path = out_map[tag]
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content)
            print(f"Written {tag}: {out_path}")

    write_generated_sources(shared_dir, strip_map, out_map)
    gen_stamp.write_text("# Generated by aot_transpiler.py\n")
    print(f"Written generator stamp: {gen_stamp}")


if __name__ == "__main__":
    main()

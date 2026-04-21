"""
pass_compiler.py

Compiles all .pass files from a directory into Python generators,
then runs them.

Usage:
    python pass_compiler.py <pass_file_or_dir> <gen_py_dir> <out_base_dir> <tag=filename>...

Example:
    python pass_compiler.py tile.pass build/gen_py out server=server.h client=client.h
"""

import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_pass_file(source: str) -> dict:
    section_re = re.compile(
        r'^[ \t]*(pass|where|what|init|instance|out\s+\w+)\b',
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


def parse_out_sections(sections: dict):
    outs = {}
    for key, body in sections.items():
        if key.startswith("out:"):
            tag = key.split(":", 1)[1]
            template = body.strip()
            outs[tag] = template
    return outs


def parse_what_template(what_body: str):
    lines = what_body.strip().splitlines()
    header = lines[0].strip()
    m = re.match(r'(\w+)\s+<<(\w+)>>\s*\{', header)
    if not m:
        raise ValueError(f"Invalid what header: {header!r}")
    block_keyword = m.group(1)
    name_field = m.group(2)

    fields = {}
    for line in lines[1:]:
        line = line.strip()
        if not line or line in ('{', '}'):
            continue
        fm = re.match(r'(\w+)\s*=\s*<<(\w+)>>\s*;', line)
        if fm:
            fields[fm.group(1)] = fm.group(2)

    return block_keyword, name_field, fields


def parse_init_section(init_body: str) -> dict:
    init_vars = {}
    for line in init_body.strip().splitlines():
        m = re.match(r'(\w+)\s*=\s*(.*)', line.strip())
        if not m:
            continue
        var, val = m.group(1), m.group(2).strip()
        if val == '[]':
            init_vars[var] = []
        elif val.isdigit():
            init_vars[var] = int(val)
        else:
            init_vars[var] = val
    return init_vars


def parse_instance_section(instance_body: str) -> list:
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


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

def compile_pass(source: str, root_dir: str, out_map: dict) -> str:
    sections = parse_pass_file(source)

    glob_pattern = sections['where'].strip()
    block_keyword, name_field, fields = parse_what_template(sections['what'])
    init_vars = parse_init_section(sections['init'])
    instance_ops = parse_instance_section(sections['instance'])
    out_sections = parse_out_sections(sections)

    L = []
    def w(x=""): L.append(x)

    w("import re")
    w("from pathlib import Path")
    w()

    w(f"ROOT_DIR = Path({repr(root_dir)})")
    w(f"GLOB_PATTERN = {repr(glob_pattern)}")
    w(f"OUT_MAP = {repr(out_map)}")
    w(f"OUT_TEMPLATES = {repr(out_sections)}")
    w(f"INIT_VARS = {repr(init_vars)}")
    w(f"INSTANCE_OPS = {repr(instance_ops)}")
    w()

    w("INSTANCE_RE = re.compile(")
    w(f"    r'{block_keyword}\\s+(?P<{name_field}>\\w+)\\s*{{(?P<__body>.*?)}}', re.DOTALL")
    w(")")
    w("FIELD_RE = {")
    for source_field, target_field in fields.items():
        w(f"    {target_field!r}: re.compile(r'{source_field}\\s*=\\s*(.*?)\\s*;', re.DOTALL),")
    w("}")

    w("""
def render_expr(expr, fields, counters):
    expr = expr.strip()

    if expr.endswith("++"):
        var = expr[:-2].strip()
        val = counters.get(var, 0)
        counters[var] = val + 1
        return str(val)

    m = re.match(r'"([^"]*?)"\\s+if\\s+(\\w+)\\s*(==|!=)\\s*"([^"]*?)"\\s+else\\s+"([^"]*?)"', expr)
    if m:
        t, f, op, cmp, e = m.groups()
        cond = (fields.get(f, "") == cmp)
        if op == "!=":
            cond = not cond
        return t if cond else e

    if expr in fields:
        return fields[expr]

    if expr in counters:
        return str(counters[expr])

    return ""
""")

    w("""
def render_line(t, fields, counters):
    return re.sub(r"<<(.*?)>>", lambda m: render_expr(m.group(1), fields, counters), t)


def format_cpp_like(source):
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

        line_indent = indent

        lines.append("  " * line_indent + line)

        trailing_closes = closes - leading_closes
        indent = max(indent + opens - trailing_closes, 0)

    return "\\n".join(lines).rstrip() + "\\n"
""")

    w("""
def main():
    import copy
    state = copy.deepcopy(INIT_VARS)
    counters = {k:v for k,v in state.items() if isinstance(v,int)}
    accs = {k:v for k,v in state.items() if isinstance(v,list)}

    for f in ROOT_DIR.glob(GLOB_PATTERN):
        txt = f.read_text()
        for m in INSTANCE_RE.finditer(txt):
            fields = m.groupdict()
            body = fields.pop("__body")
            for field_name, field_re in FIELD_RE.items():
                field_match = field_re.search(body)
                fields[field_name] = field_match.group(1).strip() if field_match else ""
            for var, tmpl in INSTANCE_OPS:
                r = render_line(tmpl, fields, counters)
                if var in accs:
                    accs[var].append(r)
                else:
                    counters[var] = r

    all_vars = {**counters, **accs}

    for tag, tmpl in OUT_TEMPLATES.items():
        out = tmpl
        for k,v in all_vars.items():
            if isinstance(v,list):
                v = "\\n".join(v)
            out = out.replace(f"<<{k}>>", str(v))
        out = format_cpp_like(out)

        out_path = Path(OUT_MAP[tag])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out)
        print(f"Written {tag}: {out_path}")

if __name__ == "__main__":
    main()
""")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 5:
        print("Usage: pass_compiler.py <pass_file_or_dir> <gen_py_dir> <out_base_dir> tag=file ...")
        sys.exit(1)

    pass_source = Path(sys.argv[1])
    gen_dir = Path(sys.argv[2])
    out_base = Path(sys.argv[3])

    tag_map = {}
    for arg in sys.argv[4:]:
        tag, fname = arg.split("=")
        tag_map[tag] = str(out_base / fname)

    gen_dir.mkdir(parents=True, exist_ok=True)

    root_dir = pass_source.parent if pass_source.is_file() else pass_source
    pass_files = [pass_source] if pass_source.is_file() else sorted(pass_source.glob("*.pass"))
    if not pass_files:
        print(f"No .pass files found in {pass_source}")
        sys.exit(1)

    for p in pass_files:
        compiled = compile_pass(p.read_text(), str(root_dir), tag_map)

        out_py = gen_dir / (p.stem + ".py")
        out_py.write_text(compiled)

        print(f"Running {out_py}", flush=True)
        subprocess.run([sys.executable, str(out_py)], check=True)


if __name__ == "__main__":
    main()

"""Deterministic item graph over a rust-analyzer SCIP index.

Every node and edge this script emits comes from the index or the ast-grep role scan. The caller
(Claude) only chooses filters and writes prose around the output.

  codemap.py build  <index_dir> <src_dir> <scip_pb_dir>
  codemap.py find   <index_dir> <regex> [--kinds K,K]
  codemap.py refs   <index_dir> <regex>
  codemap.py render <index_dir> <filter.json> <out_dir>
  codemap.py seeds  <index_dir> <filter.json>
  codemap.py page   <out_dir> [--artifact]
  codemap.py crates <index_dir> [regex]
  codemap.py modules <index_dir> <crate>... [--depth N] [--externals REGEX] [--exclude REGEX] [--skip REGEX]
  codemap.py verify <index_dir> <doc>
"""
import bisect, collections, json, pickle, re, sys, time, zlib
from pathlib import Path

TYPE_KINDS = {"Struct", "Enum", "Trait", "TypeAlias", "Union"}
FN_KINDS = {"Function", "Method", "StaticMethod", "TraitMethod"}
# Modules are spans too, so a reference outside any item (a `use`, a const type) still has a container.
SPAN_KINDS = TYPE_KINDS | FN_KINDS | {"Impl", "Macro", "Constant", "StaticVariable", "Module"}
MEMBER_KINDS = {"Field", "EnumMember"}
DEFAULT_EXCLUDE = r"(^|::)tests?(::|$)"
# Bumped when build() output changes meaning; older graph.pkl files need `codemap rebuild`.
GRAPH_VERSION = 2
# Placeholders page() fills in page.html; each must appear exactly once.
PAGE_MARKERS = ("__TITLE__", "/*__DATA__*/", "<!--__SVGS__-->", "<!--__NOTES__-->", "<!--__BODY_START__-->", "<!--__BODY_END__-->")


# Symbol names ------------------------------------------------------------------------------------

def parse(sym):
    """Split "rust-analyzer cargo <crate> <ver> <descriptors>" into (crate, path, trait, self_type)."""
    parts = sym.split(" ", 4)
    if len(parts) < 5:
        return ("?", sym, None, None)
    desc = parts[4].replace("`", "")
    # Newer rust-analyzer prefixes the target kind; keep paths comparable across versions.
    desc = re.sub(r"^(lib|bin/[^/]+)/", "", desc)
    trait = self_type = None
    m = re.match(r"(.*?)impl#\[([^\]]*)\](?:\[([^\]]*)\])?(.*)$", desc)
    if m:
        trait = m.group(3)
        self_type = re.sub(r"<.*", "", m.group(2))
        desc = f"{m.group(1)}{self_type}/{m.group(4)}"
    path = re.sub(r"[#./]+", "::", desc).strip(":").replace("()", "")
    return (parts[2], path, trait, self_type)


def crate_of(sym):
    return parse(sym)[0]


def path_of(sym):
    return parse(sym)[1]


def name_of(sym):
    return path_of(sym).split("::")[-1]


def module_of(sym):
    """(crate, module path) from a symbol's leading namespace descriptors; "" is the crate root."""
    parts = sym.split(" ", 4)
    if len(parts) < 5:
        return ("?", "")
    desc = re.sub(r"^(lib|bin/[^/]+)/", "", parts[4].replace("`", ""))
    ns = re.match(r"(?:[^/#.()\[\]]+/)*", desc).group(0).split("/")[:-1]
    if ns[:1] == ["crate"]:
        ns = ns[1:]
    return (parts[2], "::".join(ns))


def parent_of(sym):
    # "...Type#field." / "...Enum#Variant#" -> "...Type#"
    return re.sub(r"(#[^#]+[#.])$", "#", sym)


def nid(sym):
    return "n%08x" % zlib.crc32(sym.encode())


def dot_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def esc(s):
    return s.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;").replace("|", "#124;")


# Selection: the one place that turns a user regex into symbols ---------------------------------------
# Every command that takes a regex (find, refs, seed_types, seed_fns) goes through here, so a pattern
# behaves the same everywhere and a miss is explained instead of silently yielding nothing.

ALL_KINDS = TYPE_KINDS | FN_KINDS | MEMBER_KINDS | {"Impl", "Macro", "Constant", "StaticVariable", "Module"}
FILTER_KEYS = {"title", "question", "seed_types", "seed_fns", "callers", "callees", "crates", "exclude", "roles",
               "max_nodes", "max_members", "modules"}
ROLE_NAMES = {"constructs", "matches", "uses"}


def die(msg):
    sys.exit(f"codemap: {msg}")


def compile_rx(what, pat):
    try:
        return re.compile(pat)
    except re.error as e:
        die(f"{what}: bad regex {pat!r}: {e}")


def hits(rx, path):
    """Regexes see `module::path::Name` (no crate name). A leading `::` also works for a top-level
    item, so `::Name$` finds `Name` as well as `a::Name`."""
    return bool(rx.search(path) or rx.search("::" + path))


def split_top(s):
    """Split a regex on `|` outside groups, character classes and escapes."""
    parts, depth, cls, cur, i = [], 0, False, "", 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            cur += s[i:i + 2]
            i += 2
            continue
        if cls:
            cls = c != "]"
        elif c == "[":
            cls = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            parts.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    return parts + [cur]


def alternatives(pat):
    """`A|B` -> [A, B]; `(^A|^B)$` -> [^A$, ^B$]. The union is unchanged; splitting lets the report
    show which alternative matched nothing. Anything fancier stays one pattern."""
    if re.search(r"\(\?[aiLmsux-]", pat):
        return [pat]  # inline flags apply to the whole pattern; splitting would drop them
    alts = split_top(pat)
    if len(alts) > 1:
        return alts
    depth, cls, i, start = 0, False, 0, None
    while i < len(pat):
        c = pat[i]
        if c == "\\":
            i += 2
            continue
        if cls:
            cls = c != "]"
        elif c == "[":
            cls = True
        elif c == "(":
            if depth == 0:
                if start is not None:
                    return [pat]
                start = i
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0 and start is not None:
                prefix, body, suffix = pat[:start], pat[start + 1:i], pat[i + 1:]
                body = body[2:] if body.startswith("?:") else body
                if body.startswith("?") or "(" in suffix or "|" in suffix + prefix:
                    return [pat]
                inner = split_top(body)
                return [prefix + a + suffix for a in inner] if len(inner) > 1 else [pat]
        i += 1
    return [pat]


def patterns(flt, key):
    """A filter value is one regex or a list of regexes (each reported separately, so a list shows
    which entry matched nothing). Returns [(text, compiled)]."""
    v = flt.get(key)
    if v in (None, "", []):
        return []
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not all(isinstance(i, str) and i for i in items):
        die(f"{key} must be a regex string or a list of non-empty regex strings, got {v!r}")
    return [(a, compile_rx(key, a)) for p in items for a in alternatives(p)]


def close(word, options, n=4):
    import difflib
    return difflib.get_close_matches(word, sorted(options), n=n, cutoff=0.6)


def crate_names(g):
    if "_crates" not in g:
        g["_crates"] = {crate_of(s) for s in g["info"]}
    return g["_crates"]


def resolve_crates(g, names):
    """Package names as the index spells them; `-` and `_` are interchangeable on input."""
    if isinstance(names, str):
        names = [names]
    known = crate_names(g)
    norm = {c.replace("-", "_"): c for c in known}
    out = []
    for n in names:
        c = n if n in known else norm.get(str(n).replace("-", "_"))
        if c is None:
            hint = close(n, known)
            die(f"unknown crate {n!r}. " + (f"Did you mean: {', '.join(hint)}? " if hint else "") + "Crate names are package names; `codemap crates` lists the workspace ones.")
        out.append(c)
    return out


def check_kinds(kinds):
    bad = sorted(set(kinds) - ALL_KINDS)
    if bad:
        die(f"unknown kind(s) {', '.join(bad)}. Valid: {', '.join(sorted(ALL_KINDS))}")


def validate_filter(flt):
    """Reject what would otherwise be silently ignored: typo'd keys, wrong value types."""
    if not isinstance(flt, dict):
        die("filter.json must be a JSON object")
    for k in flt:
        if k not in FILTER_KEYS:
            hint = close(k, FILTER_KEYS, 1)
            die(f"unknown filter key {k!r}" + (f" (did you mean {hint[0]!r}?)" if hint else "") + f". Valid keys: {', '.join(sorted(FILTER_KEYS))}")
    for k in ("callers", "callees", "max_nodes", "max_members"):
        if k in flt and (not isinstance(flt[k], int) or isinstance(flt[k], bool) or flt[k] < 0):
            die(f"{k} must be a non-negative integer, got {flt[k]!r}")
    for k in ("crates", "roles"):
        v = flt.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(i, str) for i in v)):
            die(f"{k} must be a list of strings, got {v!r}")
    bad = sorted(set(flt.get("roles") or []) - ROLE_NAMES)
    if bad:
        die(f"unknown role(s) {', '.join(bad)}. Valid: {', '.join(sorted(ROLE_NAMES))}")
    if flt.get("exclude"):
        compile_rx("exclude", flt["exclude"])
    if not (flt.get("seed_types") or flt.get("seed_fns") or flt.get("modules") not in (None, False)):
        die("filter selects nothing: give at least one of seed_types, seed_fns, modules")


def why_no_match(g, pat, kinds, indent="    "):
    """Lines explaining a regex that matched no symbol of `kinds` by path."""
    rx = compile_rx("pattern", pat)
    out = []
    syms = list(g["info"].items())
    other = collections.Counter(i["kind"] for s, i in syms if i["kind"] not in kinds and hits(rx, path_of(s)))
    if other:
        out.append("matches exist with other kinds: " + ", ".join(f"{k} x{n}" for k, n in other.most_common(4))
                   + " (seed_types takes " + "/".join(sorted(TYPE_KINDS)) + "; seed_fns takes " + "/".join(sorted(FN_KINDS)) + ")")
    qualified = [s for s, i in syms if i["kind"] in kinds and rx.search(f"{crate_of(s)}::{path_of(s)}")]
    if qualified:
        out.append(f"it only matches with the crate name prepended (e.g. {crate_of(qualified[0])}::{path_of(qualified[0])}); regexes see "
                   "`module::path::Name` without the crate, so scope with `crates` instead")
    ci = [s for s, i in syms if i["kind"] in kinds and re.search(pat, path_of(s), re.I)] if not other and not qualified else []
    if ci:
        out.append("matches only ignoring case, e.g. " + path_of(ci[0]))
    if not out:
        crates = {c.replace("-", "_") for c in crate_names(g)}
        words = sorted((w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pat) if w not in crates), key=len, reverse=True)
        near = []
        if words:
            w = words[0].lower()
            near = sorted({path_of(s) for s, i in syms if i["kind"] in kinds and w in path_of(s).lower()})[:6]
        if not near and words:
            names = {name_of(s): path_of(s) for s, i in syms if i["kind"] in kinds}
            near = [names[n] for n in close(words[0], names)]
        out.append("no symbol path matches" + (": similar paths: " + ", ".join(near) if near else "; check the spelling with `codemap find`"))
    return [indent + o for o in out]


def select(g, flt, key, kinds, crate_set, exclude):
    """Symbols of `kinds` chosen by flt[key]. Returns (set, report) with one report row per pattern
    counting matches before and after the `crates` and `exclude` scope."""
    chosen, report = set(), []
    for pat, rx in patterns(flt, key):
        raw = [s for s, i in g["info"].items() if i["kind"] in kinds and hits(rx, path_of(s))]
        in_crate = [s for s in raw if not crate_set or crate_of(s) in crate_set]
        kept = [s for s in in_crate if not exclude.search(path_of(s))]
        chosen.update(kept)
        report.append({"pattern": pat, "raw": raw, "in_crate": in_crate, "kept": kept})
    return chosen, report


def print_selection(g, key, kinds, report, crate_set, limit=10):
    """Show what each pattern selected, and why a pattern selected nothing. True if any pattern missed."""
    missed = False
    for n, r in enumerate(report):
        tag = f"{key}[{n}]" if len(report) > 1 else key
        print(f"{tag} /{r['pattern']}/ -> {len(r['kept'])} symbols" + ("" if len(r["raw"]) == len(r["kept"]) else f" ({len(r['raw'])} before crates/exclude)"))
        names = sorted(f"{crate_of(s)}::{path_of(s)}" for s in r["kept"])
        for nm in names[:limit]:
            print(f"    {nm}")
        if len(names) > limit:
            print(f"    ... {len(names) - limit} more")
        if r["kept"]:
            # A pattern without a closing `$` also selects longer names (Pack -> PackKind).
            short = sorted({name_of(s) for s in r["kept"]})
            if not r["pattern"].endswith("$") and len(short) > 1:
                print(f"    warning: not anchored at the end, selects {len(short)} distinct names "
                      f"({', '.join(short[:6])}{', ...' if len(short) > 6 else ''}); end it with $ if that is too broad")
            continue
        missed = True
        if r["raw"] and not r["in_crate"]:
            print(f"    matched {len(r['raw'])} in other crates only ({', '.join(sorted({crate_of(s) for s in r['raw']})[:5])}) but crates = {sorted(crate_set)}")
        elif r["raw"]:
            print(f"    all {len(r['raw'])} matches are dropped by `exclude` (default drops test modules), e.g. {path_of(r['raw'][0])}")
        else:
            for line in why_no_match(g, r["pattern"], kinds):
                print(line)
    return missed


def filter_scope(g, flt):
    validate_filter(flt)
    crate_set = set(resolve_crates(g, flt["crates"])) if flt.get("crates") else set()
    exclude = compile_rx("exclude", flt.get("exclude") or DEFAULT_EXCLUDE)
    return crate_set, exclude


def seeds_cmd(g, flt):
    """Dry run of seed selection: what render would pick, with reasons for misses. No files written."""
    crate_set, exclude = filter_scope(g, flt)
    bad = False
    for key, kinds in (("seed_types", TYPE_KINDS), ("seed_fns", FN_KINDS)):
        chosen, report = select(g, flt, key, kinds, crate_set, exclude)
        if report:
            missed = print_selection(g, key, kinds, report, crate_set)
            if chosen and missed:
                print(f"warning: some {key} patterns matched nothing (see above)")
            bad |= not chosen
    sys.exit(2 if bad else 0)


# Build ------------------------------------------------------------------------------------------

def classify_text(text, name):
    # Fallback for macro bodies, which tree-sitter does not parse.
    i = text.find(name)
    if i < 0:
        return "uses"
    after, before = text[i + len(name):], text[:i]
    if (
        "=>" in after
        or re.match(r"\s*\|\s*[A-Za-z_:]*$", before)
        or re.match(r"\s*(\(.*\)|\{.*\})?\s*(\||if\b|=>)", after)
        or re.search(r"\b(if let|while let|matches!\()", before)
        or (before.lstrip().startswith("let ") and "else" in after)
    ):
        return "matches"
    return "constructs"


def build(index_dir, src_dir, pb_dir):
    sys.path.insert(0, pb_dir)
    import scip_pb2

    t = time.time()
    index_dir, src_dir = Path(index_dir), Path(src_dir)
    idx = scip_pb2.Index()
    idx.ParseFromString((index_dir / "index.scip").read_bytes())
    kind_name = scip_pb2.SymbolInformation.Kind.Name

    roles = collections.defaultdict(set)
    for line in (index_dir / "roles.jsonl").read_text().splitlines():
        j = json.loads(line)
        end = j["range"]["end"]
        roles[(j["file"].removeprefix("./"), end["line"], end["column"])].add(j["ruleId"])

    info = {}
    for s in list(idx.external_symbols) + [s for d in idx.documents for s in d.symbols]:
        if s.symbol.startswith("local "):
            continue
        # rust-analyzer reports impl blocks ("...impl#[T][Trait]") as TypeAlias.
        kind = "Impl" if s.symbol.endswith("]") else kind_name(s.kind)
        info[s.symbol] = {"kind": kind, "sig": s.signature_documentation.text, "doc": "\n".join(s.documentation)[:400]}

    defs, edges = {}, []
    for d in idx.documents:
        lines = (src_dir / d.relative_path).read_text(errors="replace").splitlines()
        spans = []
        for o in d.occurrences:
            if not (o.symbol_roles & 1) or o.symbol.startswith("local "):
                continue
            kind = info.get(o.symbol, {}).get("kind")
            defs[o.symbol] = {"file": d.relative_path, "line": o.range[0] + 1}
            if kind in SPAN_KINDS and len(o.enclosing_range):
                er = list(o.enclosing_range)
                spans.append((er[0], er[2] if len(er) == 4 else er[0], o.symbol))
        # Wider spans first among equal starts, so the reverse scan below finds the innermost one
        # (a file module and its first item both start on line 0).
        spans.sort(key=lambda s: (s[0], -s[1], s[2]))
        starts = [s[0] for s in spans]
        for o in d.occurrences:
            if o.symbol_roles & 1 or o.symbol.startswith("local "):
                continue
            line, endcol = o.range[0], o.range[-1]
            container = None
            for s in reversed(spans[: bisect.bisect_right(starts, line)]):
                if s[0] <= line <= s[1]:
                    container = s[2]
                    break
            text = lines[line].strip() if line < len(lines) else ""
            tags = roles.get((d.relative_path, line, endcol), set())
            kind = info.get(o.symbol, {}).get("kind")
            if "pattern" in tags:
                role = "matches"
            elif "struct-literal" in tags or ("call-target" in tags and kind == "Struct"):
                role = "constructs"
            elif kind == "EnumMember":
                role = classify_text(text, name_of(o.symbol)) if "macro" in tags else "constructs"
            else:
                role = "uses"
            edges.append((container, o.symbol, d.relative_path, line + 1, role))
    # Write-then-rename so a reader never sees a half-written pickle. graph.version lets the
    # wrapper detect a stale graph without unpickling it (it rebuilds on its own).
    tmp_pkl = index_dir / "graph.pkl.tmp"
    with open(tmp_pkl, "wb") as fh:
        pickle.dump({"version": GRAPH_VERSION, "info": info, "defs": defs, "edges": edges}, fh)
    tmp_pkl.replace(index_dir / "graph.pkl")
    (index_dir / "graph.version").write_text(f"{GRAPH_VERSION}\n")
    print(f"graph: {len(defs)} defs, {len(edges)} refs, {sum(len(v) for v in roles.values())} role tags in {time.time() - t:.1f}s")


def opt_value(args, name, default=None):
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 >= len(args):
        die(f"{name} needs a value")
    return args[i + 1]


def load_filter(path):
    try:
        return json.load(open(path))
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON: {e}")
    except FileNotFoundError:
        die(f"no filter at {path}")


def load(index_dir):
    g = pickle.load(open(Path(index_dir) / "graph.pkl", "rb"))
    if g.get("version") != GRAPH_VERSION:
        sys.exit(f"codemap: {index_dir}/graph.pkl was built by an older codemap.py. Run: codemap rebuild (seconds); the wrapper normally does this itself")
    return g


# Queries -----------------------------------------------------------------------------------------

def find(g, rx, kinds, limit=60):
    check_kinds(kinds)
    r = compile_rx("find", rx)
    rows = []
    for sym, i in g["info"].items():
        if i["kind"] not in kinds:
            continue
        p = path_of(sym)
        if hits(r, p):
            d = g["defs"].get(sym)
            rows.append((crate_of(sym), p, i["kind"], f"{d['file']}:{d['line']}" if d else "external", i["sig"].splitlines()[0][:100] if i["sig"] else ""))
    for c, p, k, loc, sig in sorted(rows)[:limit]:
        print(f"{k:13} {c}::{p}  {loc}  {sig}")
    print(f"-- {len(rows)} matches" + (f", first {limit} shown (--limit N, or narrow the regex)" if len(rows) > limit else ""))
    if not rows:
        print("\n".join(why_no_match(g, rx, kinds, "-- ")))


def refs(g, rx, limit=25, by_fn=False):
    r = compile_rx("refs", rx)
    by = collections.defaultdict(list)
    for container, target, f, line, role in g["edges"]:
        if hits(r, path_of(target)):
            by[target].append((container, f, line, role))
    if not by:
        print(f"-- no references to a symbol matching /{rx}/")
        print("\n".join(why_no_match(g, rx, ALL_KINDS, "-- ")))
    for target, rs in sorted(by.items(), key=lambda kv: path_of(kv[0])):
        print(f"\n## {crate_of(target)}::{path_of(target)}  ({len(rs)} refs)")
        if by_fn:
            # One line per enclosing function: role counts and the first site.
            fns = collections.defaultdict(list)
            for container, f, line, role in sorted(rs, key=lambda x: (x[1], x[2])):
                fns[container].append((role, f, line))
            for container, items in sorted(fns.items(), key=lambda kv: (-len(kv[1]), path_of(kv[0]) if kv[0] else "")):
                cn = f"{crate_of(container)}::{path_of(container)}" if container else "<module scope>"
                roles = ",".join(f"{k}x{v}" for k, v in sorted(collections.Counter(i[0] for i in items).items()))
                print(f"  {cn:72} {roles:24} {items[0][1]}:{items[0][2]}")
            continue
        for container, f, line, role in sorted(rs, key=lambda x: (x[1], x[2]))[:limit]:
            cn = f"{crate_of(container)}::{path_of(container)}" if container else "<module scope>"
            print(f"  {role:10} {cn:72} {f}:{line}")
        if len(rs) > limit:
            print(f"  ... {len(rs) - limit} more (--limit N, or --by-fn to group by enclosing function)")


def crates(index_dir, rx):
    m = json.load(open(Path(index_dir) / "metadata.json"))
    ws = set(m["workspace_members"])
    names = {p["id"]: p["name"] for p in m["packages"]}
    r = re.compile(rx or ".")
    out = ["flowchart LR"]
    for n in sorted(m["resolve"]["nodes"], key=lambda n: names[n["id"]]):
        if n["id"] not in ws or not r.search(names[n["id"]]):
            continue
        for d in sorted(n["deps"], key=lambda d: names[d["pkg"]]):
            if d["pkg"] in ws:
                out.append(f"  {names[n['id']].replace('-', '_')} --> {names[d['pkg']].replace('-', '_')}")
    print("\n".join(out))


# Modules -----------------------------------------------------------------------------------------

def module_graph(g, crates, exclude, depth=None, externals=None, skip=None):
    """Module -> module edges from every reference: the module enclosing the reference depends on
    the module defining what it names. Nodes are ("mod", crate, path) or ("ext", crate).
    `skip` drops references by the name they reach, as printed (`warn!`, `Pose::new`)."""
    ext_rx = re.compile(externals) if externals else None
    skip_rx = re.compile(skip) if skip else None
    internal = set(crates) or {module_of(c)[0] for c, *_ in g["edges"] if c}

    def cut(path):
        return "::".join(path.split("::")[:depth]) if depth is not None and path else path

    # Every module in scope is a node, including ones with no edges; `folded` records which module
    # paths a node stands for once `depth` collapses submodules into it.
    folded, modsym = collections.defaultdict(set), {}
    for sym, i in g["info"].items():
        if i["kind"] != "Module":
            continue
        c, m = module_of(sym)
        if c in internal and not exclude.search(m):
            folded[("mod", c, cut(m))].add(m)
            modsym[(c, m)] = sym

    edges = collections.defaultdict(lambda: {"sites": set(), "names": collections.Counter()})
    for container, target, f, line, _role in g["edges"]:
        # A module symbol as target is a path segment (`use crate::plan`, `mod plan;`); the item
        # named after it carries the dependency.
        if not container or target.endswith("/"):
            continue
        sc, sm = module_of(container)
        if sc not in internal or exclude.search(sm):
            continue
        tc, tm = module_of(target)
        if tc in internal:
            if exclude.search(tm):
                continue
            dst = ("mod", tc, cut(tm))
        elif ext_rx and ext_rx.search(tc):
            dst = ("ext", tc)
        else:
            continue
        src = ("mod", sc, cut(sm))
        if src == dst:
            continue
        name = re.sub(r"^crate::", "", path_of(target))
        if tm and name.startswith(tm + "::"):
            name = name[len(tm) + 2:]
        if skip_rx and skip_rx.search(name):
            continue
        e = edges[(src, dst)]
        e["sites"].add((f, line))
        e["names"][name] += 1

    nodes = set(folded) | {n for e in edges for n in e}
    adj = collections.defaultdict(list)
    for a, b in sorted(edges):
        adj[a].append(b)
    cycles = [c for c in strongly_connected(nodes, adj) if len(c) > 1]
    # Two modules naming each other: the smallest cycles, often hidden inside a large component.
    mutual = sorted((a, b) for a, b in edges if a < b and (b, a) in edges)
    return {"nodes": nodes, "edges": edges, "cycles": cycles, "mutual": mutual, "folded": folded, "modsym": modsym}


def strongly_connected(nodes, adj):
    """Tarjan's strongly connected components, each sorted, visited in sorted order."""
    index, low, stack, on, out = {}, {}, [], set(), []

    def visit(v):
        index[v] = low[v] = len(index)
        stack.append(v)
        on.add(v)
        for w in adj[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] != index[v]:
            return
        comp = []
        while True:
            w = stack.pop()
            on.discard(w)
            comp.append(w)
            if w == v:
                break
        out.append(sorted(comp))

    sys.setrecursionlimit(max(1000, 4 * len(nodes)))
    for v in sorted(nodes):
        if v not in index:
            visit(v)
    return sorted(out)


def module_label(key):
    return key[1] if key[0] == "ext" else key[2] or "crate"


def module_names(names, limit=6):
    top = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))
    s = ", ".join(f"{n}x{c}" if c > 1 else n for n, c in top[:limit])
    return s + (f", +{len(top) - limit} more" if len(top) > limit else "")


def modules(g, crates, depth, externals, exclude, skip):
    mg = module_graph(g, crates, exclude, depth, externals, skip)
    multi = len({n[1] for n in mg["nodes"]}) > 1
    lab = (lambda k: f"{k[1]}::{module_label(k)}" if k[0] == "mod" else f"[{k[1]}]") if multi else (lambda k: module_label(k) if k[0] == "mod" else f"[{k[1]}]")
    print(f"{len(mg['nodes'])} modules, {len(mg['edges'])} edges, {len(mg['cycles'])} cycles, {len(mg['mutual'])} mutual pairs")
    for (a, b), e in sorted(mg["edges"].items(), key=lambda kv: (lab(kv[0][0]), lab(kv[0][1]))):
        print(f"  {lab(a):28} -> {lab(b):28} {len(e['sites']):4} sites  {module_names(e['names'])}")
    for i, comp in enumerate(mg["cycles"], 1):
        members = set(comp)
        print(f"\ncycle {i}: " + ", ".join(lab(n) for n in comp))
        inner = [(a, b, e) for (a, b), e in mg["edges"].items() if a in members and b in members]
        # Fewest sites first: the cheapest references to move when breaking the cycle.
        for a, b, e in sorted(inner, key=lambda x: (len(x[2]["sites"]), lab(x[0]), lab(x[1]))):
            f, line = min(e["sites"])
            print(f"  {lab(a):28} -> {lab(b):28} {len(e['sites']):4} sites  {f}:{line}  {module_names(e['names'])}")
    if mg["mutual"]:
        print("\nmutual pairs:")
    for a, b in mg["mutual"]:
        print(f"  {lab(a)} <-> {lab(b)}  ({len(mg['edges'][(a, b)]['sites'])} / {len(mg['edges'][(b, a)]['sites'])} sites)")


# Render ------------------------------------------------------------------------------------------

def display_names(syms):
    """Short labels, widened until unique: Type::method, then crate/trait qualifiers."""
    out = {}
    for s in syms:
        p = path_of(s).split("::")
        self_type = parse(s)[3]
        # Impl members read as "SelfType::method", keeping a path-qualified self type intact.
        out[s] = f"{self_type}::{p[-1]}" if self_type and p[-1] != self_type.split("::")[-1] else "::".join(p[-2:]) if len(p) > 1 else p[0]
    clash = collections.Counter(out.values())
    for s in syms:
        if clash[out[s]] > 1:
            trait = parse(s)[2]
            out[s] = f"{out[s]} [{trait}]" if trait else f"{crate_of(s)}::{out[s]}"
    clash = collections.Counter(out.values())
    for s in syms:
        if clash[out[s]] > 1:
            out[s] = f"{crate_of(s)}::{path_of(s)}"
    return out


def render(g, flt, out_dir, meta):
    out = Path(out_dir)
    info, defs = g["info"], g["defs"]
    crate_set, exclude = filter_scope(g, flt)
    max_nodes = flt.get("max_nodes", 30)
    max_members = flt.get("max_members", 12)
    roles_wanted = set(flt.get("roles") or ["constructs", "matches"])

    def in_scope(sym):
        return (not crate_set or crate_of(sym) in crate_set) and not exclude.search(path_of(sym))

    # Seed selection is reported before anything is drawn, and a key that selected nothing stops the
    # render before it touches the output folder (an old good render stays intact).
    seed_set, type_report = select(g, flt, "seed_types", TYPE_KINDS, crate_set, exclude)
    fn_seed_set, fn_report = select(g, flt, "seed_fns", FN_KINDS, crate_set, exclude)
    failed = False
    for key, kinds, report, chosen in (("seed_types", TYPE_KINDS, type_report, seed_set), ("seed_fns", FN_KINDS, fn_report, fn_seed_set)):
        if report:
            missed = print_selection(g, key, kinds, report, crate_set)
            if not chosen:
                failed = True
            elif missed:
                print(f"warning: some {key} patterns matched nothing (see above); the diagram omits them")
    if failed:
        sys.stdout.flush()
        print("ERROR: no seeds selected, nothing rendered. Fix the pattern (`codemap seeds SLUG` re-runs just this check).", file=sys.stderr)
        sys.exit(2)
    if len(seed_set) > max_nodes:
        print(f"note: {len(seed_set)} seed types is more than max_nodes={max_nodes}; an unanchored pattern may be matching names that merely contain yours (anchor with ^ and $)")
    out.mkdir(parents=True, exist_ok=True)
    seeds = sorted(seed_set, key=path_of)

    members = collections.defaultdict(list)
    for s, i in info.items():
        if i["kind"] in MEMBER_KINDS and parent_of(s) in seed_set:
            members[parent_of(s)].append(s)

    # Type -> type edges from member signatures; works for proto types that have no source span.
    by_name = collections.defaultdict(list)
    for s in seeds:
        by_name[name_of(s)].append(s)
    type_edges = set()
    for s in seeds:
        for m in members[s]:
            for word in set(re.findall(r"[A-Z][A-Za-z0-9_]*", info[m]["sig"])):
                for t in by_name.get(word, []):
                    if t != s and (len(by_name[word]) == 1 or crate_of(t) == crate_of(s)):
                        type_edges.add((s, t, name_of(m)))

    # fn -> type edges with roles, and evidence for each.
    flow = collections.defaultdict(list)
    uses = collections.defaultdict(set)
    for container, target, f, line, role in g["edges"]:
        if not container or info.get(container, {}).get("kind") not in FN_KINDS or not in_scope(container):
            continue
        tkind = info.get(target, {}).get("kind")
        owner = parent_of(target) if tkind == "EnumMember" else target
        if owner not in seed_set:
            continue
        uses[owner].add(container)
        if role in roles_wanted:
            flow[(container, owner, role)].append(f"{f}:{line}")

    flow_nodes = {fn for fn, _, _ in flow} | {t for _, t, _ in flow}

    # Call graph: fn -> fn references, walked up (callers) and down (callees) from seed_fns.
    calls = collections.defaultdict(list)
    call_nodes = set()
    if fn_seed_set:
        up, down = collections.defaultdict(set), collections.defaultdict(set)
        sites = collections.defaultdict(list)
        for container, target, f, line, _role in g["edges"]:
            if container == target or not container:
                continue
            if info.get(container, {}).get("kind") in FN_KINDS and info.get(target, {}).get("kind") in FN_KINDS and in_scope(container) and in_scope(target):
                up[target].add(container)
                down[container].add(target)
                sites[(container, target)].append(f"{f}:{line}")
        call_nodes |= fn_seed_set
        for adj, depth, forward in ((up, flt.get("callers", 3), False), (down, flt.get("callees", 1), True)):
            frontier = set(fn_seed_set)
            for _ in range(depth):
                nxt = set()
                for n in frontier:
                    for m in adj[n]:
                        edge = (n, m) if forward else (m, n)
                        calls[edge] = sites[edge]
                        if m not in call_nodes:
                            nxt.add(m)
                call_nodes |= nxt
                frontier = nxt

    labels = display_names(flow_nodes | seed_set | call_nodes | {c for cs in uses.values() for c in cs})

    # Type boxes use the bare name unless two seeds share it.
    name_count = collections.Counter(name_of(t) for t in seeds)
    tlabel = {t: name_of(t) if name_count[name_of(t)] == 1 else f"{crate_of(t)}::{name_of(t)}" for t in seeds}

    def member_sig(m):
        sig = info[m]["sig"].splitlines()[0] if info[m]["sig"] else name_of(m)
        return re.sub(r"^pub(\([^)]*\))?\s+", "", sig)

    td = ["classDiagram"]
    for s in seeds:
        td.append(f'  class {nid(s)}["{esc(tlabel[s])}"] {{')
        td.append(f"    <<{info[s]['kind']}>>")
        ms = sorted(members[s], key=lambda m: defs.get(m, {}).get("line", 0))
        for m in ms[:max_members]:
            td.append("    " + member_sig(m).replace("{", "(").replace("}", ")").replace("<", "~").replace(">", "~").replace('"', "'")[:80])
        if len(ms) > max_members:
            td.append(f"    ... {len(ms) - max_members} more")
        td.append("  }")
    for a, b, via in sorted(type_edges):
        td.append(f"  {nid(a)} --> {nid(b)} : {via}")

    def flowchart(direction, nodes, edges, is_seed):
        lines = [f"flowchart {direction}"]
        groups = collections.defaultdict(set)
        for n in nodes:
            groups[crate_of(n)].add(n)
        for crate, ns in sorted(groups.items()):
            lines.append(f"  subgraph {re.sub(r'[^A-Za-z0-9_]', '_', crate)}[\"{crate}\"]")
            for n in sorted(ns, key=lambda n: labels[n]):
                lines.append(f'    {nid(n)}[["{esc(labels[n])}"]]' if is_seed(n) else f'    {nid(n)}("{esc(labels[n])}")')
            lines.append("  end")
        return lines + edges

    fn_seed = lambda n: n in fn_seed_set
    cc = flowchart("TD", call_nodes, [f"  {nid(a)} --> {nid(b)}" for (a, b) in sorted(calls, key=lambda e: (labels[e[0]], labels[e[1]]))], fn_seed)
    fc = flowchart("LR", flow_nodes, [
        f"  {nid(fn)} -->|{len(ev)}| {nid(t)}" if role == "constructs" else f"  {nid(t)} -.->|{len(ev)}| {nid(fn)}"
        for (fn, t, role), ev in sorted(flow.items(), key=lambda kv: (labels[kv[0][0]], labels[kv[0][1]], kv[0][2]))
    ], lambda n: n in seed_set)

    # Module dependencies: one node per module (or per external crate), cycles marked.
    mods, mc, mod_nodes = None, [], {}
    # `"modules": true` or an options object, `{}` included.
    if flt.get("modules") not in (None, False):
        mopt = flt["modules"] if isinstance(flt["modules"], dict) else {}
        mods = module_graph(g, crate_set, exclude, mopt.get("depth"), mopt.get("externals"), mopt.get("skip"))
        mid = {k: nid(":".join(k)) for k in mods["nodes"]}
        in_cycle = {n: i for i, comp in enumerate(mods["cycles"]) for n in comp}
        # Graphviz rather than mermaid: dot's layered layout and spline routing stay readable at
        # the edge density module graphs have. Node and edge ids match what page.html expects;
        # cycle colours are in the .dot too so the file reads on its own.
        mc = [
            "digraph modules {",
            '  graph [rankdir=TB, bgcolor="transparent", fontname="Arial", fontsize=11, nodesep=0.3, ranksep=0.5, pad=0.2]',
            '  node [shape=box, style=rounded, fontname="Arial", fontsize=12, height=0.32, margin="0.14,0.05"]',
            '  edge [fontname="Arial", fontsize=10, arrowsize=0.6]',
        ]
        groups = collections.defaultdict(list)
        for k in mods["nodes"]:
            groups[k[1] if k[0] == "mod" else ""].append(k)
        for crate, ks in sorted(groups.items()):
            mc.append(f'  subgraph "cluster_{crate or "external"}" {{')
            mc.append(f'    label={dot_str(crate or "external crates")}; labeljust=l; style=rounded')
            for k in sorted(ks, key=module_label):
                attrs = [f"label={dot_str(module_label(k))}", f'id="{mid[k]}"']
                if k[0] == "ext":
                    attrs.append('style="rounded,dashed"')
                if k in in_cycle:
                    attrs += ['class="cycle"', 'color="#c62828"', "penwidth=2"]
                mc.append(f"    {mid[k]} [{', '.join(attrs)}]")
            mc.append("  }")
        for (a, b), e in sorted(mods["edges"].items(), key=lambda kv: (mid[kv[0][0]], mid[kv[0][1]])):
            attrs = [f'label="{len(e["sites"])}"', f'id="L_{mid[a]}_{mid[b]}_0"']
            if a in in_cycle and in_cycle.get(b) == in_cycle[a]:
                attrs += ['class="cycle"', 'color="#c62828"', "penwidth=1.6"]
            mc.append(f"  {mid[a]} -> {mid[b]} [{', '.join(attrs)}]")
        mc.append("}")

        for k in mods["nodes"]:
            sym = mods["modsym"].get((k[1], k[2])) if k[0] == "mod" else None
            d = defs.get(sym) if sym else None
            n = {
                "label": module_label(k),
                "path": f"{k[1]}::{k[2]}" if k[0] == "mod" and k[2] else k[1],
                "crate": k[1],
                "kind": "Module" if k[0] == "mod" else "Crate",
                "def": f"{d['file']}:{d['line']}" if d else None,
                "sig": "",
                "seed": False,
            }
            sub = sorted(m for m in mods["folded"].get(k, ()) if m != (k[2] if k[0] == "mod" else None))
            if sub:
                n["includes"] = sub
            if k in in_cycle:
                n["cycle"] = in_cycle[k]
            mod_nodes[mid[k]] = n

    n_mods = len(mods["nodes"]) if mods else 0
    too_big = len(flow_nodes) > max_nodes or len(seeds) > max_nodes or len(call_nodes) > max_nodes or n_mods > max_nodes
    for name in ("calls", "flow", "types", "modules"):
        for ext in (".mmd", ".dot", ".svg"):
            (out / f"{name}{ext}").unlink(missing_ok=True)
    diagrams = []
    if not too_big:
        if call_nodes:
            (out / "calls.mmd").write_text("\n".join(cc) + "\n")
            diagrams.append("calls")
        if flow:
            (out / "flow.mmd").write_text("\n".join(fc) + "\n")
            diagrams.append("flow")
        if seeds:
            (out / "types.mmd").write_text("\n".join(td) + "\n")
            diagrams.append("types")
        if mods:
            (out / "modules.dot").write_text("\n".join(mc) + "\n")
            diagrams.append("modules")
    (out / "filter.json").write_text(json.dumps(flt, indent=2) + "\n")

    # Everything the page shows, keyed by the same node ids the diagrams use.
    def node(sym):
        d = defs.get(sym)
        i = info.get(sym, {})
        n = {
            "label": tlabel.get(sym) or labels.get(sym) or name_of(sym),
            "path": f"{crate_of(sym)}::{path_of(sym)}",
            "crate": crate_of(sym),
            "kind": i.get("kind", "?"),
            "def": f"{d['file']}:{d['line']}" if d else None,
            "sig": "\n".join(i.get("sig", "").splitlines()[:8]),
            "seed": sym in seed_set or fn_seed(sym),
        }
        if sym in seed_set:
            n["members"] = [member_sig(m) for m in sorted(members[sym], key=lambda m: defs.get(m, {}).get("line", 0))]
            n["usedBy"] = len(uses[sym])
        return n

    edges = [{"d": "calls", "from": nid(a), "to": nid(b), "role": "calls", "sites": ev, "m": [nid(a), nid(b)]} for (a, b), ev in calls.items()]
    edges += [
        {"d": "flow", "from": nid(fn), "to": nid(t), "role": role, "sites": ev, "m": [nid(fn), nid(t)] if role == "constructs" else [nid(t), nid(fn)]}
        for (fn, t, role), ev in flow.items()
    ]
    edges += [{"d": "types", "from": nid(a), "to": nid(b), "role": "field", "via": via, "sites": [], "m": [nid(a), nid(b)]} for a, b, via in type_edges]
    if mods:
        for (a, b), e in mods["edges"].items():
            same = a in in_cycle and in_cycle.get(b) == in_cycle[a]
            edges.append({
                "d": "modules", "from": mid[a], "to": mid[b], "role": "cycle" if same else "depends",
                "sites": [f"{f}:{line}" for f, line in sorted(e["sites"])],
                "names": sorted(e["names"].items(), key=lambda kv: (-kv[1], kv[0])),
                "m": [mid[a], mid[b]],
            })
    edges.sort(key=lambda e: (e["d"], e["from"], e["to"], e["role"]))
    data = {
        "meta": {
            "title": flt.get("title", "Codemap"),
            "question": flt.get("question", ""),
            "commit": meta["commit"],
            "workspace": meta["workspace"],
            "repo": meta.get("repo", ""),
            "indexedAt": meta["indexed_at"],
            "filter": {k: v for k, v in flt.items() if k not in ("title", "question")},
        },
        "diagrams": diagrams,
        "tooBig": {"maxNodes": max_nodes, "types": len(seeds), "flow": len(flow_nodes), "calls": len(call_nodes), "modules": n_mods} if too_big else None,
        "nodes": {**{nid(sym): node(sym) for sym in sorted(seed_set | flow_nodes | call_nodes, key=path_of)}, **mod_nodes},
        "edges": edges,
        "cycles": [[mid[k] for k in comp] for comp in mods["cycles"]] if mods else [],
        "mutual": [[mid[a], mid[b]] for a, b in mods["mutual"]] if mods else [],
    }
    (out / "graph.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"seeds {len(seeds)}, type edges {len(type_edges)}, flow nodes {len(flow_nodes)}, flow edges {len(flow)}, call nodes {len(call_nodes)}"
          + (f", modules {n_mods}, module edges {len(mods['edges'])}, cycles {len(mods['cycles'])}" if mods else "") + f" -> {out}/graph.json")
    if mods:
        for i, comp in enumerate(mods["cycles"], 1):
            print(f"cycle {i}: " + ", ".join(module_label(k) for k in comp))
        for a, b in mods["mutual"]:
            print(f"mutual: {module_label(a)} <-> {module_label(b)}")
    hubs = collections.Counter()
    for (fn, t, _r), ev in flow.items():
        hubs[t] += len(ev)
    if too_big:
        print(f"TOO BIG (max_nodes={max_nodes}): diagrams omitted. Busiest types: " + ", ".join(f"{name_of(t)}({n})" for t, n in hubs.most_common(8)))
    else:
        # Enough to judge the layout without opening the SVGs or taking a screenshot.
        print("diagrams: " + (", ".join(diagrams) if diagrams else "none"))
        if hubs:
            top = hubs.most_common(3)
            warn = " <- dominates; consider excluding it or splitting the filter" if len(top) > 0 and top[0][1] > 0.4 * sum(hubs.values()) and len(hubs) > 4 else ""
            print("busiest types: " + ", ".join(f"{name_of(t)}({n})" for t, n in top) + warn)


def page(out_dir, artifact):
    """Assemble index.html from graph.json, the rendered SVGs and the optional notes.html."""
    out = Path(out_dir)
    if not (out / "graph.json").exists():
        sys.exit(f"codemap: no graph.json in {out}; run `codemap render` first")
    data = json.load(open(out / "graph.json"))
    html = (Path(__file__).parent / "page.html").read_text()
    missing = [m for m in PAGE_MARKERS if html.count(m) != 1]
    if missing:
        sys.exit(f"codemap: page.html must contain each of {missing} exactly once (run `codemap lint`)")
    svgs = {}
    for name in data["diagrams"]:
        if not (out / f"{name}.svg").exists():
            sys.exit(f"codemap: {out}/{name}.svg is missing; run `codemap render` (not `page`) to redraw it")
        svg = (out / f"{name}.svg").read_text()
        # mermaid-cli pins a white background and a max-width; the page themes and sizes it.
        # Graphviz output starts with an XML prolog and doctype, which don't belong inside HTML.
        svgs[name] = re.sub(r'style="max-width:[^"]*"', "", svg[svg.index("<svg"):], count=1)
    notes_file = out / "notes.html"
    payload = json.dumps(data, sort_keys=True).replace("</", "<\\/")
    html = (
        html.replace("__TITLE__", data["meta"]["title"].replace("&", "&amp;").replace("<", "&lt;"))
        .replace("/*__DATA__*/", payload)
        .replace("<!--__SVGS__-->", "".join(f'<template id="svg-{k}">{v}</template>' for k, v in svgs.items()))
        .replace("<!--__NOTES__-->", notes_file.read_text() if notes_file.exists() else "")
    )
    if artifact:
        # The Artifact viewer adds its own document skeleton.
        html = re.sub(r"(?s)^.*?<!--__BODY_START__-->", "", html)
        html = re.sub(r"(?s)<!--__BODY_END__-->.*$", "", html)
        target = out / "artifact.html"
    else:
        target = out / "index.html"
    target.write_text(html)
    print(f"page: {target}")


# Query graph.json -----------------------------------------------------------------------------------

def graph_cmd(out_dir, args):
    """Read a rendered graph.json without hand-written Python. `nodes` maps id -> node dict."""
    gp = Path(out_dir) / "graph.json"
    if not gp.exists():
        sys.exit(f"codemap: no graph.json in {out_dir}; run `codemap render` first")
    d = json.load(open(gp))
    nodes, edges = d["nodes"], d["edges"]
    sub = args[0] if args else "summary"
    rx = re.compile(args[1]) if len(args) > 1 else re.compile("")

    def label(i):
        return nodes[i]["label"] if i in nodes else i

    if sub == "summary":
        print(f"{d['meta']['title']} @ {d['meta']['commit'][:10]}; diagrams: {', '.join(d['diagrams']) or 'none'}")
        print(f"nodes {len(nodes)}, edges {len(edges)}, cycles {len(d['cycles'])}, mutual {len(d['mutual'])}, tooBig {bool(d['tooBig'])}")
        for k, n in sorted(collections.Counter(e["d"] for e in edges).items()):
            print(f"  {k} edges: {n}")
    elif sub == "nodes":
        for i, n in sorted(nodes.items(), key=lambda kv: kv[1]["path"]):
            if rx.search(n["path"]):
                print(f"{i}  {n['kind']:<10} {'seed ' if n['seed'] else '     '}{n['path']}  {n['def'] or ''}")
    elif sub == "edges":
        for e in edges:
            if rx.search(label(e["from"])) or rx.search(label(e["to"])):
                print(f"{e['d']:<7} {label(e['from'])} -[{e['role']}]-> {label(e['to'])}  {', '.join(e['sites'][:3])}")
    elif sub == "cycles":
        for c in d["cycles"]:
            print("cycle: " + ", ".join(label(i) for i in c))
        for a, b in d["mutual"]:
            print(f"mutual: {label(a)} <-> {label(b)}")
    else:
        sys.exit("usage: codemap graph SLUG [summary|nodes [REGEX]|edges [REGEX]|cycles]")


# Lint the skill itself ------------------------------------------------------------------------------

def lint():
    """Checks that make edits to the skill fail early instead of silently drifting."""
    root = Path(__file__).resolve().parent.parent
    bad = 0
    for f in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".md", ".py", ".html", ".yml", ".json") or p.name == "codemap"):
        if "__pycache__" in f.parts:
            continue
        text = f.read_text()
        # page.html carries display glyphs (arrows, middle dots) in UI strings on purpose.
        for n, line in enumerate(text.splitlines(), 1):
            if not line.isascii() and f.suffix != ".html":
                print(f"non-ASCII  {f.relative_to(root)}:{n}: {line.strip()[:70]}")
                bad += 1
        if f.suffix == ".json":
            json.loads(text)
    html = (root / "scripts" / "page.html").read_text()
    for m in PAGE_MARKERS:
        if html.count(m) != 1:
            print(f"page.html: {m} appears {html.count(m)} times, expected 1")
            bad += 1
    compile((root / "scripts" / "codemap.py").read_text(), "codemap.py", "exec")
    print("lint: ok" if not bad else f"lint: {bad} problem(s)")
    sys.exit(1 if bad else 0)


# Verify ------------------------------------------------------------------------------------------

def verify(g, doc):
    text = Path(doc).read_text(errors="replace")
    text = re.sub(r"<style.*?</style>|<script.*?</script>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    types = set(re.findall(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b", text))
    # Functions written as "name()" or "Type::name"; std-only methods would always read as missing.
    std = {"cfg", "derive", "matches", "into", "try_into", "as_ref", "to_owned", "to_string", "unwrap", "expect", "clone", "iter", "map"}
    fns = set(re.findall(r"\b([a-z_][a-z0-9_]{2,})\(", text)) | set(re.findall(r"\b[A-Z][A-Za-z0-9]*::([a-z_][a-z0-9_]+)\b", text))
    fns -= std
    known_types, known_fns = set(), set()
    for s, i in g["info"].items():
        if i["kind"] in TYPE_KINDS | {"EnumMember"}:
            known_types.add(name_of(s))
        elif i["kind"] in FN_KINDS:
            known_fns.add(name_of(s))
    for title, names, known in (("Type/variant names", types, known_types), ("Function names", fns, known_fns)):
        missing = sorted(n for n in names if n not in known)
        print(f"{title}: {len(names)} in doc, {len(names) - len(missing)} found, {len(missing)} missing")
        for n in missing:
            print(f"  missing  {n}")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "build":
        build(*args[:3])
    elif cmd == "find":
        if len(args) < 2:
            die("usage: codemap find REGEX [--kinds K,K] [--limit N]")
        kinds = TYPE_KINDS | FN_KINDS | MEMBER_KINDS
        if "--kinds" in args:
            kinds = set(opt_value(args, "--kinds").split(","))
        find(load(args[0]), args[1], kinds, int(opt_value(args, "--limit", 60)))
    elif cmd == "refs":
        if len(args) < 2:
            die("usage: codemap refs REGEX [--limit N] [--by-fn]")
        refs(load(args[0]), args[1], int(opt_value(args, "--limit", 25)), "--by-fn" in args)
    elif cmd == "render":
        meta = json.load(open(Path(args[0]) / "meta.json"))
        render(load(args[0]), load_filter(args[1]), args[2], meta)
    elif cmd == "seeds":
        seeds_cmd(load(args[0]), load_filter(args[1]))
    elif cmd == "page":
        page(args[0], "--artifact" in args)
    elif cmd == "graph":
        graph_cmd(args[0], args[1:])
    elif cmd == "version":
        print(GRAPH_VERSION)
    elif cmd == "lint":
        lint()
    elif cmd == "crates":
        crates(args[0], args[1] if len(args) > 1 else None)
    elif cmd == "modules":
        opts, names, it = {}, [], iter(args[1:])
        for a in it:
            if a in ("--depth", "--externals", "--exclude", "--skip"):
                opts[a[2:]] = next(it)
            else:
                names.append(a)
        if not names:
            sys.exit("usage: codemap modules CRATE... [--depth N] [--externals REGEX] [--exclude REGEX] [--skip REGEX]")
        depth = int(opts["depth"]) if "depth" in opts else None
        g = load(args[0])
        modules(g, resolve_crates(g, names), depth, opts.get("externals"), re.compile(opts.get("exclude", DEFAULT_EXCLUDE)), opts.get("skip"))
    elif cmd == "verify":
        verify(load(args[0]), args[1])
    else:
        sys.exit(f"unknown command {cmd}")

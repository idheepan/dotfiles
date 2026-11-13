"""Deterministic item graph over a rust-analyzer SCIP index.

Every node and edge this script emits comes from the index or the ast-grep role scan. The caller
(Claude) only chooses filters and writes prose around the output.

  codemap.py build  <index_dir> <src_dir> <scip_pb_dir>
  codemap.py find   <index_dir> <regex> [--kinds K,K]
  codemap.py refs   <index_dir> <regex>
  codemap.py render <index_dir> <filter.json> <out_dir>
  codemap.py seeds  <index_dir> <filter.json>
  codemap.py page   <out_dir> [--artifact]      (also finishes the SVGs: palette, classes, badges)
  codemap.py mermaid-config                   (mermaid.json merged with palette themeVariables)
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
# Test code by module name: `tests`, `foo_tests`, `test_support`, `test_utils`, `testing`.
DEFAULT_EXCLUDE = r"(^|::)(tests?|\w+_tests?|test_(support|utils?|helpers?)|testing)(::|$)"
# Bumped when build() output changes meaning; an older graph.pkl is refused with "run codemap rebuild".
GRAPH_VERSION = 2
# Placeholders page() fills in page.html; each must appear exactly once.
PAGE_MARKERS = ("__TITLE__", "/*__PALETTE__*/", "/*__DATA__*/", "<!--__SVGS__-->", "<!--__BODY_START__-->", "<!--__BODY_END__-->")
# The only file that holds colors; see palette_css() and `codemap lint`.
PALETTE_FILE = Path(__file__).parent / "palette.json"
# Hidden first word of every flowchart label: mermaid sizes the box with it, so the kind badge
# drawn by finish_svg() has room at the left edge. Mermaid trims leading spaces, hence a word.
BADGE_PAD = "000"
KIND_CLASS = {"Function": "fn", "Method": "fn", "StaticMethod": "fn", "TraitMethod": "fn", "Struct": "struct",
              "Enum": "enum", "Trait": "trait", "TypeAlias": "alias", "Union": "union"}
# f-hook for functions, m for methods, identical-to sign for type aliases.
KIND_GLYPH = {"Function": "\u0192", "Method": "m", "StaticMethod": "m", "TraitMethod": "m", "Struct": "S",
              "Enum": "E", "Trait": "T", "TypeAlias": "\u2261", "Union": "U"}


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
               "max_members", "modules", "classes"}
CLASSES_KEYS = {"methods"}
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


def patterns(flt, key):
    """A filter value is a list of regexes, each reported separately so the report shows which
    entry matched nothing. Returns [(text, compiled)]."""
    items = flt.get(key)
    if items is None:
        return []
    if not isinstance(items, list) or not all(isinstance(i, str) and i for i in items):
        die(f"{key} must be a list of non-empty regex strings, got {items!r}")
    return [(i, compile_rx(key, i)) for i in items]


def close(word, options, n=4):
    import difflib
    return difflib.get_close_matches(word, sorted(options), n=n, cutoff=0.6)


def crate_names(g):
    if "_crates" not in g:
        g["_crates"] = {crate_of(s) for s in g["info"]}
    return g["_crates"]


def resolve_crates(g, names):
    """Package names as the index spells them; `-` and `_` are interchangeable on input."""
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
    for k in ("callers", "callees", "max_members"):
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
    cls = flt.get("classes")
    if cls is not None:
        if not isinstance(cls, (bool, dict)):
            die(f"classes must be true, false or an object, got {cls!r}")
        if isinstance(cls, dict):
            bad = sorted(set(cls) - CLASSES_KEYS)
            if bad:
                die(f"unknown classes option(s) {', '.join(bad)}. Valid: {', '.join(sorted(CLASSES_KEYS))}")
            if "methods" in cls and not isinstance(cls["methods"], bool):
                die(f"classes.methods must be true or false, got {cls['methods']!r}")
        if cls is not False and not flt.get("seed_types"):
            die("classes draws the seed_types; give seed_types too")
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
    # Write-then-rename so a reader never sees a half-written pickle.
    tmp_pkl = index_dir / "graph.pkl.tmp"
    with open(tmp_pkl, "wb") as fh:
        pickle.dump({"version": GRAPH_VERSION, "info": info, "defs": defs, "edges": edges}, fh)
    tmp_pkl.replace(index_dir / "graph.pkl")
    print(f"graph: {len(defs)} defs, {len(edges)} refs, {sum(len(v) for v in roles.values())} role tags in {time.time() - t:.1f}s")


def opt_value(args, name, default=None):
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 >= len(args):
        die(f"{name} needs a value")
    return args[i + 1]


def positional(args, valued):
    """The first argument after the index dir that is neither an option nor an option's value."""
    it = iter(args[1:])
    for a in it:
        if a in valued:
            next(it, None)
        elif not a.startswith("--"):
            return a
    return None


def cli_scope(g, args):
    """(crate set, exclude regex) from --crates/--exclude, as a filter's `crates`/`exclude` would.
    Without --crates every crate is searched, and test modules are always dropped by default."""
    crates = opt_value(args, "--crates")
    crate_set = set(resolve_crates(g, crates.split(","))) if crates else set()
    return crate_set, compile_rx("exclude", opt_value(args, "--exclude", DEFAULT_EXCLUDE))


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
        sys.exit(f"codemap: {index_dir}/graph.pkl was built by an older codemap.py. Run: codemap rebuild (seconds)")
    return g


# Queries -----------------------------------------------------------------------------------------

def find(g, rx, kinds, limit=60, crate_set=frozenset(), exclude=None):
    check_kinds(kinds)
    r = compile_rx("find", rx)
    rows, dropped = [], 0
    for sym, i in g["info"].items():
        if i["kind"] not in kinds:
            continue
        p = path_of(sym)
        if hits(r, p):
            if (crate_set and crate_of(sym) not in crate_set) or (exclude and exclude.search(p)):
                dropped += 1
                continue
            d = g["defs"].get(sym)
            rows.append((crate_of(sym), p, i["kind"], f"{d['file']}:{d['line']}" if d else "external", i["sig"].splitlines()[0][:100] if i["sig"] else ""))
    for c, p, k, loc, sig in sorted(rows)[:limit]:
        print(f"{k:13} {c}::{p}  {loc}  {sig}")
    print(f"-- {len(rows)} matches" + (f", first {limit} shown (--limit N, or narrow the regex)" if len(rows) > limit else "")
          + (f"; {dropped} more outside --crates or dropped by --exclude (default: test modules)" if dropped else ""))
    if not rows and not dropped:
        print("\n".join(why_no_match(g, rx, kinds, "-- ")))


def refs(g, rx, limit=25, by_fn=False, crate_set=frozenset(), exclude=None):
    r = compile_rx("refs", rx)
    by = collections.defaultdict(list)
    for container, target, f, line, role in g["edges"]:
        if hits(r, path_of(target)):
            # The scope applies to the referring side: who uses this, from which crate, not in tests.
            if container and ((crate_set and crate_of(container) not in crate_set) or (exclude and exclude.search(path_of(container)))):
                continue
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
    max_members = flt.get("max_members", 12)
    roles_wanted = set(flt.get("roles") or ["constructs", "matches"])
    # `classes` swaps the mermaid type and flow views for one Graphviz class diagram nested by
    # module. It is meant for whole crates.
    classes = flt.get("classes") not in (None, False)
    copt = flt["classes"] if isinstance(flt.get("classes"), dict) else {}
    if classes:
        roles_wanted = set()

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
    out.mkdir(parents=True, exist_ok=True)
    seeds = sorted(seed_set, key=path_of)

    members = collections.defaultdict(list)
    for s, i in info.items():
        if i["kind"] in MEMBER_KINDS and parent_of(s) in seed_set:
            members[parent_of(s)].append(s)

    # Type -> type edges (shared by the types and classes views). A member with a source span takes
    # its arrows from the index's references: a reference counts when it sits on the member's lines
    # and the member's signature names the target, which drops doc links between fields. Matching
    # names instead links every namesake (two `RunConfig`s, a variant called `Empty`). Members
    # without a span (generated proto types) fall back to the names in their signature.
    # type_sites keeps the reference lines behind each (container, target, member) edge so the page
    # can list them; fallback edges have no source span and stay without sites.
    type_edges = set()
    type_sites = collections.defaultdict(set)
    spans = {}
    for s in seeds:
        ms = sorted((defs[m]["line"], m) for m in members[s] if m in defs)
        spans[s] = [(line, ms[k + 1][0] if k + 1 < len(ms) else line + 50, m) for k, (line, m) in enumerate(ms)]
    for container, target, f, line, _role in g["edges"]:
        if container not in seed_set:
            continue
        t = parent_of(target) if info.get(target, {}).get("kind") == "EnumMember" else target
        if t not in seed_set or t == container:
            continue
        for lo, hi, m in spans[container]:
            if lo <= line < hi and name_of(t) in re.findall(r"[A-Za-z0-9_]+", info[m]["sig"]):
                type_edges.add((container, t, name_of(m)))
                type_sites[(container, t, name_of(m))].add((f, line))
                break
    by_name = collections.defaultdict(list)
    for s in seeds:
        by_name[name_of(s)].append(s)
    for s in seeds:
        for m in members[s]:
            if m in defs:
                continue
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

    # Class diagram: one box per seed type (kind, name, fields or variants, optionally methods),
    # inside nested Graphviz clusters crate -> module -> submodule, so where a type lives reads
    # off the picture. Arrows are the same field-mentions-type edges as `types`, one per pair.
    cl, class_pairs = [], {}
    if classes:
        methods = collections.defaultdict(list)
        if copt.get("methods"):
            # Impl methods are `impl#[Type][Trait]name().` under the module holding the impl, which
            # need not be the type's module, so match on the impl's self type: by path when the
            # impl sits beside the type, else by name when that name is unique in the crate.
            by_path = {(crate_of(s), path_of(s)): s for s in seeds}
            by_type_name = collections.defaultdict(list)
            for s in seeds:
                by_type_name[(crate_of(s), name_of(s))].append(s)
            for s, i in info.items():
                crate, path, _trait, self_type = parse(s)
                # No definition site means derive- or macro-generated (`eq` from PartialEq).
                if i["kind"] not in FN_KINDS or not self_type or not in_scope(s) or s not in defs:
                    continue
                owner = by_path.get((crate, path.rsplit("::", 1)[0]))
                named = by_type_name.get((crate, self_type.split("::")[-1]), [])
                if not owner and len(named) == 1:
                    owner = named[0]
                if owner:
                    methods[owner].append(s)

        def html(s):
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        def tidy(sig):
            # rust-analyzer prints the default allocator; it is noise in every Vec and map.
            sig = re.sub(r",\s*Global>", ">", sig)
            return sig if len(sig) <= 90 else sig[:87] + "..."

        def rows(syms, sig):
            ms = sorted(syms, key=lambda m: defs.get(m, {}).get("line", 0))
            out = [f'<TR><TD ALIGN="LEFT">{html(tidy(sig(m)))}</TD></TR>' for m in ms[:max_members]]
            if len(ms) > max_members:
                out.append(f'<TR><TD ALIGN="LEFT"><I>... {len(ms) - max_members} more</I></TD></TR>')
            return out

        def fn_sig(m):
            return re.sub(r"^(const\s+|async\s+|unsafe\s+)*fn\s+", "", member_sig(m))

        def box(s):
            kind = info[s]["kind"]
            # No colors here: finish_svg() classes the <<Kind>> row and palette.json colors it.
            parts = [
                '<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" STYLE="rounded">',
                f'<TR><TD><FONT POINT-SIZE="9">&lt;&lt;{kind}&gt;&gt;</FONT></TD></TR>',
                f"<TR><TD><B>{html(name_of(s))}</B></TD></TR>",
            ]
            body = rows(members[s], member_sig)
            if body:
                parts += ["<HR/>"] + body
            # One section per impl block group: (module holding the impl, trait). A header row
            # names the trait and, when the impl is not beside the type, the module it is in.
            home = module_of(s)[1]
            impls = collections.defaultdict(list)
            for m in methods[s]:
                impls[(module_of(m)[1], parse(m)[2] or "")].append(m)
            for (where, trait) in sorted(impls, key=lambda k: (k[0] != home, k[1] != "", k)):
                head = ("impl " + trait if trait else "impl") + ("" if where == home else f" in {where or 'crate root'}")
                parts += ["<HR/>", f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="9"><I>{html(head)}</I></FONT></TD></TR>']
                parts += rows(impls[(where, trait)], fn_sig)
            parts.append("</TABLE>")
            return "<" + "".join(parts) + ">"

        # Module tree: key = (crate, segments...), types listed under the module that defines them.
        placed = collections.defaultdict(list)
        children = collections.defaultdict(set)
        for s in seeds:
            crate, mod = module_of(s)
            key = (crate, *(mod.split("::") if mod else []))
            placed[key].append(s)
            for i in range(1, len(key)):
                children[key[:i]].add(key[: i + 1])

        cl = [
            "digraph classes {",
            '  graph [rankdir=TB, newrank=true, bgcolor="transparent", fontname="Arial", fontsize=12, nodesep=0.25, ranksep=0.7, pad=0.3]',
            '  node [shape=plain, fontname="Arial", fontsize=11]',
            '  edge [fontname="Arial", fontsize=9, arrowsize=0.6]',
        ]

        def cluster(key, depth):
            pad = "  " * (depth + 1)
            label = key[0] if len(key) == 1 else "::".join(key[1:])
            cid = "cluster_" + re.sub(r"[^A-Za-z0-9_]", "_", "__".join(key))
            cl.append(f'{pad}subgraph "{cid}" {{')
            # depth-N picks the frame fill (cluster, cluster2, cluster3) in palette_css().
            cl.append(f"{pad}  label={dot_str(label)}; labeljust=l; style=rounded; class=\"depth-{min(depth + 1, 3)}\"; fontsize={max(16 - 2 * depth, 11)}; penwidth={max(2.0 - 0.5 * depth, 1.0)}")
            for s in sorted(placed[key], key=path_of):
                cl.append(f'{pad}  {nid(s)} [id="{nid(s)}", label={box(s)}]')
            for child in sorted(children[key]):
                cluster(child, depth + 1)
            cl.append(f"{pad}}}")

        for root in sorted({k[:1] for k in placed} | {k[:1] for k in children}):
            cluster(root, 0)
        # One arrow per type pair, labelled with every member that makes it.
        for a, b, via in type_edges:
            class_pairs.setdefault((a, b), set()).add(via)
        for (a, b), vias in sorted(class_pairs.items(), key=lambda kv: (nid(kv[0][0]), nid(kv[0][1]))):
            text = ", ".join(sorted(vias))
            cl.append(f'  {nid(a)} -> {nid(b)} [id="L_{nid(a)}_{nid(b)}_0", label={dot_str(text if len(text) <= 40 else text[:37] + "...")}]')
        cl.append("}")

    def flowchart(direction, nodes, edges, is_seed):
        lines = [f"flowchart {direction}"]
        groups = collections.defaultdict(set)
        for n in nodes:
            groups[crate_of(n)].add(n)
        for crate, ns in sorted(groups.items()):
            lines.append(f"  subgraph {re.sub(r'[^A-Za-z0-9_]', '_', crate)}[\"{crate}\"]")
            for n in sorted(ns, key=lambda n: labels[n]):
                text = f"{BADGE_PAD} {esc(labels[n])}"
                lines.append(f'    {nid(n)}[["{text}"]]' if is_seed(n) else f'    {nid(n)}("{text}")')
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
            mc.append(f'    label={dot_str(crate or "external crates")}; labeljust=l; style=rounded; class="depth-1"')
            for k in sorted(ks, key=module_label):
                attrs = [f"label={dot_str(module_label(k))}", f'id="{mid[k]}"']
                if k[0] == "ext":
                    attrs.append('style="rounded,dashed"')
                if k in in_cycle:
                    attrs += ['class="cycle"', "penwidth=2"]
                mc.append(f"    {mid[k]} [{', '.join(attrs)}]")
            mc.append("  }")
        for (a, b), e in sorted(mods["edges"].items(), key=lambda kv: (mid[kv[0][0]], mid[kv[0][1]])):
            attrs = [f'label="{len(e["sites"])}"', f'id="L_{mid[a]}_{mid[b]}_0"']
            if a in in_cycle and in_cycle.get(b) == in_cycle[a]:
                attrs += ['class="cycle"', "penwidth=1.6"]
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
    for name in ("calls", "flow", "types", "modules", "classes"):
        for ext in (".mmd", ".dot", ".svg"):
            (out / f"{name}{ext}").unlink(missing_ok=True)
    # A screenshot of the previous render would otherwise pass for this one.
    (out / "screenshot.png").unlink(missing_ok=True)
    diagrams = []
    if classes and seeds:
        (out / "classes.dot").write_text("\n".join(cl) + "\n")
        diagrams.append("classes")
    if call_nodes:
        (out / "calls.mmd").write_text("\n".join(cc) + "\n")
        diagrams.append("calls")
    if flow:
        (out / "flow.mmd").write_text("\n".join(fc) + "\n")
        diagrams.append("flow")
    if seeds and not classes:
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
            "module": module_of(sym)[1],
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
    def field_sites(a, b, vias):
        return [f"{f}:{line}" for f, line in sorted(set().union(*(type_sites[(a, b, v)] for v in vias)))]

    if classes:
        edges += [{"d": "classes", "from": nid(a), "to": nid(b), "role": "field", "via": ", ".join(sorted(v)), "sites": field_sites(a, b, v), "m": [nid(a), nid(b)]}
                  for (a, b), v in class_pairs.items()]
    else:
        edges += [{"d": "types", "from": nid(a), "to": nid(b), "role": "field", "via": via, "sites": field_sites(a, b, [via]), "m": [nid(a), nid(b)]}
                  for a, b, via in type_edges]
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
        # 37 modules / 179 edges was unreadable at fit-to-width; 13 / 28 at depth 1 read cleanly.
        if n_mods > 20 and mopt.get("depth") is None:
            print(f"dense: {n_mods} modules, {len(mods['edges'])} edges unfolded. Render a second filter with "
                  "\"depth\": 1 as the overview and keep this one as the detail view.")
    hubs = collections.Counter()
    for (fn, t, _r), ev in flow.items():
        hubs[t] += len(ev)
    # Enough to judge the layout without opening the SVGs or taking a screenshot.
    print("diagrams: " + (", ".join(diagrams) if diagrams else "none"))
    if hubs:
        top = hubs.most_common(3)
        warn = " <- dominates; consider excluding it or splitting the filter" if len(top) > 0 and top[0][1] > 0.4 * sum(hubs.values()) and len(hubs) > 4 else ""
        print("busiest types: " + ", ".join(f"{name_of(t)}({n})" for t, n in top) + warn)


# Palette and SVG finishing -----------------------------------------------------------------------
#
# palette.json holds every color as --<token> custom properties. The rules below color the diagrams
# from those tokens, and the same stylesheet goes into the page and into each standalone SVG, so the
# two never drift. `svg.cm` is the root of a finished SVG. !important beats the ID-scoped rules
# mermaid embeds and Graphviz's presentation attributes.

# A hex color outside palette.json; `&#45;` entities and mermaid's `#124;` escapes are not colors.
HEX_RX = re.compile(r"(?<![&\w])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![\w;])")

DIAGRAM_CSS = """
svg.cm { background: var(--canvas); }
svg.cm text, svg.cm tspan { fill: var(--fg) !important; font-family: Arial, Helvetica, sans-serif !important; }
svg.cm .node .label-container, svg.cm g.node > path { filter: drop-shadow(0 1px 1.5px var(--node-shadow)); }
svg.cm .node .label-container, svg.cm .node .outer-path path:first-child, svg.cm g.node > path, svg.cm g.node > polygon { fill: var(--node) !important; stroke: var(--node-line) !important; stroke-width: 1px !important; }
svg.cm .node .outer-path path + path, svg.cm .divider path { stroke: var(--node-line) !important; }
svg.cm .node.gen .label-container, svg.cm .node.gen .outer-path path:first-child, svg.cm g.node.gen > path { fill: var(--cm-hatch, var(--node)) !important; }
svg.cm .node.seed .label-container, svg.cm .node.seed .outer-path path + path { stroke: var(--accent) !important; stroke-width: 2px !important; }
svg.cm .node.cycle .label-container, svg.cm g.node.cycle > path, svg.cm g.node.cycle > polygon { stroke: var(--cycle) !important; stroke-width: 2px !important; }
svg.cm .node.hit .label-container, svg.cm .node.hit .outer-path path:first-child, svg.cm g.node.hit > path, svg.cm g.node.hit > polygon { fill: var(--hit) !important; stroke: var(--hit-line) !important; }
svg.cm .node.sel .label-container, svg.cm .node.sel .outer-path path:first-child, svg.cm g.node.sel > path, svg.cm g.node.sel > polygon { fill: var(--accent-soft) !important; stroke: var(--accent) !important; stroke-width: 3px !important; }
svg.cm g.node > text[font-style="italic"] { fill: var(--muted) !important; }
svg.cm .cluster rect, svg.cm g.cluster > path, svg.cm g.cluster > polygon { fill: var(--cluster) !important; stroke: var(--cluster-line) !important; }
svg.cm g.cluster.depth-2 > path, svg.cm g.cluster.depth-2 > polygon { fill: var(--cluster2) !important; }
svg.cm g.cluster.depth-3 > path, svg.cm g.cluster.depth-3 > polygon { fill: var(--cluster3) !important; }
svg.cm .cluster-label text, svg.cm .cluster-label tspan, svg.cm g.cluster > text { fill: var(--muted) !important; font-weight: 600; }
svg.cm .edgePaths path, svg.cm path[data-role] { stroke: var(--edge) !important; }
svg.cm path[data-role="constructs"] { stroke: var(--make) !important; }
svg.cm path[data-role="matches"] { stroke: var(--take) !important; }
svg.cm path[data-role="cycle"] { stroke: var(--cycle) !important; stroke-width: 2px !important; }
svg.cm marker path, svg.cm .arrowMarkerPath { fill: var(--edge) !important; stroke: var(--edge) !important; }
svg.cm .edgeLabel rect, svg.cm .edgeLabel .background, svg.cm .labelBkg { fill: var(--cluster) !important; background: var(--cluster) !important; opacity: 1 !important; }
svg.cm .edgeLabel text, svg.cm .edgeLabel tspan { fill: var(--muted) !important; }
svg.cm g.edge > path { stroke: var(--edge) !important; }
svg.cm g.edge > polygon { fill: var(--edge) !important; stroke: var(--edge) !important; }
svg.cm g.edge > text { fill: var(--muted) !important; }
svg.cm g.edge.cycle > path { stroke: var(--cycle) !important; stroke-width: 2px !important; }
svg.cm g.edge.cycle > polygon { fill: var(--cycle) !important; stroke: var(--cycle) !important; }
svg.cm .cm-pad { fill-opacity: 0 !important; }
svg.cm .cm-badge rect { fill-opacity: 0.16; stroke: none; }
svg.cm .cm-badge text { font-size: 11px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
svg.cm .cm-tag rect { fill: var(--node) !important; stroke: var(--muted) !important; stroke-width: 1px; }
svg.cm .cm-tag text { fill: var(--muted) !important; font-size: 8.5px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
svg.cm .cm-hatch-bg { fill: var(--node); }
svg.cm .cm-hatch-line { stroke: var(--node-line); stroke-width: 1.5px; opacity: 0.6; }
"""


def load_palette():
    return json.loads(PALETTE_FILE.read_text())


def contrast(a, b):
    """WCAG 2 contrast ratio of two #rrggbb colors."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    lo, hi = sorted((lum(a), lum(b)))
    return (hi + 0.05) / (lo + 0.05)


def palette_css(page):
    """Tokens for light and dark, then the diagram rules. The page also honors its theme toggle."""
    toks = load_palette()["tokens"]
    light = "".join(f"--{k}:{v[0]};" for k, v in toks.items())
    dark = "".join(f"--{k}:{v[1]};" for k, v in toks.items())
    if page:
        head = (f":root{{{light}}}\n@media (prefers-color-scheme: dark){{:root:not([data-theme=\"light\"]){{{dark}color-scheme:dark;}}}}\n"
                f":root[data-theme=\"dark\"]{{{dark}color-scheme:dark;}}\n")
    else:
        head = f":root{{{light}}}\n@media (prefers-color-scheme: dark){{:root{{{dark}}}}}\n"
    kinds = "".join(
        f"svg.cm .cm-kind.kind-{k} {{ fill: var(--kind-{k}) !important; }}\n"
        f"svg.cm .cm-badge.kind-{k} rect {{ fill: var(--kind-{k}); }}\n"
        f"svg.cm .cm-badge.kind-{k} text {{ fill: var(--kind-{k}) !important; }}\n"
        for k in sorted(set(KIND_CLASS.values())))
    return head + DIAGRAM_CSS + kinds


def mermaid_config():
    """mermaid.json (layout) plus themeVariables from the light palette, so an SVG read without its
    stylesheet still has palette colors. The stylesheet in the SVG wins wherever it applies."""
    cfg = json.loads((Path(__file__).parent / "mermaid.json").read_text())
    t = {k: v[0] for k, v in load_palette()["tokens"].items()}
    cfg["themeVariables"].update({
        "background": t["canvas"], "primaryColor": t["node"], "mainBkg": t["node"], "primaryBorderColor": t["node-line"],
        "nodeBorder": t["node-line"], "primaryTextColor": t["fg"], "textColor": t["fg"], "lineColor": t["edge"],
        "clusterBkg": t["cluster"], "clusterBorder": t["cluster-line"], "edgeLabelBackground": t["cluster"],
    })
    print(json.dumps(cfg, indent=2))


def coords(s):
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", s)]
    return nums[0::2], nums[1::2]


def shape_box(seg, mermaid):
    """(x0, y0, x1, y1) of a node's outline, in the coordinates of the node's own group."""
    if mermaid:
        r = re.search(r'<rect class="basic label-container"[^>]*>', seg)
        if r:
            a = dict(re.findall(r'\b(x|y|width|height)="([-\d.]+)"', r.group(0)))
            x, y = float(a["x"]), float(a["y"])
            return x, y, x + float(a["width"]), y + float(a["height"])
        # Subroutine shape: the first four points are the inner box, the outer lines sit 8 px out.
        p = re.search(r'<polygon points="([^"]*)" class="label-container"(?: transform="translate\(([-\d.]+),\s*([-\d.]+)\)")?', seg)
        if p:
            xs, ys = coords(p.group(1))
            tx, ty = float(p.group(2) or 0), float(p.group(3) or 0)
            return min(xs[:4]) + tx, min(ys[:4]) + ty, max(xs[:4]) + tx, max(ys[:4]) + ty
        q = re.search(r'<g class="basic label-container outer-path">\s*<path d="([^"]*)"', seg)
        if not q:
            return None
        xs, ys = coords(q.group(1))
    else:
        xs, ys = [], []
        for s in re.findall(r'\b(?:points|d)="([^"]*)"', seg):
            x, y = coords(s)
            xs += x
            ys += y
        if not xs:
            return None
    return min(xs), min(ys), max(xs), max(ys)


NODE_OPEN = re.compile(r'<g class="node([^"]*)" id="([^"]*)"|<g id="(n[0-9a-f]{8})" class="node([^"]*)"')


def finish_node(seg, mo, nodes):
    """Classes, kind badge and generated tag for one node group; `seg` runs to the next node."""
    mermaid = not mo.group(3)
    if mermaid:
        hit = re.search(r"-(?:flowchart|classId)-(n[0-9a-f]{8})-\d+$", mo.group(2))
        n = nodes.get(hit.group(1)) if hit else None
    else:
        n = nodes.get(mo.group(3))
        # A Graphviz node group has no nested groups; edges follow it, so work on the group only.
        end = seg.index("</g>")
        seg, rest = seg[:end], seg[end:]
    if not n:
        return seg if mermaid else seg + rest
    kind = KIND_CLASS.get(n["kind"])
    # No definition site: prost output, derive or macro expansion. Crate: an external crate.
    gen = n["kind"] == "Crate" or (n["kind"] in TYPE_KINDS | FN_KINDS and not n.get("def"))
    classes = " ".join(c for c in (f"kind-{kind}" if kind else "", "seed" if n.get("seed") else "", "gen" if gen else "") if c)
    if classes:
        seg = seg.replace('class="node', f'class="node {classes}', 1)
    if not mermaid:
        # HTML-label rules (<HR/>) come out before the table outline, whose fill would hide them.
        outline = re.search(r"<path [^>]*/>", seg)
        if outline:
            rules = re.findall(r"<polygon [^>]*/>\n?", seg[:outline.start()])
            if rules:
                seg = re.sub(r"<polygon [^>]*/>\n?", "", seg[:outline.start()]) + outline.group(0) + "\n" + "".join(rules) + seg[outline.end():]
    box = shape_box(seg, mermaid)
    extra = ""
    pad = f'class="text-inner-tspan" font-weight="normal">{BADGE_PAD}</tspan>'
    if box and kind and mermaid and "-flowchart-" in mo.group(2) and pad in seg:
        seg = seg.replace(pad, pad.replace('text-inner-tspan"', 'text-inner-tspan cm-pad"'), 1)
        x0, y0, _x1, y1 = box
        h = min(16.0, y1 - y0 - 8)
        bx, by = x0 + 5, (y0 + y1 - h) / 2
        extra += (f'<g class="cm-badge kind-{kind}"><rect x="{bx:.1f}" y="{by:.1f}" width="{h:.1f}" height="{h:.1f}" rx="3"/>'
                  f'<text x="{bx + h / 2:.1f}" y="{by + h / 2:.1f}">{KIND_GLYPH[n["kind"]]}</text></g>')
    if box and gen:
        _x0, y0, x1, _y1 = box
        extra += (f'<g class="cm-tag"><rect x="{x1 - 28:.1f}" y="{y0 - 6:.1f}" width="22" height="12" rx="6"/>'
                  f'<text x="{x1 - 17:.1f}" y="{y0:.1f}">{"ext" if n["kind"] == "Crate" else "gen"}</text></g>')
    if not extra:
        return seg if mermaid else seg + rest
    if mermaid:
        at = seg.find('<g class="label"')
        return seg + extra if at < 0 else seg[:at] + extra + seg[at:]
    return seg + extra + rest


def finish_svg(svg, name, data):
    """Theme one rendered SVG and add what the renderers can't: node classes from graph.json (seed,
    kind, generated), kind badges on flowchart boxes, edge roles and the palette stylesheet.
    A finished SVG (root class `cm`) only gets its stylesheet replaced, so this is safe to rerun."""
    svg = re.sub(r'<style id="cm-palette">.*?</style>', "", svg[svg.index("<svg"):], count=1, flags=re.S)
    root = re.match(r"<svg\b[^>]*>", svg)
    if not re.search(r'\sclass="[^"]*\bcm\b', root.group(0)):
        sid = f"dg-{name}"
        # mermaid-cli pins a white background and a max-width; the stylesheet and page take over.
        tag = re.sub(r'\s(?:style|id)="[^"]*"', "", root.group(0))
        if re.search(r'\sclass="', tag):
            tag = re.sub(r'\sclass="([^"]*)"', r' class="\1 cm"', tag, count=1)
        else:
            tag = tag[:-1] + ' class="cm">'
        tag = tag[:4] + f' id="{sid}" style="--cm-hatch:url(#{sid}-hatch)"' + tag[4:]
        defs = (f'<defs><pattern id="{sid}-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
                '<rect width="6" height="6" class="cm-hatch-bg"/><line x1="0" y1="0" x2="0" y2="6" class="cm-hatch-line"/></pattern></defs>')
        body = svg[root.end():]
        roles = {(e["m"][0], e["m"][1]): e["role"] for e in data["edges"] if e["d"] == name}

        def role(m):
            r = roles.get((m.group(1), m.group(2)))
            return m.group(0) + (f' data-role="{r}"' if r else "")
        body = re.sub(r'data-id="(?:L|id)_(n[0-9a-f]{8})_(n[0-9a-f]{8})_\d+"', role, body)
        opens = list(NODE_OPEN.finditer(body))
        parts, last = [], 0
        for k, mo in enumerate(opens):
            end = opens[k + 1].start() if k + 1 < len(opens) else len(body)
            parts += [body[last:mo.start()], finish_node(body[mo.start():end], mo, data["nodes"])]
            last = end
        body = "".join(parts) + body[last:]
        # <<Kind>> rows: Graphviz class boxes write the text directly, mermaid class diagrams as
        # a guillemet annotation.
        body = re.sub(r"<text([^>]*)>&lt;&lt;(\w+)&gt;&gt;</text>",
                      lambda m: f'<text class="cm-kind kind-{KIND_CLASS.get(m.group(2), "fn")}"{m.group(1)}>&lt;&lt;{m.group(2)}&gt;&gt;</text>', body)
        body = re.sub('class="text-inner-tspan"([^>]*)>\u00ab(\\w+)\u00bb<',
                      lambda m: f'class="text-inner-tspan cm-kind kind-{KIND_CLASS.get(m.group(2), "fn")}"{m.group(1)}>\u00ab{m.group(2)}\u00bb<', body)
        svg = tag + defs + body
    at = svg.index(">") + 1
    return svg[:at] + f'<style id="cm-palette">{palette_css(False)}</style>' + svg[at:]


def page(out_dir, artifact):
    """Assemble index.html from graph.json, and the rendered SVGs."""
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
        # The standalone file is rewritten finished too, so it matches the page in both themes.
        svg = finish_svg((out / f"{name}.svg").read_text(), name, data)
        (out / f"{name}.svg").write_text(svg)
        # The page carries the palette stylesheet once instead of once per diagram.
        svgs[name] = re.sub(r'<style id="cm-palette">.*?</style>', "", svg, count=1, flags=re.S)
    payload = json.dumps(data, sort_keys=True).replace("</", "<\\/")
    html = (
        html.replace("/*__PALETTE__*/", palette_css(True))
        .replace("__TITLE__", data["meta"]["title"].replace("&", "&amp;").replace("<", "&lt;"))
        .replace("/*__DATA__*/", payload)
        .replace("<!--__SVGS__-->", "".join(f'<template id="svg-{k}">{v}</template>' for k, v in svgs.items()))
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
        print(f"nodes {len(nodes)}, edges {len(edges)}, cycles {len(d['cycles'])}, mutual {len(d['mutual'])}")
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
    elif sub == "by-module":
        # For the types/classes views: where the seed types live, and which modules' fields reach
        # into which others. Class-map counts come from here.
        def mod(i):
            n = nodes[i]
            return n.get("module", "::".join(n["path"].split("::")[1:-1])) or "(crate root)"
        per = collections.defaultdict(collections.Counter)
        for i, n in nodes.items():
            if n["seed"] and n["kind"] in TYPE_KINDS:
                per[mod(i)][n["kind"]] += 1
        total = collections.Counter()
        for c in per.values():
            total.update(c)
        print("types: " + ", ".join(f"{k} {v}" for k, v in sorted(total.items())) + f" in {len(per)} modules")
        for m in sorted(per):
            print(f"  {m:48} " + ", ".join(f"{k} {v}" for k, v in sorted(per[m].items())))
        cross = collections.defaultdict(list)
        for e in edges:
            if e["d"] in ("types", "classes") and e["from"] in nodes and e["to"] in nodes and mod(e["from"]) != mod(e["to"]):
                cross[(mod(e["from"]), mod(e["to"]))].append(f"{label(e['from'])} -> {label(e['to'])}")
        print("cross-module field links:")
        for (a, b), es in sorted(cross.items()):
            print(f"  {a} -> {b}: {len(es)}  ({'; '.join(es[:4])}{'; ...' if len(es) > 4 else ''})")
    else:
        sys.exit("usage: codemap graph SLUG [summary|nodes [REGEX]|edges [REGEX]|cycles|by-module]")


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
    # Colors live in palette.json only, so a theme change is one edit and contrast stays checked.
    for f in ("codemap.py", "page.html", "mermaid.json", "codemap"):
        for n, line in enumerate((root / "scripts" / f).read_text().splitlines(), 1):
            if HEX_RX.search(line):
                print(f"color outside palette.json  scripts/{f}:{n}: {line.strip()[:70]}")
                bad += 1
    pal = load_palette()
    toks = pal["tokens"]
    for k, v in toks.items():
        if not (isinstance(v, list) and len(v) == 2 and all(isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", c) for c in v)):
            print(f"palette.json: {k} must be [light, dark], each #rrggbb or #rrggbbaa")
            bad += 1
            toks[k] = None
    for k in ("kind-" + c for c in sorted(set(KIND_CLASS.values()))):
        if k not in toks:
            print(f"palette.json: missing token {k}")
            bad += 1
    for a, b, need, why in pal["contrast"]:
        if not toks.get(a) or not toks.get(b):
            print(f"palette.json: contrast pair {a}/{b} names a missing or invalid token")
            bad += 1
            continue
        for i, theme in enumerate(("light", "dark")):
            r = contrast(toks[a][i][:7], toks[b][i][:7])
            if r < need:
                print(f"palette.json: {a}/{b} is {r:.2f} in {theme}, needs {need} ({why})")
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
        rx = positional(args, ("--kinds", "--limit", "--crates", "--exclude"))
        if rx is None:
            die("usage: codemap find REGEX [--kinds K,K] [--crates C,C] [--exclude REGEX] [--limit N]")
        kinds = TYPE_KINDS | FN_KINDS | MEMBER_KINDS
        if "--kinds" in args:
            kinds = set(opt_value(args, "--kinds").split(","))
        g = load(args[0])
        find(g, rx, kinds, int(opt_value(args, "--limit", 60)), *cli_scope(g, args))
    elif cmd == "refs":
        rx = positional(args, ("--limit", "--crates", "--exclude"))
        if rx is None:
            die("usage: codemap refs REGEX [--crates C,C] [--exclude REGEX] [--limit N] [--by-fn]")
        g = load(args[0])
        refs(g, rx, int(opt_value(args, "--limit", 25)), "--by-fn" in args, *cli_scope(g, args))
    elif cmd == "render":
        meta = json.load(open(Path(args[0]) / "meta.json"))
        render(load(args[0]), load_filter(args[1]), args[2], meta)
    elif cmd == "seeds":
        seeds_cmd(load(args[0]), load_filter(args[1]))
    elif cmd == "page":
        page(args[0], "--artifact" in args)
    elif cmd == "graph":
        graph_cmd(args[0], args[1:])
    elif cmd == "lint":
        lint()
    elif cmd == "mermaid-config":
        mermaid_config()
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

import fnmatch
from pathlib import Path
from tree_sitter import Language, Parser

def get_parser(language_name):
    # Map language names to tree_sitter language modules
    try:
        if language_name == "python":
            import tree_sitter_python as tsp
            lang = Language(tsp.language())
        elif language_name == "javascript":
            import tree_sitter_javascript as tsj
            lang = Language(tsj.language())
        elif language_name == "typescript":
            import tree_sitter_typescript as tsts
            lang = Language(tsts.language_typescript())
        elif language_name == "tsx":
            import tree_sitter_typescript as tsts
            lang = Language(tsts.language_tsx())
        elif language_name == "jsx":
            import tree_sitter_javascript as tsj
            lang = Language(tsj.language())
        elif language_name == "go":
            import tree_sitter_go as tsg
            lang = Language(tsg.language())
        elif language_name == "rust":
            import tree_sitter_rust as tsr
            lang = Language(tsr.language())
        elif language_name == "java":
            import tree_sitter_java as tsj
            lang = Language(tsj.language())
        elif language_name == "c":
            import tree_sitter_c as tsc
            lang = Language(tsc.language())
        elif language_name == "cpp":
            import tree_sitter_cpp as tscpp
            lang = Language(tscpp.language())
        elif language_name == "csharp":
            import tree_sitter_csharp as tscs
            lang = Language(tscs.language())
        elif language_name == "ruby":
            import tree_sitter_ruby as tsrb
            lang = Language(tsrb.language())
        elif language_name == "php":
            import tree_sitter_php as tsp
            lang = Language(tsp.language())
        elif language_name == "swift":
            import tree_sitter_swift as tssw
            lang = Language(tssw.language())
        elif language_name == "kotlin":
            import tree_sitter_kotlin as tsk
            lang = Language(tsk.language())
        elif language_name == "scala":
            import tree_sitter_scala as tssc
            lang = Language(tssc.language())
        elif language_name == "vue":
            import tree_sitter_vue as tsv
            lang = Language(tsv.language())
        elif language_name == "svelte":
            import tree_sitter_svelte as tssvelte
            lang = Language(tssvelte.language())
        else:
            return None
        try:
            parser = Parser()
            parser.set_language(lang)
        except AttributeError:
            # Fallback for newer tree-sitter versions
            parser = Parser(lang)
        return parser
    except ImportError:
        return None

SUPPORTED_EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".go": "go", ".rs": "rust",
    ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".scala": "scala", ".vue": "vue", ".svelte": "svelte",
}

DEFAULT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}


def load_ignore_patterns(root: Path) -> list[str]:
    patterns = []
    for ignore_name in (".gitignore", ".mgrepignore"):
        ignore_file = root / ignore_name
        if not ignore_file.exists():
            continue
        for line in ignore_file.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                patterns.append(stripped)
    return patterns


def is_ignored(path: Path, root: Path, patterns: list[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    parts = set(path.relative_to(root).parts)
    if parts & DEFAULT_IGNORED_DIRS:
        return True
    for pattern in patterns:
        normalized = pattern.strip("/")
        if pattern.endswith("/"):
            if normalized in parts or relative.startswith(f"{normalized}/"):
                return True
        elif fnmatch.fnmatch(relative, normalized) or fnmatch.fnmatch(path.name, normalized):
            return True
    return False


def collect_indexable_files(path: Path) -> list[Path]:
    root = path
    resolved_root = root.resolve()
    patterns = load_ignore_patterns(root)
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        for candidate in root.rglob(f"*{ext}"):
            if candidate.is_file() and not is_ignored(candidate.resolve(), resolved_root, patterns):
                files.append(candidate)
    return sorted(files)


# Regex used by ``extract_symbol`` to recover a representative symbol name
# from the first lines of a chunk. The patterns are deliberately broad —
# matching is best-effort and feeds an embedding prefix, not anything that
# parses code. The first non-empty captured group wins.
import re as _re

_SYMBOL_RE = _re.compile(
    r"(?:^|\s)(?:"
    r"fn\s+([A-Za-z_]\w*)"            # rust fn
    r"|def\s+([A-Za-z_]\w*)"          # python def
    r"|function\s+([A-Za-z_]\w*)"     # js / php function
    r"|class\s+([A-Za-z_]\w*)"        # python / js / ts / java class
    r"|struct\s+([A-Za-z_]\w*)"       # rust / go struct
    r"|trait\s+([A-Za-z_]\w*)"        # rust trait
    r"|impl\s+(?:[\w<>]+\s+for\s+)?([\w<>]+)"  # rust impl
    r"|enum\s+([A-Za-z_]\w*)"         # rust / ts enum
    r"|interface\s+([A-Za-z_]\w*)"    # ts / java interface
    r"|mod\s+([A-Za-z_]\w*)"          # rust mod
    r"|type\s+([A-Za-z_]\w*)"         # go / ts type
    r")"
)


def extract_symbol(chunk_text: str) -> str:
    """Return a short symbol name found in the first lines of ``chunk_text``.

    Used as the ``[symbol: ...]`` field in the chunk-text prefix. Best-effort
    only; an empty string is fine and just omits the symbol field.
    """

    for line in chunk_text.splitlines()[:25]:
        match = _SYMBOL_RE.search(line)
        if not match:
            continue
        for group in match.groups():
            if group:
                return group
    return ""


def make_chunk_prefix(relative_path: str, language: str, symbol: str) -> str:
    """Build the ``[file: ...] [lang: ...] [symbol: ...]`` prefix.

    The prefix is prepended to chunk text before embedding so the embedder
    can see the path / filename / enclosing symbol — words that frequently
    bridge the gap between user-language queries (``microphone audio``) and
    code-vocabulary chunk bodies (``AudioInput::start``).
    """

    parts = [f"[file: {relative_path}]", f"[lang: {language}]"]
    if symbol:
        parts.append(f"[symbol: {symbol}]")
    return " ".join(parts) + "\n\n"


def split_text_chunks(content: str, max_lines: int = 50, max_chars: int = 1000) -> list[dict]:
    chunks = []
    current_lines = []
    current_chars = 0
    start_line = 1
    start_byte = 0
    current_byte = 0
    for line_number, line in enumerate(content.splitlines(), start=1):
        line_chars = len(line) + 1
        if current_lines and (
            len(current_lines) >= max_lines or current_chars + line_chars > max_chars
        ):
            chunk = "\n".join(current_lines)
            chunks.append({
                "chunk": chunk,
                "start_line": start_line,
                "end_line": line_number - 1,
                "start_byte": start_byte,
                "end_byte": current_byte,
            })
            current_lines = []
            current_chars = 0
            start_line = line_number
            start_byte = current_byte
        current_lines.append(line)
        current_chars += line_chars
        current_byte += len(line.encode("utf8")) + 1
    if current_lines:
        chunks.append({
            "chunk": "\n".join(current_lines),
            "start_line": start_line,
            "end_line": start_line + len(current_lines) - 1,
            "start_byte": start_byte,
            "end_byte": len(content.encode("utf8")),
        })
    return chunks or [{
        "chunk": content[:min(max_chars, 2000)],
        "start_line": 1,
        "end_line": 1,
        "start_byte": 0,
        "end_byte": min(len(content.encode("utf8")), max_chars),
    }]

def extract_code_chunks(content: str, language: str, max_lines: int = 80, max_chars: int = 2000) -> list[dict]:
    """Emit non-overlapping tree-sitter chunks, preferring the largest fitting node.

    The previous implementation walked the tree unconditionally, so an ``impl``
    block, every ``fn`` it contained, and large expressions inside those
    functions were all emitted as separate chunks. Top-k retrieval was then
    forced to apply a hard ``MAX_RESULTS_PER_FILE = 2`` cap to compensate.

    This walker emits a node only if it fits in ``max_lines`` and ``max_chars``
    AND ``returns`` rather than recursing — so descendants of an emitted node
    are skipped. ``max_chars`` is bumped from 1000 → 2000 so we use closer to
    the embedding model's 512-token (~2000 char) capacity.
    """

    parser = get_parser(language)
    if parser is None:
        return split_text_chunks(content, max_lines=max_lines, max_chars=max_chars)
    try:
        tree = parser.parse(bytes(content, "utf8"))
    except ValueError:
        return split_text_chunks(content, max_lines=max_lines, max_chars=max_chars)
    chunks = []
    encoded = content.encode("utf8")

    def walk(node):
        line_count = node.end_point[0] - node.start_point[0]
        char_count = node.end_byte - node.start_byte
        if line_count < max_lines and char_count < max_chars:
            chunk = encoded[node.start_byte:node.end_byte].decode("utf8", errors="ignore")
            if len(chunk.splitlines()) >= 3:
                chunks.append({
                    "chunk": chunk,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "start_byte": node.start_byte,
                    "end_byte": node.end_byte,
                })
                return  # Largest-fit emit: do not recurse into descendants.
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return chunks or split_text_chunks(content, max_lines=max_lines, max_chars=max_chars)

def prepare_file_chunks(filepath: Path, root: Path | None = None) -> list[dict]:
    """Chunk ``filepath`` and prepend a path / language / symbol prefix to each.

    The prefix is the ``[file: …] [lang: …] [symbol: …]`` header from
    ``make_chunk_prefix``. It is stored as part of the chunk text and sent
    through the embedder, so a question phrased in user-language can match
    via the path / filename / symbol tokens even when the chunk body itself
    uses code-vocabulary that doesn't surface-overlap.

    ``root`` is used to compute the relative path that ends up in the prefix.
    When ``root`` is ``None`` we fall back to the file's basename, which keeps
    backward compatibility (existing call sites that don't pass ``root`` still
    work and still benefit from a partial prefix).
    """

    ext = filepath.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return []
    try:
        content = filepath.read_text(errors="ignore")
    except Exception:
        return []
    lang = SUPPORTED_EXTENSIONS[ext]
    chunks = extract_code_chunks(content, lang)
    file_mtime = filepath.stat().st_mtime
    if root is not None:
        try:
            relative_path = filepath.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            relative_path = filepath.name
    else:
        relative_path = filepath.name
    results = []
    for i, chunk in enumerate(chunks):
        body = chunk["chunk"]
        symbol = extract_symbol(body)
        prefix = make_chunk_prefix(relative_path, lang, symbol)
        results.append({
            "file": str(filepath),
            "chunk": prefix + body,
            "language": lang,
            "chunk_index": i,
            "file_mtime": file_mtime,
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "start_byte": chunk["start_byte"],
            "end_byte": chunk["end_byte"],
            "embedding": None,
        })
    return results

# ---------------------------------------------------------------------------
# L2 symbol extraction
# ---------------------------------------------------------------------------

# Tree-sitter node-type → kind mappings keyed by language. Languages without
# a tree-sitter grammar (or where parsing fails) fall back to the regex path
# in ``_extract_symbols_regex``.
_TS_SYMBOL_KINDS: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "function",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "function",
        "interface_declaration": "class",
    },
    "tsx": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "function",
        "interface_declaration": "class",
    },
    "jsx": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "function",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
        "enum_item": "struct",
    },
}

# Regex fallback patterns for languages without tree-sitter or when parsing
# fails. Each entry is ``(pattern, kind)`` where the pattern's first group is
# the symbol name. Used by Rust today; can be extended.
_REGEX_SYMBOL_PATTERNS: dict[str, list[tuple[_re.Pattern, str]]] = {
    "rust": [
        (_re.compile(r"\bfn\s+([A-Za-z_][\w]*)"), "function"),
        (_re.compile(r"\bstruct\s+([A-Za-z_][\w]*)"), "struct"),
        (_re.compile(r"\btrait\s+([A-Za-z_][\w]*)"), "trait"),
        (
            _re.compile(r"\bimpl(?:\s*<[^>]*>)?\s+(?:[\w:<>,\s]+\s+for\s+)?([A-Za-z_][\w]*)"),
            "impl",
        ),
        (_re.compile(r"\bmod\s+([A-Za-z_][\w]*)"), "module"),
    ],
}


def _split_camel_lower(name: str) -> str:
    """Lowercase ``name`` with camelCase boundaries inserted as spaces.

    ``LanguageModelClient`` → ``language model client``. Used so a
    natural-language query like ``language model`` can substring-match the
    space-joined lowered form even though the source identifier was
    PascalCase or camelCase.
    """

    return _re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower()


def _ts_extract_node_name(node, encoded: bytes) -> str | None:
    """Return the identifier name for a tree-sitter declaration node.

    Tries the standard ``name`` field first; if that's missing (e.g. an
    ``impl`` block has a ``type`` field with the implementing type) walks
    the children for a likely identifier-bearing node.
    """

    name_node = None
    try:
        name_node = node.child_by_field_name("name")
    except Exception:
        name_node = None
    if name_node is None:
        # impl blocks expose the type via the ``type`` field in tree-sitter
        # rust; try that before giving up.
        try:
            name_node = node.child_by_field_name("type")
        except Exception:
            name_node = None
    if name_node is None:
        for child in node.children:
            if child.type in {"identifier", "type_identifier", "property_identifier"}:
                name_node = child
                break
    if name_node is None:
        return None
    raw = encoded[name_node.start_byte:name_node.end_byte].decode("utf8", errors="ignore")
    raw = raw.strip()
    return raw or None


def _extract_symbols_treesitter(content: str, language: str) -> list[dict]:
    kind_map = _TS_SYMBOL_KINDS.get(language)
    if not kind_map:
        return []
    parser = get_parser(language)
    if parser is None:
        return []
    try:
        tree = parser.parse(bytes(content, "utf8"))
    except (ValueError, Exception):
        return []
    encoded = content.encode("utf8")
    out: list[dict] = []

    def walk(node):
        kind = kind_map.get(node.type)
        if kind is not None:
            name = _ts_extract_node_name(node, encoded)
            if name:
                out.append({
                    "name": name,
                    "kind": kind,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return out


def _extract_symbols_regex(content: str, language: str) -> list[dict]:
    patterns = _REGEX_SYMBOL_PATTERNS.get(language)
    if not patterns:
        return []
    out: list[dict] = []
    for line_index, line in enumerate(content.splitlines(), start=1):
        for pattern, kind in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            if not name:
                continue
            out.append({
                "name": name,
                "kind": kind,
                "start_line": line_index,
                "end_line": line_index,
            })
    return out


def extract_file_symbols(file: Path, root: Path) -> list[dict]:
    """Return symbol rows for ``file`` suitable for the ``symbols`` table.

    Each row has keys ``file/name/name_lower/kind/start_line/end_line/file_mtime``.
    ``file`` is the absolute path string (matching the ``chunks.file`` form
    used elsewhere in storage). ``name_lower`` is the camelCase-split lower
    form so query terms can substring-match against ``"language model client"``
    even when the identifier was ``LanguageModelClient``.

    Tree-sitter is the preferred extractor (Python, JS, TS, Rust). When a
    grammar is unavailable or parsing fails, a regex fallback covers Rust's
    `fn` / `struct` / `trait` / `impl` / `mod` shapes; other languages with
    no grammar return an empty list.
    """

    ext = file.suffix.lower()
    language = SUPPORTED_EXTENSIONS.get(ext)
    if language is None:
        return []
    try:
        content = file.read_text(errors="ignore")
    except Exception:
        return []
    try:
        file_mtime = file.stat().st_mtime
    except OSError:
        file_mtime = 0.0
    raw_symbols = _extract_symbols_treesitter(content, language)
    if not raw_symbols:
        raw_symbols = _extract_symbols_regex(content, language)
    file_str = str(file)
    rows: list[dict] = []
    for sym in raw_symbols:
        name = sym["name"]
        rows.append({
            "file": file_str,
            "name": name,
            "name_lower": _split_camel_lower(name),
            "kind": sym["kind"],
            "start_line": sym.get("start_line"),
            "end_line": sym.get("end_line"),
            "file_mtime": file_mtime,
        })
    return rows


def batch_embed(chunks: list[dict], embedder, batch_size: int = 10) -> list[dict]:
    results = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        texts = [chunk["chunk"] for chunk in batch]
        if hasattr(embedder, "embed_batch"):
            embeddings = embedder.embed_batch(texts)
        else:
            embeddings = [embedder.embed(text) for text in texts]
        for chunk, embedding in zip(batch, embeddings):
            chunk["embedding"] = embedding
            results.append(chunk)
    return results

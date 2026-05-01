import os
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
            # Default to a simple parser for unsupported languages
            return Parser()
        try:
            parser = Parser()
            parser.set_language(lang)
        except AttributeError:
            # Fallback for newer tree-sitter versions
            parser = Parser(lang)
        return parser
    except ImportError:
        # Fallback to a basic parser if language package not installed
        return Parser()

SUPPORTED_EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".go": "go", ".rs": "rust",
    ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".scala": "scala", ".vue": "vue", ".svelte": "svelte",
}

def extract_code_chunks(content: str, language: str, max_lines: int = 50, max_chars: int = 1000) -> list[str]:
    parser = get_parser(language)
    tree = parser.parse(bytes(content, "utf8"))
    chunks = []
    def walk(node):
        # Check both line count and character count
        line_count = node.end_point[0] - node.start_point[0]
        char_count = node.end_byte - node.start_byte
        if line_count < max_lines and char_count < max_chars:
            chunk = content.encode()[node.start_byte:node.end_byte].decode("utf8")
            if len(chunk.splitlines()) >= 3:
                chunks.append(chunk)
        for child in node.children:
            walk(child)
    walk(tree.root_node)
    return chunks or [content[:min(max_chars, 2000)]]

def prepare_file_chunks(filepath: Path) -> list[dict]:
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
    results = []
    for i, chunk in enumerate(chunks):
        results.append({
            "file": str(filepath),
            "chunk": chunk,
            "language": lang,
            "chunk_index": i,
            "file_mtime": file_mtime,
            "embedding": None,
        })
    return results

def batch_embed(chunks: list[dict], embedder, batch_size: int = 10) -> list[dict]:
    results = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        for chunk in batch:
            embedding = embedder.embed(chunk["chunk"])
            chunk["embedding"] = embedding
            results.append(chunk)
    return results

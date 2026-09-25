def extract_source_evidence(repo_root: str, node_strings: list[str]) -> list[dict[str, Any]]:
    """
    Extracts source evidence from Graphify node strings.

    Args:
        repo_root: The root path of the repository.
        node_strings: A list of Graphify node strings (e.g., "src=/path/to/file.py,symbol=multiply()").

    Returns:
        A list of dictionaries containing symbol, kind, file, start_line, end_line, source, and truncated.
    """
    results = []
    repo_root_path = Path(repo_root).resolve()

    for node_str in node_strings:
        try:
            # Parse src=<path> and symbol
            src_match = re.search(r"src=([^\s,]+)", node_str)
            symbol_match = re.search(r"symbol=([^\s,]+)", node_str)

            if not src_match or not symbol_match:
                continue

            src_path = Path(src_match.group(1)).resolve()
            symbol_raw = symbol_match.group(1)

            # Safety: Reject resolved paths outside repository root
            if not str(src_path).startswith(str(repo_root_path)):
                continue

            if not src_path.exists() or src_path.suffix != ".py":
                continue

            # Normalize symbol name
            # Handles multiply(), add(), test_multiply(), RunManager
            symbol = symbol_raw.replace("()", "")
            
            # Determine kind
            if symbol.startswith("test_"):
                kind = "test_function"
            elif "Manager" in symbol or "Class" in symbol or "Model" in symbol:
                kind = "class"
            else:
                kind = "function"

            with open(src_path, "r", encoding="utf-8") as f:
                content = f.read()

            tree = ast.parse(content)

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    # Exact name match or contains
                    if node.name == symbol or (symbol in node.name and symbol != ""):
                        start_line = node.lineno
                        end_line = getattr(node, "end_lineno", start_line)

                        # Extract source
                        lines = content.splitlines()
                        snippet_lines = lines[start_line-1 : end_line]
                        source = "\n".join(snippet_lines)

                        # Truncation logic
                        truncated = False
                        if len(snippet_lines) > 80:
                            source = "\n".join(snippet_lines[:80])
                            truncated = True
                        
                        source_bytes = source.encode("utf-8")
                        if len(source_bytes) > 16384:
                            source = source[:16384]
                            truncated = True

                        results.append({
                            "symbol": symbol,
                            "kind": kind,
                            "file": str(src_path),
                            "start_line": start_line,
                            "end_line": end_line,
                            "source": source,
                            "truncated": truncated
                        })
                        break # Found it

        except Exception:
            continue

    return results

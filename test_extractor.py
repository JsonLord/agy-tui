import os
from pathlib import Path
import ast
import re
from typing import Any

def extract_source_evidence(repo_root: str, node_strings: list[str]) -> list[dict[str, Any]]:
    results = []
    repo_root_path = Path(repo_root).resolve()

    for node_str in node_strings:
        try:
            src_match = re.search(r"src=([^\s,]+)", node_str)
            symbol_match = re.search(r"symbol=([^\s,]+)", node_str)

            if not src_match or not symbol_match:
                continue

            src_path = Path(src_match.group(1)).resolve()
            symbol_raw = symbol_match.group(1)

            if not str(src_path).startswith(str(repo_root_path)):
                continue

            if not src_path.exists() or src_path.suffix != ".py":
                continue

            symbol = symbol_raw.replace("()", "")
            
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
                    if node.name == symbol or (symbol in node.name and symbol != ""):
                        start_line = node.lineno
                        end_line = getattr(node, "end_lineno", start_line)

                        lines = content.splitlines()
                        snippet_lines = lines[start_line-1 : end_line]
                        source = "\n".join(snippet_lines)

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
                        break

        except Exception:
            continue

    return results

# Test execution
repo_root = "/home/leon/ag-harness-test"
os.makedirs(repo_root, exist_ok=True)
test_file = Path(repo_root) / "test_file.py"
with open(test_file, "w") as f:
    f.write("""def multiply():
    return a * b

def add():
    return a + b

class RunManager:
    pass

def test_multiply():
    pass
""")

nodes = [
    "src=/home/leon/ag-harness-test/test_file.py,symbol=multiply()",
    "src=/home/leon/ag-harness-test/test_file.py,symbol=add()",
    "src=/home/leon/ag-harness-test/test_file.py,symbol=test_multiply()",
    "src=/home/leon/ag-harness-test/test_file.py,symbol=RunManager"
]

evidence = extract_source_evidence(repo_root, nodes)
for item in evidence:
    print(item)

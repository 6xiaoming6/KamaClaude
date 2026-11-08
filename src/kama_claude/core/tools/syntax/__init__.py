from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_rust
import tree_sitter_typescript
from tree_sitter import Language, Node

from kama_claude.core.tools.syntax import cpp, go, java, rust, web
from kama_claude.core.tools.syntax.common import Definition

_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".mts": "typescript",
    ".cts": "typescript", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".h": "cpp", ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".h++": "cpp",
    ".ipp": "cpp", ".tpp": "cpp", ".java": "java", ".rs": "rust", ".go": "go",
}
EXTENSIONS = frozenset(_LANGUAGES)
SUPPORTED_LANGUAGES = "Python, JS/JSX, TS/TSX, C++, Java, Rust and Go"


@dataclass(frozen=True)
class Syntax:
    language: Language
    definition: Callable[[Node, bytes, str], Definition | None]
    separator: str = "."
    child_scope: Callable[[Node, bytes, str, str], tuple[str, str]] | None = None


# Unix 上常用的大写 .C 表示 C++，不能简单转小写后误作 C 文件。
def normalized_suffix(suffix: str) -> str:
    return ".cpp" if suffix == ".C" else suffix.lower()


# 每次解析创建语言适配器和 Parser，只共享不可变规则，不缓存可变语法树。
def syntax_for(suffix: str) -> Syntax:
    language = _LANGUAGES[suffix]
    if language == "cpp":
        return Syntax(Language(tree_sitter_cpp.language()), cpp.definition, "::", cpp.child_scope)
    if language == "java":
        return Syntax(Language(tree_sitter_java.language()), java.definition)
    if language == "rust":
        return Syntax(
            Language(tree_sitter_rust.language()), rust.definition, "::", rust.child_scope,
        )
    if language == "go":
        return Syntax(Language(tree_sitter_go.language()), go.definition)
    if language == "python":
        grammar = Language(tree_sitter_python.language())
    elif language == "typescript":
        grammar = Language(tree_sitter_typescript.language_typescript())
    elif language == "tsx":
        grammar = Language(tree_sitter_typescript.language_tsx())
    else:
        grammar = Language(tree_sitter_javascript.language())
    return Syntax(grammar, web.definition)

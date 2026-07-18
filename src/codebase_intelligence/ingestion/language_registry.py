"""Deterministic path-to-language and Tree-sitter grammar registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePath

from tree_sitter import Parser
from tree_sitter_language_pack import get_parser


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """Language metadata used by scanning and semantic chunking."""

    name: str
    parser_name: str | None
    extensions: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    semantic: bool = True
    symbol_kinds: dict[str, str] = field(default_factory=dict)

    def kind_for(self, node_type: str) -> str | None:
        return self.symbol_kinds.get(node_type)


_FUNCTIONS = {
    "function_definition": "function",
    "function_declaration": "function",
    "function_item": "function",
    "function_expression": "function",
    "generator_function_declaration": "function",
    "arrow_function": "function",
    "method_definition": "method",
    "method_declaration": "method",
    "method": "method",
    "constructor_declaration": "constructor",
}
_TYPES = {
    "class_definition": "class",
    "class_declaration": "class",
    "class_specifier": "class",
    "interface_declaration": "interface",
    "trait_declaration": "trait",
    "struct_item": "struct",
    "struct_specifier": "struct",
    "struct_declaration": "struct",
    "enum_declaration": "enum",
    "enum_item": "enum",
    "record_declaration": "record",
    "object_declaration": "object",
    "module": "module",
    "module_declaration": "module",
    "namespace_definition": "namespace",
    "namespace_declaration": "namespace",
    "impl_item": "implementation",
}


def _symbols(*extra: tuple[str, str]) -> dict[str, str]:
    return {**_FUNCTIONS, **_TYPES, **dict(extra)}


DEFAULT_LANGUAGE_SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", "python", (".py", ".pyi"), symbol_kinds=_symbols()),
    LanguageSpec(
        "javascript",
        "javascript",
        (".js", ".mjs", ".cjs", ".jsx"),
        symbol_kinds=_symbols(("lexical_declaration", "declaration")),
    ),
    LanguageSpec(
        "typescript",
        "typescript",
        (".ts", ".mts", ".cts"),
        symbol_kinds=_symbols(
            ("type_alias_declaration", "type"),
            ("abstract_class_declaration", "class"),
        ),
    ),
    LanguageSpec(
        "tsx",
        "tsx",
        (".tsx",),
        symbol_kinds=_symbols(
            ("type_alias_declaration", "type"),
            ("abstract_class_declaration", "class"),
        ),
    ),
    LanguageSpec("java", "java", (".java",), symbol_kinds=_symbols()),
    LanguageSpec("kotlin", "kotlin", (".kt", ".kts"), symbol_kinds=_symbols()),
    LanguageSpec(
        "go",
        "go",
        (".go",),
        symbol_kinds=_symbols(
            ("method_declaration", "method"),
            ("type_declaration", "type"),
        ),
    ),
    LanguageSpec("rust", "rust", (".rs",), symbol_kinds=_symbols()),
    LanguageSpec("c", "c", (".c", ".h"), symbol_kinds=_symbols()),
    LanguageSpec(
        "cpp",
        "cpp",
        (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
        symbol_kinds=_symbols(),
    ),
    LanguageSpec("csharp", "csharp", (".cs",), symbol_kinds=_symbols()),
    LanguageSpec("ruby", "ruby", (".rb",), symbol_kinds=_symbols()),
    LanguageSpec("php", "php", (".php",), symbol_kinds=_symbols()),
    LanguageSpec("swift", "swift", (".swift",), symbol_kinds=_symbols()),
    LanguageSpec("scala", "scala", (".scala", ".sc"), symbol_kinds=_symbols()),
    LanguageSpec(
        "bash",
        "bash",
        (".sh", ".bash", ".zsh"),
        (".bashrc", ".zshrc"),
        symbol_kinds=_symbols(("function_definition", "function")),
    ),
    LanguageSpec("sql", "sql", (".sql",), symbol_kinds=_symbols()),
    LanguageSpec("html", "html", (".html", ".htm"), semantic=False),
    LanguageSpec("css", "css", (".css",), semantic=False),
    LanguageSpec("json", "json", (".json", ".jsonc"), semantic=False),
    LanguageSpec("yaml", "yaml", (".yaml", ".yml"), semantic=False),
    LanguageSpec("toml", "toml", (".toml",), semantic=False),
    LanguageSpec("markdown", "markdown", (".md", ".mdx"), semantic=False),
    LanguageSpec(
        "dockerfile",
        "dockerfile",
        (),
        ("Dockerfile",),
        semantic=False,
    ),
    LanguageSpec("terraform", "hcl", (".tf", ".hcl"), semantic=False),
)

TEXT_LANGUAGE = LanguageSpec("text", None, semantic=False)


class LanguageRegistry:
    """Map paths to language metadata and lazily cache Language Pack parsers."""

    def __init__(self, specs: tuple[LanguageSpec, ...] = DEFAULT_LANGUAGE_SPECS) -> None:
        self._specs = specs
        self._extensions: dict[str, LanguageSpec] = {}
        self._filenames: dict[str, LanguageSpec] = {}
        self._parsers: dict[str, Parser | None] = {}
        for spec in specs:
            for extension in spec.extensions:
                self._extensions[extension.casefold()] = spec
            for filename in spec.filenames:
                self._filenames[filename.casefold()] = spec

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    def get(self, language: str) -> LanguageSpec:
        for spec in self._specs:
            if spec.name == language:
                return spec
        return TEXT_LANGUAGE

    def detect(self, path: str | PurePath) -> LanguageSpec:
        candidate = Path(path)
        filename = candidate.name.casefold()
        exact = self._filenames.get(filename)
        if exact is not None:
            return exact
        if filename.startswith("dockerfile."):
            return self._filenames["dockerfile"]
        # Longest suffix first keeps compound extensions deterministic.
        for suffix in sorted(candidate.suffixes, key=len, reverse=True):
            spec = self._extensions.get(suffix.casefold())
            if spec is not None:
                return spec
        return TEXT_LANGUAGE

    def language_for_path(self, path: str | PurePath) -> str:
        return self.detect(path).name

    def parser_for(self, spec_or_path: LanguageSpec | str | PurePath) -> Parser | None:
        spec = spec_or_path if isinstance(spec_or_path, LanguageSpec) else self.detect(spec_or_path)
        if not spec.semantic or spec.parser_name is None:
            return None
        if spec.parser_name not in self._parsers:
            try:
                self._parsers[spec.parser_name] = get_parser(spec.parser_name)  # type: ignore[arg-type]
            except (LookupError, OSError):
                self._parsers[spec.parser_name] = None
        return self._parsers[spec.parser_name]


DEFAULT_LANGUAGE_REGISTRY = LanguageRegistry()


__all__ = [
    "DEFAULT_LANGUAGE_REGISTRY",
    "DEFAULT_LANGUAGE_SPECS",
    "TEXT_LANGUAGE",
    "LanguageRegistry",
    "LanguageSpec",
]

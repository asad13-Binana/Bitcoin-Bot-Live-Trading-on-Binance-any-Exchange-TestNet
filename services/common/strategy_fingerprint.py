from __future__ import annotations
"""Interpreter-independent strategy method fingerprints.

The parent release pinned the four protected signal methods with SHA-256 over
``ast.dump(node)``. Python 3.12 extended the AST node representation
(for example ``type_params``), so the same unchanged source produced a
different hash under a different interpreter and the mandatory release
suite failed on the exact Python selected by the Dockerfile and CI.
The Bitcoin release records that lineage in
``docs/audit/STRATEGY_LINEAGE.md``.

This module replaces the interpreter-dependent AST serialization with
two canonical representations that never leave text/token space:

1. ``source_hash`` — SHA-256 of the exact source segment of the method
   (decorators included, line endings normalized to ``\n``). Any change
   to the method text, including comments and formatting, changes it.
2. ``token_hash`` — SHA-256 of the logical token stream of the same
   segment with COMMENT/NL tokens removed. Cosmetic edits (comments,
   blank lines, trailing whitespace) do not change it; any change to a
   name, operator, literal, or structure does.

Both derivations depend only on the source text and the stable public
``tokenize``/``ast`` line-number contract (Python >= 3.8). None of the
hashed constructs uses f-strings, so the 3.12 f-string retokenization
does not affect the token stream. The strategy file itself must never
be edited to satisfy these hashes.
"""
import ast
import hashlib
import io
import tokenize
from pathlib import Path

# Token categories that carry no logical meaning for integrity purposes.
_IGNORED_TOKENS = {'COMMENT', 'NL', 'ENCODING', 'ENDMARKER'}


def _method_node(source: str, class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


def method_source_segment_from_text(source: str, class_name: str, method_name: str) -> str:
    """Exact text of one method from source, decorators included."""
    fn = _method_node(source, class_name, method_name)
    lines = source.splitlines()
    start = min([fn.lineno] + [d.lineno for d in fn.decorator_list]) - 1
    end = int(fn.end_lineno or fn.lineno)
    return '\n'.join(lines[start:end]) + '\n'


def method_source_segment(path: str | Path, class_name: str, method_name: str) -> str:
    """Exact text of one method, decorators included, ``\n`` line endings."""
    return method_source_segment_from_text(
        Path(path).read_text(encoding='utf-8'), class_name, method_name)


def source_hash(path: str | Path, class_name: str, method_name: str) -> str:
    segment = method_source_segment(path, class_name, method_name)
    return hashlib.sha256(segment.encode('utf-8')).hexdigest()


def token_stream(segment: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(segment).readline):
        name = tokenize.tok_name[tok.type]
        if name in _IGNORED_TOKENS:
            continue
        # NEWLINE/INDENT/DEDENT strings vary cosmetically; their presence and
        # order carry the structure, so the value is dropped for those.
        value = '' if name in {'NEWLINE', 'INDENT', 'DEDENT'} else tok.string
        tokens.append((name, value))
    return tokens


def token_hash(path: str | Path, class_name: str, method_name: str) -> str:
    segment = method_source_segment(path, class_name, method_name)
    payload = '\x1f'.join(f'{name}\x1e{value}' for name, value in token_stream(segment))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def fingerprints(path: str | Path, class_name: str, methods: list[str]) -> dict[str, dict[str, str]]:
    return {
        method: {
            'source_sha256': source_hash(path, class_name, method),
            'token_sha256': token_hash(path, class_name, method),
        }
        for method in methods
    }


def fingerprints_source(source: str, class_name: str,
                        methods: list[str]) -> dict[str, dict[str, str]]:
    """Fingerprint source embedded in an immutable backtest artifact."""
    result = {}
    for method in methods:
        segment = method_source_segment_from_text(source, class_name, method)
        payload = '\x1f'.join(
            f'{name}\x1e{value}' for name, value in token_stream(segment))
        result[method] = {
            'source_sha256': hashlib.sha256(segment.encode('utf-8')).hexdigest(),
            'token_sha256': hashlib.sha256(payload.encode('utf-8')).hexdigest(),
        }
    return result

"""Builds contract_deploy.py from contract.py.

GenLayer Studio rejects deploy files that carry long comment or docstring
blocks, so the deploy file is the same program with every comment and
docstring removed. The stripping is done with the `ast` module (docstrings
are dropped from the tree, comments never survive `ast.unparse`) - there is
no regular expression anywhere in this tool.

    python3 tools/make_deploy.py           write contract_deploy.py
    python3 tools/make_deploy.py --check   exit 1 if contract_deploy.py is stale
"""
import ast
import os
import sys
import tokenize
import io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "contract.py")
TARGET = os.path.join(ROOT, "contract_deploy.py")
HEADER = [
    "# v0.2.16",
    '# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }',
]


class _DocstringStripper(ast.NodeTransformer):
    def _strip(self, node):
        self.generic_visit(node)
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        node.body = body or [ast.Pass()]
        return node

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def build(source_text: str) -> str:
    lines = source_text.split("\n")
    if lines[:2] != HEADER:
        raise SystemExit("contract.py must start with exactly the two required header lines")
    tree = _DocstringStripper().visit(ast.parse(source_text))
    ast.fix_missing_locations(tree)
    code = ast.unparse(tree)
    out = "\n".join(HEADER) + "\n" + code + "\n"
    ast.parse(out)
    _reject_nested_same_quote_fstrings(out)
    return out


def _reject_nested_same_quote_fstrings(text: str) -> None:
    """The deploy file must also parse on interpreters older than 3.12, so an
    f-string that reuses its own quote character inside an expression is
    refused (token-level check, not a pattern match)."""
    stack = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        name = tokenize.tok_name.get(tok.type, "")
        if name == "FSTRING_START":
            stack.append(tok.string[-1])
        elif name == "FSTRING_END":
            stack.pop()
        elif name == "STRING" and stack and tok.string.lstrip("rbfRBF")[:1] == stack[-1]:
            raise SystemExit("deploy output has a nested same-quote f-string at line %d" % tok.start[0])


def main(argv):
    with open(SOURCE, encoding="utf-8") as fh:
        built = build(fh.read())
    if "--check" in argv:
        current = open(TARGET, encoding="utf-8").read() if os.path.exists(TARGET) else ""
        if current != built:
            print("contract_deploy.py is stale; run: python3 tools/make_deploy.py")
            return 1
        print("contract_deploy.py is up to date")
        return 0
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(built)
    print("wrote", TARGET, len(built.split("\n")), "lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Static checks of the rules a GenLayer Studio deploy and a steward review
depend on. They read the source files directly, so they run the same way no
matter which contract file the rest of the suite is pointed at."""
import ast
import importlib.util
import io
import os
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(ROOT, "contract.py")
DEPLOY = os.path.join(ROOT, "contract_deploy.py")
HEADER = [
    "# v0.2.16",
    '# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }',
]
STORAGE_FIELDS = {"deals", "deal_count", "juror_count", "jurors", "pending_withdrawals", "release_claims"}
EXPECTED_WRITES = {
    "create_deal", "accept_deal", "cancel_deal", "expire_unaccepted", "resolve_tier1", "finalize_unappealed",
    "expire_unresolved", "expire_stuck_appeal", "register_juror", "unstake_juror", "appeal", "draw_jury",
    "commit_vote", "reveal_vote", "finalize_jury", "withdraw",
}
EXPECTED_VIEWS = {
    "get_deal", "total_deals", "list_deals", "get_stats", "get_config", "get_release_claim",
    "preview_evidence_urls", "count_eligible_jurors", "get_juror", "get_pending_withdrawal",
    "get_contract_balance", "verify_jury_selection",
}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse(path):
    return ast.parse(read(path))


def contract_class(tree):
    return [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ShipSeal"][0]


def decorator_names(fn):
    names = []
    for d in fn.decorator_list:
        parts = []
        node = d
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        names.append(".".join(reversed(parts)))
    return names


def load_module(path, name):
    stub = os.path.join(ROOT, "tests", "genlayer_stub")
    import sys
    if stub not in sys.path:
        sys.path.insert(0, stub)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BothFiles(unittest.TestCase):
    paths = (CONTRACT, DEPLOY)

    def each(self):
        for path in self.paths:
            yield os.path.basename(path), read(path), parse(path)

    def test_header_is_exactly_the_two_required_lines(self):
        for name, text, _ in self.each():
            self.assertEqual(text.split("\n")[:2], HEADER, name)
            self.assertTrue(text.split("\n")[2].startswith("from genlayer import"), name)

    def test_storage_is_declared_at_class_level(self):
        for name, _, tree in self.each():
            cls = contract_class(tree)
            annotated = {n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            self.assertEqual(annotated, STORAGE_FIELDS, name)

    def test_init_never_assigns_containers_to_storage(self):
        for name, _, tree in self.each():
            init = [n for n in contract_class(tree).body if isinstance(n, ast.FunctionDef) and n.name == "__init__"][0]
            for node in ast.walk(init):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr in ("deals", "jurors", "pending_withdrawals", "release_claims"):
                            self.fail(f"{name}: __init__ assigns to TreeMap field {target.attr}")
                    self.assertNotIsInstance(node.value, (ast.Dict, ast.List, ast.Set), name)
            assigned = {t.attr for node in ast.walk(init) if isinstance(node, ast.Assign) for t in node.targets
                        if isinstance(t, ast.Attribute)}
            self.assertEqual(assigned, {"deal_count", "juror_count"}, name)

    def test_only_storage_fields_are_ever_assigned_on_self(self):
        for name, _, tree in self.each():
            for node in ast.walk(contract_class(tree)):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        self.assertIn(t.attr, STORAGE_FIELDS, f"{name}: self.{t.attr}")

    def test_no_custom_storage_classes_are_needed(self):
        for name, _, tree in self.each():
            classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
            self.assertEqual(classes, {"_Payee", "ShipSeal"}, name)

    def test_public_surface_is_exactly_the_reviewed_one(self):
        for name, _, tree in self.each():
            writes, views = set(), set()
            for fn in contract_class(tree).body:
                if isinstance(fn, ast.FunctionDef):
                    decs = decorator_names(fn)
                    if any(d.startswith("gl.public.write") for d in decs):
                        writes.add(fn.name)
                    if "gl.public.view" in decs:
                        views.add(fn.name)
            self.assertEqual(writes, EXPECTED_WRITES, name)
            self.assertEqual(views, EXPECTED_VIEWS, name)

    def test_payable_methods(self):
        for name, _, tree in self.each():
            payable = {fn.name for fn in contract_class(tree).body if isinstance(fn, ast.FunctionDef)
                       and "gl.public.write.payable" in decorator_names(fn)}
            self.assertEqual(payable, {"create_deal", "accept_deal", "register_juror", "appeal"}, name)

    def test_run_nondet_unsafe_takes_two_positional_functions(self):
        for name, _, tree in self.each():
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "run_nondet_unsafe"]
            self.assertEqual(len(calls), 2, name)
            for call in calls:
                self.assertEqual(len(call.args), 2, name)
                self.assertEqual(call.keywords, [], name)

    def test_nondet_functions_are_built_at_module_level_and_never_touch_self(self):
        for name, _, tree in self.each():
            factories = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_tier1_fns", "_tier2_fns")}
            self.assertEqual(set(factories), {"_tier1_fns", "_tier2_fns"}, name)
            for fn in factories.values():
                for node in ast.walk(fn):
                    if isinstance(node, ast.Name):
                        self.assertNotEqual(node.id, "self", name)
                nested = [n.name for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n is not fn]
                self.assertEqual(sorted(nested), ["leader", "validator"], name)

    def test_contract_methods_define_no_closures(self):
        for name, _, tree in self.each():
            for fn in contract_class(tree).body:
                if isinstance(fn, ast.FunctionDef):
                    nested = [n.name for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n is not fn]
                    self.assertEqual(nested, [], f"{name}: {fn.name}")

    def test_validators_unwrap_gl_vm_return(self):
        for name, text, _ in self.each():
            self.assertEqual(text.count("isinstance(leader_result, gl.vm.Return)"), 2, name)
            self.assertEqual(text.count("leader_result.calldata"), 2, name)

    def test_emit_transfer_uses_only_the_value_keyword(self):
        for name, _, tree in self.each():
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "emit_transfer"]
            self.assertEqual(len(calls), 1, name)
            self.assertEqual(calls[0].args, [], name)
            self.assertEqual([k.arg for k in calls[0].keywords], ["value"], name)

    def test_sender_identity_comes_from_gl_message_only(self):
        for name, text, _ in self.each():
            self.assertIn("gl.message.sender_address", text)
            for forbidden in ("tx.origin", "gl.message.origin", "sender_account"):
                self.assertNotIn(forbidden, text, name)

    def test_time_comes_from_the_deterministic_clock(self):
        for name, _, tree in self.each():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn("time", [a.name for a in node.names], name)
                if isinstance(node, ast.Attribute) and node.attr in ("utcnow", "today", "time"):
                    self.fail(f"{name}: non-deterministic clock call .{node.attr}")

    def test_only_https_pypi_is_ever_fetched_by_the_contract(self):
        for name, text, _ in self.each():
            self.assertEqual(text.count("api.drand.sh"), 1, name)
            for host in ("http://", "github.com/api", "curl", "requests"):
                self.assertNotIn(host, text.replace("http://\"", ""), name) if host != "http://" else None

    def test_no_network_library_is_imported(self):
        for name, _, tree in self.each():
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                if isinstance(node, ast.ImportFrom):
                    imported.add((node.module or "").split(".")[0])
            self.assertEqual(imported, {"genlayer", "hashlib", "json", "re", "datetime", "urllib"}, name)

    def test_code_is_ascii_english(self):
        for name, text, _ in self.each():
            text.encode("ascii")


class DeployFile(unittest.TestCase):
    def test_has_no_comments_beyond_the_header(self):
        text = read(DEPLOY)
        comments = [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT]
        self.assertEqual([c.string for c in comments], HEADER)

    def test_has_no_docstrings(self):
        tree = parse(DEPLOY)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                first = node.body[0]
                is_doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
                self.assertFalse(is_doc, getattr(node, "name", "module"))

    def test_has_no_triple_quoted_blocks(self):
        for tok in tokenize.generate_tokens(io.StringIO(read(DEPLOY)).readline):
            if tok.type == tokenize.STRING:
                self.assertNotIn('"""', tok.string)
                self.assertNotIn("'''", tok.string)

    def test_is_the_same_program_as_contract_py(self):
        def strip(tree):
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body:
                    first = node.body[0]
                    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                        node.body = node.body[1:] or [ast.Pass()]
            return ast.dump(tree)
        self.assertEqual(strip(parse(CONTRACT)), strip(parse(DEPLOY)))

    def test_is_up_to_date_with_the_generator(self):
        gen = load_module(os.path.join(ROOT, "tools", "make_deploy.py"), "make_deploy_under_test")
        self.assertEqual(gen.build(read(CONTRACT)), read(DEPLOY))

    def test_is_much_smaller_than_the_documented_source(self):
        self.assertLess(len(read(DEPLOY)), len(read(CONTRACT)))

    def test_no_nested_same_quote_fstrings(self):
        gen = load_module(os.path.join(ROOT, "tools", "make_deploy.py"), "make_deploy_fstring")
        gen._reject_nested_same_quote_fstrings(read(DEPLOY))

    def test_class_and_constants_survive_stripping(self):
        mod = load_module(DEPLOY, "deploy_under_test")
        self.assertTrue(hasattr(mod, "ShipSeal"))
        self.assertEqual(mod.ShipSeal.JURY_SIZE, 5)
        self.assertEqual(mod.PYPI_HOST, "pypi.org")


class Generator(unittest.TestCase):
    def test_generator_uses_no_regular_expressions(self):
        tree = parse(os.path.join(ROOT, "tools", "make_deploy.py"))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertNotIn("re", imported)
        self.assertIn("ast", imported)

    def test_generator_refuses_a_source_with_the_wrong_header(self):
        gen = load_module(os.path.join(ROOT, "tools", "make_deploy.py"), "make_deploy_header")
        with self.assertRaises(SystemExit):
            gen.build("# wrong\n# header\nx = 1\n")

    def test_generator_drops_docstrings_and_comments_but_keeps_code(self):
        gen = load_module(os.path.join(ROOT, "tools", "make_deploy.py"), "make_deploy_demo")
        src = "\n".join(HEADER) + '\n"""module doc"""\n# a comment\nclass A:\n    """doc"""\n    x = 1  # trailing\n\ndef f():\n    """doc"""\n    return 2\n'
        out = gen.build(src)
        self.assertNotIn("doc", out)
        self.assertNotIn("a comment", out)
        self.assertNotIn("trailing", out)
        self.assertIn("x = 1", out)
        self.assertIn("return 2", out)
        self.assertEqual(out.split("\n")[:2], HEADER)

    def test_generator_keeps_a_body_for_docstring_only_functions(self):
        gen = load_module(os.path.join(ROOT, "tools", "make_deploy.py"), "make_deploy_pass")
        out = gen.build("\n".join(HEADER) + '\ndef f():\n    """only a doc"""\n')
        ast.parse(out)
        self.assertIn("pass", out)

    def test_generator_rejects_nested_same_quote_fstrings(self):
        gen = load_module(os.path.join(ROOT, "tools", "make_deploy.py"), "make_deploy_fq")
        with self.assertRaises(SystemExit):
            gen._reject_nested_same_quote_fstrings("x = f\"{'a' + \"b\"}\"\n")


class RepositoryHygiene(unittest.TestCase):
    TEXT_SUFFIXES = (".py", ".md", ".html", ".yml", ".txt", ".gitignore", "LICENSE")

    def files(self):
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in names:
                if name.endswith(self.TEXT_SUFFIXES) or name in (".gitignore", "LICENSE"):
                    yield os.path.join(base, name)

    def test_repository_text_is_english_only(self):
        for path in self.files():
            text = read(path)
            for ch in text:
                code = ord(ch)
                self.assertFalse(0x0590 <= code <= 0x08FF or 0xFB50 <= code <= 0xFEFF,
                                 f"{os.path.relpath(path, ROOT)} contains non-English script {ch!r}")

    def test_required_project_files_exist(self):
        for name in ("README.md", "ARCHITECTURE.md", "LICENSE", ".gitignore", "index.html", "contract.py",
                     "contract_deploy.py", os.path.join(".github", "workflows", "tests.yml"),
                     os.path.join("tools", "make_deploy.py")):
            self.assertTrue(os.path.exists(os.path.join(ROOT, name)), name)

    def test_license_is_mit(self):
        text = read(os.path.join(ROOT, "LICENSE"))
        self.assertIn("MIT License", text)
        self.assertIn("Permission is hereby granted, free of charge", text)


if __name__ == "__main__":
    unittest.main()

"""A trial build's patch is made and checked on our side (ADR 0027): every refusal class, and what passes.
Run: .venv/bin/python -m unittest tests.test_trial_patch"""
import unittest

from server import trial_patch as tp

PRISTINE = {"src/app.py": ("100644", b"x = 1\n"), "run.sh": ("100755", b"echo hi\n"), "tests/test_app.py": ("100644", b"ok\n"),
            "docs/link": ("120000", b"")}


def after(**changes) -> dict:
    """The sandbox's repo: the pristine plain files, then these changes (None deletes)."""
    files = {p: d for p, (m, d) in PRISTINE.items() if m in tp.PLAIN_MODES}
    for path, data in changes.items():
        path = path.replace("__", "/").replace("_DOT_", ".")
        if data is None:
            files.pop(path, None)
        else:
            files[path] = data
    return files


class Passes(unittest.TestCase):
    def test_a_change_an_addition_and_a_deletion(self):
        files = after(src__app_DOT_py=b"x = 2\n", src__new_DOT_py=b"y = 1\n", run_DOT_sh=None)
        patch = tp.validate(PRISTINE, files)
        self.assertEqual([(c.path, c.mode, c.data) for c in patch],
                         [("run.sh", "100755", None), ("src/app.py", "100644", b"x = 2\n"), ("src/new.py", "100644", b"y = 1\n")])

    def test_modes_come_from_the_pristine_copy_never_the_sandbox(self):
        patch = tp.validate(PRISTINE, after(run_DOT_sh=b"echo bye\n"))
        self.assertEqual(patch[0].mode, "100755")

    def test_build_output_is_left_out(self):
        files = after(src__app_DOT_py=b"x = 2\n")
        files["src/__pycache__/app.cpython-312.pyc"] = b"\0"
        files["node_modules/x/index.js"] = b"x"
        self.assertEqual([c.path for c in tp.validate(PRISTINE, files)], ["src/app.py"])

    def test_a_symlink_in_the_template_that_isnt_touched_is_fine(self):
        self.assertEqual(len(tp.validate(PRISTINE, after(src__app_DOT_py=b"x = 3\n"))), 1)


class Refusals(unittest.TestCase):
    def refused(self, files, links=(), other=()):
        with self.assertRaises(tp.Refused) as e:
            tp.validate(PRISTINE, files, list(links), list(other))
        return e.exception.rule

    def test_empty(self):
        self.assertEqual(self.refused(after()), tp.EMPTY)

    def test_protected_paths(self):
        for path in (".github/workflows/ci.yml", ".GitHub/x", ".gitattributes", "sub/.gitmodules", ".devcontainer/d.json",
                     ".vscode/settings.json", ".pre-commit-config.yaml", ".git/config", "a/.git/hooks/pre-commit"):
            files = after()
            files[path] = b"x\n"
            self.assertEqual(self.refused(files), tp.PROTECTED, path)

    def test_symlinks_and_other_kinds(self):
        self.assertEqual(self.refused(after(src__app_DOT_py=b"x = 2\n"), links=["src/evil"]), tp.LINK)
        self.assertEqual(self.refused(after(src__app_DOT_py=b"x = 2\n"), other=["src/fifo"]), tp.LINK)
        files = after()
        files["docs/link"] = b"not a link any more\n"      # over the template's symlink: a mode change
        self.assertEqual(self.refused(files), tp.LINK)

    def test_binary(self):
        self.assertEqual(self.refused(after(src__app_DOT_py=b"x\0y")), tp.BINARY)
        self.assertEqual(self.refused(after(src__app_DOT_py=b"\xff\xfe")), tp.BINARY)

    def test_tests_and_test_config(self):
        for path in ("tests/test_app.py", "tests/new_test.py", "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg",
                     "src/test_thing.py", "web/app.test.js", "web/x.spec.ts", "package.json", "jest.config.js", "tox.ini"):
            files = after()
            files[path] = b"x\n"
            self.assertEqual(self.refused(files), tp.TESTS, path)
        self.assertEqual(self.refused(after(tests__test_app_DOT_py=None)), tp.TESTS)   # deleting a test too

    def test_size_limits(self):
        files = after()
        files.update({f"src/f{i}.py": b"x\n" for i in range(tp.MAX_FILES + 1)})
        self.assertEqual(self.refused(files), tp.TOO_BIG)
        self.assertEqual(self.refused(after(src__app_DOT_py=b"x" * (tp.MAX_BYTES + 1))), tp.TOO_BIG)

    def test_bad_paths(self):
        for path in ("/etc/passwd", "../x.py", "src/../../x.py", "src//x.py", "src/./x.py", "src\\x.py", "src/a\nb.py",
                     "src/café.py", "src/`x`.py", "src/<x>.py"):
            files = after()
            files[path] = b"x\n"
            self.assertEqual(self.refused(files), tp.BAD_PATH, repr(path))

    def test_case_and_unicode_collisions(self):
        files = after()
        files["SRC/app.py"] = b"x = 9\n"                # one file on a case-insensitive disk
        self.assertEqual(self.refused(files), tp.BAD_PATH)
        files = after()
        files["src/ａpp.py"] = b"x\n"              # fullwidth "a": one name once NFKC-folded
        self.assertEqual(self.refused(files), tp.BAD_PATH)

    def test_the_refusal_names_a_plain_rule(self):
        with self.assertRaises(tp.Refused) as e:
            tp.validate(PRISTINE, after(src__app_DOT_py=b"x\0"))
        self.assertEqual(str(e.exception), tp.BINARY)


if __name__ == "__main__":
    unittest.main()

import unittest

from agentic_sim.assignment.semantic_cpu_model import SemanticCpuModel, semantic_features


class SemanticFeatureTests(unittest.TestCase):
    def test_cd_env_and_git_flags_choose_real_executable(self):
        features = semantic_features(
            "cd /repo && env GIT_PAGER=cat git -C /repo show HEAD",
            "/repo",
        )
        self.assertEqual(features["executable"], "git")
        self.assertEqual(features["operation"], "git_show")
        self.assertEqual(features["git_pager_susceptibility"], "explicit_no_pager")

    def test_pipeline_and_or_and_quoted_payloads_are_distinct(self):
        piped = semantic_features("git show HEAD | cat")
        alternate = semantic_features("git show HEAD || cat")
        quoted = semantic_features("echo 'a | pytest; not a command'")
        test_pipeline = semantic_features("python -m pytest tests/test_x.py | grep x")
        find_pipeline = semantic_features("find . -type f | grep pytest")
        self.assertEqual(piped["git_pager_susceptibility"], "piped")
        self.assertEqual(piped["pipeline_executables"], ("git", "cat"))
        self.assertEqual(alternate["git_pager_susceptibility"], "tty_candidate")
        self.assertEqual(quoted["pipeline_executables"], ("echo",))
        self.assertEqual(quoted["semantic_class"], "shell")
        self.assertEqual(test_pipeline["semantic_class"], "test")
        self.assertEqual(find_pipeline["semantic_class"], "traversal")

    def test_find_exec_and_predicates_keep_escaped_semicolon_opaque(self):
        features = semantic_features(r"find . -name '*.py' -type f -prune -maxdepth 2 -exec echo {} \;")
        self.assertEqual(features["semantic_class"], "traversal")
        self.assertEqual(features["find_exec_mode"], "per_file")
        self.assertEqual(features["find_predicate"], ("name", "type", "prune", "maxdepth"))
        self.assertEqual(features["scope"], "root")
        self.assertEqual(features["scope_depth_bucket"], 0)

    def test_editor_and_search_payloads_do_not_become_tests(self):
        editor = semantic_features("str_replace_editor str_replace src/a.py pytest --help")
        search = semantic_features("rg 'pytest -q' src")
        self.assertEqual(editor["semantic_class"], "editor")
        self.assertEqual(search["semantic_class"], "search")
        self.assertEqual(editor["runner"], "")
        self.assertEqual(search["runner"], "")

    def test_actual_test_runners_and_python_opaque_modes(self):
        self.assertEqual(semantic_features("python -m pytest tests/test_x.py")["runner"], "pytest")
        self.assertEqual(semantic_features("python -m django test")["runner"], "django")
        self.assertEqual(semantic_features("python -m django.core.management")["runner"], "module:django.core.management")
        self.assertEqual(semantic_features("manage.py test app")["runner"], "manage.py")
        self.assertEqual(semantic_features("tests/runtests.py")["runner"], "runtests.py")
        inline = semantic_features("python -c 'import pytest; pytest.main()'")
        self.assertEqual(inline["python_inline_imports"], ("pytest",))
        self.assertNotEqual(inline["semantic_class"], "test")

    def test_metadata_and_unknown_fallback(self):
        self.assertEqual(semantic_features("git --version")["operation"], "version")
        self.assertEqual(semantic_features("pytest --help")["operation"], "help")
        unknown = semantic_features("echo 'pytest is only text'")
        self.assertEqual(unknown["semantic_class"], "shell")
        self.assertEqual(unknown["operation"], "shell")
        self.assertNotIn("observed_ms", unknown)


def _row(action, observed, instance, repository="repo-a", operation_class="shell"):
    return {
        "operation_class": operation_class,
        "action": action,
        "repository": repository,
        "instance_id": instance,
        "run_id": f"run-{instance}-{observed}",
        "observed_ms": observed,
        # Original Grok CPU flags are accepted and deliberately not used as
        # semantic keys when an action is present.
        "tool_name": "git",
        "launch_family": "git",
        "subcommand": "show",
        "recursive": 0,
        "has_pipe": int("|" in action),
        "declared_path_count": 1,
    }


class SemanticCpuModelTests(unittest.TestCase):
    def test_sparse_piped_git_backoff_preserves_pager_mode(self):
        rows = [
            _row("git show HEAD", 100.0, f"tty-{index}") for index in range(8)
        ] + [
            _row("git log --oneline | cat", 900.0, f"pipe-{index}") for index in range(12)
        ] + [
            _row("git show HEAD | cat", 1200.0, f"show-pipe-{index}") for index in range(4)
        ]
        model = SemanticCpuModel().fit(rows)
        details = model.predict_details(_row("git show HEAD | cat", 1.0, "new"))
        self.assertNotEqual(details["selected_level"], "fine")
        self.assertEqual(details["semantic_features"]["git_pager_susceptibility"], "piped")
        self.assertGreater(details["prediction"], 500.0)

    def test_unseen_repository_uses_non_repository_semantic_backoff(self):
        rows = [_row("pytest tests/test_x.py", 300.0, f"i-{index}") for index in range(8)]
        model = SemanticCpuModel().fit(rows)
        row = _row("pytest tests/test_x.py", 1.0, "new", repository="unseen")
        details = model.predict_details(row)
        self.assertIn(details["selected_level"], {"fine", "coarse", "operation_executable", "semantic_class"})
        self.assertEqual(model.predict(row), 300.0)

    def test_support_counts_distinct_instances_not_repetitions(self):
        rows = [_row("pytest tests/test_x.py", 300.0 + index, "same") for index in range(32)]
        model = SemanticCpuModel().fit(rows)
        details = model.predict_details(_row("pytest tests/test_x.py", 1.0, "new"))
        self.assertEqual(details["selected_level"], "baseline")
        self.assertEqual(details["support"]["distinct_instances"], 0)

    def test_invalid_labels_raise(self):
        for value in (-1.0, 0.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SemanticCpuModel().fit([_row("pytest tests/test_x.py", value, "i")])

    def test_missing_action_explicitly_uses_baseline(self):
        rows = [_row("pytest tests/test_x.py", 300.0, f"i-{index}") for index in range(8)]
        model = SemanticCpuModel().fit(rows)
        details = model.predict_details({"operation_class": "shell", "tool_name": "pytest"})
        self.assertEqual(details["selected_level"], "baseline")
        self.assertEqual(details["fallback_reason"], "missing_action")

    def test_labels_do_not_enter_inference_row(self):
        rows = [_row("pytest tests/test_x.py", 300.0, f"i-{index}") for index in range(8)]
        model = SemanticCpuModel().fit(rows)
        first = _row("pytest tests/test_x.py", 1.0, "query")
        second = dict(first, observed_ms=999999.0)
        self.assertEqual(model.predict(first), model.predict(second))

    def test_mapping_roundtrip_and_gate_center(self):
        rows = [_row("pytest tests/test_x.py", 100.0, f"a-{index}") for index in range(8)]
        rows += [_row("pytest tests/test_x.py", 1000.0, f"b-{index}") for index in range(8)]
        model = SemanticCpuModel(center="gate").fit(rows)
        restored = SemanticCpuModel.from_mapping(model.to_mapping())
        probe = _row("pytest tests/test_x.py", 1.0, "probe")
        self.assertEqual(model.predict(probe), restored.predict(probe))
        self.assertEqual(model.to_mapping(), restored.to_mapping())
        details = model.predict_details(probe)
        self.assertIn("p10", details["distribution"])
        self.assertIn("p90", details["distribution"])
        self.assertIn("gate_coverage", details["distribution"])

    def test_semantic_modes_do_not_average_git_pager_modes(self):
        rows = [_row("git show HEAD", 100.0, f"u-{index}") for index in range(8)]
        rows += [_row("git show HEAD | cat", 1000.0, f"p-{index}") for index in range(8)]
        model = SemanticCpuModel().fit(rows)
        unpiped = model.predict(_row("git show HEAD", 1.0, "q1"))
        piped = model.predict(_row("git show HEAD | cat", 1.0, "q2"))
        self.assertEqual(unpiped, 100.0)
        self.assertEqual(piped, 1000.0)


if __name__ == "__main__":
    unittest.main()

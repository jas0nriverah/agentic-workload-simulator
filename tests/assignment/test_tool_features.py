import unittest

from agentic_sim.assignment.event_simulator import EventSimulatorError
from agentic_sim.assignment.tool_features import (
    EXTRACTOR_ID,
    extract_tool_features,
    extractor_source_sha256,
)


GOLDEN = (
    (
        'cd /testbed && grep -r "foo" --include="*.py" .',
        {"tool_name": "grep", "operation_class": "search", "has_pipe": 0, "has_glob": 1},
    ),
    (
        "cd /testbed && find /testbed -type f -name '*.py'",
        {"tool_name": "find", "operation_class": "traversal"},
    ),
    (
        "str_replace_editor view /testbed/sympy/core/expr.py",
        {"tool_name": "str_replace_editor", "subcommand": "view", "operation_class": "read"},
    ),
    (
        "str_replace_editor view /testbed",
        {"tool_name": "str_replace_editor", "subcommand": "view", "operation_class": "traversal"},
    ),
    (
        "str_replace_editor str_replace /testbed/a.py --old foo --new bar",
        {"tool_name": "str_replace_editor", "subcommand": "str_replace", "operation_class": "patch"},
    ),
    (
        "str_replace_editor create /testbed/new.py",
        {"tool_name": "str_replace_editor", "subcommand": "create", "operation_class": "write"},
    ),
    (
        "cd /testbed && pytest tests/test_foo.py",
        {"tool_name": "pytest", "operation_class": "test"},
    ),
    (
        "cd /testbed && awk '{print}' file.py | head -100",
        {"tool_name": "awk", "operation_class": "shell", "has_pipe": 1},
    ),
    (
        "cat /testbed/README.md",
        {"tool_name": "cat", "operation_class": "read"},
    ),
    (
        "cd /testbed && sed -n '1,20p' foo.py",
        {"tool_name": "sed", "operation_class": "read"},
    ),
    (
        "cd /testbed && sed -i 's/a/b/' foo.py",
        {"tool_name": "sed", "operation_class": "patch"},
    ),
    (
        "mkdir /tmp/work",
        {"tool_name": "mkdir", "operation_class": "write"},
    ),
    (
        "ls /testbed",
        {"tool_name": "ls", "operation_class": "traversal"},
    ),
    (
        'rg --files /testbed | grep "test_"',
        {"tool_name": "rg", "operation_class": "search", "has_pipe": 1},
    ),
    (
        "cd /testbed ; python -m pytest",
        {"tool_name": "python", "operation_class": "test"},
    ),
    (
        "apply_patch <<'EOF'\n*** Update File: a.py\nEOF",
        {"tool_name": "apply_patch", "operation_class": "patch"},
    ),
        (
            "cd /testbed && python3 setup.py test",
            {"tool_name": "python3", "operation_class": "shell"},
        ),
    (
        "head -n 40 /testbed/pkg/mod.py",
        {"tool_name": "head", "operation_class": "read"},
    ),
    (
        "cd /testbed && git status",
        {"tool_name": "git", "operation_class": "shell"},
    ),
    (
        "str_replace_editor view /testbed/pkg --view_range 1 20",
        {"tool_name": "str_replace_editor", "operation_class": "read"},
    ),
    (
        "cd /testbed && echo hi > /tmp/out.txt",
        {"tool_name": "echo", "operation_class": "write"},
    ),
    (
        "touch /testbed/new_file.py",
        {"tool_name": "touch", "operation_class": "write"},
    ),
    (
        "cd /testbed && cargo test",
        {"tool_name": "cargo", "operation_class": "test"},
    ),
    (
        "find . -name '*.py'",
        {"tool_name": "find", "operation_class": "traversal", "has_glob": 1},
    ),
    (
        "grep -n TODO ./src",
        {"tool_name": "grep", "operation_class": "search"},
    ),
    (
        "cd /testbed && npm test",
        {"tool_name": "npm", "operation_class": "test"},
    ),
    (
        "cd /testbed && tox -e py310",
        {"tool_name": "tox", "operation_class": "test"},
    ),
    (
        "open_file /testbed/a.py",
        {"tool_name": "open_file", "operation_class": "read"},
    ),
    (
        "list_dir /testbed/pkg",
        {"tool_name": "list_dir", "operation_class": "traversal"},
    ),
    (
        "cd /testbed",
        {"tool_name": "cd", "operation_class": "shell"},
    ),
)


class ToolFeatureExtractorTests(unittest.TestCase):
    def test_golden_actions_bind_one_definition(self):
        self.assertEqual(len(GOLDEN), 30)
        for action, expected in GOLDEN:
            with self.subTest(action=action):
                features = extract_tool_features(action)
                for key, value in expected.items():
                    self.assertEqual(getattr(features, key), value, key)
                self.assertEqual(features.extractor_id, EXTRACTOR_ID)
                self.assertEqual(features.extractor_sha256, extractor_source_sha256())
                self.assertEqual(features.declared_command_bytes, len(action.encode("utf-8")))
                self.assertGreaterEqual(features.declared_path_count, 0)
                self.assertEqual(len(features.command_sha256), 64)

    def test_cd_prefix_is_not_the_tool(self):
        features = extract_tool_features("cd /testbed && grep -n foo bar.py")
        self.assertEqual(features.tool_name, "grep")
        self.assertNotEqual(features.tool_name, "cd")
        self.assertEqual(features.operation_class, "search")

    def test_str_replace_editor_view_is_not_patch(self):
        view = extract_tool_features("str_replace_editor view /testbed/a.py")
        patch = extract_tool_features("str_replace_editor str_replace /testbed/a.py")
        self.assertEqual(view.operation_class, "read")
        self.assertEqual(patch.operation_class, "patch")

    def test_empty_action_is_rejected(self):
        with self.assertRaisesRegex(EventSimulatorError, "non-empty"):
            extract_tool_features("   ")

    def test_historical_and_live_call_the_same_function(self):
        action = "cd /testbed && awk '{print $1}' | head"
        first = extract_tool_features(action)
        second = extract_tool_features(action)
        self.assertEqual(first, second)
        self.assertEqual(first.command_sha256, second.command_sha256)


if __name__ == "__main__":
    unittest.main()

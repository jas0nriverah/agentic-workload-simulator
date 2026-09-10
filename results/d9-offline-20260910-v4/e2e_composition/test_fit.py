import importlib.util
from pathlib import Path


PATH = Path(__file__).with_name("fit.py")
SPEC = importlib.util.spec_from_file_location("e2e_composition_fit", PATH)
FIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FIT)


def test_grouped_fold_is_stable_and_instance_bound():
    assert FIT.grouped_fold("astropy__astropy-14182") == FIT.grouped_fold(
        "astropy__astropy-14182"
    )
    assert 0 <= FIT.grouped_fold("django__django-10914") < 5


def test_saved_design_excludes_result_fields():
    forbidden = {
        "observed_ms",
        "duration_ms",
        "residual_ms",
        "outcome",
        "resolved",
        "status",
        "retry_index",
        "case_id",
        "instance_id",
    }
    features = set(FIT.CPU_FEATURES) | set(FIT.GPU_FEATURES) | set(FIT.REMAINDER_FEATURES)
    assert not features & forbidden


def test_instance_grouped_predictions_and_exact_composition():
    report = FIT.run()
    rows, _ = FIT.load_rows()
    assert report["cohort"] == {"cases": 43, "instances": 22}
    saved = [FIT.json.loads(line) for line in FIT.PREDICTIONS.read_text().splitlines()]
    assert len(saved) == len(rows)
    for row in saved:
        assert row["composed_prediction_ms"] == (
            row["cpu_prediction_ms"]
            + row["native_prediction_ms"]
            + row["remainder_prediction_ms"]
        )
        assert row["remainder_ms"] > 0


def test_nnls_predictions_are_nonnegative():
    beta = FIT.fit_nnls([[1.0, 0.0], [1.0, 1.0]], [1.0, 2.0], relative=True)
    assert all(value >= 0 for value in beta)
    assert FIT.predict(beta, [1.0, 3.0]) >= 0


def test_coverage_scale_finds_non_observed_optimum():
    scale = FIT.fit_coverage_scale([1.0, 1.0], [100.0, 160.0])
    assert 120.0 <= scale <= 125.0
    assert scale not in {100.0, 160.0}


def test_saved_instance_groups_do_not_cross_folds():
    rows, _ = FIT.load_rows()
    folds = {}
    for row in rows:
        folds.setdefault(row["instance_id"], set()).add(row["fold"])
    assert all(len(values) == 1 for values in folds.values())

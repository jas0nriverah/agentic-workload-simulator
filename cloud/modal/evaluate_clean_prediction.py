"""Evaluate a provenance-preserving clean version of a measured prediction.

This does not alter the raw SWE-agent output. It creates a derived prediction
that retains only the intended production source file, then runs the pinned
official SWE-bench evaluator in a Docker-enabled Modal Sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import modal

RESULT_ROOT = os.environ.get("EIC_RAW_ROOT", "lite-control-full-prompt")
CLEAN_ROOT = os.environ.get("EIC_CLEAN_ROOT", "lite-control-full-prompt-clean")
INSTANCE_ID = os.environ.get("EIC_INSTANCE_ID", "astropy__astropy-12907")
DATASET_NAME = os.environ.get("EIC_DATASET_NAME", "SWE-bench/SWE-bench_Lite")
SOURCE_PATH = os.environ.get("EIC_SOURCE_PATH", "astropy/modeling/separable.py")
RUN_ID = os.environ.get("EIC_RUN_ID", "modal-lite-control-full-prompt-clean")
PATCH_MODE = os.environ.get("EIC_PATCH_MODE", "production_only")
SWE_BENCH_REVISION = os.environ.get(
    "EIC_SWE_BENCH_REVISION", "726c5461e2ef52d83cf1ea2107870a8bb3328d57"
)

app = modal.App("eic-clean-prediction-evaluator")
results = modal.Volume.from_name("eic-results", create_if_missing=True)

evaluator_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates", "docker.io", "git")
    .pip_install(
        "beautifulsoup4==4.15.0",
        "chardet==7.6.0",
        "datasets==5.0.1",
        "docker==7.2.0",
        "ghapi==2.1.2",
        "gitpython==3.1.59",
        "modal==1.5.4",
        "python-dotenv==1.2.2",
        "requests==2.34.2",
        "rich==15.0.0",
        "tenacity==9.1.4",
        "tqdm==4.70.0",
        "unidiff==1.0.0",
    )
    .run_commands(
        f"git clone --filter=blob:none https://github.com/SWE-bench/SWE-bench.git /opt/SWE-bench "
        f"&& git -C /opt/SWE-bench fetch --depth 1 origin {SWE_BENCH_REVISION} "
        f"&& git -C /opt/SWE-bench checkout --detach {SWE_BENCH_REVISION} "
        "&& python -m pip install --no-cache-dir --no-deps -e /opt/SWE-bench",
    )
)


def _production_patch(raw_patch: str, source_path: str, patch_mode: str) -> str:
    """Keep the measured source diff and drop agent-created helper files."""

    sections = raw_patch.split("diff --git ")
    kept = [
        "diff --git " + section
        for section in sections[1:]
        if section.startswith(f"a/{source_path} ")
    ]
    if len(kept) != 1:
        raise ValueError("Expected exactly one production source diff")
    patch = "".join(kept)
    if patch_mode == "reference_completion":
        if source_path != "astropy/io/ascii/qdp.py":
            raise ValueError("reference_completion is only defined for the QDP task")
        patch += (
            "@@ -307,7 +307,7 @@ def _get_tables_from_qdp_file(qdp_file, input_colnames=None, delimiter=None):\n"
            "            values = []\n"
            "            for v in line.split(delimiter):\n"
            '-                if v == "NO":\n'
            '+                if v.upper() == "NO":\n'
            "                    values.append(np.ma.masked)\n"
        )
    elif patch_mode == "reference_completion_rst":
        if source_path != "astropy/io/ascii/rst.py":
            raise ValueError("reference_completion_rst is only defined for the RST task")
        patch = (
            "diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py\n"
            "--- a/astropy/io/ascii/rst.py\n"
            "+++ b/astropy/io/ascii/rst.py\n"
            "@@ -27,3 +27,2 @@ class SimpleRSTData(FixedWidthData):\n"
            "-    start_line = 3\n"
            "     end_line = -1\n"
            "     splitter_class = FixedWidthTwoLineDataSplitter\n"
            "@@ -57,10 +57,15 @@ class RST(FixedWidth):\n"
            "     data_class = SimpleRSTData\n"
            "     header_class = SimpleRSTHeader\n"
            " \n"
            "-    def __init__(self):\n"
            "-        super().__init__(delimiter_pad=None, bookend=False)\n"
            "+    def __init__(self, header_rows=None):\n"
            "+        super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)\n"
            " \n"
            "     def write(self, lines):\n"
            "         lines = super().write(lines)\n"
            "-        lines = [lines[1]] + lines + [lines[1]]\n"
            "+        idx = len(self.header.header_rows)\n"
            "+        lines = [lines[idx]] + lines + [lines[idx]]\n"
            "         return lines\n"
            "+\n"
            "+    def read(self, table):\n"
            "+        self.data.start_line = 2 + len(self.header.header_rows)\n"
            "+        return super().read(table)\n"
        )
    elif patch_mode == "reference_completion_wcs":
        if source_path != "astropy/wcs/wcs.py":
            raise ValueError("reference_completion_wcs is only defined for the WCS task")
        patch += (
            "@@ -1235,6 +1238,8 @@ def _return_single_array(xy, origin):\n"
            "                 raise ValueError(\n"
            '                     \"When providing two arguments, the array must be \"\n'
            "                     \"of shape (N, {0})\".format(self.naxis))\n"
            "+            if 0 in xy.shape:\n"
            "+                return xy\n"
            "             if ra_dec_order and sky == 'input':\n"
        )
    elif patch_mode != "production_only":
        raise ValueError(f"unsupported patch mode: {patch_mode}")
    return patch


@app.function(image=modal.Image.debian_slim(python_version="3.11"), volumes={"/results": results})
def prepare_clean_prediction(config: dict[str, str]) -> dict:
    result_root = config["result_root"]
    clean_root_name = config["clean_root"]
    instance_id = config["instance_id"]
    source_path = config["source_path"]
    dataset_name = config["dataset_name"]
    patch_mode = config["patch_mode"]
    raw_path = Path("/results") / result_root / "preds.json"
    clean_root = Path("/results") / clean_root_name
    clean_root.mkdir(parents=True, exist_ok=True)

    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    if isinstance(raw_payload, dict):
        records = raw_payload
        record = records[instance_id]
        patch = record["model_patch"]
        record["model_patch"] = _production_patch(patch, source_path, patch_mode)
    elif isinstance(raw_payload, list):
        records = raw_payload
        matching = [record for record in records if record.get("instance_id") == instance_id]
        if len(matching) != 1:
            raise ValueError("Expected exactly one matching prediction")
        matching[0]["model_patch"] = _production_patch(
            matching[0]["model_patch"], source_path, patch_mode
        )
    else:
        raise TypeError("Unsupported prediction JSON shape")

    clean_path = clean_root / "preds-clean.json"
    clean_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    patch_text = (
        record["model_patch"] if isinstance(records, dict) else matching[0]["model_patch"]
    )
    (clean_root / "clean.patch").write_text(patch_text, encoding="utf-8")
    payload = {
        "schema_version": "modal-clean-prediction.v1",
        "provenance": "derived_from_measured_prediction",
        "instance_id": instance_id,
        "raw_prediction": str(raw_path),
        "clean_prediction": str(clean_path),
        "raw_prediction_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "clean_prediction_sha256": hashlib.sha256(clean_path.read_bytes()).hexdigest(),
        "clean_patch_sha256": hashlib.sha256(patch_text.encode()).hexdigest(),
        "clean_patch_files": [source_path],
        "dataset": dataset_name,
        "patch_mode": patch_mode,
    }
    (clean_root / "prepare_result.json").write_text(json.dumps(payload, indent=2) + "\n")
    results.commit()
    return payload


@app.function(image=modal.Image.debian_slim(), volumes={"/results": results})
def store_evaluation_output(output: str, config: dict[str, str]) -> None:
    path = Path("/results") / config["clean_root"] / "evaluator.stdout.log"
    path.write_text(output, encoding="utf-8")
    results.commit()


@app.local_entrypoint()
def main() -> None:
    config = {
        "result_root": RESULT_ROOT,
        "clean_root": CLEAN_ROOT,
        "instance_id": INSTANCE_ID,
        "dataset_name": DATASET_NAME,
        "source_path": SOURCE_PATH,
        "run_id": RUN_ID,
        "patch_mode": PATCH_MODE,
    }
    prepared = prepare_clean_prediction.remote(config)
    print(json.dumps(prepared, indent=2))

    eval_command = (
        "set -Eeuo pipefail; "
        f"mkdir -p /results/{config['clean_root']}/evaluation; "
        "dockerd --host=unix:///var/run/docker.sock >/tmp/dockerd.log 2>&1 & "
        "for i in $(seq 1 180); do docker info >/dev/null 2>&1 && break; sleep 1; done; "
        "docker info >/dev/null 2>&1; "
        f"cd /results/{config['clean_root']}/evaluation; "
        "python -m swebench.harness.run_evaluation "
        f"--dataset_name {config['dataset_name']} "
        "--split test "
        f"--predictions_path /results/{config['clean_root']}/preds-clean.json "
        f"--instance_ids {config['instance_id']} "
        "--max_workers 1 --timeout 1800 --cache_level instance --clean False "
        f"--run_id {config['run_id']} --namespace swebench "
        "--instance_image_tag latest "
        f"--report_dir /results/{config['clean_root']}/evaluation"
    )
    sandbox = modal.Sandbox.create(
        "bash",
        "-lc",
        eval_command,
        app=app,
        image=evaluator_image,
        timeout=90 * 60,
        idle_timeout=90 * 60,
        cpu=8,
        memory=32768,
        volumes={"/results": results},
        experimental_options={"vm_runtime": True},
    )
    output = sandbox.stdout.read()
    stderr = sandbox.stderr.read()
    sandbox.wait(raise_on_termination=False)
    combined = output + ("\nSTDERR:\n" + stderr if stderr else "")
    print(combined)
    store_evaluation_output.remote(combined, config)
    print(f"Evaluator sandbox exit code: {sandbox.returncode}")

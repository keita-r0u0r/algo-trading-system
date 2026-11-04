"""前の版のイメージが消えていたら、その版のソースから作り直す（make rollback が使う）。

  python scripts/fx_rebuild_image.py 333b491

Artifact Registry は最新3個のイメージだけを残す（infra/main.tf の cleanup_policies）。戻す先の版のイメージが無ければ、
git の別の作業フォルダにその版を取り出し、``make image`` と同じ設定で Cloud Build に作らせる。あれば何もしない。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tofu_output(name: str) -> str:
    return subprocess.run(["tofu", "-chdir=infra", "output", "-raw", name], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def project() -> str:
    return subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True,
                          check=True).stdout.strip()


def main() -> int:
    if len(sys.argv) != 2:
        print("使い方: fx_rebuild_image.py <版>", file=sys.stderr)
        return 2
    version = sys.argv[1]
    image = tofu_output("image")
    found = subprocess.run(["gcloud", "artifacts", "docker", "images", "describe", f"{image}:{version}",
                            f"--project={project()}"], capture_output=True, text=True)
    if found.returncode == 0:
        print(f"版 {version} のイメージはある")
        return 0
    print(f"版 {version} のイメージが無い（古いイメージは自動で消える）。その版のソースから作り直す")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(source), version], cwd=ROOT, check=True)
        try:
            build = subprocess.run([
                "gcloud", "builds", "submit", str(source), f"--project={project()}", "--region=asia-northeast1",
                f"--config={source / 'deploy' / 'cloudbuild.yaml'}",
                f"--service-account={tofu_output('builder_service_account')}",
                f"--gcs-source-staging-dir=gs://{tofu_output('build_source_bucket')}/source",
                f"--substitutions=_IMAGE={image},_TAG={version},_VERSION={version}"], cwd=ROOT)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(source)], cwd=ROOT, check=False)
    return build.returncode


if __name__ == "__main__":
    raise SystemExit(main())

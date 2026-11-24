"""Python Lambda packaging that works with or without Docker.

``cdk synth`` bundles each service directory together with ``services/shared``
and its pinned ``requirements.txt``. We try *local* bundling first using pip's
cross-platform wheel download (``--platform manylinux2014_aarch64
--only-binary=:all:``), which works on macOS, Linux and CI without Docker. If
that fails (for example a dependency only ships an sdist) CDK falls back to the
official SAM build image in Docker.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as _lambda

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO_ROOT / "services"
PYTHON_VERSION = "3.12"
RUNTIME = _lambda.Runtime.PYTHON_3_12
ARCHITECTURE = _lambda.Architecture.ARM_64
PIP_PLATFORM = "manylinux2014_aarch64"

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests")


@jsii.implements(ILocalBundling)
class _LocalPipBundler:
    def __init__(self, service: str):
        self.service = service

    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:  # noqa: ARG002 - jsii signature
        out = Path(output_dir)
        src = SERVICES_DIR / self.service
        req = src / "requirements.txt"
        try:
            if req.exists():
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--quiet",
                        "--disable-pip-version-check",
                        "--requirement",
                        str(req),
                        "--target",
                        str(out),
                        "--platform",
                        PIP_PLATFORM,
                        "--implementation",
                        "cp",
                        "--python-version",
                        PYTHON_VERSION,
                        "--only-binary=:all:",
                        "--upgrade",
                    ],
                    check=True,
                )
            shutil.copytree(src, out, dirs_exist_ok=True, ignore=_IGNORE)
            shutil.copytree(SERVICES_DIR / "shared", out / "shared", dirs_exist_ok=True, ignore=_IGNORE)
            return True
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[bundling] local bundling of '{self.service}' failed ({exc}); falling back to Docker", file=sys.stderr)
            return False


def service_code(service: str) -> _lambda.Code:
    """Asset for ``services/<service>`` + ``services/shared`` with dependencies."""
    docker_cmd = (
        f"pip install --no-cache-dir -r {service}/requirements.txt -t /asset-output "
        f"&& cp -r {service}/. /asset-output/ && cp -r shared /asset-output/shared"
    )
    return _lambda.Code.from_asset(
        str(SERVICES_DIR),
        exclude=["*", f"!{service}", f"!{service}/**", "!shared", "!shared/**", "**/__pycache__", "**/*.pyc"],
        bundling=BundlingOptions(
            image=DockerImage.from_registry(f"public.ecr.aws/sam/build-python{PYTHON_VERSION}:latest-arm64"),
            command=["bash", "-c", docker_cmd],
            local=_LocalPipBundler(service),
            platform="linux/arm64",
        ),
    )

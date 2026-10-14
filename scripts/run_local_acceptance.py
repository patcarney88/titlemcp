"""Build and test disposable local containers serially; no providers or deployment.

Requires the local OrbStack socket and public image/package downloads for the build.
No host environment, Docker credential store, .env, or provider credential is loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOCKET = Path.home() / ".orbstack/run/docker.sock"
DOCKER = "/usr/local/bin/docker"


def main() -> None:
    run_id = secrets.token_hex(6)
    label = f"titlemcp.acceptance={run_id}"
    prefix = f"titlemcp-accept-{run_id}"
    output = ROOT.parent / "acceptance-results" / run_id
    output.mkdir(parents=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    version = f"0.3.7+acceptance.{revision}"
    image = f"titlemcp-local-acceptance:{run_id}"
    results = {
        "run_id": run_id,
        "source_revision": revision,
        "version": version,
        "status": "running",
        "containers_memory_limit_mib": 768,
        "published_ports": [],
        "live_credentials": False,
        "deployment": False,
        "checks": {},
    }
    artifacts = ROOT / "scripts/acceptance"
    containers: list[str] = []
    network = None
    built = False

    def interrupted(signum, _frame):
        raise RuntimeError(f"Acceptance interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="titlemcp-local-docker-") as temporary:
        private = Path(temporary)
        config = private / "docker"
        config.mkdir()
        plugin = Path.home() / ".docker/cli-plugins/docker-buildx"
        if plugin.is_file():
            (config / "cli-plugins").mkdir()
            (config / "cli-plugins/docker-buildx").symlink_to(plugin)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "DOCKER_CONFIG": str(config),
            "DOCKER_HOST": f"unix://{SOCKET}",
            "DOCKER_BUILDKIT": "1",
            "BUILDX_GIT_INFO": "false",
        }

        def call(name: str, args: list[str], *, timeout: int = 30, check: bool = True) -> str:
            # Each subprocess is task-owned, including any buildx child processes.
            with (output / f"{name}.log").open("a+") as log:
                offset = log.tell()
                process = subprocess.Popen(
                    [DOCKER, *args],
                    env=env,
                    cwd=ROOT,
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    process.wait(timeout=timeout)
                except BaseException:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
                log.seek(offset)
                text = log.read()
            if check and process.returncode:
                raise RuntimeError(
                    f"{name} failed ({process.returncode}); see {output / (name + '.log')}"
                )
            return text.strip()

        def common(name: str) -> list[str]:
            return [
                "--name",
                name,
                "--label",
                label,
                "--cpus",
                "0.5",
                "--memory",
                "384m",
                "--memory-swap",
                "384m",
                "--pids-limit",
                "128",
                "--network",
                network,
            ]

        password = secrets.token_hex(24)
        token = secrets.token_hex(32)
        pg_env = private / "postgres.env"
        app_env = private / "app.env"
        pg_env.write_text(
            f"POSTGRES_USER=acceptance_user\nPOSTGRES_PASSWORD={password}\n"
            f"POSTGRES_DB=titlemcp_acceptance_{run_id}\n"
        )
        app_env.write_text(
            f"TITLEMCP_ACCEPTANCE_RUN={run_id}\n"
            f"TITLEMCP_ACCEPTANCE_VERSION={version}\nPORT=18000\n"
            "TITLE_MCP_STATE_BACKEND=postgres\n"
            f"TITLE_MCP_POSTGRES_DSN=postgresql://acceptance_user:{password}@{prefix}-db:5432/titlemcp_acceptance_{run_id}\n"
            f"TITLE_MCP_MCP_ACCESS_TOKEN={token}\nTITLE_MCP_BACKGROUND_WORKER_CONCURRENCY=1\n"
            "TITLE_MCP_LOG_LEVEL=WARNING\n"
        )
        for file in (pg_env, app_env):
            file.chmod(0o600)
        mount = ["--mount", f"type=bind,src={artifacts},dst=/acceptance,readonly"]
        try:
            print(f"Acceptance {run_id}: build first; evidence {output}", flush=True)
            baseline = call("baseline", ["ps", "-q"]).splitlines()
            # Content identity covers exactly the inputs COPY uses, not test harness files.
            inputs = [
                ROOT / "Dockerfile",
                ROOT / "constraints-docker.txt",
                ROOT / "scripts/validate_image_identity.py",
            ]
            for directory in (
                "packages/titlemcp",
                "packages/jurisdictions/us/oh/franklin/recorder",
            ):
                inputs += [ROOT / directory / f for f in ("pyproject.toml", "README.md")]
                inputs += [
                    p
                    for p in (ROOT / directory / "src").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
                ]
            inputs += [
                ROOT / "packages/titlemcp/LICENSE",
                ROOT / "packages/jurisdictions/us/oh/franklin/recorder/titlemcp-capability.toml",
            ]
            digest = hashlib.sha256()
            for file in sorted(inputs):
                digest.update(
                    file.relative_to(ROOT).as_posix().encode() + b"\0" + file.read_bytes()
                )
            results["build_input_sha256"] = digest.hexdigest()
            context = private / "build-context"
            for file in [*inputs, ROOT / ".dockerignore"]:
                destination = context / file.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, destination)
            call(
                "build",
                [
                    "build",
                    "--builder",
                    "default",
                    "--progress=plain",
                    "--ulimit",
                    "data=1073741824:1073741824",
                    "--shm-size",
                    "32m",
                    "--build-arg",
                    f"TITLEMCP_VERSION={version}",
                    "--build-arg",
                    f"SOURCE_REVISION={revision}",
                    "--label",
                    label,
                    "-t",
                    image,
                    str(context),
                ],
                timeout=480,
            )
            built = True
            results["image"] = json.loads(
                call("image-identity", ["image", "inspect", image, "--format", "{{json .Id}}"])
            )
            results["image_labels"] = json.loads(
                call(
                    "image-labels",
                    ["image", "inspect", image, "--format", "{{json .Config.Labels}} "],
                )
            )
            assert results["image_labels"]["org.opencontainers.image.revision"] == revision
            assert results["image_labels"]["org.opencontainers.image.version"] == version
            identity_name = prefix + "-identity"
            containers.append(identity_name)
            results["checks"]["identity"] = json.loads(
                call(
                    "installed-identity",
                    [
                        "run",
                        "--rm",
                        "--name",
                        identity_name,
                        "--label",
                        label,
                        "--network",
                        "none",
                        "--memory",
                        "256m",
                        "--memory-swap",
                        "256m",
                        "--cpus",
                        "0.5",
                        "--pids-limit",
                        "64",
                        "--env-file",
                        str(app_env),
                        *mount,
                        image,
                        "python",
                        "/acceptance/runtime_checks.py",
                        "identity",
                    ],
                )
            )
            print(
                "Image built and installed identity checked; starting disposable Postgres",
                flush=True,
            )
            network = call(
                "network-create",
                ["network", "create", "--internal", "--label", label, prefix + "-net"],
            )
            pg = prefix + "-db"
            containers.append(pg)
            call(
                "postgres-start",
                [
                    "run",
                    "-d",
                    *common(pg),
                    "--env-file",
                    str(pg_env),
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,nosuid,nodev,size=256m",
                    "--shm-size",
                    "32m",
                    "--pull=never",
                    "postgres:16-alpine",
                    "postgres",
                    "-c",
                    "shared_buffers=16MB",
                    "-c",
                    "max_connections=40",
                    "-c",
                    "work_mem=1MB",
                    "-c",
                    "maintenance_work_mem=16MB",
                ],
            )
            for _ in range(30):
                ready = call(
                    "postgres-ready",
                    [
                        "exec",
                        pg,
                        "pg_isready",
                        "-U",
                        "acceptance_user",
                        "-d",
                        f"titlemcp_acceptance_{run_id}",
                    ],
                    check=False,
                )
                if "accepting connections" in ready:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Disposable Postgres did not become ready")
            pg_test = prefix + "-postgres-tests"
            containers.append(pg_test)
            call(
                "postgres-tests",
                [
                    "run",
                    "--rm",
                    *common(pg_test),
                    "--env-file",
                    str(app_env),
                    *mount,
                    image,
                    "python",
                    "/acceptance/postgres_checks.py",
                ],
                timeout=90,
            )
            results["checks"]["postgres"] = {"tests": 8, "status": "passed"}
            print("Postgres acceptance passed; starting the real HTTP/MCP server", flush=True)
            app = prefix + "-app"
            containers.append(app)
            call(
                "app-start",
                [
                    "run",
                    "-d",
                    *common(app),
                    "--read-only",
                    "--tmpfs",
                    "/tmp:size=32m",
                    "--env-file",
                    str(app_env),
                    *mount,
                    image,
                ],
            )
            created = json.loads(
                call(
                    "http-mcp",
                    ["exec", app, "python", "/acceptance/runtime_checks.py", "create"],
                    timeout=40,
                )
            )
            results["checks"]["http_mcp"] = created
            call("app-stop", ["stop", "--time", "10", app])
            stopped = json.loads(
                call("app-stopped", ["inspect", app, "--format", "{{json .State}} "])
            )
            # The pinned server restores and re-raises SIGTERM after graceful cleanup.
            assert stopped["ExitCode"] in {0, 143} and not stopped["OOMKilled"], (
                "Server must stop without forced termination or OOM"
            )
            results["checks"]["graceful_stop"] = {
                "exit_code": stopped["ExitCode"],
                "oom_killed": stopped["OOMKilled"],
            }
            seed_name = prefix + "-seed"
            containers.append(seed_name)
            seeded = json.loads(
                call(
                    "seed-recovery",
                    [
                        "run",
                        "--rm",
                        *common(seed_name),
                        "--env-file",
                        str(app_env),
                        *mount,
                        image,
                        "python",
                        "/acceptance/runtime_checks.py",
                        "seed",
                        created["workflow_id"],
                    ],
                )
            )
            call("app-restart", ["start", app])
            results["checks"]["process_restart"] = json.loads(
                call(
                    "restart-recovery",
                    [
                        "exec",
                        app,
                        "python",
                        "/acceptance/runtime_checks.py",
                        "verify",
                        json.dumps(seeded),
                    ],
                    timeout=40,
                )
            )
            results["resource_snapshot"] = call(
                "task-stats",
                [
                    "stats",
                    "--no-stream",
                    app,
                    pg,
                    "--format",
                    "{{.Name}} {{.MemUsage}} {{.CPUPerc}}",
                ],
                timeout=15,
            )
            results["status"] = "passed"
        except BaseException as exc:
            results["status"] = "failed"
            results["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            cleanup_errors = []
            for container in reversed(containers):
                try:
                    call("container-logs", ["logs", "--tail", "60", container], check=False)
                    call("cleanup", ["rm", "--force", "--volumes", container], check=False)
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if network:
                call("cleanup-network", ["network", "rm", network], check=False)
            if built:
                call("cleanup-image", ["image", "rm", image], check=False)
            remaining = call("cleanup-readback", ["ps", "-a", "--filter", f"label={label}", "-q"])
            remaining_network = call(
                "network-readback", ["network", "ls", "--filter", f"label={label}", "-q"]
            )
            results["cleanup"] = {
                "remaining_containers": remaining.splitlines(),
                "remaining_networks": remaining_network.splitlines(),
                "errors": cleanup_errors,
            }
            results["baseline_containers_still_running"] = (
                set(baseline) <= set(call("baseline-readback", ["ps", "-q"]).splitlines())
                if "baseline" in locals()
                else None
            )
            if remaining or remaining_network or cleanup_errors:
                results["status"] = "failed"
            (output / "result.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(results, indent=2), flush=True)
    if results["status"] != "passed":
        raise RuntimeError("Acceptance failed; inspect the retained result and logs")


if __name__ == "__main__":
    main()

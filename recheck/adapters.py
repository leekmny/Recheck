"""Real Ghidra Headless and Docker CLI adapters; no host execution of targets."""

from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .process import run_process
from .models import atomic_json, TestCase
from .paths import filesystem_path, plain_local_path


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "resources"


def _event(callback, **event):
    if callback:
        callback(event)


def _ok(process):
    return process.get("returncode") == 0 and not any(
        process.get(k) for k in ("error", "timed_out", "cancelled", "truncated")
    )


def _desktop_path():
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DockerDesktop/Docker Desktop.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Docker/Docker/Docker Desktop.exe",
    ]
    return next((str(p) for p in candidates if p.is_file()), None)


def discover_docker_cli():
    """PATH and known Windows installer paths; no installation or environment edits."""
    found = shutil.which("docker")
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs/DockerDesktop/resources/bin/docker.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Docker/Docker/resources/bin/docker.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


class DockerAdapter:
    images = {"linux": "recheck-analysis-linux:1", "windows": "recheck-analysis-windows:1"}

    def __init__(self, cli=None, context="", resources=None, *, auto_prepare=False):
        self.cli = cli or discover_docker_cli()
        self.context = context
        self.resources = Path(resources) if resources else resource_root()
        self.auto_prepare = auto_prepare

    def _definition_hash(self, target_os):
        definition_dir = self.resources / "docker" / target_os
        definition = hashlib.sha256()
        for source in sorted(p for p in definition_dir.rglob("*") if p.is_file()):
            definition.update(source.relative_to(definition_dir).as_posix().encode("utf-8"))
            definition.update(b"\0" + source.read_bytes())
        return definition.hexdigest()

    async def image_readiness(self, target_os, cancel=None):
        """Inspect the selected legacy image without building or downloading it."""
        inspected = await self.call("image", "inspect", self.images[target_os], timeout=15, cancel=cancel)
        ready = False
        if _ok(inspected):
            try:
                details = json.loads(inspected["stdout"])[0]
                labels = (details.get("Config") or {}).get("Labels") or {}
                ready = (details.get("Os") == target_os
                         and details.get("Architecture") in ("amd64", "x86_64")
                         and labels.get("org.recheck.definition_sha256") == self._definition_hash(target_os))
            except (ValueError, IndexError, TypeError, AttributeError):
                pass
        return dict(ready=ready, status='ready' if ready else 'blocked', image=self.images[target_os])

    def command(self, *args):
        if not self.cli:
            raise RuntimeError(
                "Docker CLI 미설치: https://docs.docker.com/desktop/setup/install/windows-install/"
            )
        return [self.cli] + (["--context", self.context] if self.context else []) + list(args)

    async def call(self, *args, **kwargs):
        return await run_process(self.command(*args), **kwargs)

    async def probe(self):
        result = {
            "cli": self.cli,
            "desktop": _desktop_path(),
            "ready": False,
            "status": "not_installed",
            "context": self.context,
            "install_url": "https://docs.docker.com/desktop/setup/install/windows-install/",
        }
        if not self.cli:
            return result
        version = await self.call("version", "--format", "{{json .}}", timeout=15)
        result["process"] = version
        context = await self.call("context", "inspect", timeout=8)
        try:
            info = json.loads(context["stdout"])[0]
            result["context_info"] = info
            result["context"] = info.get("Name", self.context)
            endpoint = info.get("Endpoints", {}).get("docker", {}).get("Host", "")
            result["local_endpoint"] = endpoint.startswith(("npipe:", "unix:"))
        except (ValueError, IndexError, TypeError):
            result["local_endpoint"] = False
        try:
            data = json.loads(version["stdout"])
            result["version"] = data
            server = data.get("Server")
            if _ok(version) and server:
                result.update(
                    ready=True,
                    status="ready",
                    os=server.get("Os"),
                    arch=server.get("Arch"),
                    server_version=server.get("Version"),
                    kernel=server.get("KernelVersion"),
                )
            else:
                result["status"] = "engine_unreachable"
        except ValueError:
            result["status"] = "engine_unreachable"
        if not result["ready"]:
            text = version.get("stderr", "")
            result["reason"] = text or version.get("error") or "Docker 엔진 연결 실패"
            result["checks"] = [
                "Docker Desktop 실행 및 선택 context 확인",
                "WSL 2 및 BIOS 가상화 확인",
                "권한과 Linux/Windows 컨테이너 모드 확인",
            ]
        else:
            remaining = await self.leftovers()
            result["leftover_containers"] = remaining.get("containers", [])
            result["leftover_inventory_process"] = remaining.get("process")
        return result

    async def start_desktop(self, cancel=None, timeout=90):
        initial = await self.probe()
        if initial["ready"]:
            return initial
        if self.context and not initial.get("local_endpoint"):
            return {**initial, "reason": "원격 Docker 연결의 서비스는 자동 시작하지 않습니다."}
        exe = _desktop_path()
        if not exe:
            return initial
        subprocess.Popen(
            [exe],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return {**initial, "status": "cancelled"}
            checked = await self.probe()
            if checked["ready"]:
                return checked
            await asyncio.sleep(1)
        return {
            **initial,
            "status": "readiness_timeout",
            "reason": "Docker Desktop 준비 제한 시간 초과",
        }

    async def prepare_image(self, target_os, cancel=None, on_event=None):
        if target_os not in self.images:
            raise ValueError("Unsupported target OS")
        env = await self.probe()
        if (
            not env["ready"]
            or env.get("os") != target_os
            or env.get("arch") not in ("amd64", "x86_64")
        ):
            return {
                "status": "blocked",
                "environment": env,
                "error": "엔진 OS/아키텍처와 대상이 일치해야 합니다. 모드 전환은 사용자가 Docker Desktop에서 수행합니다.",
            }
        image = self.images[target_os]
        definition_dir = self.resources / "docker" / target_os
        definition_hash = self._definition_hash(target_os)
        inspected = await self.call("image", "inspect", image, timeout=15)
        build = None
        reusable = False
        if _ok(inspected):
            try:
                cached = json.loads(inspected["stdout"])[0]
                labels = (cached.get("Config") or {}).get("Labels") or {}
                reusable = labels.get("org.recheck.definition_sha256") == definition_hash
            except (ValueError, IndexError, TypeError, AttributeError):
                reusable = False
        if not reusable:
            if not self.auto_prepare:
                return dict(status='blocked', error_code='execution_image_unprepared',
                            error='실행 이미지를 명시적으로 준비한 뒤 다시 분석하세요. 자동 빌드는 시작하지 않았습니다.')
            _event(
                on_event,
                stage="image",
                status="running",
                message="분석 이미지 준비 (다운로드 네트워크 사용)",
            )
            build = await self.call(
                "build",
                "--label",
                "org.recheck.managed=true",
                "--label",
                "org.recheck.definition_sha256=" + definition_hash,
                "-t",
                image,
                str(definition_dir),
                timeout=900,
                cancel=cancel,
                output_limit=4 * 1024 * 1024,
            )
            if not _ok(build):
                return {
                    "status": "cancelled" if build["cancelled"] else "failed",
                    "error": "분석 이미지 빌드 실패",
                    "build": build,
                }
            inspected = await self.call("image", "inspect", image, timeout=15)
        if not _ok(inspected):
            return {"status": "failed", "error": "이미지 식별자 조회 실패", "process": inspected}
        try:
            details = json.loads(inspected["stdout"])[0]
        except (ValueError, IndexError):
            return {
                "status": "failed",
                "error": "이미지 메타데이터 형식 오류",
                "process": inspected,
            }
        if details.get("Os") != target_os or details.get("Architecture") not in ("amd64", "x86_64"):
            return {"status": "failed", "error": "이미지 OS/아키텍처 불일치", "details": details}
        return {
            "status": "success",
            "image": image,
            "image_id": details["Id"],
            "details": details,
            "build": build,
            "definition_sha256": definition_hash,
            "reused": reusable,
            "network_usage": "이미지 준비에만 네트워크 허용; 대상 실행 network=none",
        }

    async def execute(
        self,
        binary,
        test,
        run_id,
        attempt_id,
        workspace,
        limits=None,
        target_os="linux",
        cancel=None,
        image=None,
        on_event=None,
    ):
        # Validate direct adapter calls as well as GUI/CLI requests.
        env_values = TestCase.safe_env(test.get("env", {}))
        limits = limits or {}
        limits = {**limits, **{k: v for k, v in test.get("limits", {}).items() if v is not None}}
        memory = int(limits.get("memory_mb", 256))
        cpus = float(limits.get("cpus", limits.get("cpu", 1)))
        output_limit = int(limits.get("output_bytes", limits.get("output_limit", 1048576)))
        timeout = float(test.get("timeout_seconds", test.get("timeout", 10)))
        if (
            not 16 <= memory <= 32768
            or not 0.1 <= cpus <= 16
            or not 0.1 <= timeout <= 300
            or not 1 <= output_limit <= 64 * 1024 * 1024
        ):
            raise ValueError("테스트 자원 제한 범위 초과")
        args = test.get("args", [])
        if not isinstance(args, list) or not all(
            isinstance(a, str) and "\0" not in a for a in args
        ):
            raise ValueError("인자는 문자열 배열이어야 합니다.")
        env = await self.probe()
        if (
            not env.get("ready")
            or env.get("os") != target_os
            or env.get("arch") not in ("amd64", "x86_64")
        ):
            return {
                "status": "blocked",
                "infrastructure_ok": False,
                "error": "Docker 엔진 OS/아키텍처 불일치 또는 연결 실패",
                "environment": env,
            }
        if not env.get("local_endpoint"):
            return {
                "status": "blocked",
                "infrastructure_ok": False,
                "error": "로컬 입력 전달에는 로컬 Docker 연결이 필요합니다. 원격 bind 경로는 검증할 수 없습니다.",
            }
        if target_os not in self.images:
            raise ValueError("Unsupported OS")
        binary = plain_local_path(filesystem_path(binary).resolve(strict=True))
        work = plain_local_path(filesystem_path(workspace).resolve())
        filesystem_path(work).mkdir(parents=True, exist_ok=True)
        stage = work / ("input-" + uuid.uuid4().hex)
        filesystem_path(stage).mkdir()
        filename = "target.exe" if target_os == "windows" else "target"
        shutil.copy2(filesystem_path(binary), filesystem_path(stage / filename))
        try:
            filesystem_path(stage / filename).chmod(0o555)
        except OSError:
            pass
        aux_record = []
        for item in test.get("auxiliary_files", test.get("aux_files", [])):
            original_source = plain_local_path(item if isinstance(item, str) else item["source"])
            if filesystem_path(original_source).is_symlink():
                raise ValueError("보조 파일 심볼릭 링크는 지원하지 않습니다.")
            source = plain_local_path(filesystem_path(original_source).resolve(strict=True))
            name = source.name if isinstance(item, str) else item.get("name", source.name)
            if (
                filesystem_path(source).is_symlink()
                or not filesystem_path(source).is_file()
                or Path(name).name != name
                or name in {filename, ".", ".."}
                or any(c in name for c in '/\\:\0')
            ):
                raise ValueError("보조 파일은 충돌 없는 단일 파일명으로 등록해야 합니다.")
            if filesystem_path(stage / name).exists():
                raise ValueError("중복 보조 파일명")
            shutil.copy2(filesystem_path(source), filesystem_path(stage / name))
            aux_record.append(
                {
                    "name": name,
                    "sha256": hashlib.sha256(filesystem_path(stage / name).read_bytes()).hexdigest(),
                }
            )
        # Docker --mount uses CSV; quote source paths containing commas.
        # Preserve run-local artifacts; Docker Desktop may reject execution behind
        # a long Windows bind path even when Python can read the same file.
        short_stage = None
        if os.name == "nt" and len(str(stage)) >= 220:
            staging_root = Path(tempfile.gettempdir()) / "Recheck-staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            short_stage = Path(tempfile.mkdtemp(prefix="input-", dir=staging_root)).resolve()
            shutil.copytree(filesystem_path(stage), short_stage, dirs_exist_ok=True)
        mount_source = str(short_stage or stage)
        if any(c in mount_source for c in '\r\n"'):
            raise ValueError("Docker 입력 경로에 지원하지 않는 문자가 있습니다.")
        mount = (
            'type=bind,"src='
            + mount_source
            + '",dst='
            + ("C:\\input" if target_os == "windows" else "/input")
            + ",readonly"
        )
        name = "recheck-" + uuid.uuid4().hex
        run_label = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(run_id))[:100]
        image = image or self.images[target_os]
        create = [
            "create",
            "--name",
            name,
            "--label",
            "org.recheck.managed=true",
            "--label",
            "org.recheck.run=" + run_label,
            "--label",
            "org.recheck.attempt=" + str(attempt_id),
            "--network",
            "none",
            "--memory",
            str(memory) + "m",
            "--cpus",
            str(cpus),
            "--mount",
            mount,
            "--interactive",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
        ]
        if target_os == "linux":
            create += [
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--memory-swap",
                str(memory) + "m",
                "--user",
                "65534:65534",
                "--tmpfs",
                "/work:rw,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=32m",
                "--workdir",
                "/work",
                "--env",
                "LANG=C.UTF-8",
                "--env",
                "TZ=UTC",
                "--env",
                "LD_LIBRARY_PATH=/input",
                "--entrypoint",
                "/usr/local/bin/recheck-run-target",
            ]
        else:
            create += [
                "--isolation",
                "hyperv",
                "--user",
                "ContainerUser",
                "--workdir",
                "C:\\work",
                "--entrypoint",
                "C:\\input\\target.exe",
            ]
        for key, value in sorted(env_values.items()):
            create += ["--env", key + "=" + value]
        create += [image] + (["/input/target"] if target_os == "linux" else []) + args
        result = dict(
            status="failed",
            infrastructure_ok=False,
            container_name=name,
            test_id=test.get("id"),
            actual_input={
                "args": args,
                "stdin": test.get("stdin", ""),
                "auxiliary_files": aux_record,
                "env": env_values,
            },
            limits={
                "memory_mb": memory,
                "cpus": cpus,
                "timeout_seconds": timeout,
                "output_bytes": output_limit,
            },
            image=image,
            environment=env,
            cleanup_errors=[],
            target_exit_code=None,
            timed_out=False,
            cancelled=False,
            truncated=False,
        )
        result["binary_sha256"] = hashlib.sha256(filesystem_path(stage / filename).read_bytes()).hexdigest()
        result["input_staging"] = dict(
            artifact=str(stage), mount_source=mount_source, short_path_mirror=bool(short_stage)
        )
        result["protections"] = {
            "input_readonly": True,
            "network": "none",
            "root_readonly": target_os == "linux",
            "isolation": "hyperv" if target_os == "windows" else "linux-namespaces",
            "note": (
                "Windows는 Hyper-V + ContainerUser + 읽기 전용 입력, 컨테이너 루트는 쓰기 가능"
                if target_os == "windows"
                else "읽기 전용 루트 + cap-drop ALL + no-new-privileges"
            ),
        }
        try:
            created = await self.call(*create, timeout=45, cancel=cancel)
            result["create"] = created
            if not _ok(created):
                result.update(
                    error="컨테이너 생성 실패. 보안 제한/Windows Hyper-V/이미지 호환성을 확인하세요.",
                    status="cancelled" if created["cancelled"] else "failed",
                )
                # A lost CLI response may have created the uniquely named container.
                return result
            result["container_id"] = created["stdout"].strip()
            _event(
                on_event, stage="dynamic", status="running", container=name, test_id=test.get("id")
            )
            execution = await self.call(
                "start",
                "--attach",
                "--interactive",
                name,
                stdin=test.get("stdin", "").encode("utf-8"),
                timeout=timeout,
                cancel=cancel,
                output_limit=output_limit,
            )
            result["execution"] = execution
            for key in (
                "stdout",
                "stderr",
                "stdout_b64",
                "stderr_b64",
                "duration",
                "timed_out",
                "cancelled",
                "truncated",
            ):
                result[key] = execution[key]
            if any(execution[k] for k in ("timed_out", "cancelled", "truncated")) or execution.get(
                "error"
            ):
                result["stop"] = await self.call("kill", name, timeout=15)
            inspected = await self.call("inspect", name, timeout=15)
            result["inspect_process"] = inspected
            try:
                details = json.loads(inspected["stdout"])[0]
                state = details["State"]
                result.update(
                    container_state=state,
                    image_id=details.get("Image"),
                    target_exit_code=state.get("ExitCode"),
                    exit_code=state.get("ExitCode"),
                    oom_killed=state.get("OOMKilled", False),
                )
                result["infrastructure_ok"] = (
                    state.get("Status") == "exited"
                    and not state.get("Error")
                    and not any(
                        result[k] for k in ("timed_out", "cancelled", "truncated", "oom_killed")
                    )
                    and not execution.get("error")
                    and execution.get("returncode") in (0, state.get("ExitCode"))
                )
                result["status"] = "success" if result["infrastructure_ok"] else "failed"
                # Nonzero program exits remain comparable. Known launcher/loader
                # diagnostics indicate that the target could not start at all.
                stderr = result.get("stderr", "")
                launcher_failure = (
                    target_os == "linux"
                    and state.get("ExitCode") in (126, 127)
                    and (
                        bool(
                            re.search(
                                r"^/usr/local/bin/recheck-run-target: \d+: (?:exec: )?/input/target:",
                                stderr,
                            )
                        )
                        or stderr.startswith("/input/target: error while loading shared libraries:")
                    )
                )
                if launcher_failure:
                    result.update(
                        status="failed",
                        infrastructure_ok=False,
                        launch_status="failed",
                        error="대상 프로그램을 시작하지 못했습니다. Docker 입력 공유·실행 권한·로더·라이브러리를 확인하세요.",
                    )
            except (ValueError, IndexError, KeyError):
                result["error"] = "컨테이너 상태 확인 실패"
            # Capture both retained logs and inspection before any removal.
            logs = await self.call(
                "logs", "--timestamps", name, timeout=15, output_limit=output_limit
            )
            result["container_logs"] = logs
            result["status"] = "cancelled" if result["cancelled"] else result["status"]
            self._persist(work / (name + ".json"), result)
            return result
        except asyncio.CancelledError:
            result.update(status="cancelled", cancelled=True, infrastructure_ok=False)
            result["stop"] = await self.call("kill", name, timeout=15)
            raise
        except Exception as exc:
            result.update(
                status="failed", infrastructure_ok=False, error=f"{type(exc).__name__}: {exc}"
            )
            result["stop"] = await self.call("kill", name, timeout=15)
            return result
        finally:
            if "inspect_process" not in result:
                result["inspect_process"] = await self.call("inspect", name, timeout=15)
                result["container_logs"] = await self.call(
                    "logs", "--timestamps", name, timeout=15, output_limit=output_limit
                )
                self._persist(work / (name + ".json"), result)
            removed = await self.call("rm", "--force", name, timeout=20)
            if not _ok(removed) and "No such container" not in removed.get("stderr", ""):
                result["cleanup_errors"].append(removed)
            if short_stage:
                try:
                    short_stage.resolve().relative_to(staging_root.resolve())
                    if (
                        short_stage.parent != staging_root.resolve()
                        or not short_stage.name.startswith("input-")
                    ):
                        raise ValueError("임시 입력 정리 경로가 소유 범위를 벗어났습니다")
                    for child in short_stage.iterdir():
                        child.chmod(0o600)
                    shutil.rmtree(short_stage)
                    result["input_staging"]["mirror_removed"] = True
                except (ValueError, OSError) as exc:
                    result["cleanup_errors"].append(dict(component="input_staging", error=str(exc)))
            self._persist(work / (name + ".json"), result)

    @staticmethod
    def _persist(path, data):
        try:
            temporary = path.with_suffix(".json.tmp")
            with filesystem_path(temporary).open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filesystem_path(temporary), filesystem_path(path))
        except OSError as exc:
            # Cleanup must proceed even if disk/database storage is unavailable.
            data.setdefault("storage_errors", []).append(str(exc))
            data.update(status="failed", infrastructure_ok=False)

    async def leftovers(self):
        response = await self.call(
            "ps",
            "-a",
            "--filter",
            "label=org.recheck.managed=true",
            "--format",
            "{{json .}}",
            timeout=15,
        )
        try:
            containers = [
                json.loads(line) for line in response["stdout"].splitlines() if line.strip()
            ]
            containers = [
                row
                for row in containers
                if 'org.recheck.component=ollama' not in str(row.get('Labels', '')).split(',')
            ]
        except ValueError:
            containers = []
        return {"process": response, "containers": containers}


class GhidraAdapter:
    _cached_home = None

    def __init__(self, home=None, java_home=None, resources=None, cache_dir=None):
        self.home = self.discover(home)
        self.java_home = java_home
        self.resources = Path(resources) if resources else resource_root()
        self.cache_dir = Path(cache_dir) if cache_dir else None

    @classmethod
    def discover(cls, explicit=None):
        if explicit:
            exp_p = Path(explicit)
            if exp_p.is_dir():
                return exp_p
        if cls._cached_home is not None and cls._cached_home.is_dir():
            return cls._cached_home
        candidates = [explicit, os.environ.get("GHIDRA_HOME"), os.environ.get("GHIDRA_INSTALL_DIR")]
        exe = shutil.which("analyzeHeadless") or shutil.which("analyzeHeadless.bat")
        if exe:
            candidates.append(str(Path(exe).parent.parent))
        for folder in (
            Path.home() / "Downloads",
            Path.home() / "Desktop",
            Path("C:/Tools"),
            Path("C:/Program Files"),
            Path("D:/Tools"),
        ):
            if folder.is_dir():
                candidates.extend(
                    str(p) for p in sorted(folder.glob("*ghidra*"), reverse=True) if p.is_dir()
                )
        for candidate in candidates:
            if not candidate:
                continue
            root = Path(candidate)
            if root.name.lower().startswith("analyzeheadless"):
                root = root.parent.parent
            if any(
                (root / "support" / name).is_file()
                for name in ("analyzeHeadless.bat", "analyzeHeadless")
            ):
                cls._cached_home = root
                return root
        cls._cached_home = Path(explicit) if explicit else None
        return cls._cached_home

    def environment(self):
        env = os.environ.copy()
        if self.java_home:
            env["JAVA_HOME"] = str(self.java_home)
            env["PATH"] = str(Path(self.java_home) / "bin") + os.pathsep + env.get("PATH", "")
            env["JAVA_HOME_OVERRIDE"] = str(self.java_home)
        return env

    async def probe(self):
        from .prerequisites import _candidate_paths, _java_major

        props = self.home / "Ghidra/application.properties" if self.home else None
        values = {}
        if props and props.is_file():
            for line in props.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
        required = int(values.get("application.java.min", "21") or "21")
        maximum = values.get("application.java.max")
        candidates = ([str(Path(self.java_home) / ("bin/java.exe" if os.name == "nt" else "bin/java"))]
                      if self.java_home else _candidate_paths('java'))
        java_result = {"error": "Java 미설치", "stderr": "", "stdout": "", "returncode": None}
        major = None
        for java in candidates:
            path = Path(java)
            if not self.java_home and (not path.is_file() or not path.with_name(
                    'javac.exe' if os.name == 'nt' else 'javac').is_file()):
                continue
            java_result = await run_process([java, "-version"], timeout=10)
            major = _java_major(java_result.get("stderr", "") + java_result.get("stdout", ""))
            if (_ok(java_result) and major is not None and major >= required
                    and (not maximum or major <= int(maximum))):
                self.java_home = str(path.parent.parent)
                break
        result = {
            "home": str(self.home) if self.home else None,
            "ready": False,
            "java": java_result,
            "java_major": major,
            "install_url": "https://github.com/NationalSecurityAgency/ghidra/releases",
            "java_install_url": "https://adoptium.net/temurin/releases/",
            "status": "not_installed",
        }
        result["configured_environment_paths"] = {
            key: os.environ.get(key)
            for key in ("GHIDRA_HOME", "GHIDRA_INSTALL_DIR")
            if os.environ.get(key)
        }
        if not self.home:
            result["reason"] = (
                "유효한 Ghidra Headless 설치 경로를 찾지 못했습니다. 환경 변수 경로가 존재하는지 확인하세요."
            )
            return result
        result.update(version=values.get("application.version"), properties=values)
        result["java_required_min"] = required
        launcher = (
            self.home
            / "support"
            / ("analyzeHeadless.bat" if os.name == "nt" else "analyzeHeadless")
        )
        result["launcher"] = str(launcher)
        result["ready"] = (
            launcher.is_file()
            and _ok(java_result)
            and major is not None
            and major >= required
            and (not maximum or major <= int(maximum))
        )
        result["status"] = "ready" if result["ready"] else "java_or_path_mismatch"
        return result

    async def analyze(self, binary, workspace, *, timeout=300, cancel=None, on_event=None):
        env = await self.probe()
        if not env["ready"]:
            return {
                "schema_version": "1.0",
                "status": "blocked",
                "complete": False,
                "functions": [],
                "error": "Ghidra/Java 환경을 준비하세요.",
                "environment": env,
            }
        binary = Path(binary).resolve(strict=True)
        expected_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
        script = self.resources / "ghidra/RecheckExport.java"
        signature = dict(
            schema_version="1.0",
            binary_sha256=expected_hash,
            tool_home=env.get("home"),
            properties=env.get("properties"),
            java_version=env.get("java", {}).get("stderr"),
            timeout=timeout,
            script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        )
        cache_key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        cache_path = self.cache_dir / (cache_key + ".json") if self.cache_dir else None
        cache_error = None
        if cache_path and cache_path.is_file() and not (cancel and cancel.is_set()):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                artifact = validate_static(cached["artifact"])
                checksum = hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()
                if (
                    cached["signature"] != signature
                    or cached["artifact_sha256"] != checksum
                    or artifact.get("sha256") != expected_hash
                    or artifact.get("status") != "success"
                    or not artifact.get("complete")
                    or not _ok(artifact.get("process", {}))
                    or artifact.get("analysis_timed_out")
                    or artifact.get("functions_truncated")
                ):
                    raise ValueError("캐시 identity/완료 검증 실패")
                # The extraction timestamp and original process/logs stay explicit.
                artifact["cache"] = dict(
                    hit=True,
                    key=cache_key,
                    source=str(cache_path),
                    extracted_at=cached["created"],
                    verified_signature=signature,
                )
                _event(
                    on_event,
                    stage="static",
                    status="cached",
                    message="동일 해시·도구·추출 설정의 검증된 정적 결과 재사용",
                )
                return artifact
            except (ValueError, KeyError, TypeError, OSError) as exc:
                cache_error = str(exc)
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        project = workspace / ("project-" + uuid.uuid4().hex)
        project.mkdir()
        output = workspace / "static.json"
        marker = workspace / "static.complete"
        log = workspace / "ghidra.log"
        script_log = workspace / "ghidra-script.log"
        # Remove only completion artifacts from this dedicated invocation directory.
        output.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        command = [
            env["launcher"],
            str(project),
            "Recheck",
            "-import",
            str(binary),
            "-overwrite",
            "-analysisTimeoutPerFile",
            str(max(1, int(timeout))),
            "-max-cpu",
            "1",
            "-scriptPath",
            str(self.resources / "ghidra"),
            "-postScript",
            "RecheckExport.java",
            str(output),
            str(marker),
            "-log",
            str(log),
            "-scriptlog",
            str(script_log),
            "-deleteProject",
        ]
        _event(on_event, stage="static", status="running", message="Ghidra Headless 정적 분석")
        process = await run_process(
            command,
            timeout=timeout + 120,
            cancel=cancel,
            env=self.environment(),
            output_limit=4 * 1024 * 1024,
        )
        result = {
            "schema_version": "1.0",
            "status": "failed",
            "complete": False,
            "functions": [],
            "environment": env,
            "process": process,
            "logs": {"analysis": str(log), "script": str(script_log)},
            "sha256": expected_hash,
            "script_version": "1.0",
        }
        if output.is_file():
            try:
                data = validate_static(json.loads(output.read_text(encoding="utf-8")))
                result.update(data)
                if data.get("sha256") != expected_hash:
                    raise ValueError("Ghidra SHA-256 mismatch")
                complete = (
                    marker.is_file()
                    and marker.read_text(encoding="utf-8").strip() == "RECHECK_COMPLETE_1"
                )
                result["complete"] = complete
                result["status"] = (
                    "success"
                    if complete
                    and _ok(process)
                    and not data.get("analysis_timed_out")
                    and not data.get("functions_truncated")
                    else "partial"
                )
            except (ValueError, TypeError, KeyError) as exc:
                result.update(
                    status="failed", complete=False, error="정적 JSON 스키마 오류: " + str(exc)
                )
        else:
            result["error"] = "추출 산출물이 없습니다. Ghidra/Java/스크립트 로그를 확인하세요."
        if process["cancelled"]:
            result["status"] = "cancelled"
        if process["timed_out"]:
            result["external_timeout"] = True
        result["cache"] = dict(hit=False, key=cache_key, rejected=cache_error)
        if cache_path and result["status"] == "success":
            from .storage import now

            artifact = {k: v for k, v in result.items() if k != "cache"}
            try:
                atomic_json(
                    cache_path,
                    dict(
                        signature=signature,
                        artifact=artifact,
                        created=now(),
                        artifact_sha256=hashlib.sha256(
                            json.dumps(artifact, sort_keys=True).encode()
                        ).hexdigest(),
                    ),
                )
            except OSError as exc:
                result["cache"]["write_error"] = str(exc)
        return result


class CFGNode(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    id: str = Field(min_length=1)
    start: str
    end: str
    size: int = Field(ge=0)


class StaticFunction(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    id: str = Field(min_length=1)
    name: str
    address: str = Field(min_length=1)
    blocks: list[CFGNode] = Field(max_length=10000)
    edges: list[Annotated[list[str], Field(min_length=2, max_length=2)]] = Field(max_length=80000)
    calls: list[str]
    mnemonics: list[str] = Field(max_length=100000)
    decompile: str | None = Field(default=None, max_length=32768)

    @model_validator(mode="after")
    def references(self):
        ids = {node.id for node in self.blocks}
        if len(ids) != len(self.blocks):
            raise ValueError("Duplicate CFG node ID")
        if any(edge[0] not in ids for edge in self.edges):
            raise ValueError("CFG edge references unknown source node")
        # Destinations may leave the function (tail calls); retain their addresses.
        return self


class StaticArtifact(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    schema_version: str = Field(pattern=r"^1\.0$")
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    functions: list[StaticFunction] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique(self):
        if len({function.id for function in self.functions}) != len(self.functions):
            raise ValueError("Duplicate function ID")
        return self


def validate_static(data):
    StaticArtifact.model_validate(data)
    return data

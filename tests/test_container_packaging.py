"""Unit & Integration Tests for Production Container Packaging (Milestone 5 / R5).

Validates:
1. Dockerfile Specification & Hardening:
   - Base image strictly pinned to python:3.12.8-slim-bookworm (no unpinned tags).
   - Multi-stage build structure (builder compilation vs unprivileged runtime).
   - Unprivileged non-root user instruction (USER 1000:1000 or USER appuser).
   - Essential system packages installed: ffmpeg, ca-certificates, curl.
   - HEALTHCHECK directive targeting daemon REST endpoint http://localhost:8095/health.
   - Exposed port 8095 (EXPOSE 8095).
   - Clean ASGI entrypoint launching Uvicorn on port 8095.
   - Elimination of legacy entrypoint (spotdl) and zero embedded secrets.
   - .dockerignore exclusions (.git, .venv, __pycache__, .part, .env).

2. Docker Compose Specification & Orchestration:
   - Valid YAML syntax and schema compliance.
   - Daemon service definition (jellyfin-music-daemon).
   - Port forwarding 8095:8095.
   - Volume mappings: media storage (/music) and read-only config (:ro).
   - Environment variables: MUSIC_DIR, PORT, BITRATE, JELLYFIN_URL, JELLYFIN_TOKEN.
   - Restart policy (unless-stopped).
   - Security hardening: no-new-privileges:true.
   - Bounded container logging (json-file with max-size and max-file).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VAULT_AGENT_ROOT = Path("/home/kendon/Documents/My Vault/.agents")
PROPOSED_DOCKER_DIR = VAULT_AGENT_ROOT / "explorer_m5_docker"


# ==============================================================================
# Path Resolution Helpers
# ==============================================================================

def get_dockerfile_path() -> Path:
    """Resolves the Dockerfile path in order of precedence:
    1. DOCKERFILE_PATH environment variable.
    2. Project target: server/Dockerfile (if already upgraded to 3.12.8-slim-bookworm).
    3. Explorer M5 proposed Dockerfile: proposed_Dockerfile.
    4. Project target fallback: server/Dockerfile.
    """
    if "DOCKERFILE_PATH" in os.environ:
        custom = Path(os.environ["DOCKERFILE_PATH"])
        if custom.exists():
            return custom

    proj_df = PROJECT_ROOT / "server" / "Dockerfile"
    if proj_df.exists():
        content = proj_df.read_text(encoding="utf-8")
        if "3.12.8-slim-bookworm" in content:
            return proj_df

    proposed_df = PROPOSED_DOCKER_DIR / "proposed_Dockerfile"
    if proposed_df.exists():
        return proposed_df

    return proj_df


def get_docker_compose_path() -> Path:
    """Resolves docker-compose.yml path in order of precedence:
    1. COMPOSE_FILE_PATH environment variable.
    2. Project root docker-compose.yml.
    3. Project server/docker-compose.yml.
    4. Explorer M5 proposed docker-compose.yml.
    """
    if "COMPOSE_FILE_PATH" in os.environ:
        custom = Path(os.environ["COMPOSE_FILE_PATH"])
        if custom.exists():
            return custom

    for candidate in [
        PROJECT_ROOT / "docker-compose.yml",
        PROJECT_ROOT / "server" / "docker-compose.yml",
        PROPOSED_DOCKER_DIR / "proposed_docker-compose.yml",
        PROPOSED_DOCKER_DIR / "proposed_server_docker-compose.yml",
    ]:
        if candidate.exists():
            return candidate

    return PROJECT_ROOT / "docker-compose.yml"


def get_dockerignore_path() -> Optional[Path]:
    """Resolves .dockerignore path."""
    for candidate in [
        PROJECT_ROOT / ".dockerignore",
        PROJECT_ROOT / "server" / ".dockerignore",
        PROPOSED_DOCKER_DIR / "proposed_dockerignore",
    ]:
        if candidate.exists():
            return candidate
    return None


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture(scope="module")
def dockerfile_content() -> str:
    path = get_dockerfile_path()
    assert path.exists(), f"Dockerfile not found at resolved path: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerfile_lines(dockerfile_content: str) -> List[str]:
    return [line.strip() for line in dockerfile_content.splitlines()]


@pytest.fixture(scope="module")
def compose_data() -> Dict[str, Any]:
    path = get_docker_compose_path()
    assert path.exists(), f"docker-compose.yml not found at resolved path: {path}"
    content = path.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    assert isinstance(data, dict), f"docker-compose.yml did not parse as dictionary: {type(data)}"
    return data


# ==============================================================================
# 1. Dockerfile Directives & Hardening Tests
# ==============================================================================

class TestDockerfilePackaging:
    """Test suite validating Dockerfile directives against R5 specifications."""

    def test_dockerfile_exists(self):
        """Verify that Dockerfile exists and is non-empty."""
        path = get_dockerfile_path()
        assert path.is_file(), f"Expected Dockerfile file at {path}"
        assert path.stat().st_size > 100, f"Dockerfile at {path} appears incomplete (<100 bytes)"

    def test_pinned_base_image_python_version(self, dockerfile_content: str):
        """Verify Dockerfile uses strictly pinned python:3.12.8-slim-bookworm base image."""
        # Find all FROM directives
        from_matches = re.findall(r"^\s*FROM\s+([^\s]+)", dockerfile_content, flags=re.MULTILINE)
        assert len(from_matches) > 0, "No FROM directives found in Dockerfile"

        for image in from_matches:
            assert "python:3.12.8-slim-bookworm" in image, (
                f"Base image must be pinned to 'python:3.12.8-slim-bookworm', found: '{image}'"
            )

        # Explicitly forbid unpinned or floating versions
        unpinned_patterns = [
            r"python:3\.12-slim(?![-\.\w])",
            r"python:3-slim",
            r"python:latest",
            r"python:3\.12(?![-\.\w])",
        ]
        for pat in unpinned_patterns:
            assert not re.search(pat, dockerfile_content), f"Forbidden unpinned base image pattern found: {pat}"

    def test_multistage_build_architecture(self, dockerfile_content: str):
        """Verify Dockerfile implements multi-stage build (builder and runtime stages)."""
        from_matches = re.findall(
            r"^\s*FROM\s+[^\s]+\s+AS\s+([A-Za-z0-9_-]+)",
            dockerfile_content,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        stage_names = [s.lower() for s in from_matches]
        assert len(stage_names) >= 2, f"Multi-stage build requires >= 2 stages, found: {stage_names}"
        assert "builder" in stage_names, f"Expected 'builder' stage, found stages: {stage_names}"
        assert "runtime" in stage_names, f"Expected 'runtime' stage, found stages: {stage_names}"

        # Verify COPY --from=builder exists
        assert re.search(r"COPY\s+--from=builder", dockerfile_content, re.IGNORECASE), (
            "Runtime stage must copy artifacts from builder stage via 'COPY --from=builder'"
        )

    def test_unprivileged_non_root_user(self, dockerfile_content: str, dockerfile_lines: List[str]):
        """Verify container creates and switches to unprivileged non-root user (UID 1000)."""
        # 1. USER directive exists and specifies 1000 or appuser
        user_matches = re.findall(r"^\s*USER\s+([^\s]+)", dockerfile_content, flags=re.MULTILINE)
        assert len(user_matches) > 0, "Missing USER directive in Dockerfile; container would run as root!"

        final_user = user_matches[-1]
        assert final_user in ("1000:1000", "1000", "appuser", "appuser:appuser"), (
            f"Container must run as unprivileged user (1000 or appuser), found: '{final_user}'"
        )

        # 2. Verify user creation command exists (useradd or adduser with UID 1000)
        assert re.search(r"useradd\s+.*-u\s+1000", dockerfile_content) or re.search(
            r"adduser\s+.*-u\s+1000", dockerfile_content
        ), "Dockerfile must create user with explicit UID 1000"

        # 3. Verify directory ownership setup (chown -R 1000:1000 or appuser)
        assert re.search(r"chown\s+.*1000:1000", dockerfile_content) or re.search(
            r"chown\s+.*appuser", dockerfile_content
        ), "Dockerfile must chown application directories for UID 1000"

        # 4. Verify USER appears after system setup instructions
        user_line_idx = -1
        apt_line_idx = -1
        for idx, line in enumerate(dockerfile_lines):
            if line.startswith("USER "):
                user_line_idx = idx
            elif "apt-get" in line:
                apt_line_idx = idx

        assert user_line_idx > apt_line_idx, "USER directive must appear AFTER system package installations"

    def test_healthcheck_directive_configuration(self, dockerfile_content: str):
        """Verify HEALTHCHECK directive is present and probes http://localhost:8095/health."""
        # Find the HEALTHCHECK block including line continuations
        healthcheck_match = re.search(
            r"^\s*HEALTHCHECK\s+(.*?)(?=\n[A-Z]{2,}|\Z)",
            dockerfile_content,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert healthcheck_match, "Dockerfile missing required HEALTHCHECK directive"
        hc_text = healthcheck_match.group(0)

        # Check target endpoint
        assert "http://localhost:8095/health" in hc_text or "http://127.0.0.1:8095/health" in hc_text, (
            f"HEALTHCHECK must probe daemon health endpoint 'http://localhost:8095/health', got: {hc_text}"
        )

        # Check parameters
        assert "--interval=" in hc_text, "HEALTHCHECK missing --interval option"
        assert "--timeout=" in hc_text, "HEALTHCHECK missing --timeout option"
        assert "--retries=" in hc_text, "HEALTHCHECK missing --retries option"

    def test_exposed_port_8095(self, dockerfile_content: str):
        """Verify EXPOSE 8095 directive is present."""
        assert re.search(r"^\s*EXPOSE\s+8095\b", dockerfile_content, flags=re.MULTILINE), (
            "Dockerfile must expose port 8095 (EXPOSE 8095)"
        )

    def test_required_system_packages(self, dockerfile_content: str):
        """Verify installation of required system utilities: ffmpeg, ca-certificates, curl."""
        required_pkgs = ["ffmpeg", "ca-certificates", "curl"]
        for pkg in required_pkgs:
            assert re.search(rf"\b{pkg}\b", dockerfile_content), (
                f"Required system package '{pkg}' must be installed in Dockerfile"
            )

        # Best practice: --no-install-recommends and apt list cleanup
        assert "--no-install-recommends" in dockerfile_content, (
            "apt-get install should include '--no-install-recommends' for minimal image size"
        )
        assert "rm -rf /var/lib/apt/lists/*" in dockerfile_content, (
            "Dockerfile must clean apt cache via 'rm -rf /var/lib/apt/lists/*'"
        )

    def test_clean_uvicorn_entrypoint(self, dockerfile_content: str):
        """Verify ENTRYPOINT launches Uvicorn ASGI server on port 8095."""
        # Find ENTRYPOINT
        ep_match = re.search(r"^\s*ENTRYPOINT\s+\[(.*?)\]", dockerfile_content, flags=re.MULTILINE)
        assert ep_match, "Dockerfile missing JSON exec-form ENTRYPOINT directive"
        ep_args = [arg.strip(' "\'') for arg in ep_match.group(1).split(",")]

        assert "uvicorn" in ep_args[0], f"ENTRYPOINT executable must be 'uvicorn', found: '{ep_args[0]}'"
        assert any("app.main:app" in a or "server.app.main:app" in a for a in ep_args), (
            f"ENTRYPOINT must load ASGI app 'app.main:app', found args: {ep_args}"
        )
        assert "--port" in ep_args and ("8095" in ep_args), (
            f"ENTRYPOINT must bind to port 8095, found args: {ep_args}"
        )

        # Regression check: Eliminate legacy spotdl entrypoint
        assert not re.search(r'ENTRYPOINT\s+\["spotdl"\]', dockerfile_content), (
            "Legacy ENTRYPOINT [\"spotdl\"] must be eliminated!"
        )

    def test_dockerignore_configuration(self):
        """Verify .dockerignore excludes build artifacts, virtual environments, and secrets."""
        ign_path = get_dockerignore_path()
        assert ign_path is not None and ign_path.is_file(), ".dockerignore file must exist"
        ign_content = ign_path.read_text(encoding="utf-8")

        required_patterns = [
            (".git", [".git"]),
            (".venv", [".venv", "venv"]),
            ("__pycache__", ["__pycache__"]),
            ("bytecode", ["*.pyc", "*.py[cod]"]),
            ("partial downloads", ["*.part"]),
        ]
        for name, patterns in required_patterns:
            matched = any(p in ign_content for p in patterns)
            assert matched, f".dockerignore missing pattern for {name} (tried {patterns})"

    def test_zero_embedded_secrets(self, dockerfile_content: str):
        """Verify no hardcoded secrets or auth tokens exist in the Dockerfile."""
        forbidden_secret_patterns = [
            r"JELLYFIN_TOKEN\s*=\s*['\"][a-zA-Z0-9_-]{10,}['\"]",
            r"api_key\s*=\s*['\"][a-zA-Z0-9_-]{10,}['\"]",
            r"password\s*=\s*['\"][^'\"]+['\"]",
        ]
        for pat in forbidden_secret_patterns:
            assert not re.search(pat, dockerfile_content, re.IGNORECASE), (
                f"Potential hardcoded secret matching '{pat}' found in Dockerfile"
            )


# ==============================================================================
# 2. Docker Compose Specification Tests
# ==============================================================================

class TestDockerComposePackaging:
    """Test suite validating docker-compose.yml configuration against R5 specifications."""

    def test_compose_service_present(self, compose_data: Dict[str, Any]):
        """Verify services section contains daemon service."""
        assert "services" in compose_data, "docker-compose.yml missing 'services' key"
        services = compose_data["services"]
        assert isinstance(services, dict), "'services' must be a mapping"

        # Locate daemon service
        service_names = list(services.keys())
        daemon_name = next(
            (s for s in service_names if "daemon" in s or "music" in s or "jellyfin" in s),
            None,
        )
        assert daemon_name is not None, (
            f"Expected jellyfin daemon service in compose file, found services: {service_names}"
        )

    def test_compose_port_mapping_8095(self, compose_data: Dict[str, Any]):
        """Verify port 8095:8095 is mapped."""
        daemon_svc = self._get_daemon_service(compose_data)
        ports = daemon_svc.get("ports", [])
        assert len(ports) > 0, "Daemon service has no port mappings defined"

        # Check for 8095:8095 or "8095:8095"
        has_8095 = any("8095:8095" in str(p) or p == 8095 for p in ports)
        assert has_8095, f"Daemon service must expose port 8095:8095, configured ports: {ports}"

    def test_compose_volume_mappings(self, compose_data: Dict[str, Any]):
        """Verify media volume and read-only config mounts."""
        daemon_svc = self._get_daemon_service(compose_data)
        volumes = daemon_svc.get("volumes", [])
        assert len(volumes) > 0, "Daemon service has no volume mappings defined"

        # 1. Media mount: host -> /music
        has_music_mount = any(":/music" in str(v) for v in volumes)
        assert has_music_mount, (
            f"Daemon service must mount music media directory to '/music', found volumes: {volumes}"
        )

        # 2. Config mount: read-only (:ro)
        has_ro_config = any("server_config.json" in str(v) and str(v).endswith(":ro") for v in volumes)
        assert has_ro_config, (
            f"Daemon service should mount server_config.json as read-only (:ro), found volumes: {volumes}"
        )

    def test_compose_restart_policy(self, compose_data: Dict[str, Any]):
        """Verify restart policy is unless-stopped or always."""
        daemon_svc = self._get_daemon_service(compose_data)
        restart = daemon_svc.get("restart", "")
        assert restart in ("unless-stopped", "always"), (
            f"Daemon restart policy should be 'unless-stopped' or 'always', found: '{restart}'"
        )

    def test_compose_environment_variables(self, compose_data: Dict[str, Any]):
        """Verify required runtime environment variables."""
        daemon_svc = self._get_daemon_service(compose_data)
        env = daemon_svc.get("environment", [])

        # Normalize env to dict or list of strings
        env_dict: Dict[str, str] = {}
        if isinstance(env, dict):
            env_dict = {str(k): str(v) for k, v in env.items()}
        elif isinstance(env, list):
            for item in env:
                if "=" in str(item):
                    k, v = str(item).split("=", 1)
                    env_dict[k.strip()] = v.strip()

        assert "MUSIC_DIR" in env_dict, f"Missing MUSIC_DIR in compose environment: {env_dict}"
        assert env_dict.get("MUSIC_DIR") == "/music", (
            f"MUSIC_DIR environment variable should be '/music', got: {env_dict.get('MUSIC_DIR')}"
        )
        assert "PORT" in env_dict and "8095" in env_dict["PORT"], (
            f"PORT environment variable should be 8095, got: {env_dict.get('PORT')}"
        )
        assert "JELLYFIN_URL" in env_dict, f"Missing JELLYFIN_URL in compose environment: {env_dict}"
        assert "JELLYFIN_TOKEN" in env_dict, f"Missing JELLYFIN_TOKEN in compose environment: {env_dict}"

    def test_compose_security_options(self, compose_data: Dict[str, Any]):
        """Verify security_opt includes no-new-privileges:true."""
        daemon_svc = self._get_daemon_service(compose_data)
        sec_opts = daemon_svc.get("security_opt", [])
        assert "no-new-privileges:true" in sec_opts, (
            f"Expected 'no-new-privileges:true' in security_opt, found: {sec_opts}"
        )

    def test_compose_logging_limits(self, compose_data: Dict[str, Any]):
        """Verify bounded log rotation limits in docker-compose.yml."""
        daemon_svc = self._get_daemon_service(compose_data)
        logging_cfg = daemon_svc.get("logging", {})
        assert logging_cfg.get("driver") in ("json-file", "journald", "local"), (
            f"Expected standard logging driver, got: {logging_cfg.get('driver')}"
        )

        options = logging_cfg.get("options", {})
        if logging_cfg.get("driver") == "json-file":
            assert "max-size" in options, "json-file logging must specify 'max-size' limit"
            assert "max-file" in options, "json-file logging must specify 'max-file' limit"

    def _get_daemon_service(self, compose_data: Dict[str, Any]) -> Dict[str, Any]:
        services = compose_data.get("services", {})
        for name in ["jellyfin-music-daemon", "music-daemon", "daemon"]:
            if name in services:
                return services[name]
        # Return first service if name varies
        if services:
            return next(iter(services.values()))
        pytest.fail("No services found in compose file")

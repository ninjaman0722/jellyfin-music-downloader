"""Empirical Verification Tests for Safe Subprocess Execution & Targeted Process Cancellation.

Validates:
- Safe subprocess invocation with argv arrays (shell=False), preventing command injection.
- POSIX process group isolation (preexec_fn=os.setsid, pgid == pid).
- Sibling job isolation during targeted cancellation (Job A kill does not touch Job B).
- Non-blocking stream draining preventing 64KB kernel pipe buffer deadlocks.
- Carriage return (\\r) and newline (\\n) delimiter parsing without buffer overflow.
- Scoped partial file cleanup unlinking only matching in-flight .part/.tmp files while preserving completed tracks.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from server.app.process import ProcessManager, compute_valid_temp_names, stream_process_lines


def test_compute_valid_temp_names_includes_dotfile_prefixes():
    """Verify compute_valid_temp_names matches both .part suffix and .part_ prefix."""
    target = Path("/music/Artist/Album/01-01 - Title.mp3")
    names = compute_valid_temp_names(target)
    assert ".part_01-01 - Title.mp3" in names
    assert "01-01 - Title.mp3.part" in names
    assert "01-01 - Title.part.mp3" in names
    assert "01-01 - Title.mp3" not in names


@pytest.mark.asyncio
async def test_scoped_partial_file_cleanup_dotfile_prefix():
    """Verify cleanup_job_temp_files cleans .part_{stem}.mp3 while protecting completed and sibling files."""
    with tempfile.TemporaryDirectory() as td:
        dir_p = Path(td)
        completed = dir_p / "01 - Complete.mp3"
        completed.write_text("valid audio")

        partial = dir_p / ".part_02 - InFlight.mp3"
        partial.write_text("in-flight data")

        sibling_partial = dir_p / ".part_03 - Sibling.mp3"
        sibling_partial.write_text("sibling data")

        pm = ProcessManager()
        job = await pm.register_job("job_dotfile", "user1", "Album")
        job.completed_files.add(completed)
        job.in_flight_targets.add(dir_p / "02 - InFlight.mp3")

        cleaned = pm.cleanup_job_temp_files(job)
        assert cleaned == 1
        assert completed.exists()
        assert not partial.exists()
        assert sibling_partial.exists()



@pytest.mark.asyncio
async def test_safe_subprocess_argument_escaping():
    """Verify that arguments with spaces, quotes, and shell metacharacters are NOT evaluated by a shell."""
    manager = ProcessManager()
    await manager.register_job("job_safe_args", "user1", "Test Playlist")

    special_arg = "Rock & Roll 90's; rm -rf /tmp/fake; echo $HOME"
    cmd = [sys.executable, "-c", "import sys; print(sys.argv[1])", special_arg]

    captured = []

    async def capture(line: str):
        captured.append(line)

    handle = await manager.spawn_process("job_safe_args", cmd, on_stdout=capture)
    rc = await manager.wait_process(handle)

    assert rc == 0
    assert len(captured) == 1
    assert captured[0] == special_arg, "Argument must be passed verbatim without shell evaluation"


@pytest.mark.asyncio
async def test_process_group_creation():
    """Verify that spawned subprocess receives its own process group ID equal to its PID."""
    manager = ProcessManager()
    await manager.register_job("job_pgid", "user1", "Test")

    cmd = [sys.executable, "-c", "import time; time.sleep(1)"]
    handle = await manager.spawn_process("job_pgid", cmd)

    try:
        assert handle.pgid == handle.pid, f"PGID {handle.pgid} must match PID {handle.pid}"
        assert os.getpgid(handle.pid) == handle.pid
    finally:
        await manager.cancel_job("job_pgid")


@pytest.mark.asyncio
async def test_sibling_isolation_during_cancellation():
    """Verify that cancelling Job A terminates Job A but leaves concurrent Job B running undisturbed."""
    manager = ProcessManager()
    await manager.register_job("job_a", "user_a", "Playlist A")
    await manager.register_job("job_b", "user_b", "Playlist B")

    # Job A runs for 10 seconds (will be cancelled)
    cmd_a = [sys.executable, "-c", "import time; time.sleep(10)"]
    handle_a = await manager.spawn_process("job_a", cmd_a)

    # Job B runs for 0.4 seconds (should complete naturally)
    cmd_b = [sys.executable, "-c", "import time; time.sleep(0.4)"]
    handle_b = await manager.spawn_process("job_b", cmd_b)

    # Cancel Job A
    cancel_res = await manager.cancel_job("job_a")
    assert cancel_res.status == "cancelled"
    assert handle_a.pid in cancel_res.terminated_pids

    # Verify Job A terminated via signal (SIGTERM or SIGKILL)
    rc_a = await handle_a.process.wait()
    assert rc_a in (-signal.SIGTERM, -signal.SIGKILL)

    # Verify Job B completes naturally with exit code 0
    rc_b = await manager.wait_process(handle_b)
    assert rc_b == 0, "Sibling Job B must complete naturally with exit code 0"


@pytest.mark.asyncio
async def test_pipe_deadlock_prevention():
    """Verify that a process outputting >200KB of stdout/stderr is drained without pipe deadlock."""
    manager = ProcessManager()
    await manager.register_job("job_pipe", "user1", "Pipe Test")

    total_bytes = 300_000
    cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.write('x' * {total_bytes} + '\\n'); sys.stdout.flush()",
    ]

    captured = []

    async def capture(line: str):
        captured.append(line)

    handle = await manager.spawn_process("job_pipe", cmd, on_stdout=capture)

    # Must complete within 3 seconds without kernel buffer blocking
    rc = await asyncio.wait_for(manager.wait_process(handle), timeout=3.0)
    assert rc == 0
    assert sum(len(x) for x in captured) >= total_bytes


@pytest.mark.asyncio
async def test_carriage_return_streaming():
    """Verify that \\r progress updates are delivered in real-time and do not cause StreamReader errors."""
    manager = ProcessManager()
    await manager.register_job("job_cr", "user1", "CR Test")

    cmd = [
        sys.executable,
        "-c",
        "import sys, time; "
        "sys.stdout.write('PROGRESS: 25%\\r'); sys.stdout.flush(); "
        "time.sleep(0.05); "
        "sys.stdout.write('PROGRESS: 50%\\r'); sys.stdout.flush(); "
        "time.sleep(0.05); "
        "sys.stdout.write('PROGRESS: 100%\\n'); sys.stdout.flush()",
    ]

    received = []

    async def capture(line: str):
        received.append(line)

    handle = await manager.spawn_process("job_cr", cmd, on_stdout=capture)
    rc = await manager.wait_process(handle)

    assert rc == 0
    assert "PROGRESS: 25%" in received
    assert "PROGRESS: 50%" in received
    assert "PROGRESS: 100%" in received


@pytest.mark.asyncio
async def test_scoped_partial_file_cleanup():
    """Verify that cancellation unlinks matching partial files but never touches completed files or siblings."""
    with tempfile.TemporaryDirectory() as td:
        dir_p = Path(td)

        # Files on disk
        completed_song = dir_p / "01 - Complete.mp3"
        completed_song.write_text("audio data")

        partial_song = dir_p / "02 - InFlight.mp3.part"
        partial_song.write_text("partial data")

        sibling_partial = dir_p / "03 - Sibling.mp3.part"
        sibling_partial.write_text("sibling partial")

        manager = ProcessManager()
        job = await manager.register_job("job_clean", "user1", "Clean Test")
        job.completed_files.add(completed_song)
        job.in_flight_targets.add(dir_p / "02 - InFlight.mp3")

        cleaned = manager.cleanup_job_temp_files(job)

        assert cleaned == 1
        assert completed_song.exists(), "Completed songs must NEVER be unlinked"
        assert not partial_song.exists(), "Matching partial file must be unlinked"
        assert sibling_partial.exists(), "Sibling partial files must NEVER be unlinked"


@pytest.mark.asyncio
async def test_cancel_nonexistent_and_duplicate():
    """Verify error handling on canceling invalid or already cancelled jobs."""
    manager = ProcessManager()
    with pytest.raises(KeyError):
        await manager.cancel_job("nonexistent-job")

    await manager.register_job("job_dup", "user1", "Dup Test")
    res1 = await manager.cancel_job("job_dup")
    assert res1.status == "cancelled"

    # Canceling again returns status "cancelled" with 0 files cleaned
    res2 = await manager.cancel_job("job_dup")
    assert res2.status == "cancelled"
    assert res2.cleaned_files == 0


def test_config_permissions_enforcement():
    """Verify enforce_config_permissions tightens permissive modes to 0600."""
    import stat
    from server.app.config import enforce_config_permissions, ServerConfig

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tf.write(b'{"jellyfin_token": "secret123"}')
        tf_path = Path(tf.name)

    try:
        tf_path.chmod(0o666)
        enforce_config_permissions(tf_path)
        current_mode = stat.S_IMODE(tf_path.stat().st_mode)
        assert current_mode == 0o600, f"Expected 0o600 permissions, got {oct(current_mode)}"

        # Verify ServerConfig loads token but to_public() strips it
        cfg = ServerConfig(jellyfin_token="super-secret-token")
        pub = cfg.to_public()
        pub_dict = pub.model_dump()
        assert "jellyfin_token" not in pub_dict, "jellyfin_token must never be exposed in public config"
    finally:
        if tf_path.exists():
            tf_path.unlink()


@pytest.mark.asyncio
async def test_spawn_during_cancellation_race_condition(tmp_path):
    """Regression Test: Verify that cancelling a job while spawn_process is in flight
    immediately terminates the newly spawned process group with SIGKILL, cleans in-flight
    partial files, raises asyncio.CancelledError, and leaves zero orphan processes running in the OS.
    """
    manager = ProcessManager()
    job_id = "job_race_cancel"
    await manager.register_job(job_id, "user1", "Race Test")

    # In-flight partial file that should be cleaned
    target_file = tmp_path / "song.mp3"
    partial_file = tmp_path / "song.mp3.part"
    partial_file.write_text("in-flight partial audio data")

    # Subprocess command that would run for 30 seconds if orphaned
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

    spawned_pid = None
    spawned_pgid = None

    orig_create = asyncio.create_subprocess_exec

    async def intercepted_create(*args, **kwargs):
        nonlocal spawned_pid, spawned_pgid
        proc = await orig_create(*args, **kwargs)
        spawned_pid = proc.pid
        spawned_pgid = os.getpgid(proc.pid)
        # Interleave cancel_job precisely while create_subprocess_exec has completed
        # in the kernel, but before spawn_process() acquires job.lock
        await manager.cancel_job(job_id)
        return proc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", intercepted_create)

        with pytest.raises(asyncio.CancelledError):
            await manager.spawn_process(
                job_id=job_id,
                argv=cmd,
                in_flight_target=target_file,
            )

    # 1. Verify that a real OS process was created
    assert spawned_pid is not None, "create_subprocess_exec must have returned a PID"
    assert spawned_pgid is not None

    # 2. Verify process was reaped and is NO LONGER running in the OS
    await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pid, 0)

    # 3. Verify entire process group was terminated
    with pytest.raises(ProcessLookupError):
        os.killpg(spawned_pgid, 0)

    # 4. Verify in-flight partial file was unlinked
    assert not partial_file.exists(), "Matching partial file (.part) must be unlinked"

    # 5. Verify job state integrity
    job = manager.get_job(job_id)
    assert job is not None
    assert job.status == "cancelled"
    assert spawned_pid not in job.active_processes
    assert len(job.active_processes) == 0


@pytest.mark.asyncio
async def test_run_command_execution():
    """Verify that ProcessManager.run_command executes commands and returns exit codes."""
    manager = ProcessManager()
    await manager.register_job("job_run_cmd", "user1", "RunCmd Test")

    # Test exit code 0
    cmd_success = [sys.executable, "-c", "import sys; print('run_cmd_ok'); sys.exit(0)"]
    rc, stdout, stderr = await manager.run_command(cmd_success, job_id="job_run_cmd")
    assert rc == 0
    assert "run_cmd_ok" in stdout

    # Test non-zero exit code
    cmd_fail = [sys.executable, "-c", "import sys; sys.stderr.write('run_cmd_err\\n'); sys.exit(42)"]
    rc_fail, stdout_fail, stderr_fail = await manager.run_command(cmd_fail, job_id="job_run_cmd")
    assert rc_fail == 42
    assert "run_cmd_err" in stderr_fail


@pytest.mark.asyncio
async def test_run_command_basic_success():
    """Verify run_command executes basic commands and captures stdout/stderr with returncode 0."""
    manager = ProcessManager()
    cmd = [sys.executable, "-c", "print('hello from run_command')"]

    rc, stdout, stderr = await manager.run_command(cmd)

    assert rc == 0
    assert stdout == "hello from run_command"
    assert stderr == ""
    # Ephemeral job must be unregistered
    assert len(manager.get_active_jobs()) == 0


@pytest.mark.asyncio
async def test_run_command_nonzero_exit():
    """Verify run_command returns non-zero returncodes without raising CalledProcessError."""
    manager = ProcessManager()
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('out text\\n'); sys.stderr.write('err text\\n'); sys.exit(42)",
    ]

    rc, stdout, stderr = await manager.run_command(cmd)

    assert rc == 42
    assert stdout == "out text"
    assert stderr == "err text"
    assert len(manager.get_active_jobs()) == 0


@pytest.mark.asyncio
async def test_run_command_with_explicit_job_id():
    """Verify run_command operates within a pre-registered job without unregistering it on completion."""
    manager = ProcessManager()
    job = await manager.register_job("explicit_job", "user1", "Playlist 1")

    cmd = [sys.executable, "-c", "print('explicit run')"]
    rc, stdout, stderr = await manager.run_command(cmd, job_id="explicit_job")

    assert rc == 0
    assert stdout == "explicit run"
    # Explicit job remains registered
    assert manager.get_job("explicit_job") is not None
    # Process handle was popped from active_processes in finally block
    assert len(job.active_processes) == 0


@pytest.mark.asyncio
async def test_run_command_timeout_and_cleanup():
    """Verify run_command enforces timeout, terminates child process group, and unlinks partial files."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        part_file = td_path / "track_01.mp3.part"
        part_file.write_text("in-flight download data")

        manager = ProcessManager()
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]

        captured_pid = None
        orig_spawn = manager.spawn_process

        async def _spy_spawn(*args, **kwargs):
            handle = await orig_spawn(*args, **kwargs)
            nonlocal captured_pid
            captured_pid = handle.pid
            return handle

        manager.spawn_process = _spy_spawn

        t0 = time.time()
        with pytest.raises(TimeoutError):
            await manager.run_command(cmd, temp_dir=td_path, timeout=0.3)
        elapsed = time.time() - t0

        assert elapsed < 2.0, f"Command took too long to time out: {elapsed:.2f}s"
        assert not part_file.exists(), "Partial file must be unlinked upon timeout"
        assert len(manager.get_active_jobs()) == 0, "Ephemeral job must be unregistered"

        # Verify child process was killed and no orphan remains
        assert captured_pid is not None
        try:
            os.kill(captured_pid, 0)
            pytest.fail(f"Subprocess PID {captured_pid} was leaked as an orphan!")
        except ProcessLookupError:
            pass  # Successfully terminated


@pytest.mark.asyncio
async def test_run_command_cwd_and_env():
    """Verify run_command respects custom working directory and environment variables."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        manager = ProcessManager()
        cmd = [
            sys.executable,
            "-c",
            "import os; print(os.getcwd()); print(os.environ.get('CUSTOM_ENV_VAR'))",
        ]
        custom_env = {**os.environ, "CUSTOM_ENV_VAR": "omarchy_special_value"}

        rc, stdout, _ = await manager.run_command(cmd, cwd=td_path, env=custom_env)

        assert rc == 0
        lines = stdout.split("\n")
        assert lines[0] == str(td_path.resolve())
        assert lines[1] == "omarchy_special_value"


@pytest.mark.asyncio
async def test_run_command_empty_argv_validation():
    """Verify run_command raises ValueError when passed an empty argument list."""
    manager = ProcessManager()
    with pytest.raises(ValueError, match="argv must be a non-empty list"):
        await manager.run_command([])


@pytest.mark.asyncio
async def test_cleanup_stem_prefix_isolation_overlapping_stems():
    """Verify cleanup_job_temp_files uses stem-prefix matching and isolates
    overlapping stem names (e.g. track_1 vs track_10, Love vs 01 - I Love Rock and Roll).
    """
    with tempfile.TemporaryDirectory() as td:
        dir_p = Path(td)

        # 1. Target files for Job 1: track_1.mp3 and Love.mp3
        target_track1 = dir_p / "track_1.mp3"
        target_love = dir_p / "Love.mp3"

        # In-flight temporary files belonging to Job 1 (MUST be cleaned)
        clean_candidates = [
            dir_p / "track_1.mp3.part",
            dir_p / "track_1.part",
            dir_p / "track_1.tmp",
            dir_p / "Love.mp3.part",
        ]
        for p in clean_candidates:
            p.write_text("in-flight partial data")

        # Completed audio files (MUST NOT be cleaned)
        completed_files = [
            dir_p / "track_0.mp3",
            dir_p / "track_10.mp3",
            dir_p / "01 - I Love Rock and Roll.mp3",
        ]
        for p in completed_files:
            p.write_text("valid audio data")

        # Sibling in-flight partial files with overlapping stem names (MUST NOT be cleaned)
        sibling_partials = [
            dir_p / "track_10.mp3.part",
            dir_p / "track_10.part",
            dir_p / "track_100.mp3.part",
            dir_p / "track_1_remix.mp3.part",
            dir_p / "track_1 - Acoustic.mp3.part",
            dir_p / "super_track_1.mp3.part",
            dir_p / "01 - I Love Rock and Roll.mp3.part",
            dir_p / "Love Is All.mp3.part",
            dir_p / "Lovely Day.mp3.part",
        ]
        for p in sibling_partials:
            p.write_text("sibling in-flight data")

        manager = ProcessManager()
        job = await manager.register_job("job_overlap", "user1", "Overlap Test")
        job.in_flight_targets.add(target_track1)
        job.in_flight_targets.add(target_love)
        job.completed_files.add(dir_p / "track_0.mp3")

        cleaned = manager.cleanup_job_temp_files(job)

        # Exactly the 4 matching candidates must be cleaned
        assert cleaned == 4, f"Expected 4 cleaned files, got {cleaned}"

        # Verify target partials were unlinked
        for p in clean_candidates:
            assert not p.exists(), f"Target partial file {p.name} was not unlinked"

        # Verify sibling partials were preserved
        for p in sibling_partials:
            assert p.exists(), f"Sibling partial file {p.name} was collaterally unlinked!"

        # Verify completed audio files were preserved
        for p in completed_files:
            assert p.exists(), f"Completed audio file {p.name} was destroyed!"


@pytest.mark.asyncio
async def test_cancel_job_pgid_boundary_guard(monkeypatch):
    """Verify cancel_job never calls os.killpg with pgid <= 1 (e.g., 0, 1, -1),
    protecting the daemon process group and init from accidental signals.
    """
    manager = ProcessManager()
    job = await manager.register_job("job_guard", "user1", "Guard Test")

    killed_pgids = []

    def mock_killpg(pgid, sig):
        killed_pgids.append((pgid, sig))

    monkeypatch.setattr("os.killpg", mock_killpg)

    class MockProcess:
        def __init__(self, pid):
            self.pid = pid
            self.returncode = 0

        async def wait(self):
            return self.returncode

    from server.app.process import ProcessHandle

    handle_0 = ProcessHandle(job_id="job_guard", pid=5000, pgid=0, process=MockProcess(5000), cmd=["dummy"])
    handle_1 = ProcessHandle(job_id="job_guard", pid=5001, pgid=1, process=MockProcess(5001), cmd=["dummy"])
    handle_neg = ProcessHandle(job_id="job_guard", pid=5002, pgid=-1, process=MockProcess(5002), cmd=["dummy"])
    handle_valid = ProcessHandle(job_id="job_guard", pid=5003, pgid=1001, process=MockProcess(5003), cmd=["dummy"])

    job.active_processes[5000] = handle_0
    job.active_processes[5001] = handle_1
    job.active_processes[5002] = handle_neg
    job.active_processes[5003] = handle_valid

    res = await manager.cancel_job("job_guard")

    assert res.status == "cancelled"
    # Verify ONLY valid pgid (1001) was signalled
    assert (1001, signal.SIGTERM) in killed_pgids
    for pgid, _ in killed_pgids:
        assert pgid > 1, f"os.killpg was illegally called with pgid={pgid} <= 1!"


@pytest.mark.asyncio
async def test_cleanup_dot_separated_sibling_isolation():
    """Verify cleanup_job_temp_files uses exact set membership and preserves
    dot-separated sibling tracks (e.g. song.extended.mp3.part, song.remix.mp3.part,
    track.vol.1.deluxe.mp3.part) when cleaning target song.mp3 or track.vol.1.mp3.
    """
    with tempfile.TemporaryDirectory() as td:
        dir_p = Path(td)
        target_song = dir_p / "song.mp3"
        target_vol1 = dir_p / "track.vol.1.mp3"

        # In-flight partial files belonging to Job A targets (MUST be cleaned)
        job_a_partials = [
            dir_p / "song.mp3.part",
            dir_p / "song.part",
            dir_p / "song.tmp",
            dir_p / "track.vol.1.mp3.part",
            dir_p / "track.vol.1.part",
        ]
        for p in job_a_partials:
            p.write_text("job a in-flight partial data")

        # Dot-separated sibling partial files belonging to concurrent sibling jobs (MUST NOT be cleaned)
        sibling_partials = [
            dir_p / "song.extended.mp3.part",
            dir_p / "song.remix.mp3.part",
            dir_p / "song.acoustic.mp3.part",
            dir_p / "track.vol.1.deluxe.mp3.part",
            dir_p / "track.vol.10.mp3.part",
        ]
        for p in sibling_partials:
            p.write_text("sibling in-flight partial data")

        manager = ProcessManager()
        job = await manager.register_job("job_dot_isolation", "user1", "Dot Test")
        job.in_flight_targets.add(target_song)
        job.in_flight_targets.add(target_vol1)

        cleaned = manager.cleanup_job_temp_files(job)

        assert cleaned == len(job_a_partials), f"Expected {len(job_a_partials)} cleaned files, got {cleaned}"
        for p in job_a_partials:
            assert not p.exists(), f"Target partial file {p.name} was not unlinked"
        for p in sibling_partials:
            assert p.exists(), f"Sibling partial file {p.name} was collaterally unlinked!"


@pytest.mark.asyncio
async def test_shared_album_dir_isolation_on_timeout():
    """Verify that timeout during run_command in a shared album directory
    does not collaterally destroy sibling in-flight partial download files.
    """
    with tempfile.TemporaryDirectory() as td:
        shared_album_dir = Path(td) / "The Beatles - Abbey Road"
        shared_album_dir.mkdir(parents=True, exist_ok=True)

        # In-flight partial download for Job 1
        job1_target = shared_album_dir / "01 - Come Together.mp3"
        job1_part = shared_album_dir / "01 - Come Together.mp3.part"
        job1_part.write_text("Job 1 partial data")

        # Sibling in-flight partial download for concurrent sibling Job 2
        job2_part = shared_album_dir / "02 - Something.mp3.part"
        job2_part.write_text("Job 2 partial data")

        # Sibling in-flight partial download for concurrent sibling Job 3
        job3_part = shared_album_dir / "03 - Maxwell's Silver Hammer.mp3.part"
        job3_part.write_text("Job 3 partial data")

        manager = ProcessManager()
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]

        # 1. Timeout with explicit in_flight_target: Job 1 partial unlinked, siblings preserved
        with pytest.raises(TimeoutError):
            await manager.run_command(
                cmd,
                temp_dir=shared_album_dir,
                in_flight_target=job1_target,
                timeout=0.2,
            )

        assert not job1_part.exists(), "Job 1 partial file should be cleaned up on timeout"
        assert job2_part.exists(), "Sibling Job 2 partial file was collaterally destroyed!"
        assert job3_part.exists(), "Sibling Job 3 partial file was collaterally destroyed!"

        # 2. Timeout without in_flight_target in shared album directory:
        # Blind sweep must be blocked to protect sibling in-flight files
        job4_part = shared_album_dir / "04 - Oh! Darling.mp3.part"
        job4_part.write_text("Job 4 partial data")

        with pytest.raises(TimeoutError):
            await manager.run_command(
                cmd,
                temp_dir=shared_album_dir,
                timeout=0.2,
            )

        assert job2_part.exists(), "Sibling Job 2 partial destroyed by blind directory sweep!"
        assert job3_part.exists(), "Sibling Job 3 partial destroyed by blind directory sweep!"
        assert job4_part.exists(), "Sibling Job 4 partial destroyed by blind directory sweep!"




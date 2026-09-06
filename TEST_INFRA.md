# 🧪 End-to-End (E2E) Test Infrastructure & Strategy: Jellyfin Music Downloader V2

## 1. Overview & Test Philosophy

This document defines the automated testing architecture, testing tiers, fixture design, and quality standards for the Jellyfin Music Downloader V2 rewrite (`jellyfin-music-app`).

### Test Philosophy
- **Opaque-Box & Requirement-Driven**: Tests validate observable interfaces, REST API contracts, WebSocket event streams, file system state, and data invariants rather than private implementation details.
- **Empirical & Authoritative Derivation**: Expected test outputs are strictly derived from requirements in `ORIGINAL_REQUEST.md`, architectural specifications in `PROJECT.md`, and authoritative RFC/API contracts (Jellyfin REST API, LRCLIB API, Mutagen ID3v2.4/FLAC standards). Facade tests that pass vacuously are strictly prohibited.
- **Progressive Testability & Isolation**: Every test is self-contained, creates its own isolated state (temporary directories, in-memory mock routers), cleans up after itself, and does not depend on test execution order or external network connectivity.
- **Defect Prevention**: Tests explicitly assert the permanent resolution of critical legacy defects:
  - **SEC-01**: Zero command injection via shell arguments (`shell=False` with argv lists).
  - **SEC-03**: Process-targeted cancellation without killing sibling jobs.
  - **DAT-01**: Non-ASCII Unicode preservation via NFKC (Japanese, Korean, Cyrillic, accented Latin).
  - **DAT-02**: Absolute preservation of audio files under 350KB (zero file unlinks).
  - **DAT-03 / DAT-04**: Elimination of recursive multi-TB `os.walk` scans via deterministic path construction.
  - **DAT-07**: LRCLIB lyric duration validation within $\pm 3.0\text{s}$.
  - **JEL-01 / JEL-02**: Zero direct SQLite or XML disk mutations; strict `OwnerUserId` isolation.
  - **JEL-05**: Sequential 50-track chunking for playlist track population.

---

## 2. 4-Tier Test Strategy

The test suite is structured across four distinct verification tiers:

```
+-------------------------------------------------------------------------------+
|                       4-TIER TEST ARCHITECTURE                                |
+-------------------------------------------------------------------------------+
| Tier 1: Feature Verification (Happy Path)                                     |
|   - REST API endpoints (/health, /api/config, /api/users, /api/resolve)       |
|   - In-memory library index construction and lookup                           |
|   - Synchronized lyric fetching and Mutagen metadata tagging                  |
|   - Jellyfin REST client user query and playlist creation                     |
+-------------------------------------------------------------------------------+
| Tier 2: Boundary & Edge Case Verification                                     |
|   - Unicode NFKC non-ASCII preservation (Japanese, Korean, Cyrillic, Latin)   |
|   - Audio file size boundaries (<350KB short tracks preserved)               |
|   - Lyric duration tolerance boundary (|Δt| <= 3.0s accepted, > 3.0s rejected)|
|   - Jellyfin 50-track sequential chunking (50, 51, 125 tracks)                |
|   - Malformed/injection inputs in URLs, names, and parameters                 |
+-------------------------------------------------------------------------------+
| Tier 3: Pairwise & State Integration                                          |
|   - Resolve -> Ingest pipeline state flow                                     |
|   - Multi-user isolation (User A vs User B playlist separation)               |
|   - Process group cancellation (Job A kill does not affect Job B)             |
|   - WebSocket event sequence: job_started -> stage -> progress -> complete    |
+-------------------------------------------------------------------------------+
| Tier 4: Real-World & Performance Stress                                       |
|   - Sub-20ms Diff Benchmark: 200 tracks against 10,000+ indexed tracks        |
|   - Concurrent download worker pool dispatching under simulated load          |
|   - Invariant verification: Zero sqlite3 imports and zero playlist.xml writes  |
+-------------------------------------------------------------------------------+
```

### Tier 1: Feature Verification (Happy Path)
- **Scope**: Verifies individual components perform their primary function with standard valid inputs.
- **Key Tests**:
  - `GET /health` returns HTTP 200 with uptime, version `2.0.0`, and active job count.
  - `GET /api/config` returns server paths, bitrates, default user, and Jellyfin URL.
  - `GET /api/users` parses Jellyfin users and private playlists via REST.
  - `POST /api/resolve` partitions a test playlist into existing vs missing tracks.
  - `POST /api/ingest` validates payload and enqueues job returning HTTP 202.

### Tier 2: Boundary & Edge Cases
- **Scope**: Tests limits, non-standard scripts, special characters, and defensive boundaries.
- **Key Tests**:
  - **NFKC Normalization**: Verifies that Japanese (`前前前世`), Korean (`봄날 (Spring Day)`), Cyrillic (`Группа крови`), and accented Latin (`Beyoncé - Déjà Vu`) never collapse to `""`.
  - **Small Audio Files**: Verifies that 50KB, 120KB, 250KB, and 340KB audio files are indexed and never deleted.
  - **Lyric Duration Matching**: Verifies audio with 210s matches 211s (+1s), matches 213s (+3s), but rejects 214s (+4s) or 285s (live acoustic version).
  - **Playlist Chunking**: 125 tracks sent as sequential calls of 50, 50, and 25 items in exact order.
  - **Input Sanitization**: Single quotes (`"Kendon's 90's Rock"`) and slashes in titles handle safely without shell execution or directory traversal.

### Tier 3: Pairwise & State Integration
- **Scope**: Verifies interactions between two or more system subsystems.
- **Key Tests**:
  - Pre-flight diff results fed directly to ingest queue.
  - Jellyfin user scoping: User A cannot see or append to User B's playlists.
  - Targeted cancellation: Triggering `POST /api/cancel` for Job 1 sends `SIGTERM` to Job 1's process group and cleans `.part` files, while Job 2 continues downloading.
  - WebSocket event stream emitting ordered frames (`job_started`, `stage_transition`, `progress`, `track_completed`, `job_completed`).

### Tier 4: Real-World & Performance Stress
- **Scope**: Non-functional requirements, benchmarks, and structural integrity.
- **Key Tests**:
  - **Sub-20ms Diff Benchmark**: 200 tracks diffed against a 10,000-track in-memory library in under 20ms (target $<5\text{ms}$).
  - **Zero SQLite & Zero XML Audit**: AST/grep assertion ensuring zero `sqlite3` or `playlist.xml` mutations exist in `server/app/`.
  - **Log Rotation Boundary**: Bounded log growth ($5\text{MB} \times 3 \le 15\text{MB}$).

---

## 3. Minimum Coverage Thresholds

| Metric | Required Threshold | Verification Method |
| :--- | :--- | :--- |
| **Acceptance Criteria Coverage** | 100% (All 9 criteria from ORIGINAL_REQUEST.md) | Tier 1–4 Test Suite |
| **Daemon REST Endpoints** | 100% (`/health`, `/api/config`, `/api/users`, `/api/resolve`, `/api/ingest`, `/api/cancel`) | `test_daemon.py` |
| **Unicode Script Coverage** | 100% (Japanese, Korean, Cyrillic, Accented Latin) | `test_indexer.py` |
| **File Destruction Protection** | 100% (Files <350KB preserved, zero unlinks) | `test_indexer.py` |
| **Sub-20ms Diff Benchmark** | 100% passing (<20ms for 200 tracks) | `test_resolver.py` |
| **Lyric Duration Verification** | 100% ($\pm 3\text{s}$ tolerance boundary) | `test_lyrics.py` |
| **Jellyfin Chunking & Isolation** | 100% (50-item chunks, `OwnerUserId` scoping) | `test_jellyfin.py` |
| **Test Pass Rate** | 100% passing (0 failures, 0 errors) | `pytest tests/ -v` |

---

## 4. Test Directory Layout

```
tests/
├── conftest.py            # Reusable fixtures: mock Jellyfin, mock LRCLIB, dummy audio files, ASGI client
├── test_daemon.py         # REST API routes (/health, /api/config, /api/cancel) & daemon lifecycle
├── test_indexer.py        # Unicode NFKC non-ASCII title preservation & <350KB short track protection
├── test_resolver.py       # Sub-20ms diff engine benchmark (200 tracks) & playlist partitioning
├── test_lyrics.py         # Async LRCLIB client, ±3s duration validation, Mutagen tagger
└── test_jellyfin.py       # Jellyfin REST integration, 50-track chunking, multi-user isolation
```

---

## 5. Reusable Fixtures Guide (`conftest.py`)

All tests share fixtures defined in `tests/conftest.py`:

1. **`async_client` (`httpx.AsyncClient`)**:
   Asynchronous ASGI client for sending non-blocking HTTP requests to the FastAPI daemon without binding a real TCP port.
2. **`mock_jellyfin`**:
   In-memory mock HTTP transport intercepting calls to the Jellyfin server (`http://192.168.1.159:8096`):
   - Mocks `GET /Users`, `GET /Users/{userId}/Items`, `POST /Playlists`, `POST /Playlists/{id}/Items`, `GET /Library/VirtualFolders`.
   - Records request history, headers (`X-Emby-Token`), chunk sizes, and user scoping.
3. **`mock_lrclib`**:
   In-memory mock HTTP transport intercepting calls to `https://lrclib.net/api/get` and `/api/search`:
   - Returns synchronized `.lrc` lyrics with configurable duration tags.
4. **`sample_audio_files`**:
   Generates synthetic, valid audio files in a temporary directory:
   - Tracks under 350KB (145KB album intro/skit).
   - Non-ASCII titles across Japanese, Korean, Cyrillic, and accented Latin.
   - Collaborative multi-artist tracks.
5. **`temp_music_dir`**:
   Creates an isolated media folder hierarchy (`{artist}/{album}/{track}.mp3`) for deterministic path and indexing tests.

---

## 6. How to Run the Tests

### Recommended Command
```bash
# Execute entire test suite with verbose output
.venv/bin/pytest tests/ -v

# Run specific test modules
.venv/bin/pytest tests/test_daemon.py -v
.venv/bin/pytest tests/test_indexer.py -v

# Run with benchmark timing
.venv/bin/pytest tests/ -v --durations=10
```

### CI / Container Test Execution
Inside the project root or Docker container:
```bash
pytest tests/ -v --tb=short
```

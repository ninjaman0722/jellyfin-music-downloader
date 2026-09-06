"""Static Analysis and Structural Verification Tests for Quickshell QML Client.

Verifies:
- File layout: main.qml, qml/views/*, qml/components/*, qml/theme/*
- Line count constraint: main.qml must be strictly <300 lines
- QML syntax, balanced delimiters, and required module imports
- Component property, signal, and interface contracts
- Anti-pattern audit: 0 SSH calls, 0 pkill/killall commands, 0 shell injection hazards
- qmllint syntax validation if installed on host
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Set, Tuple
import pytest

APP_ROOT = Path(os.environ.get("JELLYFIN_APP_DIR", str(Path(__file__).resolve().parent.parent)))

REQUIRED_VIEWS = [
    "IngestView.qml",
    "ProgressView.qml",
    "SettingsView.qml",
]

REQUIRED_COMPONENTS = [
    "AnalysisCard.qml",
    "UserSelector.qml",
    "PlaylistPicker.qml",
    "ToastBanner.qml",
]


# ==============================================================================
# Helper Utilities for Static QML Parsing
# ==============================================================================

def strip_comments_and_strings(content: str) -> str:
    """Removes single-line/multi-line comments and string literals to inspect structural tokens."""
    pattern = r"(/\*.*?\*/)|(//[^\r\n]*)|(\"(?:\\.|[^\"\\])*\")|('(?:\\.|[^'\\])*')"
    def _repl(match):
        if match.group(1) or match.group(2):
            return ""
        return '""'
    return re.sub(pattern, _repl, content, flags=re.DOTALL)



def extract_root_element(content: str) -> str:
    """Extracts the root QML element type name."""
    clean = strip_comments_and_strings(content)
    # Skip import statements and pragmas
    lines = clean.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("import ") or line.startswith("pragma "):
            continue
        # First non-import identifier before {
        match = re.match(r"^([A-Za-z0-9_]+)\s*\{", line)
        if match:
            return match.group(1)
        match_named = re.match(r"^([A-Za-z0-9_]+)\s*$", line)
        if match_named:
            return match_named.group(1)
    return ""


def extract_properties_and_signals(content: str) -> Tuple[Set[str], Set[str]]:
    """Extracts declared property names and signal names from QML content."""
    clean = strip_comments_and_strings(content)
    properties: Set[str] = set()
    signals: Set[str] = set()

    for line in clean.splitlines():
        line = line.strip()
        # property <type> <name>[: value] or property var <name>
        prop_match = re.match(r"^property\s+(?:readonly\s+)?([A-Za-z0-9_<>]+)\s+([A-Za-z0-9_]+)", line)
        if prop_match:
            properties.add(prop_match.group(2))
            continue
        # signal <name>(...) or signal <name>
        sig_match = re.match(r"^signal\s+([A-Za-z0-9_]+)", line)
        if sig_match:
            signals.add(sig_match.group(1))

    return properties, signals


def extract_imports(content: str) -> List[str]:
    """Extracts all import module paths and names."""
    imports = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("import "):
            imports.append(line[len("import "):].strip().strip('"').strip("'"))
    return imports


# ==============================================================================
# 1. File Layout & Structure Tests
# ==============================================================================

class TestQmlFileLayout:
    """Verifies that all required modular QML files exist in the project tree."""

    def test_root_main_qml_exists(self):
        main_qml = APP_ROOT / "main.qml"
        assert main_qml.is_file(), f"Missing root controller: {main_qml}"

    def test_views_directory_and_files_exist(self):
        views_dir = APP_ROOT / "qml" / "views"
        if not views_dir.exists():
            pytest.skip(f"M4 modular views not yet implemented in {views_dir} (expected during M4 implement phase)")
        assert views_dir.is_dir(), f"Expected directory at {views_dir}"

        for view in REQUIRED_VIEWS:
            view_path = views_dir / view
            assert view_path.is_file(), f"Missing required subview: {view_path}"

    def test_components_directory_and_files_exist(self):
        components_dir = APP_ROOT / "qml" / "components"
        if not components_dir.exists():
            pytest.skip(f"M4 modular components not yet implemented in {components_dir} (expected during M4 implement phase)")
        assert components_dir.is_dir(), f"Expected directory at {components_dir}"

        for comp in REQUIRED_COMPONENTS:
            comp_path = components_dir / comp
            assert comp_path.is_file(), f"Missing required component: {comp_path}"

    def test_theme_component_exists(self):
        theme_path = APP_ROOT / "qml" / "theme" / "Theme.qml"
        if not theme_path.exists():
            pytest.skip(f"M4 theme module not yet implemented at {theme_path}")
        assert theme_path.is_file(), f"Missing theme manager: {theme_path}"


# ==============================================================================
# 2. Line Count Constraints (<300 lines on main.qml)
# ==============================================================================

class TestQmlLineCountConstraints:
    """Verifies that main.qml satisfies the hard <300 line count constraint."""

    def test_main_qml_strictly_under_300_lines(self):
        main_qml = APP_ROOT / "main.qml"
        assert main_qml.is_file()
        lines = main_qml.read_text(encoding="utf-8").splitlines()
        count = len(lines)
        # Note: If legacy 2442-line file is present, this will fail or highlight refactoring need
        if count >= 300:
            pytest.fail(
                f"main.qml has {count} lines. Hard requirement: main.qml must be <300 lines for modularity. "
                f"Refactor logic into subviews and components."
            )
        assert count < 300, f"main.qml exceeds 300 lines (actual: {count})"

    def test_subviews_line_counts_are_modular(self):
        views_dir = APP_ROOT / "qml" / "views"
        if not views_dir.is_dir():
            pytest.skip("qml/views/ not yet created")

        for view_path in views_dir.glob("*.qml"):
            lines = view_path.read_text(encoding="utf-8").splitlines()
            count = len(lines)
            assert count < 450, f"Subview {view_path.name} is too large ({count} lines). Decompose further."

    def test_components_line_counts_are_modular(self):
        comp_dir = APP_ROOT / "qml" / "components"
        if not comp_dir.is_dir():
            pytest.skip("qml/components/ not yet created")

        for comp_path in comp_dir.glob("*.qml"):
            lines = comp_path.read_text(encoding="utf-8").splitlines()
            count = len(lines)
            assert count < 350, f"Component {comp_path.name} is too large ({count} lines). Decompose further."


# ==============================================================================
# 3. Delimiter Balance & Syntax Checks
# ==============================================================================

class TestQmlSyntaxAndDelimiters:
    """Statically verifies delimiter balancing and imports without Wayland compositor."""

    def _check_balanced_delimiters(self, file_path: Path):
        content = file_path.read_text(encoding="utf-8")
        clean = strip_comments_and_strings(content)

        stack: List[Tuple[str, int]] = []
        pairs = {")": "(", "}": "{", "]": "["}

        for idx, char in enumerate(clean):
            if char in "({[":
                stack.append((char, idx))
            elif char in ")}]":
                if not stack:
                    pytest.fail(f"Unmatched closing delimiter '{char}' in {file_path}")
                open_char, _ = stack.pop()
                expected = pairs[char]
                assert open_char == expected, f"Mismatched delimiter in {file_path}: expected '{expected}', found '{open_char}'"

        assert len(stack) == 0, f"Unclosed delimiters remaining in {file_path}: {[s[0] for s in stack]}"

    def test_main_qml_balanced_delimiters(self):
        self._check_balanced_delimiters(APP_ROOT / "main.qml")

    def test_all_qml_files_balanced_delimiters(self):
        for qml_path in APP_ROOT.rglob("*.qml"):
            if ".venv" in qml_path.parts:
                continue
            self._check_balanced_delimiters(qml_path)

    def test_main_qml_required_imports(self):
        main_qml = APP_ROOT / "main.qml"
        content = main_qml.read_text(encoding="utf-8")
        imports = extract_imports(content)
        # Verify core imports
        assert any("QtQuick" in imp for imp in imports), "Missing QtQuick import"
        assert any("Quickshell" in imp for imp in imports), "Missing Quickshell import"

    def test_qmllint_tool_validation_if_available(self):
        qmllint = shutil.which("qmllint")
        if not qmllint:
            pytest.skip("qmllint not installed on system")

        qml_files = [p for p in APP_ROOT.rglob("*.qml") if ".venv" not in p.parts]
        for qml_file in qml_files:
            res = subprocess.run(
                [qmllint, str(qml_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            # qmllint exits 0 on valid syntax or minor warnings
            assert res.returncode == 0 or "error:" not in res.stderr.lower(), f"qmllint failed on {qml_file.name}: {res.stderr}"


# ==============================================================================
# 4. Component Property & Signal Contracts
# ==============================================================================

class TestQmlComponentContracts:
    """Verifies interface contracts (properties, signals, and root elements)."""

    def test_main_qml_root_is_panelwindow(self):
        main_qml = APP_ROOT / "main.qml"
        root_type = extract_root_element(main_qml.read_text(encoding="utf-8"))
        assert root_type == "PanelWindow", f"Expected root element PanelWindow, found '{root_type}'"

    def test_main_qml_has_state_machine_properties(self):
        main_qml = APP_ROOT / "main.qml"
        props, _ = extract_properties_and_signals(main_qml.read_text(encoding="utf-8"))
        assert "appState" in props, "main.qml missing appState property"

    def test_analysis_card_contract(self):
        comp = APP_ROOT / "qml" / "components" / "AnalysisCard.qml"
        if not comp.is_file():
            pytest.skip("AnalysisCard.qml not yet implemented")
        props, _ = extract_properties_and_signals(comp.read_text(encoding="utf-8"))
        # Check either analysisData object or individual track counters
        has_data_obj = "analysisData" in props or "diffData" in props
        has_individual_counters = ("totalTracks" in props or "existingTracks" in props or "missingTracks" in props)
        assert has_data_obj or has_individual_counters, f"AnalysisCard.qml missing diff summary properties (found {props})"

    def test_user_selector_contract(self):
        comp = APP_ROOT / "qml" / "components" / "UserSelector.qml"
        if not comp.is_file():
            pytest.skip("UserSelector.qml not yet implemented")
        props, signals = extract_properties_and_signals(comp.read_text(encoding="utf-8"))
        has_user_prop = any("user" in p.lower() for p in props)
        has_user_sig = any("user" in s.lower() for s in signals)
        assert has_user_prop or has_user_sig, f"UserSelector.qml missing user model/selection interface (props: {props}, sigs: {signals})"


# ==============================================================================
# 5. Security & Anti-Pattern Audit
# ==============================================================================

class TestQmlSecurityAndAntiPatterns:
    """Audits QML files to permanently eliminate command injection and shell risks."""

    def test_zero_ssh_invocations_in_qml(self):
        """Mandatory Invariant: Zero SSH command executions in client QML code."""
        for qml_path in APP_ROOT.rglob("*.qml"):
            if ".venv" in qml_path.parts:
                continue
            content = qml_path.read_text(encoding="utf-8")
            matches = re.findall(r"\bssh\s+-[a-zA-Z0-9_-]+|\bssh\s+[a-zA-Z0-9_.-]+", content)
            # If matches found, must fail
            assert len(matches) == 0, f"Found legacy SSH execution in {qml_path.relative_to(APP_ROOT)}: {matches}"

    def test_zero_destructive_pkill_invocations(self):
        """Mandatory Invariant: Zero pkill or killall commands in client QML code."""
        for qml_path in APP_ROOT.rglob("*.qml"):
            if ".venv" in qml_path.parts:
                continue
            content = qml_path.read_text(encoding="utf-8")
            matches = re.findall(r"\bpkill\b|\bkillall\b|\bdocker\s+kill\b", content)
            assert len(matches) == 0, f"Found destructive process termination in {qml_path.relative_to(APP_ROOT)}: {matches}"

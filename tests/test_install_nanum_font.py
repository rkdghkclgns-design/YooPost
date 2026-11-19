"""Run the font installer with isolated package/network fixtures, never sudo."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_nanum_font.sh"
ARCHIVE = b"test font archive\n"
PACKAGE_HASH = "3cd2d18fc67c616c3a2e037ece0eb6aece414755025e2ddeec4f6e828783dc76"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Run Linux installer tests in Linux or WSL")


@pytest.fixture
def installer(tmp_path):
    bash = shutil.which("bash")
    if not bash or subprocess.run([bash, "--version"], capture_output=True).returncode:
        pytest.skip("Bash is required for the installer integration tests")

    bins = tmp_path / "bin"
    bins.mkdir()
    fonts = tmp_path / "fonts"
    state = tmp_path / "state"
    state.mkdir()
    font_path, bin_path, state_path = map(str, (fonts, bins, state))
    script = tmp_path / "install.sh"
    # Only redirect system paths and the expected archive digest to test fixtures.
    source = SCRIPT.read_text(encoding="utf-8")
    source = source.replace("font_dir=/usr/share/fonts/truetype/nanum", f'font_dir="{font_path}"', 1)
    source = source.replace(PACKAGE_HASH, hashlib.sha256(ARCHIVE).hexdigest(), 1)
    script.write_text(source, encoding="utf-8", newline="\n")
    commands = {
        "sudo": 'if [[ "$1" == apt-get ]]; then exit 100; fi\nexec "$@"\n',
        "curl": r'''
count=0
[[ ! -f "$TEST_STATE/curl_count" ]] || read -r count < "$TEST_STATE/curl_count"
count=$((count + 1))
printf '%s\n' "$count" > "$TEST_STATE/curl_count"
while [[ "$#" -gt 0 ]]; do
    if [[ "$1" == --output ]]; then output="$2"; shift 2; else shift; fi
done
printf 'partial transfer\n' > "$output"
if [[ "$count" -le "$TEST_FAILURES" ]]; then exit 28; fi
if [[ "$TEST_BAD_DIGEST" == 1 ]]; then
    printf 'corrupted archive\n' > "$output"
else
    printf 'test font archive\n' > "$output"
fi
''',
        "dpkg-deb": r'''
printf 'extract\n' >> "$TEST_STATE/events"
mkdir -p "$3$TEST_FONT_DIR"
printf 'regular fixture\n' > "$3$TEST_FONT_DIR/NanumGothic.ttf"
printf 'bold fixture\n' > "$3$TEST_FONT_DIR/NanumGothicBold.ttf"
''',
        "sleep": 'printf "sleep %s\\n" "$1" >> "$TEST_STATE/events"\n',
    }
    for name, body in commands.items():
        command = bins / name
        command.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8", newline="\n")
        command.chmod(0o755)

    def run(failures=0, bad_digest=False):
        return subprocess.run(
            [bash, "-c", 'set -eu; : "${1:?fixture directory required}"; export PATH="$1:$PATH" TEST_STATE="$2" '
             'TEST_FONT_DIR="$3" TEST_FAILURES="$4" TEST_BAD_DIGEST="$5"; bash "$6"',
             "bash", bin_path, state_path, font_path, str(failures), str(int(bad_digest)), str(script)],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )

    return run, fonts, state


@pytest.mark.parametrize("failures", [0, 1, 2])
def test_download_recovers_within_three_attempts_and_installs_both_fonts(installer, failures):
    run, fonts, state = installer
    result = run(failures=failures)
    assert result.returncode == 0, result.stdout + result.stderr
    assert int((state / "curl_count").read_text()) == failures + 1
    events = (state / "events").read_text().splitlines()
    assert events.count("sleep 2") == failures
    assert events.count("extract") == 1
    assert (fonts / "NanumGothic.ttf").is_file()
    assert (fonts / "NanumGothicBold.ttf").is_file()


def test_exhausted_download_stops_after_three_attempts_before_extracting(installer):
    run, fonts, state = installer
    result = run(failures=10)
    assert result.returncode != 0
    assert int((state / "curl_count").read_text()) == 3
    assert (state / "events").read_text().splitlines() == ["sleep 2", "sleep 2"]
    assert not fonts.exists()


def test_checksum_failure_never_extracts_or_installs_the_archive(installer):
    run, fonts, state = installer
    result = run(bad_digest=True)
    assert result.returncode != 0
    assert int((state / "curl_count").read_text()) == 1
    assert not (state / "events").exists()
    assert not fonts.exists()


def test_existing_font_pair_skips_all_network_requests(installer):
    run, fonts, state = installer
    fonts.mkdir()
    for name in ("NanumGothic.ttf", "NanumGothicBold.ttf"):
        (fonts / name).write_bytes(b"existing font")
    assert run().returncode == 0
    assert not (state / "curl_count").exists()

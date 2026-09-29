import json

from autopost.constants import GUIDE_FILE, HISTORY_FILE, PUBLIC_ASSET_FOLDER
from autopost.setup_engine import ensure_layout
from fakes import MemoryDrive


def test_setup_creates_layout_once():
    drive = MemoryDrive()
    first = ensure_layout(drive, "AI백과사전_포스팅자동화")
    assert first.changed
    assert first.created_folders == ("AI백과사전_포스팅자동화", f"AI백과사전_포스팅자동화/{PUBLIC_ASSET_FOLDER}")
    assert set(first.created_files) == {GUIDE_FILE, HISTORY_FILE}
    second = ensure_layout(drive, "AI백과사전_포스팅자동화")
    assert not second.changed and second.layout == first.layout
    history = drive.find_child(first.layout.root_id, HISTORY_FILE)
    assert json.loads(drive.read_text(history.id)) == {"posts": []}

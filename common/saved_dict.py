"""A small dict kept on disk: every change is written atomically (used for @pm's waiting ✅s)."""
import json
import os
from pathlib import Path


class SavedDict(dict):
    """A dict written to its file on every change (small: one entry per room at most)."""
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            super().__init__(json.loads(path.read_text()))
        except (OSError, ValueError):
            super().__init__()                # nothing waiting (or a damaged file): start empty

    def _save(self) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")   # processes sharing a file never share a temp
        tmp.write_text(json.dumps(self))
        os.replace(tmp, self.path)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self._save()

    def pop(self, *args):
        value = super().pop(*args)
        self._save()
        return value

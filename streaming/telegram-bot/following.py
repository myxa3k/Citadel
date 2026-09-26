"""Shows the bot watches for new episodes.

Sonarr does this job normally, but only searches the English title its
metadata provider hands it -- the exact limitation the bot exists to work
around. Following a show here reuses the bot's own multi-title search, so a
Russian release of a new episode is found the same way the first one was.

State is a JSON file rather than a database: it holds a handful of shows
with a few fields each, and being able to read and edit it by hand is worth
more here than anything a schema would buy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Followed:
    title: str
    kind: str
    # Release titles already accounted for, so the same episode isn't
    # announced every day. Grows slowly and is capped when saving.
    seen: list[str] = field(default_factory=list)
    last_checked: str | None = None
    # Releases found since you last looked. Kept here rather than pushed
    # straight to the chat: the notification only says *which* shows have
    # something new, and /new is what shows the releases themselves.
    pending: list[str] = field(default_factory=list)

    def remember(self, release_title: str, keep: int = 200) -> None:
        self.seen.append(release_title)
        if len(self.seen) > keep:
            del self.seen[:-keep]

    @property
    def has_news(self) -> bool:
        return bool(self.pending)


class FollowStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._shows: dict[str, Followed] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            # A corrupt file shouldn't stop the bot starting; worst case the
            # follow list is empty and gets rebuilt by hand.
            log.exception("could not read %s, starting empty", self._path)
            return
        self._shows = {k: Followed(**v) for k, v in raw.items()}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in self._shows.items()}
        # Write-then-rename so an interrupted save can't truncate the file.
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        temp.replace(self._path)

    @staticmethod
    def key(title: str) -> str:
        return title.strip().lower()

    def add(self, title: str, kind: str) -> bool:
        k = self.key(title)
        if k in self._shows:
            return False
        self._shows[k] = Followed(title=title, kind=kind)
        self._save()
        return True

    def remove(self, title: str) -> bool:
        if self._shows.pop(self.key(title), None) is None:
            return False
        self._save()
        return True

    def all(self) -> list[Followed]:
        return list(self._shows.values())

    def update(self, show: Followed) -> None:
        self._shows[self.key(show.title)] = show
        self._save()

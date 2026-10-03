"""Explicit query intent. Routing never drops entity names from a reranker query."""

from dataclasses import dataclass
import re
import unicodedata


def compact(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


@dataclass(frozen=True)
class Query:
    text: str
    topic: str | None = None
    intent: str = "direct"
    mode: str = "surface"
    exclude_ids: tuple[str, ...] = ()
    delivered_ids: tuple[str, ...] = ()
    user_utterance: bool = False

    def __post_init__(self):
        if self.mode not in {"surface", "lookup"} or self.intent not in {"direct", "latest", "progress", "timeline", "narrative", "exact"}:
            raise ValueError("Unsupported recall mode or intent")

    @property
    def search_text(self):
        if self.topic is not None:
            return self.topic.strip()
        # Quoted titles are explicit anchors; the full question remains available.
        quoted = re.findall(r'[《「“"]([^》」”"]{2,80})[》」”"]', self.text)
        return quoted[0] if len(quoted) == 1 else self.text.strip()

    def names_title(self, title):
        key = compact(title)
        return len(key) >= 2 and key in compact(self.text)

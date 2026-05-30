from dataclasses import dataclass, asdict
from typing import Iterable


@dataclass
class Item:
    id: str
    source: str
    sub_source: str = ""
    title: str = ""
    author: str = ""
    url: str = ""
    content: str = ""
    comments_blob: str = ""
    published_at: str = ""
    score: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class Collector:
    name = "base"

    def collect(self) -> Iterable[Item]:
        raise NotImplementedError

from dataclasses import dataclass


@dataclass
class AlbumInfo:
    album_id: str
    title: str
    intro: str
    author: str
    cover_url: str
    chapters: list[str]

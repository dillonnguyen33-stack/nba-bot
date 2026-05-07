from typing import NotRequired, TypedDict


class DiscordField(TypedDict):
    name: str
    value: str
    inline: bool


class DiscordFooter(TypedDict):
    text: str


class DiscordEmbed(TypedDict):
    title: str
    color: int
    description: NotRequired[str]
    fields: NotRequired[list[DiscordField]]
    footer: NotRequired[DiscordFooter]
    timestamp: NotRequired[str]

from typing import NotRequired, TypedDict


class DiscordField(TypedDict):
    name: str
    value: str
    inline: bool


class DiscordEmbed(TypedDict):
    title: str
    color: int
    description: NotRequired[str]
    fields: NotRequired[list[DiscordField]]
    footer: NotRequired[dict[str, str]]

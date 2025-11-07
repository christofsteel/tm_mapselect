from typing import Literal, Optional
from dataclasses import dataclass


@dataclass
class Style:
    bold: bool
    italic: bool
    width: Literal["normal", "wide", "narrow"]
    uppercase: bool
    shadow: bool
    color: Optional[str]

    def set_defaults(self):
        self.bold = False
        self.italic = False
        self.width = "normal"
        self.uppercase = False
        self.shadow = False
        self.color = None

    def to_css(self) -> str:
        styles = []
        if self.bold:
            styles.append("font-weight: bold;")
        if self.italic:
            styles.append("font-style: italic;")
        if self.width == "wide":
            styles.append("font-stretch: expanded;")
        elif self.width == "narrow":
            styles.append("font-stretch: condensed;")
        if self.uppercase:
            styles.append("text-transform: uppercase;")
        if self.shadow:
            styles.append("text-shadow: 2px 2px 4px #000000;")
        if self.color:
            styles.append(f"color: #{self.color};")
        return " ".join(styles)


@dataclass
class Fragment:
    text: str
    style: Style

    def to_html(self) -> str:
        style_str = self.style.to_css()
        return f'<span style="{style_str}">{self.text}</span>'


def parse_word(word: str) -> list[Fragment]:
    fragments = []
    i = 0
    current_style = Style(
        bold=False,
        italic=False,
        width="normal",
        uppercase=False,
        shadow=False,
        color=None,
    )
    last_style = Style(
        bold=False,
        italic=False,
        width="normal",
        uppercase=False,
        shadow=False,
        color=None,
    )
    current_str = ""

    while i < len(word):
        char = word[i]
        next_char = word[i + 1] if i + 1 < len(word) else None

        if char == "$":
            i += 1
            match next_char:
                case "o" | "O":
                    current_style.bold = True
                case "i" | "I":
                    current_style.italic = True
                case "w" | "W":
                    current_style.width = "wide"
                case "n" | "N":
                    current_style.width = "narrow"
                case "m" | "M":
                    current_style.width = "normal"
                case "t" | "T":
                    current_style.uppercase = True
                case "s" | "S":
                    current_style.shadow = True
                case "g" | "G":
                    current_style.color = None
                case "z" | "Z":
                    current_style.set_defaults()
                case "$":
                    current_str += "$"
                case _ if next_char in "0123456789ABCDEFabcdef":
                    hev_value = word[i : i + 3]
                    i += 2
                    current_style.color = hev_value
                case _:
                    raise ValueError(f"Unknown style code: {next_char}")
            if current_style != last_style:
                if current_str:
                    fragments.append(Fragment(text=current_str, style=last_style))
                    current_str = ""
                last_style = Style(
                    bold=current_style.bold,
                    italic=current_style.italic,
                    width=current_style.width,
                    uppercase=current_style.uppercase,
                    shadow=current_style.shadow,
                    color=current_style.color,
                )
        else:
            current_str += char

        i += 1
    if current_str:
        fragments.append(Fragment(text=current_str, style=current_style))
    return fragments


def word_to_html(word: str) -> str:
    fragments = parse_word(word)
    return "".join(fragment.to_html() for fragment in fragments)

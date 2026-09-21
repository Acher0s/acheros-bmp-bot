from pathlib import Path


class Deck:
    def __init__(self, name, png_path):
        path = Path(png_path)

        if not path.is_file():
            raise FileNotFoundError(f"Path does not exist or is not a file: {path}")

        if path.suffix.lower() != ".png":
            raise ValueError(f"File at '{path}' is not a PNG image.")

        self.name: str = name
        self.png_path: Path | None = png_path

    def __str__(self):
        return self.name

    def __lt__(self, other):
        return DECKS.index(self) < DECKS.index(other)


_deck_names = ["Red", "Blue", "Yellow", "Green", "Black", "Magic", "Nebula", "Ghost", "Abandoned", "Checkered",
               "Zodiac", "Painted", "Anaglyph", "Plasma", "Erratic", "Violet", "Orange"]

DECKS = [Deck(name, Path(f"../assets/decks/{name.lower()}.png")) for name in _deck_names]

if __name__ == "__main__":
    print(DECKS)

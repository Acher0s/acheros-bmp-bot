from pathlib import Path


class Stake:
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
        return STAKES.index(self) < STAKES.index(other)


_stake_names = ["White", "Green", "Black", "Purple", "Gold", "Spectral+"]

STAKES = [Stake(name, Path(f"../assets/stakes/{name.lower()}.png")) for name in _stake_names]


if __name__ == "__main__":
    print(STAKES)
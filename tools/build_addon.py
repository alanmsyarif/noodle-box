"""Build an offline Blender extension ZIP from the tracked source files."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "noodle_box"
DEST = ROOT / "dist" / "noodle_box-1.0.0.zip"


def build():
    DEST.parent.mkdir(exist_ok=True)
    files = sorted(PACKAGE.glob("*.py")) + [PACKAGE / "blender_manifest.toml"]
    with zipfile.ZipFile(DEST, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.name)
        archive.write(ROOT / "LICENSE", "LICENSE")
        archive.write(ROOT / "README.md", "README.md")
    print(DEST)
    return DEST


if __name__ == "__main__":
    build()

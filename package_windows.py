"""Package the portable application, excluding any participant recordings."""
import hashlib
from pathlib import Path
import shutil
import zipfile
from engine import VERSION

root = Path(__file__).resolve().parent
app_dir = root / "dist" / f"v{VERSION}" / "NBackExperiment"
for name in ("README_ja.md", "VERIFICATION_ja.md"):
    shutil.copy2(root / name, app_dir / name)
for source in (root / "licenses").glob("*"):
    if source.is_file():
        shutil.copy2(source, app_dir / "licenses" / source.name)
archive = root / "NBackExperiment_Windows.zip"
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
    for path in sorted(app_dir.rglob("*")):
        relative = path.relative_to(app_dir)
        if path.is_file() and not any(part in ("results", "verification_results", "__pycache__") for part in relative.parts):
            package.write(path, Path("NBackExperiment") / relative)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(root / "NBackExperiment_Windows.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii", newline="\n")
print(f"{archive}\n{archive.stat().st_size / 1024 / 1024:.1f} MiB\nSHA256 {digest}")

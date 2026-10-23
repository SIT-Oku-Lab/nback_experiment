"""Build a portable Windows folder with bundled Python/Tk and optional LSL."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent
release = "v1.1.0"
deps = root / ".build_deps"
environment = os.environ.copy()
environment["PYTHONPATH"] = str(deps)
environment["PYINSTALLER_CONFIG_DIR"] = str(root / "build" / "cache")
subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
                "--windowed", "--name", "NBackExperiment", "--collect-all", "pylsl",
                "--distpath", str(root / "dist" / release), "--workpath", str(root / "build" / release),
                "--specpath", str(root), str(root / "app.py")],
               cwd=root, env=environment, check=True)
destination = root / "dist" / release / "NBackExperiment"
for name in ("README_ja.md", "VERIFICATION_ja.md"):
    if (root / name).exists():
        shutil.copy2(root / name, destination / name)
licenses = destination / "licenses"
licenses.mkdir(exist_ok=True)
for source in (root / "licenses").glob("*"):
    if source.is_file():
        shutil.copy2(source, licenses / source.name)
for directory in deps.glob("*.dist-info"):
    sources = [p for p in directory.rglob("*") if p.is_file()
               and ("license" in p.name.lower() or "copying" in p.name.lower())]
    for index, source in enumerate(sources):
        shutil.copy2(source, licenses / f"{directory.name}_{index}_{source.name}")
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copy2(python_license, licenses / "Python_LICENSE.txt")
print(f"Portable application: {destination}")

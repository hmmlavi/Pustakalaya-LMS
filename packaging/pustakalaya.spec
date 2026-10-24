from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path(SPECPATH).parent
hiddenimports = collect_submodules("app")
hiddenimports += ["logging.config", "logging.handlers"]

a = Analysis(
    [str(project_root / "packaging" / "windows_launcher.py")],
    pathex=[str(project_root.parent)],
    binaries=[],
    datas=[
        (str(project_root / "app" / "web" / "templates"), "app/web/templates"),
        (str(project_root / "app" / "web" / "static"), "app/web/static"),
        (str(project_root / "migrations"), "migrations"),
        (str(project_root / "alembic.ini"), "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Pustakalaya",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

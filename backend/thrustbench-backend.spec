# PyInstaller spec for the backend sidecar.  Build with:
#
#     pyinstaller thrustbench-backend.spec --noconfirm
#
# --onedir, not --onefile (DEPLOYMENT.md §4): --onefile re-extracts the
# whole bundle to a temp directory on *every* launch, and with matplotlib
# and pandas in it that is a multi-second cold start each time, plus more
# antivirus attention.  --onedir starts effectively instantly.
#
# Almost everything below exists because PyInstaller finds imports by
# reading source, and the three libraries here all choose their real
# implementation at runtime instead:
#
#   * pyserial picks a backend from sys.platform (§8 gotcha 1) — without
#     it find_esp32_port() returns no ports in the frozen build while
#     working perfectly from source, which looks like a hardware fault.
#   * uvicorn resolves "auto" to a concrete loop/protocol at startup.
#   * matplotlib loads its backend by name and needs its data files.
#
# Every one of these fails at *runtime only*, in a packaged app with no
# visible console — so they are listed explicitly rather than discovered.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = [
    # ── pyserial ──────────────────────────────────────────────────
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "serial.serialwin32",              # The Windows implementation, chosen at runtime
    "serial.win32",

    # ── uvicorn's runtime-selected pieces ─────────────────────────
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets",
    "websockets.legacy",
    "h11",

    # ── matplotlib: analysis.py forces Agg, so ship Agg ───────────
    "matplotlib.backends.backend_agg",

    # ── openpyxl writes the Tab 2 export ──────────────────────────
    "openpyxl",
    "openpyxl.cell._writer",           # Imported lazily by the writer

    # ── FastAPI's multipart upload parsing (Tab 2) ────────────────
    "multipart",
    "python_multipart",
]

# uvicorn's own submodules are imported by string name in places.
hiddenimports += collect_submodules("uvicorn")

datas = []
datas += collect_data_files("matplotlib")   # mpl-data: fonts, rcParams defaults

a = Analysis(
    ["run_backend.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Trimmed deliberately: these pull in tkinter/Qt and add ~50 MB for
    # an app that never opens a window of its own.  matplotlib only ever
    # renders to Agg here.
    excludes=[
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib.backends.backend_tkagg",
        "matplotlib.backends.backend_qtagg",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "_pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="thrustbench-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX-packed exes draw antivirus attention
    console=True,                   # The shell captures stdout/stderr; CREATE_NO_WINDOW hides it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="thrustbench-backend",
)

# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Windows Server standalone package

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

block_cipher = None

# Pull OpenCV / numpy binaries (cv2 is required by captcha solvers)
extra_datas = [
    ('bypass', 'bypass'),
    ('captcha_multi.py', '.'),
]
extra_binaries = []
extra_hidden = []

for pkg in ('cv2', 'numpy', 'curl_cffi', 'onnxruntime', 'ddddocr'):
    try:
        d, b, h = collect_all(pkg)
        extra_datas += d
        extra_binaries += b
        extra_hidden += h
    except Exception:
        pass

try:
    extra_binaries += collect_dynamic_libs('cv2')
except Exception:
    pass

a = Analysis(
    ['ui.py'],
    pathex=[
        '.',
        'bypass',
        'bypass/slide',
        'bypass/image',
        'bypass/text',
    ],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=[
        'vps',
        'bohrium_register',
        'bohrium_create_node',
        'bohrium_ssh',
        'bohrium_notebook',
        'paths',
        'captcha_multi',
        'captcha_runtime',
        'slide_solver',
        'image_solver',
        'text_solver',
        'cv2',
        'numpy',
        'numpy.core',
        'numpy.core._multiarray_umath',
        'onnxruntime',
        'curl_cffi',
        'curl_cffi.requests',
        'paramiko',
        'requests',
        'urllib3',
        'cryptography',
        'nacl',
        'bcrypt',
        'PIL',
        'PIL.Image',
    ] + list(extra_hidden),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BohriumVPS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BohriumVPS',
)

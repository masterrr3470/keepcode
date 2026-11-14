"""Write the Windows version resource that PyInstaller embeds in keepcode.exe.

It fills in the Details tab of the file's Properties window.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/keepcode/__init__.py").read_text()).group(1)
numbers = [int(part) for part in version.split(".")] + [0] * 4
four = tuple(numbers[:4])

TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={four}, prodvers={four}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Jorge Lourenco'),
      StringStruct('FileDescription', 'keepcode: find projects that are not backed up'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'keepcode'),
      StringStruct('LegalCopyright', 'MIT license'),
      StringStruct('OriginalFilename', 'keepcode.exe'),
      StringStruct('ProductName', 'keepcode'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

out = ROOT / "build" / "version_info.txt"
out.parent.mkdir(exist_ok=True)
out.write_text(TEMPLATE, encoding="utf-8")
print(f"Version resource for {version} written to {out}")

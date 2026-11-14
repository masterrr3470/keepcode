"""Print the release notes for one version: its CHANGELOG section plus install steps."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
version = sys.argv[1]
changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
match = re.search(rf"^## {re.escape(version)}\s*$(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
section = match.group(1).strip() if match else "See CHANGELOG.md."

print(f"""{section}

## Install on Windows

```powershell
irm https://raw.githubusercontent.com/masterrr3470/keepcode/main/install.ps1 | iex
```

Or download `keepcode-windows-x64.exe` below and open it. Windows may show "Windows protected your PC" the first
time, because the file is not signed: click **More info** and **Run anyway**. Already installed? keepcode updates
itself when you open it.

`SHA256SUMS.txt` has the checksum of the executable.""")

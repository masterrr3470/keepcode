"""Build or complete a .gitignore so dependencies, build output and secrets stay out."""

from __future__ import annotations

from pathlib import Path

HEADER = "# Added by keepcode"

BASE = [
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".env",
    ".env.*",
    "!.env.example",
    "!.env.sample",
    "!.env.template",
    "*.pem",
    "*.key",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
]

BY_STACK: dict[str, list[str]] = {
    "Node.js": [
        "dist/",
        ".next/",
        ".nuxt/",
        ".svelte-kit/",
        ".turbo/",
        ".vercel/",
        ".netlify/",
        "coverage/",
        "npm-debug.log*",
        "yarn-error.log*",
    ],
    "Python": [
        "*.py[cod]",
        "env/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "*.egg-info/",
    ],
    "Rust": ["target/"],
    "Go": ["bin/"],
    "Java": ["target/", "build/", ".gradle/", "*.class"],
    "Kotlin": ["build/", ".gradle/", "*.class"],
    ".NET": ["bin/", "obj/", ".vs/", "*.user"],
    "Flutter": [".dart_tool/", "build/"],
    "PlatformIO": [".pio/"],
    "Unity": ["[Ll]ibrary/", "[Tt]emp/", "[Oo]bj/", "[Bb]uild/", "[Ll]ogs/", "[Uu]ser[Ss]ettings/"],
    "Godot": [".godot/"],
    "PHP": ["vendor/"],
    "Ruby": [".bundle/", "vendor/bundle/"],
}

# Stack names produced by the scanner that share rules with a broader stack.
ALIASES = {
    "React": "Node.js",
    "Vite": "Node.js",
    "Next.js": "Node.js",
    "Vue": "Node.js",
    "Svelte": "Node.js",
    "Angular": "Node.js",
    "Express": "Node.js",
    "TypeScript": "Node.js",
    "Electron": "Node.js",
    "Tauri": "Rust",
    "Django": "Python",
    "Flask": "Python",
    "FastAPI": "Python",
}


def entries_for(stacks: list[str]) -> list[str]:
    entries = list(BASE)
    seen = set(entries)
    for stack in stacks:
        for key in {stack, ALIASES.get(stack, stack)}:
            for entry in BY_STACK.get(key, []):
                if entry not in seen:
                    seen.add(entry)
                    entries.append(entry)
    return entries


def ensure(project: Path, stacks: list[str], extra: list[str] | None = None) -> list[str]:
    """Append the missing entries to project/.gitignore and return what was added."""
    path = project / ".gitignore"
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    existing = {line.strip() for line in current.splitlines()}
    wanted = entries_for(stacks) + (extra or [])
    missing = [entry for entry in dict.fromkeys(wanted) if entry not in existing]
    if not missing:
        return []
    block = ""
    if current and not current.endswith("\n"):
        block += "\n"
    if current:
        block += "\n"
    if HEADER not in existing:
        block += HEADER + "\n"
    block += "\n".join(missing) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(block)
    return missing


def escape(path: str) -> str:
    """Turn a relative file path into a .gitignore line that matches only that file."""
    path = path.replace("\\", "/")
    for char in "*?[]!#":
        path = path.replace(char, "\\" + char)
    if path.startswith(" "):
        path = "\\" + path
    return "/" + path

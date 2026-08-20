#!/usr/bin/env python3
"""Incrementally export every stable release note from Git history."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATES_DIR = ROOT / "updates"
MD_DIR = UPDATES_DIR / "md"
IMG_DIR = UPDATES_DIR / "img"
MANIFEST_PATH = UPDATES_DIR / ".manifest.json"
STYLE_VERSION = "1"
VERSION_RE = re.compile(r"(?<![\d.])v?(\d+\.\d+\.\d+)(?![\d.])", re.IGNORECASE)
RELEASE_SUBJECT_RE = re.compile(
    r"^stable:\s*v(\d+\.\d+\.\d+)-REL-stable$", re.IGNORECASE
)
HEADING_RE = re.compile(r"^#\s+.+$", re.MULTILINE)


@dataclass(frozen=True)
class ReleaseNote:
    version: str
    release_date: str
    markdown: str

    @property
    def stem(self) -> str:
        return f"哪吒美式-{self.release_date}-v{self.version}-更新日志"

    @property
    def digest(self) -> str:
        source = f"{STYLE_VERSION}\0{self.release_date}\0{self.markdown}"
        return hashlib.sha256(source.encode()).hexdigest()


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_show(commit: str, path: str) -> str | None:
    result = run("git", "show", f"{commit}:{path}", check=False)
    return result.stdout if result.returncode == 0 else None


def parse_metadata(raw: str | None) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def metadata_version(metadata: dict[str, object] | None) -> str | None:
    if not metadata or metadata.get("testing") is not False:
        return None
    version_name = str(metadata.get("versionName", ""))
    release_version = str(metadata.get("releaseVersion", ""))
    if "-REL" not in version_name.upper() or not release_version.lower().endswith("-stable"):
        return None
    match = VERSION_RE.search(version_name)
    return match.group(1) if match else None


def metadata_date(metadata: dict[str, object] | None, fallback: str) -> str:
    build_time = str((metadata or {}).get("buildTime", ""))
    match = re.match(r"(\d{4}-\d{2}-\d{2})", build_time)
    if match:
        try:
            date.fromisoformat(match.group(1))
            return match.group(1)
        except ValueError:
            pass
    return fallback


def extract_version_section(markdown: str, version: str) -> str:
    headings = list(HEADING_RE.finditer(markdown))
    for index, heading in enumerate(headings):
        heading_versions = {match.group(1) for match in VERSION_RE.finditer(heading.group())}
        if version not in heading_versions:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        section = markdown[heading.start() : end].strip()
        return validate_single_section(section, version)

    # Early releases used titles such as "2.0 正式版" while stable.json carried
    # the full semantic version. Those files contained a single release only.
    if len(headings) <= 1:
        heading_versions = {
            match.group(1)
            for heading in headings
            for match in VERSION_RE.finditer(heading.group())
        }
        if heading_versions:
            raise ValueError(f"stable.md 中找不到 v{version} 对应章节")
        return validate_single_section(markdown.strip(), version)
    raise ValueError(f"stable.md 中找不到 v{version} 对应章节")


def validate_single_section(section: str, version: str) -> str:
    headings = HEADING_RE.findall(section)
    if len(headings) != 1:
        raise ValueError(f"v{version} 提取结果包含 {len(headings)} 个一级标题")
    return section + "\n"


def collect_releases() -> dict[str, ReleaseNote]:
    log = run(
        "git",
        "log",
        "--reverse",
        "--format=%H%x09%ad%x09%s",
        "--date=short",
        "--",
        "stable.md",
        "stable.json",
    ).stdout
    releases: dict[str, ReleaseNote] = {}
    expected_versions: set[str] = set()
    for line in log.splitlines():
        commit, commit_date, subject = line.split("\t", 2)
        subject_match = RELEASE_SUBJECT_RE.fullmatch(subject)
        if not subject_match:
            continue
        expected_versions.add(subject_match.group(1))
        metadata = parse_metadata(git_show(commit, "stable.json"))
        version = metadata_version(metadata) or subject_match.group(1)
        if version != subject_match.group(1):
            continue
        markdown = git_show(commit, "stable.md")
        if markdown is None:
            continue
        try:
            section = extract_version_section(markdown, version)
        except ValueError:
            continue
        releases[version] = ReleaseNote(
            version,
            metadata_date(metadata, commit_date),
            section,
        )

    # Allow exporting a newly prepared stable release before it is committed.
    current_metadata = parse_metadata((ROOT / "stable.json").read_text())
    current_version = metadata_version(current_metadata)
    if current_version:
        markdown = (ROOT / "stable.md").read_text()
        fallback = run("git", "log", "-1", "--format=%ad", "--date=short").stdout.strip()
        releases[current_version] = ReleaseNote(
            current_version,
            metadata_date(current_metadata, fallback),
            extract_version_section(markdown, current_version),
        )
    missing_versions = sorted(expected_versions - releases.keys(), key=version_key)
    if missing_versions:
        raise ValueError(f"以下正式版本没有可用日志: {', '.join(missing_versions)}")
    return releases


def version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def load_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        value = json.loads(MANIFEST_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def write_manifest(entries: dict[str, dict[str, str]]) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def require_tools() -> None:
    missing = [tool for tool in ("pandoc", "magick", "npx") if not shutil.which(tool)]
    if missing:
        raise RuntimeError(f"缺少图片导出工具: {', '.join(missing)}")
    probe = run("npx", "--no-install", "playwright", "--version", check=False)
    if probe.returncode != 0:
        raise RuntimeError("Playwright CLI 不可用，请先安装 playwright")


def render_image(markdown_path: Path, image_path: Path) -> None:
    css = """
html { background: #eceff1; }
body { box-sizing: border-box; width: 1120px; margin: 0 auto; padding: 76px 84px 88px;
  background: #fff; color: #202124; font-family: -apple-system, BlinkMacSystemFont,
  "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 24px; line-height: 1.75; }
h1 { margin: 0 0 34px; padding-bottom: 18px; border-bottom: 3px solid #202124;
  font-size: 42px; line-height: 1.35; }
h2 { margin: 44px 0 18px; padding-bottom: 8px; border-bottom: 1px solid #74777a;
  font-size: 31px; line-height: 1.45; }
h3 { font-size: 27px; }
p { margin: 14px 0; }
ul, ol { margin: 12px 0; padding-left: 1.5em; }
li { margin: 7px 0; }
blockquote { margin: 22px 0; padding: 10px 0 10px 22px; border-left: 5px solid #5f6368;
  color: #505357; }
blockquote > :first-child { margin-top: 0; }
blockquote > :last-child { margin-bottom: 0; }
code { padding: 2px 7px; border-radius: 4px; background: #f1f3f4; font-size: .9em; }
hr { margin: 42px 0; border: 0; border-top: 1px solid #bdc1c6; }
strong { font-weight: 700; }
"""
    with tempfile.TemporaryDirectory(prefix="stable-update-") as temp_dir:
        temp = Path(temp_dir)
        css_path = temp / "style.css"
        html_path = temp / "note.html"
        png_path = temp / "note.png"
        css_path.write_text(css)
        run(
            "pandoc",
            "--from=gfm",
            "--to=html5",
            "--standalone",
            f"--css={css_path}",
            f"--output={html_path}",
            str(markdown_path),
        )
        run(
            "npx",
            "--no-install",
            "playwright",
            "screenshot",
            "--browser=chromium",
            "--channel=chrome",
            "--full-page",
            "--viewport-size=1200,800",
            html_path.as_uri(),
            str(png_path),
        )
        run(
            "magick",
            str(png_path),
            "-background",
            "white",
            "-alpha",
            "remove",
            "-alpha",
            "off",
            "-quality",
            "92",
            str(image_path),
        )


def remove_stale_files(note: ReleaseNote) -> None:
    expected = {f"{note.stem}.md", f"{note.stem}.jpg"}
    pattern = f"哪吒美式-*-v{note.version}-更新日志.*"
    for directory in (MD_DIR, IMG_DIR):
        for path in directory.glob(pattern):
            if path.name not in expected and path.suffix in {".md", ".jpg"}:
                path.unlink()


def export(force: bool) -> tuple[int, int]:
    releases = collect_releases()
    if not releases:
        raise RuntimeError("未找到正式版更新日志")
    require_tools()
    MD_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    old_manifest = load_manifest()
    new_manifest: dict[str, dict[str, str]] = {}
    updated = 0

    for version in sorted(releases, key=version_key):
        note = releases[version]
        md_path = MD_DIR / f"{note.stem}.md"
        img_path = IMG_DIR / f"{note.stem}.jpg"
        remove_stale_files(note)
        unchanged = (
            not force
            and old_manifest.get(version, {}).get("digest") == note.digest
            and md_path.exists()
            and md_path.read_text() == note.markdown
            and img_path.exists()
        )
        if not unchanged:
            md_path.write_text(note.markdown)
            render_image(md_path, img_path)
            updated += 1
            print(f"已导出 v{version} ({note.release_date})")
        new_manifest[version] = {
            "date": note.release_date,
            "digest": note.digest,
            "markdown": md_path.name,
            "image": img_path.name,
        }

    write_manifest(new_manifest)
    return len(releases), updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="重新生成所有版本")
    args = parser.parse_args()
    try:
        total, updated = export(args.force)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"导出失败: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        return 1
    print(f"完成：共 {total} 个正式版，本次更新 {updated} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

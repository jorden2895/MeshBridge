from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY = "jorden2895/meshtelegram-bridge"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
USER_AGENT = "MeshTelegram-Bridge-Updater"


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    digest: str | None


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    page_url: str
    assets: tuple[ReleaseAsset, ...]


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", value.strip())
    if not match:
        raise UpdateError(f"無法辨識版本號：{value}")
    return tuple(int(part) for part in match.group(1).split("."))


def fetch_latest_release(timeout: float = 10) -> ReleaseInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        # LATEST_RELEASE_API is a source constant using HTTPS on api.github.com.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            payload = json.load(response)
    except Exception as exc:
        raise UpdateError(f"無法取得 GitHub Release：{exc}") from exc
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("GitHub 回傳的版本不是正式 Release")
    assets = tuple(
        ReleaseAsset(
            name=str(asset["name"]),
            url=str(asset["browser_download_url"]),
            digest=asset.get("digest"),
        )
        for asset in payload.get("assets", [])
        if isinstance(asset, dict)
        and asset.get("name")
        and asset.get("browser_download_url")
    )
    return ReleaseInfo(
        version=str(payload.get("tag_name", "")),
        page_url=str(payload.get("html_url", "")),
        assets=assets,
    )


def is_newer(release: ReleaseInfo, current_version: str) -> bool:
    return _version_tuple(release.version) > _version_tuple(current_version)


def download_asset(asset: ReleaseAsset, destination: Path, timeout: float = 60) -> Path:
    parsed_url = urlparse(asset.url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "github.com":
        raise UpdateError(f"{asset.name} 的下載網址不是受信任的 GitHub HTTPS 網址")
    request = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT})
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    try:
        # The initial asset URL is restricted to HTTPS on github.com above.
        with urllib.request.urlopen(request, timeout=timeout) as response, temporary.open(  # nosec B310
            "wb"
        ) as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        expected = asset.digest
        if not expected or not expected.startswith("sha256:"):
            raise UpdateError(f"{asset.name} 沒有 GitHub SHA-256 摘要，拒絕自動更新")
        if digest.hexdigest().lower() != expected.removeprefix("sha256:").lower():
            raise UpdateError(f"{asset.name} 的 SHA-256 驗證失敗")
        os.replace(temporary, destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def download_portable_release(release: ReleaseInfo, directory: Path) -> list[Path]:
    wanted = {"MeshTelegramBridge.exe", "MeshTelegramBridgeSettings.exe"}
    selected = {asset.name: asset for asset in release.assets if asset.name in wanted}
    missing = sorted(wanted - selected.keys())
    if missing:
        raise UpdateError(f"Release 缺少檔案：{', '.join(missing)}")
    update_dir = directory / ".update"
    update_dir.mkdir(parents=True, exist_ok=True)
    return [
        download_asset(selected[name], update_dir / name)
        for name in sorted(wanted)
    ]


def schedule_portable_install(files: list[Path], install_dir: Path) -> None:
    if os.name != "nt":
        raise UpdateError("自動安裝目前只支援 Windows 可攜版")
    if not getattr(sys, "frozen", False):
        raise UpdateError("原始碼執行模式不支援自動安裝")

    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = Path(tempfile.gettempdir()) / f"meshtelegram-update-{os.getpid()}.ps1"
    current_name = Path(sys.executable).name.casefold()
    ordered_files = sorted(
        files,
        key=lambda path: 0 if path.name.casefold() == current_name else 1,
    )
    operations = []
    for source in ordered_files:
        target = install_dir / source.name
        operations.append(
            f"$src={ps_quote(str(source))}; $dst={ps_quote(str(target))}; "
            "for($i=0;$i -lt 120;$i++){try{Move-Item -LiteralPath $src "
            "-Destination $dst -Force -ErrorAction Stop; break}catch{Start-Sleep 1}}"
        )
    restart_index = next(
        (
            index
            for index, source in enumerate(ordered_files)
            if source.name.casefold() == current_name
        ),
        -1,
    )
    script_lines = [
        f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue",
    ]
    for index, operation in enumerate(operations):
        script_lines.append(operation)
        if index == restart_index:
            script_lines.append(
                f"Start-Process -FilePath {ps_quote(str(sys.executable))}"
            )
    script.write_text(
        "\n".join(script_lines)
        + "\n"
        + "Remove-Item -LiteralPath $PSCommandPath -Force\n",
        encoding="utf-8-sig",
    )
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def should_check(state_path: Path, interval_hours: int) -> bool:
    try:
        data: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
        return time.time() - float(data.get("last_checked", 0)) >= interval_hours * 3600
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True


def record_check(state_path: Path) -> None:
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"last_checked": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, state_path)

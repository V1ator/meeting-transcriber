#!/usr/bin/env python3
"""Єдина точка керування незалежними модулями Meeting Transcriber."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline_utils import atomic_write_text

BASE = Path(__file__).resolve().parent
ENV_PATH = BASE / ".env"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
WATCHER_LABEL = "local.meeting-transcriber.watcher"
MIC_LABEL = "local.meeting-transcriber.mic-autostart"
TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Module:
    name: str
    env_key: str
    default: bool
    description: str


MODULES = {
    "audio": Module(
        "audio", "AUDIO_PIPELINE_ENABLED", True,
        "запис звуку, транскрипція та діаризація",
    ),
    "candidates": Module(
        "candidates", "CANDIDATE_EVALUATION_ENABLED", False,
        "оцінка кандидатів через локальний skill",
    ),
    "notion": Module(
        "notion", "NOTION_SYNC_ENABLED", False,
        "створення задач у Notion",
    ),
}
ALIASES = {
    "audio": "audio",
    "recording": "audio",
    "candidate": "candidates",
    "candidates": "candidates",
    "evaluation": "candidates",
    "notion": "notion",
}


def parse_bool(value: str, default: bool = False) -> bool:
    value = value.strip().split("#", 1)[0].strip().strip("'\"")
    return default if not value else value.casefold() in TRUE_VALUES


def read_env_values(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            values[key.strip()] = value
    return values


def module_states(path: Path = ENV_PATH) -> dict[str, bool]:
    values = read_env_values(path)
    return {
        name: parse_bool(values.get(module.env_key, ""), module.default)
        for name, module in MODULES.items()
    }


def update_env(changes: dict[str, bool], path: Path = ENV_PATH) -> None:
    """Оновлює лише module flags, не торкаючись секретів та інших опцій."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    pending = {
        MODULES[name].env_key: "true" if enabled else "false"
        for name, enabled in changes.items()
    }
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in pending:
            output.append(f"{key}={pending[key]}")
            seen.add(key)
        else:
            output.append(line)
    missing = [key for key in pending if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Керування модулями")
        output.extend(f"{key}={pending[key]}" for key in missing)
    atomic_write_text(path, "\n".join(output).rstrip() + "\n", mode=0o600)


def resolve_names(names: list[str]) -> list[str]:
    resolved: list[str] = []
    for raw in names:
        name = ALIASES.get(raw.casefold())
        if name is None:
            choices = ", ".join(MODULES)
            raise ValueError(f"Невідомий модуль {raw!r}. Доступні: {choices}")
        if name not in resolved:
            resolved.append(name)
    return resolved


def desired_launch_agents(states: dict[str, bool]) -> set[str]:
    labels = {WATCHER_LABEL}
    if states["audio"]:
        labels.add(MIC_LABEL)
    return labels


def _service_loaded(label: str) -> bool:
    target = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(
        ["launchctl", "print", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _bootout(label: str) -> None:
    target = f"gui/{os.getuid()}/{label}"
    subprocess.run(
        ["launchctl", "bootout", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _set_launchctl_enabled(label: str, enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    subprocess.run(
        ["launchctl", action, f"gui/{os.getuid()}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _bootstrap(label: str) -> bool:
    plist = PLIST_DIR / f"{label}.plist"
    if not plist.is_file():
        print(f"  ⚠️  немає {plist}; спочатку запустіть install.sh")
        return False
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        print(f"  ⚠️  не вдалося запустити {label}: {detail}")
        return False
    return True


def apply_modules(states: dict[str, bool] | None = None) -> bool:
    """Застосовує .env до launchd; Meet watcher потрібен усім модулям."""
    states = states or module_states()
    desired = desired_launch_agents(states)
    success = True
    for label in (WATCHER_LABEL, MIC_LABEL):
        if label in desired:
            _set_launchctl_enabled(label, True)
            if _service_loaded(label):
                _bootout(label)
            if _bootstrap(label):
                print(f"  ✅ {label} запущено")
            else:
                success = False
        else:
            if _service_loaded(label):
                _bootout(label)
            _set_launchctl_enabled(label, False)
            print(f"  ⏸  {label} зупинено")
    return success


def print_status(states: dict[str, bool] | None = None) -> None:
    states = states or module_states()
    for name, module in MODULES.items():
        state = "увімкнено" if states[name] else "вимкнено"
        icon = "✅" if states[name] else "⏸ "
        print(f"{icon} {name:<10} {state:<10} — {module.description}")
    print("\nФонові сервіси:")
    for label in (WATCHER_LABEL, MIC_LABEL):
        state = "працює" if _service_loaded(label) else "зупинено"
        print(f"  {label}: {state}")


def configure(*, apply: bool = True) -> int:
    states = module_states()
    print("Оберіть потрібні частини сервісу (Enter залишає поточний вибір):")
    selected: dict[str, bool] = {}
    for name, module in MODULES.items():
        default_label = "Y/n" if states[name] else "y/N"
        try:
            answer = input(f"  {module.description}? [{default_label}] ").strip().casefold()
        except EOFError:
            answer = ""
        selected[name] = states[name] if not answer else answer in TRUE_VALUES | {"y", "т", "так"}
    update_env(selected)
    print("\nКонфігурацію збережено.")
    if apply:
        return 0 if apply_modules(selected) else 1
    print_status(selected)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="показати стан модулів")
    for command in ("enable", "disable"):
        item = commands.add_parser(command, help=f"{command} один або кілька модулів")
        item.add_argument("modules", nargs="+", help="audio, candidates, notion")
        item.add_argument("--no-apply", action="store_true")
    configure_parser = commands.add_parser("configure", help="інтерактивний вибір")
    configure_parser.add_argument("--no-apply", action="store_true")
    commands.add_parser("apply", help="застосувати поточний .env до launchd")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        print_status()
        return 0
    if args.command == "configure":
        return configure(apply=not args.no_apply)
    if args.command == "apply":
        return 0 if apply_modules() else 1
    try:
        names = resolve_names(args.modules)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    enabled = args.command == "enable"
    update_env({name: enabled for name in names})
    action = "Увімкнено" if enabled else "Вимкнено"
    print(f"{action}: {', '.join(names)}")
    if not args.no_apply and not apply_modules():
        return 1
    print_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Terminal output and prompts. Every prompt shows its recommended default."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import questionary
from rich.console import Console
from rich.table import Table

console = Console(highlight=False)
NON_INTERACTIVE = False

STYLE = questionary.Style([("qmark", "fg:#5f87ff bold"), ("answer", "fg:#5fd75f")])


class MissingAnswer(SystemExit):
    def __init__(self, what: str) -> None:
        super().__init__(f"--non-interactive: no saved answer for '{what}'. Run ./deploy.sh without it once.")


def heading(text: str) -> None:
    console.rule(f"[bold]{text}")


def info(text: str) -> None:
    console.print(text)


def note(text: str) -> None:
    console.print(f"[dim]{text}[/dim]")


def success(text: str) -> None:
    console.print(f"[green]✔[/green] {text}")


def warn(text: str) -> None:
    console.print(f"[yellow]⚠ {text}[/yellow]")


def error(text: str) -> None:
    console.print(f"[red]✘ {text}[/red]")


def table(title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    t = Table(title=title, title_justify="left")
    for col in columns:
        t.add_column(col)
    for row in rows:
        t.add_row(*row)
    console.print(t)


def _ask(question: questionary.Question) -> Any:
    answer = question.ask()
    if answer is None:  # Ctrl-C
        raise SystemExit("Cancelled.")
    return answer


def text(
    prompt: str,
    default: str = "",
    validate: Callable[[str], bool | str] | None = None,
    help_text: str = "",
) -> str:
    if NON_INTERACTIVE:
        if default == "":
            raise MissingAnswer(prompt)
        return default
    if help_text:
        note(help_text)
    return _ask(
        questionary.text(prompt, default=str(default), validate=validate or (lambda _: True), style=STYLE)
    ).strip()


def confirm(prompt: str, default: bool = True, help_text: str = "") -> bool:
    if NON_INTERACTIVE:
        return default
    if help_text:
        note(help_text)
    return bool(_ask(questionary.confirm(prompt, default=default, style=STYLE)))


def select(prompt: str, choices: Sequence[tuple[str, str]], default: str, help_text: str = "") -> str:
    """choices: (value, label) pairs."""
    if NON_INTERACTIVE:
        return default
    if help_text:
        note(help_text)
    qchoices = [questionary.Choice(title=label, value=value) for value, label in choices]
    return _ask(questionary.select(prompt, choices=qchoices, default=default, style=STYLE))


def secret(prompt: str, confirm: bool = False, allow_empty: bool = False, min_length: int = 0) -> str:
    if NON_INTERACTIVE:
        raise MissingAnswer(prompt)

    def check(value: str) -> bool | str:
        if not value and allow_empty:
            return True
        if len(value) < max(min_length, 1):
            return f"Must be at least {max(min_length, 1)} characters."
        return True

    while True:
        value = _ask(questionary.password(prompt, validate=check, style=STYLE))
        if not confirm or value == _ask(questionary.password("Repeat to confirm", style=STYLE)):
            return value
        error("Entries didn't match, try again.")

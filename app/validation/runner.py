import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ValidationResult:
    ok: bool
    output: str


def _run(
    command: str,
    repo: Path,
) -> tuple[int, str]:

    result = subprocess.run(
        command,
        cwd=repo,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    return result.returncode, result.stdout


def validate(
    repo: Path,
    test_command: str | None,
) -> ValidationResult:

    outputs = []

    code, output = _run(
        "git diff --check",
        repo,
    )

    outputs.append(
        "=== git diff --check ===\n"
        + output
    )

    if code != 0:
        return ValidationResult(
            ok=False,
            output="\n".join(outputs),
        )

    if test_command:
        code, output = _run(
            test_command,
            repo,
        )

        outputs.append(
            f"=== {test_command} ===\n"
            + output
        )

        if code != 0:
            return ValidationResult(
                ok=False,
                output="\n".join(outputs),
            )

    return ValidationResult(
        ok=True,
        output="\n".join(outputs),
    )

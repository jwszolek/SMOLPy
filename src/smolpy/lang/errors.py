from __future__ import annotations


class SMOLSyntaxError(Exception):
    """Malformed .smol source — raised by the parser/transformer."""

    def __init__(self, file: str, line: int, message: str) -> None:
        self.file = file
        self.line = line
        self.message = message
        super().__init__(f"{file}:{line}: {message}")


class SMOLSemanticError(Exception):
    """Well-formed .smol source that violates a semantic rule — raised by the interpreter."""

    def __init__(self, file: str, line: int, message: str) -> None:
        self.file = file
        self.line = line
        self.message = message
        super().__init__(f"{file}:{line}: {message}")

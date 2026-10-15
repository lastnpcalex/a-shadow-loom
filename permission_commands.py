"""Small, non-executing command-template parser. Unsupported syntax stays ask-only.

This is not a shell evaluator. A single literal PowerShell -Command invocation
can expose a scoped template; pipelines, expansion and programs cannot.
"""
import shlex
import subprocess


def display_argv(argv):
    return subprocess.list2cmdline(argv)


def _words(text):
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def command_template(command, argv=None):
    try:
        if argv is not None:
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
                return None
            if display_argv(argv) != command:
                return None
            tokens = argv[:]
        else:
            tokens = _words(command)
        if not tokens or len(tokens) > 128:
            return None
        executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        wrapper = []
        if executable in {"powershell", "pwsh"}:
            lowered = [x.lower() for x in tokens]
            if "-command" not in lowered:
                return None
            at = lowered.index("-command")
            if any(x not in {"-noprofile", "-noninteractive", "-nologo"} for x in lowered[1:at]):
                return None
            if len(tokens) != at + 2:
                return None
            wrapper = tokens[:at + 1]
            script = tokens[-1].strip()
            if script.startswith("& "):
                script = script[2:].lstrip()  # literal call operator, not script evaluation
            tokens = _words(script)
        if not tokens or len(tokens) > 128:
            return None
        # Conservative even inside quoted operands. These programs may still
        # run after one-time approval; they cannot acquire a widened template.
        if any(any(c in token for c in ";&|<>`$%\r\n(){}*?#") for token in tokens):
            return None
        executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if executable in {"powershell", "pwsh", "cmd", "bash", "sh", "zsh", "eval", "iex", "invoke-expression", "."}:
            return None
        if any(x.lower() in {"-c", "-e", "--eval", "-command", "-encodedcommand"} for x in tokens[1:]):
            return None
        return tokens, wrapper
    except (ValueError, TypeError):
        return None


def literal_read_paths(command, argv=None):
    """Recognize one bounded PowerShell file-read form, not arbitrary scripts.

    No profiles, aliases, wildcard paths, providers, extra statements or
    evaluation. Callers still have to check cwd and EVERY returned path.
    Unknown syntax remains an ordinary execute request.
    """
    parsed = command_template(command, argv)
    if parsed is None:
        return None
    tokens, wrapper = parsed
    if (not wrapper or wrapper[0].lower() not in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
            or "-noprofile" not in [t.lower() for t in wrapper]):
        return None
    if len(tokens) < 3 or tokens[0].lower() != "get-content" or tokens[1].lower() != "-literalpath":
        return None
    path = tokens[2]
    if path.startswith("-") or any(c in path for c in ",[]@"):
        return None
    # PowerShell provider paths (Env:, Registry:, FileSystem::...) are not
    # filesystem paths relative to the workspace, even when pathlib says so.
    if ":" in path and not (len(path) > 3 and path[0].isalpha() and path[1] == ":"
                            and path[2] in "\\/" and ":" not in path[2:]):
        return None
    rest = tokens[3:]
    if rest and not (len(rest) == 1 and rest[0].lower() == "-raw"):
        if not (len(rest) == 2 and rest[0].lower() in {"-totalcount", "-tail"}
                and len(rest[1]) <= 5 and rest[1].isascii() and rest[1].isdigit() and 0 < int(rest[1]) <= 10000):
            return None
    return [path]

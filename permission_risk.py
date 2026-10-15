"""Recognize explicit destructive operations, not the intent of arbitrary programs.

No commands are executed. Quoted descriptions, file contents, recursive reads,
and opaque scripts are not evidence. A missing match is NOT a safety verdict.
"""
import shlex


def _name(token):
    return token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _tokens(text):
    # Retain quotes until AFTER splitting shell operators: echo ';' is not a
    # command separator, and echo 'git reset --hard' is not a Git invocation.
    lexer = shlex.shlex(text, posix=False, punctuation_chars=";&|\r\n")
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    lexer.escape = ""
    return list(lexer)


def _unquote(token):
    return token[1:-1] if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0] else token


def _operation(tokens):
    if not tokens:
        return ""
    name, args = _name(tokens[0]), tokens[1:]
    lower = [a.lower() for a in args]
    options = lower[:lower.index("--")] if "--" in lower else lower
    if any(a in {"--help", "-h", "-?", "/?"} for a in options):
        return ""
    if name in {"rm", "rmdir", "rd", "del", "erase", "unlink", "remove-item", "ri", "clear-content"} and args:
        if "-whatif" in lower:
            return ""
        return "Explicit file or directory deletion/clearing requires one-time review."
    if name in {"format", "format.com", "diskpart", "mkfs", "wipefs", "shred"} or name.startswith("mkfs."):
        return "Explicit disk formatting or data erasure requires one-time review."
    if name in {"copy-item", "move-item"} and "-force" in lower and "-whatif" not in lower:
        return "Forced copy/move may overwrite destination data; one-time review required."
    if name == "robocopy" and any(a in {"/mir", "/purge"} for a in lower) and "/l" not in lower:
        return "Robocopy mirror/purge can delete destination files; one-time review required."
    if name != "git":
        return ""
    # Skip only known global options. Never search arbitrary arguments for a
    # subcommand (git commit -m 'reset --hard' must remain an ordinary commit).
    while args and args[0].startswith("-"):
        if args[0] in {"-C", "-c", "--git-dir", "--work-tree"} and len(args) >= 2:
            args = args[2:]
        elif args[0] in {"--no-pager", "--literal-pathspecs"} or args[0].startswith(("--git-dir=", "--work-tree=")):
            args = args[1:]
        else:
            return ""
    if not args:
        return ""
    verb, rest = args[0], args[1:]
    options = rest[:rest.index("--")] if "--" in rest else rest
    short = lambda flag: any(a.startswith("-") and not a.startswith("--") and flag in a[1:] for a in options)
    destructive = (
        (verb == "reset" and "--hard" in options)
        or verb == "restore"
        or (verb == "clean" and (short("f") or "--force" in options) and not (short("n") or "--dry-run" in options))
        or (verb == "checkout" and "--" in rest)
        or (verb in {"checkout", "switch"} and (short("f") or any(a in {"--force", "--discard-changes"} for a in options)))
        or (verb == "stash" and rest[:1] in (["drop"], ["clear"]))
        or (verb == "branch" and (short("D") or short("d") or "--delete" in options))
        or (verb == "push" and not (short("n") or "--dry-run" in options) and (
            short("f") or short("d") or any(a == "--delete" or a == "--force" or a.startswith("--force-with-lease") for a in options)
            or any(a.startswith(("+", ":")) for a in rest)))
    )
    return "Explicit Git discard, deletion, or forced history update requires one-time review." if destructive else ""


def destructive_command_reason(command, argv=None, *, _depth=0):
    """Bounded shell/wrapper recognition; unsupported programs remain unmatched."""
    if _depth > 4:
        return ""
    try:
        if argv is not None:
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                return ""
            groups = [argv]  # native argv has argument boundaries, not shell operators
        elif isinstance(command, str) and len(command) <= 65536:
            groups, group = [], []
            for token in _tokens(command):
                if token and all(c in ";&|\r\n" for c in token):
                    if group:
                        groups.append(group)
                    group = []
                else:
                    group.append(_unquote(token))
            groups.append(group)
        else:
            return ""
        for tokens in groups:
            if not tokens or len(tokens) > 4096:
                continue
            name = _name(tokens[0])
            flags = {"powershell": {"-command", "-c"}, "pwsh": {"-command", "-c"},
                     "cmd": {"/c", "/k"}, "bash": {"-c", "-lc"}, "sh": {"-c", "-lc"}, "zsh": {"-c", "-lc"}}.get(name)
            if flags:
                index = next((i for i, t in enumerate(tokens[1:], 1) if t.lower() in flags), None)
                if index is not None and len(tokens) == index + 2:
                    reason = destructive_command_reason(tokens[-1], _depth=_depth + 1)
                else:
                    reason = ""  # profiles, encoded code and arbitrary wrappers aren't interpreted
            else:
                reason = _operation(tokens)
            if reason:
                return reason
    except (ValueError, TypeError):
        pass
    return ""

"""API keys that live in the macOS login Keychain, never in a file.

The Caddie's Meta Model API key (Muse Spark) is a generic password in the login Keychain: service
"golf-analytics.muse-spark", account "muse-spark". Shane adds it once himself (SETUP_HELP has the one
command); nothing in this app ever asks for, accepts, prints or stores the key. It is read at call time
by running /usr/bin/security (an argument list, no shell) and only its stripped stdout is used.

- `MUSE_API_KEY` in the environment overrides the Keychain (tests and CI). It is never taken from .env:
  golf.config.load_dotenv skips it, so the key can't live in a project file.
- Only get_secret() retrieves the secret itself (`-w`), and only the Caddie's ask() calls it, when a
  question is sent. Status displays use secret_source() / has_secret(), which run security WITHOUT -w:
  they learn whether the item exists (exit 0 / 44) and never load the secret into this process.
- Every error message here is written without the key or any part of it: security's stdout is never
  echoed, and errors are raised after the `except` block, so no chained exception (__context__) carries
  security's output along.
- Nothing is cached, on disk or in memory: every get_secret() asks the Keychain again, so a replaced key
  is used from the next question on.
- Tests inject a fake runner (golf.secrets.RUNNER, or runner=...) and never touch the real Keychain.
- Any process running as Shane can run the same `security` command without a prompt (the item trusts
  /usr/bin/security). The Keychain keeps the key out of files, iCloud and git, not away from local tools.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

SECURITY = "/usr/bin/security"
NOT_FOUND_EXIT = 44            # security(1): errSecItemNotFound, "The specified item could not be found"
TIMEOUT_SECONDS = 15           # a locked Keychain can put up a password dialog; don't hang forever on it


@dataclass(frozen=True)
class SecretSpec:
    service: str
    account: str
    env_var: str
    label: str


SECRETS: dict[str, SecretSpec] = {
    "muse-spark": SecretSpec(service="golf-analytics.muse-spark", account="muse-spark", env_var="MUSE_API_KEY",
                             label="Meta Model API key (Muse Spark)"),
}

_MUSE = SECRETS["muse-spark"]
SETUP_COMMAND = f"security add-generic-password -U -s {_MUSE.service} -a {_MUSE.account} -w"
REMOVE_COMMAND = f"security delete-generic-password -s {_MUSE.service} -a {_MUSE.account}"
SETUP_HELP = f"""\
The Caddie answers with Meta's Muse Spark model through the Meta Model API, using your own API key.
The key lives in your macOS login Keychain, never in a file of this project.

  1. Copy the key from the API keys tab at https://dev.meta.ai
  2. In Terminal, run this exactly as shown. It asks for the key: paste it and press Return (nothing shows
     as you paste). When it asks you to retype it, paste it again and press Return.

       {SETUP_COMMAND}

     Never type the key after -w on the command line: it would land in your shell history.
  3. Check it: golf caddie status   (says whether a key is set and where from; it never shows the key)

To replace the key, run the same command again (-U updates the entry). To remove it:
  {REMOVE_COMMAND}
If macOS asks whether "security" may use the Keychain item, click Always Allow.
MUSE_API_KEY in the environment overrides the Keychain (meant for tests; it is ignored in .env)."""

# runner(args) -> CompletedProcess with returncode and stdout. Tests replace RUNNER; None = the real tool.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
RUNNER: Runner | None = None


class SecretError(RuntimeError):
    """The Keychain could not be read (locked, access denied, timed out). The message never holds the key."""


def _spec(name: str) -> SecretSpec:
    try:
        return SECRETS[name]
    except KeyError:
        raise ValueError(f"Unknown secret {name!r}; known: {', '.join(SECRETS)}") from None


def _default_runner(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(args), shell=False, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                          check=False, stdin=subprocess.DEVNULL)


def keychain_args(name: str) -> list[str]:
    """security arguments that print the secret itself (-w). Only get_secret() uses them."""
    return presence_args(name) + ["-w"]


def presence_args(name: str) -> list[str]:
    """security arguments that only look the item up: without -w (or -g) it prints the item's attributes,
    never the secret, and exits 0 when found, 44 when not."""
    spec = _spec(name)
    return [SECURITY, "find-generic-password", "-s", spec.service, "-a", spec.account]


def _from_env(spec: SecretSpec, env: Mapping[str, str] | None) -> str | None:
    value = (os.environ if env is None else env).get(spec.env_var)
    return value.strip() if value and value.strip() else None


def _run_security(args: list[str], runner: Runner | None) -> "subprocess.CompletedProcess[str] | None":
    """security's result, or None when the tool isn't there (not a Mac). A failure to run it raises
    SecretError; the error is built inside the except block and raised after it, so it has no
    __context__ (a TimeoutExpired carries security's stdout, which with -w is the key)."""
    run = runner or RUNNER or _default_runner
    failure: SecretError | None = None
    try:
        return run(args)
    except FileNotFoundError:           # no /usr/bin/security: not a Mac, so there is no Keychain entry
        return None
    except subprocess.TimeoutExpired:
        failure = SecretError("The macOS Keychain did not answer in time (is it locked, or is a password dialog "
                              "waiting?). Unlock it and try again.")
    except OSError as e:
        failure = SecretError(f"Could not run the macOS security tool ({type(e).__name__}).")
    raise failure


def _refused(code: int) -> SecretError:
    return SecretError(f"The macOS Keychain did not hand over the key (security exited with {code}). If macOS "
                       "asked whether \"security\" may use the item, click Always Allow; if the login Keychain "
                       "is locked, unlock it. Then try again.")


def _from_keychain(name: str, runner: Runner | None) -> str | None:
    proc = _run_security(keychain_args(name), runner)
    if proc is None:
        return None
    code = getattr(proc, "returncode", 1)
    if code == NOT_FOUND_EXIT:
        return None
    if code != 0:
        raise _refused(code)
    value = (getattr(proc, "stdout", "") or "").strip()
    return value or None


def _in_keychain(name: str, runner: Runner | None) -> bool:
    """Whether the Keychain item exists, without retrieving the secret (no -w; stdout is discarded)."""
    proc = _run_security(presence_args(name), runner)
    if proc is None:
        return False
    code = getattr(proc, "returncode", 1)
    if code == NOT_FOUND_EXIT:
        return False
    if code != 0:
        raise _refused(code)
    return True


def get_secret(name: str, *, runner: Runner | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """The secret, or None when it isn't set. The environment variable wins over the Keychain.

    Raises SecretError (key-free message, no chained exception) when the Keychain exists but can't be read."""
    spec = _spec(name)
    return _from_env(spec, env) or _from_keychain(name, runner)


def secret_source(name: str, *, runner: Runner | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """Where the secret would come from ('environment variable MUSE_API_KEY' or 'macOS Keychain'), or None
    when it isn't set. For status displays: it says where, never what, and never retrieves the secret
    from the Keychain (a presence check only). Raises SecretError when the Keychain can't be searched."""
    spec = _spec(name)
    if _from_env(spec, env):
        return f"environment variable {spec.env_var}"
    return "macOS Keychain" if _in_keychain(name, runner) else None


def has_secret(name: str, *, runner: Runner | None = None, env: Mapping[str, str] | None = None) -> bool:
    """Whether the secret is set (presence only, never the secret). An unreadable Keychain counts as not set."""
    try:
        return secret_source(name, runner=runner, env=env) is not None
    except SecretError:
        return False

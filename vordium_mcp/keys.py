"""Where the agent key lives, and how it is read: only here, only when signing, never logged, returned or sent.

Order of sources (the first one set up is used):
  1. the OS keychain — macOS Keychain, Windows Credential Manager, or the Secret Service (libsecret) on Linux — through the
     optional ``keyring`` package (installed with ``vordium-mcp[trade]``). One entry per network and owner:
     service ``vordium-mcp``, account ``<network>:<owner>``. Put the key there with ``vordium-mcp key store``.
  2. ``--agent-key-file PATH``: a file holding the key as hex (mode 600), or an encrypted keystore (JSON, the standard
     Ethereum V3 format) whose passphrase is in a file named by ``--agent-key-passphrase-file`` (mode 600).
A key file that other users can read is refused when the server starts, not only when it signs.
"""
import json, os, re, stat

KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
SERVICE = "vordium-mcp"


class KeyError_(RuntimeError):
    """A key problem, named without the key."""


def entry(network, owner):
    return f"{network}:{str(owner).lower()}"


def _keyring():
    try:
        import keyring                                             # optional: vordium-mcp[trade]
        from keyring.errors import KeyringError                    # noqa: F401
    except Exception:
        return None
    return keyring


def keychain_available(kr=None):
    """True when an OS keychain backend is usable here (not the 'fail' or plaintext backends)."""
    kr = kr if kr is not None else _keyring()
    if kr is None:
        return False
    try:
        b = kr.get_keyring()
    except Exception:
        return False
    backends = getattr(b, "backends", None) or [b]                 # a chain of backends counts when one of them is real
    return any(not _weak(x) for x in backends)


def _weak(backend):
    name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
    return any(w in name for w in ("fail", "null", "plaintext"))


def keychain_get(network, owner, kr=None):
    kr = kr if kr is not None else _keyring()
    if kr is None or not owner or not keychain_available(kr):     # a plaintext or failing backend is never read
        return None
    try:
        v = kr.get_password(SERVICE, entry(network, owner))
    except Exception:
        return None
    return v.strip() if isinstance(v, str) and v.strip() else None


def keychain_put(network, owner, key, kr=None):
    kr = kr if kr is not None else _keyring()
    if kr is None:
        raise KeyError_('no OS keychain here: install it with pip install "vordium-mcp[trade]", or keep the key in a file (mode 600)')
    if not keychain_available(kr):
        raise KeyError_("this machine has no secure OS keychain (only a plaintext or failing one): keep the key in a file "
                        "(mode 600) or an encrypted keystore instead")
    k = key.strip()
    if not KEY.match(k):
        raise KeyError_("that is not one 32-byte hex key")
    try:
        kr.set_password(SERVICE, entry(network, owner), k if k.startswith("0x") else "0x" + k)
    except Exception as e:
        raise KeyError_(f"the OS keychain refused it ({type(e).__name__}); unlock it, or keep the key in a file (mode 600)")


def keychain_delete(network, owner, kr=None):
    kr = kr if kr is not None else _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE, entry(network, owner))
        return True
    except Exception:
        return False


def check_private_file(path, what="the agent key file"):
    """Refuse a file other users can read (or that is not a regular file). Errors name the path, never the content."""
    p = os.path.expanduser(str(path))
    try:
        st = os.stat(p)
    except OSError:
        raise KeyError_(f"{what} {p} cannot be read")
    if not stat.S_ISREG(st.st_mode):
        raise KeyError_(f"{what} {p} is not a regular file")
    if os.name == "posix" and st.st_mode & 0o077:
        raise KeyError_(f"{what} {p} can be read by other users; run: chmod 600 {p}")
    return p


def _from_file(path, passphrase_file=None):
    p = check_private_file(path)
    with open(p, "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw.startswith("{"):                                          # an encrypted keystore (Ethereum V3 JSON)
        if not passphrase_file:
            raise KeyError_(f"{p} is an encrypted keystore: name its passphrase file with --agent-key-passphrase-file")
        pp = check_private_file(passphrase_file, "the passphrase file")
        with open(pp, "r", encoding="utf-8") as fh:
            secret = fh.read().rstrip("\r\n")
        try:
            from eth_account import Account
            key = Account.decrypt(json.loads(raw), secret)
        except Exception:
            raise KeyError_(f"the keystore {p} could not be opened with that passphrase")
        return "0x" + bytes(key).hex()
    if not KEY.match(raw):
        raise KeyError_(f"the agent key file {p} does not hold one 32-byte hex key")
    return raw if raw.startswith("0x") else "0x" + raw


def source(cfg, kr=None):
    """Which source the server will sign with: ('keychain', None) | ('file', path) | (None, why)."""
    if cfg.agent_key_file:
        return "file", cfg.agent_key_file
    if cfg.owner and keychain_get(cfg.network, cfg.owner, kr) is not None:
        return "keychain", None
    if not cfg.owner:
        return None, "set --owner (the account whose registered agent signs)"
    return None, (f"no agent key: store it in the OS keychain with `vordium-mcp key store --owner {cfg.owner} --network "
                  f"{cfg.network}`, or give --agent-key-file")


def read(cfg, kr=None):
    """The agent key, for one signing call only."""
    kind, where = source(cfg, kr)
    if kind == "file":
        return _from_file(where, getattr(cfg, "agent_key_passphrase_file", None))
    if kind == "keychain":
        k = keychain_get(cfg.network, cfg.owner, kr)
        if k is None or not KEY.match(k):
            raise KeyError_("the keychain entry for this owner does not hold one 32-byte hex key")
        return k if k.startswith("0x") else "0x" + k
    raise KeyError_(where)

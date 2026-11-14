"""Keep collection and tests away from the caller's memory and credentials."""
import os
import tempfile

_KEYS = ('HOME', 'USERPROFILE', 'HIVEMIND_DB', 'SEM_DB_PATH', 'CLAUDE_MEMORY_DIR',
         'AGENT_KIT_CLOUD', 'HM_ADJUDICATE', 'HM_NO_LLM', 'HM_NO_SEM')
_original = {}
_home = None


def pytest_configure(config):
    global _home
    _original.update({key: os.environ.get(key) for key in _KEYS})
    _home = tempfile.TemporaryDirectory(prefix='memory-tests-')
    for key in _KEYS:
        os.environ.pop(key, None)
    os.environ.update(HOME=_home.name, USERPROFILE=_home.name)
    for key in ('HM_NO_LLM', 'HM_NO_SEM'):
        if _original[key] is not None:
            os.environ[key] = _original[key]


def pytest_unconfigure(config):
    for key in _KEYS:
        value = _original.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if _home is not None:
        _home.cleanup()

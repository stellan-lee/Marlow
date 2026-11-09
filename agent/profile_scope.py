"""Small context-local maps for existing host registries and MCP connections."""

from collections.abc import MutableMapping, MutableSet
from marlow_constants import get_marlow_home


def profile_key():
    return str(get_marlow_home().absolute())


class ProfileMap(MutableMapping):
    def __init__(self):
        self.profiles = {}

    def current(self):
        return self.profiles.setdefault(profile_key(), {})

    def __getitem__(self, key):
        return self.current()[key]

    def __setitem__(self, key, value):
        self.current()[key] = value

    def __delitem__(self, key):
        del self.current()[key]

    def __iter__(self):
        return iter(dict(self.current()))

    def __len__(self):
        return len(self.current())

    def copy(self):
        return dict(self.current())


class ProfileSet(MutableSet):
    def __init__(self):
        self.profiles = {}

    def current(self):
        return self.profiles.setdefault(profile_key(), set())

    def __contains__(self, key):
        return key in self.current()

    def __iter__(self):
        return iter(set(self.current()))

    def __len__(self):
        return len(self.current())

    def add(self, key):
        self.current().add(key)

    def discard(self, key):
        self.current().discard(key)

    def clear(self):
        self.current().clear()


class ScopedToolMap(ProfileMap):
    """Built-ins are shared; installed plugin/MCP entries are profile local."""

    def __init__(self):
        super().__init__()
        self.builtins = {}

    def current(self):
        return dict(self.builtins, **self.profiles.get(profile_key(), {}))

    def __setitem__(self, key, value):
        owner = getattr(value, "profile_scope", None)
        target = self.profiles.setdefault(owner, {}) if owner else self.builtins
        target[key] = value

    def __delitem__(self, key):
        owned = self.profiles.get(profile_key(), {})
        if key in owned:
            del owned[key]
        else:
            del self.builtins[key]

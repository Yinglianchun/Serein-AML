"""Optional contributions, loaded only by an explicitly enabled factory.

Implementations belong in separate modules. Do not import them here. A factory
receives shared Services and its settings; it must not start background work.
The future server owns execution of registered jobs and request-time hooks.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Contributions:
    tools: dict[str, Callable] = field(default_factory=dict)
    prompt_hooks: dict[str, Callable] = field(default_factory=dict)
    jobs: dict[str, Callable] = field(default_factory=dict)

"""The AppDriver interface. The orchestrator never touches GUI details."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Popup:
    text: str
    buttons: list[str] = field(default_factory=list)
    button_points: dict[str, tuple[float, float]] = field(default_factory=dict)


class AppDriver(ABC):
    name: str = "app"

    @abstractmethod
    def focus(self) -> None:
        """Bring the app window to the front and verify its identity."""

    @abstractmethod
    def new_session(self) -> None:
        """Start a fresh chat and verify it is empty."""

    @abstractmethod
    def select_model(self, name: str, effort: str) -> None:
        """Select model/effort and verify the visible label."""

    @abstractmethod
    def ring(self, pointer_line: str) -> None:
        """Type the one-line pointer and submit it."""

    @abstractmethod
    def read_context_pct(self) -> float | None:
        """Read the app's context indicator, or None when it has none."""

    @abstractmethod
    def is_busy(self) -> bool | None:
        """True while generating/running; None when unknown."""

    @abstractmethod
    def read_popup(self) -> Popup | None:
        """Return a visible approval popup, if any."""

    def verify_sent(self) -> bool | None:
        """Verify the last pointer was submitted. None when vision is unavailable."""
        return None

    def click_popup(self, popup: Popup, button: str) -> None:
        raise NotImplementedError("this driver cannot click popups")

    def wait_for_idle(self, seconds: int) -> None:
        """Block until the human has been idle long enough."""
        return None

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .inference import HPSv3RewardInferencer

__all__ = ["HPSv3RewardInferencer"]


def __getattr__(name: str):
    if name == "HPSv3RewardInferencer":
        from .inference import HPSv3RewardInferencer

        return HPSv3RewardInferencer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

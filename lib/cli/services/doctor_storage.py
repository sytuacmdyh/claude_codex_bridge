from __future__ import annotations

from storage_classification import summarize_storage


def doctor_storage_summary(context) -> dict[str, object]:
    return summarize_storage(context)


__all__ = ['doctor_storage_summary']

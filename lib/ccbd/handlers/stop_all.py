from __future__ import annotations

def build_stop_all_handler(app):
    def handle(payload: dict) -> dict:
        forced = bool(payload.get('force'))
        summary, terminated_jobs = app.prepare_project_stop(
            force=forced,
            trigger='stop_all',
            reason='stop_all',
        )

        def _after_response() -> None:
            app.finalize_project_stop(
                summary=summary,
                terminated_jobs=terminated_jobs,
                trigger='stop_all',
                forced=forced,
                reason='stop_all',
                clear_start_policy=True,
            )

        return summary.to_record(), _after_response

    return handle

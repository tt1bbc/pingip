def run_batch_build_task(job_names):
    from . import create_app
    from .jenkins_builds import run_batch_build

    app = create_app(start_scheduler=False)
    with app.app_context():
        run_batch_build(job_names)


def run_change_plan_build_schedule_task(schedule_id):
    from . import create_app
    from .jenkins_builds import run_change_plan_build_schedule

    app = create_app(start_scheduler=False)
    with app.app_context():
        run_change_plan_build_schedule(schedule_id)

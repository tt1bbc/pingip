def get_rq_queue(app=None):
    from flask import current_app
    from redis import Redis
    from rq import Queue

    app = app or current_app
    queue = app.extensions.get("rq_queue")
    if queue is not None:
        return queue

    redis_conn = Redis.from_url(app.config["REDIS_URL"])
    queue = Queue(app.config["RQ_QUEUE_NAME"], connection=redis_conn)
    app.extensions["rq_queue"] = queue
    return queue

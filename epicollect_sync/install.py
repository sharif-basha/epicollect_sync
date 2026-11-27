from epicollect_sync.targets import ensure_role


def before_install():
	ensure_role()


def after_migrate():
	ensure_role()

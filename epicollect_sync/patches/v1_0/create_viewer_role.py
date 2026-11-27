from epicollect_sync.targets import ensure_role


def execute():
	# Before model sync: Epicollect Entry's permissions name this role.
	ensure_role()

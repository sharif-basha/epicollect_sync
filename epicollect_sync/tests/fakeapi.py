"""Fake Epicollect5 API: serves a project definition and entries with uploaded_at filtering, ASC sort and page
numbers, like the real export API. Swapped in for the HTTP session in tests."""

import copy


class Resp:
    def __init__(s, status, body, headers=None):
        s.status_code, s._b, s.headers = status, body, headers or {}

    def json(s):
        return copy.deepcopy(s._b)

    def raise_for_status(s):
        if s.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {s.status_code}")


DEFINITION = {
    "data": {
        "project": {
            "name": "Household Survey",
            "slug": "household-survey",
            "forms": [
                {
                    "ref": "F1",
                    "name": "Household",
                    "inputs": [
                        {"ref": "F1_age", "type": "integer", "question": "Age of head"},
                        {"ref": "F1_dob", "type": "date", "question": "Date of birth", "datetime_format": "dd/MM/YYYY"},
                        {"ref": "F1_loc", "type": "location", "question": "House location"},
                        {
                            "ref": "F1_br",
                            "type": "branch",
                            "question": "Livestock",
                            "branch": [
                                {"ref": "F1_br_kind", "type": "dropdown", "question": "Animal"},
                                {"ref": "F1_br_n", "type": "integer", "question": "How many"},
                            ],
                        },
                        {"ref": "F1_name", "type": "text", "question": "Head name"},
                    ],
                },
                {
                    "ref": "F2",
                    "name": "Member",
                    "inputs": [
                        {"ref": "F2_name", "type": "text", "question": "Member name"},
                        {"ref": "F2_age", "type": "integer", "question": "Member age"},
                    ],
                },
            ],
        }
    },
    "meta": {
        "project_mapping": [
            {
                "map_index": 0,
                "is_default": True,
                "forms": {
                    "F1": {
                        "F1_age": {"map_to": "1_age"},
                        "F1_dob": {"map_to": "2_dob"},
                        "F1_loc": {"map_to": "3_loc"},
                        "F1_br": {
                            "map_to": "4_br",
                            "branch": {"F1_br_kind": {"map_to": "5_kind"}, "F1_br_n": {"map_to": "6_n"}},
                        },
                        "F1_name": {"map_to": "7_name"},
                    },
                    "F2": {"F2_name": {"map_to": "1_mname"}, "F2_age": {"map_to": "2_mage"}},
                },
            }
        ]
    },
}


class FakeAPI:
    def __init__(s):
        s.data = {}  # (form_ref, branch_ref) -> {uuid: entry}
        s.token_calls = 0
        s.token_status = []  # (status, headers) answers for the next token requests
        s.unauthorized = False  # answer 401 to every export request
        s.calls = []
        s.project = copy.deepcopy(DEFINITION)
        s.fail = []  # status codes to return before succeeding
        s.fail_when = None  # params -> True to answer 503 (e.g. fail one branch only)
        s.on_request = None  # hook(call_no) to change data mid-run

    def add(s, form, uuid, uploaded, branch=None, **kw):
        key = "ec5_branch_uuid" if branch else "ec5_uuid"
        e = {key: uuid, "title": uuid, "created_at": uploaded, "uploaded_at": uploaded, "created_by": "a@b.c", **kw}
        s.data.setdefault((form, branch), {})[uuid] = e
        return e

    def post(s, url, json=None, timeout=None):
        s.token_calls += 1
        if s.token_status:
            status, headers = s.token_status.pop(0)
            return Resp(status, {}, headers)
        return Resp(200, {"access_token": f"tok{s.token_calls}", "expires_in": 7200})

    def get(s, url, headers=None, params=None, timeout=None):
        params = dict(params or {})
        s.calls.append((url, params))
        if s.unauthorized:
            return Resp(401, {})
        if s.on_request:
            s.on_request(len(s.calls))
        if s.fail:
            return Resp(s.fail.pop(0), {})
        if s.fail_when and s.fail_when(params):
            return Resp(503, {})
        if "/export/project/" in url:
            return Resp(200, s.project) if s.project else Resp(404, {})
        form = params.get("form_ref") or s.project["data"]["project"]["forms"][0]["ref"]
        rows = sorted(s.data.get((form, params.get("branch_ref")), {}).values(), key=lambda e: e["uploaded_at"])
        if params.get("filter_from"):
            rows = [e for e in rows if e["uploaded_at"] >= params["filter_from"]]
        per, page = params["per_page"], params["page"]
        last = max(1, -(-len(rows) // per))
        return Resp(
            200,
            {
                "meta": {"last_page": last, "per_page": per, "current_page": page},
                "data": {"entries": rows[(page - 1) * per : page * per]},
            },
        )


def ts(i):
    return f"2026-01-01T10:{i // 60:02d}:{i % 60:02d}.000Z"

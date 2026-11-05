from types import SimpleNamespace

from googleapiclient.errors import HttpError


class Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Gmail:
    def __init__(self):
        self.ids = ["a", "b"]
        self.history_ids = []
        self.label_history_ids = []
        self.expired = False
        self.history_start = []
        self.full_list_calls = 0
        self.get_ids = []
        self.preflight_ids = []
        self.preflight_creation_errors = {}
        self.preflight_errors = {}
        self.last_get_id = None
        self.query_matches = {"a", "b"}
        self.message_labels = {}
        self.full_query = "jobs"
        self.expected_labels = ["job-alerts"]
        self.profile_result = {"emailAddress": "user@example.com", "historyId": "100"}

    def users(self):
        return self

    def messages(self):
        return self

    def history(self):
        return self

    def getProfile(self, **kwargs):
        return Request(self.profile_result)

    def history_message(self, message_id):
        return {"message": {"id": message_id, "threadId": "thread"}}

    def list(self, **kwargs):
        if "startHistoryId" in kwargs:
            self.history_start.append(kwargs["startHistoryId"])
            if self.expired:
                return Request(
                    HttpError(SimpleNamespace(status=404, reason="expired"), b"{}")
                )
            if not kwargs["pageToken"]:
                return Request(
                    {
                        "history": [
                            {
                                "messagesAdded": [
                                    self.history_message(i)
                                    for i in self.history_ids[:1]
                                ]
                            }
                        ],
                        "nextPageToken": "next",
                    }
                )
            return Request(
                {
                    "history": [
                        {
                            "messagesAdded": [
                                self.history_message(i) for i in self.history_ids[1:]
                            ],
                            "labelsAdded": [
                                {
                                    "message": {"id": i, "threadId": "thread"},
                                    "labelIds": self.message_labels.get(
                                        i, ["job-alerts"]
                                    ),
                                }
                                for i in self.label_history_ids
                            ],
                        }
                    ]
                }
            )
        if kwargs["q"] != self.full_query:
            assert kwargs["q"] == (
                f"({self.full_query}) after:1699999999 before:1700000001"
            )
            assert kwargs["labelIds"] == self.expected_labels
            assert kwargs["includeSpamTrash"] is False
            matches = (
                [{"id": self.last_get_id}]
                if self.last_get_id in self.query_matches
                else []
            )
            return Request({"messages": matches})
        assert kwargs["labelIds"] == self.expected_labels
        assert kwargs["includeSpamTrash"] is False
        self.full_list_calls += 1
        if not kwargs["pageToken"]:
            return Request(
                {"messages": [{"id": self.ids[0]}], "nextPageToken": "next"}
            )
        return Request({"messages": [{"id": i} for i in self.ids[1:]]})

    def get(self, **kwargs):
        if kwargs["format"] == "minimal":
            assert kwargs["fields"] == "id,labelIds"
            assert "metadataHeaders" not in kwargs
            self.preflight_ids.append(kwargs["id"])
            if kwargs["id"] in self.preflight_creation_errors:
                raise self.preflight_creation_errors[kwargs["id"]]
            if kwargs["id"] in self.preflight_errors:
                return Request(self.preflight_errors[kwargs["id"]])
            return Request(
                {
                    "id": kwargs["id"],
                    "labelIds": self.message_labels.get(
                        kwargs["id"], ["job-alerts"]
                    ),
                }
            )
        assert kwargs["format"] == "metadata"
        assert "fields" not in kwargs
        self.last_get_id = kwargs["id"]
        self.get_ids.append(kwargs["id"])
        return Request(
            {
                "threadId": "thread",
                "internalDate": "1700000000000",
                "snippet": "s" * 600,
                "labelIds": self.message_labels.get(kwargs["id"], ["job-alerts"]),
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Job"},
                        {"name": "Message-ID", "value": "<id>"},
                    ]
                },
            }
        )

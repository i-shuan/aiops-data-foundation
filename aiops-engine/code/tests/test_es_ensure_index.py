"""core.es_client.ES.ensure_index：兩個工作同一秒搶建同一個 index（排程器讓 rules、detect、alert 同時起跑），輸的那個收到
resource_already_exists_exception 不算錯、改補 mapping；其他 4xx 照樣是錯。用假的 session，不連 ES。"""
import unittest

from core.es_client import ES


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text, self.content = status, text, text.encode()

    def json(self):
        return {}


class FakeSession:
    def __init__(self, head=404, put=201, put_text=""):
        self.calls = []
        self._head, self._put, self._put_text = head, put, put_text

    def head(self, url, **kw):
        self.calls.append(("HEAD", url)); return Resp(self._head)

    def put(self, url, **kw):
        self.calls.append(("PUT", url)); return Resp(self._put, self._put_text)

    def request(self, method, url, **kw):
        self.calls.append((method, url)); return Resp(200, "{}")


BODY = {"mappings": {"properties": {"a": {"type": "keyword"}}}}


def es_with(session):
    es = ES.__new__(ES)
    es.url, es.s = "http://es", session
    return es


class EnsureIndexTest(unittest.TestCase):
    def test_creates_when_missing(self):
        s = FakeSession(head=404, put=200)
        es_with(s).ensure_index("aiops-x", BODY)
        self.assertEqual([c[0] for c in s.calls], ["HEAD", "PUT"])

    def test_lost_race_falls_back_to_mapping_update(self):
        s = FakeSession(head=404, put=400, put_text='{"error":{"type":"resource_already_exists_exception"}}')
        es_with(s).ensure_index("aiops-x", BODY)
        self.assertEqual(s.calls, [("HEAD", "http://es/aiops-x"), ("PUT", "http://es/aiops-x"), ("PUT", "http://es/aiops-x/_mapping")])

    def test_other_errors_still_raise(self):
        s = FakeSession(head=404, put=403, put_text='{"error":{"type":"security_exception"}}')
        with self.assertRaises(RuntimeError):
            es_with(s).ensure_index("aiops-x", BODY)

    def test_existing_index_only_updates_mapping(self):
        s = FakeSession(head=200)
        es_with(s).ensure_index("aiops-x", BODY)
        self.assertEqual([c[0] for c in s.calls], ["HEAD", "PUT"])
        self.assertTrue(s.calls[-1][1].endswith("/_mapping"))


if __name__ == "__main__":
    unittest.main()

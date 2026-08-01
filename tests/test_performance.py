import inspect
import unittest

from main import Application
from payments import Zarinpal
from supplier import G2Bulk


class PerformanceSafetyTests(unittest.TestCase):
    def test_polling_processes_chats_concurrently_but_uses_ordered_wrapper(self):
        source = inspect.getsource(Application.polling_loop)
        wrapper = inspect.getsource(Application.process_payload_ordered)
        self.assertIn("asyncio.gather", source)
        self.assertIn("process_payload_ordered", source)
        self.assertIn("async with lock", wrapper)
        self.assertIn("_update_slots", wrapper)

    def test_financial_http_clients_reuse_bounded_sessions(self):
        supplier_start = inspect.getsource(G2Bulk.start)
        supplier_call = inspect.getsource(G2Bulk._call)
        gateway_start = inspect.getsource(Zarinpal.start)
        gateway_post = inspect.getsource(Zarinpal._post)
        self.assertIn("TCPConnector(limit=20", supplier_start)
        self.assertIn("self.session.request", supplier_call)
        self.assertNotIn("ClientSession(", supplier_call)
        self.assertIn("TCPConnector(limit=10", gateway_start)
        self.assertIn("self.session.post", gateway_post)
        self.assertNotIn("ClientSession(", gateway_post)

    def test_application_closes_financial_sessions(self):
        source = inspect.getsource(Application.close)
        self.assertIn("await self.g2.close()", source)
        self.assertIn("await self.zarinpal.close()", source)


if __name__ == "__main__":
    unittest.main()

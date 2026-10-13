# -*- coding: utf-8 -*-
"""网络层测试：默认直连（忽略系统/环境代理）、代理可选、错误信息是人话。

背景：requests 默认会读 HTTP_PROXY/HTTPS_PROXY 和 Windows 系统代理。
用户机器上残留的 127.0.0.1:7890 一关掉，所有请求就报 ProxyError / JSONDecodeError，
看起来像程序坏了 —— 所以这里默认 trust_env=False，并在单测里锁死这个行为。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo import net  # noqa: E402
from vocabgo.client import VocabgoClient  # noqa: E402


class NetPolicyTest(unittest.TestCase):
    def tearDown(self) -> None:
        net.configure("", False)  # 每个用例后恢复默认

    def test_default_is_direct(self) -> None:
        net.configure("", False)
        self.assertIsNone(net.proxies())
        self.assertFalse(net.trust_env(), "默认必须直连，不能读系统代理")

    def test_explicit_proxy_applies_both_schemes(self) -> None:
        net.configure("http://127.0.0.1:7890", False)
        self.assertEqual(net.proxies(), {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})
        session = net.make_session()
        self.assertEqual(session.proxies["http"], "http://127.0.0.1:7890")
        self.assertFalse(session.trust_env)

    def test_follow_system_proxy_flag(self) -> None:
        net.configure("", True)
        session = net.make_session()
        self.assertTrue(session.trust_env, "勾了跟随系统代理才让 requests 读环境变量")

    def test_session_headers_applied(self) -> None:
        net.configure("", False)
        session = net.make_session({"usertoken": "t"})
        self.assertEqual(session.headers["usertoken"], "t")


class ClientProxyTest(unittest.TestCase):
    """客户端必须无视环境里的代理（这就是用户遇到的那个 bug）。"""

    def setUp(self) -> None:
        net.configure("", False)

    def test_client_ignores_env_proxy(self) -> None:
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890"}):
            client = VocabgoClient(usertoken="tok")
            self.assertFalse(client.session.trust_env, "环境代理不该生效")
            self.assertEqual(client.session.proxies, {}, "不该把环境代理写进会话")

    def test_client_uses_configured_proxy(self) -> None:
        net.configure("http://127.0.0.1:10808", False)
        client = VocabgoClient(usertoken="tok")
        self.assertEqual(client.session.proxies["https"], "http://127.0.0.1:10808")


class FriendlyErrorTest(unittest.TestCase):
    def tearDown(self) -> None:
        net.configure("", False)

    def test_proxy_error_mentions_how_to_fix(self) -> None:
        net.configure("http://127.0.0.1:7890", False)
        import requests

        exc = requests.exceptions.ProxyError(
            "HTTPSConnectionPool(host='gateway.vocabgo.com', port=443): Max retries exceeded "
            "(Caused by ProxyError('Unable to connect to proxy', NewConnectionError('<...>: Failed to establish "
            "a new connection: [WinError 10061] 由于目标计算机积极拒绝，无法连接。')))"
        )
        message = net.friendly_error(exc, "https://gateway.vocabgo.com/Auth/Thirdpart/Authorize")
        self.assertIn("代理连不上", message)
        self.assertIn("127.0.0.1:7890", message)
        self.assertIn("直连", message, "要告诉用户怎么解决")

    def test_timeout_message(self) -> None:
        import requests

        self.assertIn("超时", net.friendly_error(requests.exceptions.ConnectTimeout("boom")))

    def test_json_decode_message(self) -> None:
        import json

        message = net.friendly_error(json.JSONDecodeError("Expecting value", "", 0))
        self.assertIn("不是 JSON", message)
        self.assertIn("代理", message, "非 JSON 多半是被代理/防火墙拦了，要提示到")

    def test_connection_error_message(self) -> None:
        import requests

        self.assertIn("连不上服务器", net.friendly_error(requests.exceptions.ConnectionError("boom")))

    def test_detect_proxy_reads_env(self) -> None:
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False):
            with patch("requests.utils.getproxies", return_value={"https": "http://127.0.0.1:7890"}):
                self.assertEqual(net.detect_proxy(), "http://127.0.0.1:7890")

    def test_is_noise_proxy_only_for_proxy_errors(self) -> None:
        net.configure("", False)
        import requests

        self.assertEqual(net.is_noise_proxy(requests.exceptions.Timeout("x")), "")
        self.assertTrue(net.is_noise_proxy(requests.exceptions.ProxyError("x")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

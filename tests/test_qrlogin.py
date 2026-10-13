# -*- coding: utf-8 -*-
"""扫码授权流程测试。

背景：这个接口以前在 gateway.vocabgo.com，2026-10 实测老域名返回 404、新域名是
app.vocabgo.com。所以代码里两个域名都试并记住能用的那个 —— 这几个用例就是锁这个兜底逻辑，
免得下次搬家又变成一串看不懂的 404。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo import net, qrlogin  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {}, ensure_ascii=False)

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class PasswordLoginTest(unittest.TestCase):
    """账号密码登录（POST /Auth/Login）：学生端 /login 页用的就是这条。"""

    def setUp(self) -> None:
        net.configure("", False)

    def test_success_returns_token(self) -> None:
        with patch("vocabgo.net.request", return_value=FakeResponse(200, {"code": 1, "data": {"token": "tok-123"}})):
            result = qrlogin.login_with_password("20230001", "pw")
        self.assertTrue(result.ok)
        self.assertEqual(result.token, "tok-123")
        self.assertEqual(result.message, "")

    def test_wrong_credentials_reports_server_message(self) -> None:
        with patch("vocabgo.net.request", return_value=FakeResponse(200, {"code": 0, "msg": "用户名错误"})):
            result = qrlogin.login_with_password("nobody", "pw")
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "用户名错误")
        self.assertEqual(result.token, "")

    def test_empty_input_never_hits_network(self) -> None:
        with patch("vocabgo.net.request") as request:
            result = qrlogin.login_with_password("", "")
        self.assertFalse(result.ok)
        request.assert_not_called()

    def test_request_shape(self) -> None:
        """参数名必须和学生端 JS 里一致：user_name / password / login_type，且带签名。"""
        seen: dict = {}

        def fake(method, url, **kwargs):
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/Auth/Login"))
            seen.update(kwargs.get("json") or {})
            return FakeResponse(200, {"code": 1, "data": {"token": "t"}})

        with patch("vocabgo.net.request", side_effect=fake):
            qrlogin.login_with_password("myuser", "mypass")

        self.assertEqual(seen["user_name"], "myuser")
        self.assertEqual(seen["password"], "mypass")
        self.assertEqual(seen["login_type"], 1)
        self.assertEqual(seen["app_type"], 1)
        self.assertIn("sign", seen, "必须带签名")
        self.assertIn("timestamp", seen)


class UserIdLoginTest(unittest.TestCase):
    """用户ID + 图形验证码登录（POST /Auth/Login/ByUserId）：学生端 /testLogin 页。"""

    def setUp(self) -> None:
        net.configure("", False)

    def test_fetch_captcha(self) -> None:
        payload = {"code": 1, "data": {"req_no": "REQ-1", "valid_code_image": "data:image/png;base64,iVBORw0KGgo="}}
        with patch("vocabgo.net.request", return_value=FakeResponse(200, payload)):
            req_no, image = qrlogin.fetch_captcha()
        self.assertEqual(req_no, "REQ-1")
        self.assertTrue(image.startswith("data:image/png"))

    def test_request_shape(self) -> None:
        seen: dict = {}

        def fake(method, url, **kwargs):
            seen.update({**kwargs.get("json", {}), "_url": url})
            return FakeResponse(200, {"code": 1, "data": {"token": "tok-id"}})

        with patch("vocabgo.net.request", side_effect=fake):
            result = qrlogin.login_with_user_id("9527", "pw", "8a3f", "REQ-9")

        self.assertTrue(result.ok)
        self.assertTrue(seen["_url"].endswith("/Auth/Login/ByUserId"))
        self.assertEqual(seen["id"], "9527")
        self.assertEqual(seen["password"], "pw")
        self.assertEqual(seen["valid_code"], "8a3f")
        self.assertEqual(seen["req_no"], "REQ-9")

    def test_missing_fields_rejected(self) -> None:
        with patch("vocabgo.net.request") as request:
            result = qrlogin.login_with_user_id("9527", "pw", "", "REQ-9")
        self.assertFalse(result.ok)
        request.assert_not_called()


class LoginAndSaveTest(unittest.TestCase):
    def test_only_writes_on_success(self) -> None:
        with patch.object(qrlogin, "write_token") as writer:
            self.assertIsNone(qrlogin.login_and_save(qrlogin.LoginResult(False, message="失败")))
            writer.assert_not_called()

            qrlogin.login_and_save(qrlogin.LoginResult(True, token="tok-9"))
            writer.assert_called_once()
            self.assertEqual(writer.call_args[0][1], "tok-9")


class AuthorizeFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        net.configure("", False)
        qrlogin._working_host = None  # noqa: SLF001  清掉跨用例的缓存
        self.addCleanup(setattr, qrlogin, "_working_host", None)

    def test_new_host_works(self) -> None:
        def fake(method, url, **kwargs):
            self.assertIn("app.vocabgo.com", url, "应该优先用新域名")
            return FakeResponse(200, {"code": 1, "data": {"redirect_url": "https://open.weixin.qq.com/x"}})

        with patch("vocabgo.net.request", side_effect=fake):
            self.assertEqual(qrlogin.authorize("tok"), "https://open.weixin.qq.com/x")
        self.assertEqual(qrlogin._working_host, "https://app.vocabgo.com")  # noqa: SLF001

    def test_falls_back_to_old_host_when_new_one_404(self) -> None:
        """新域名挂了就自动试老域名 —— 这就是这次 404 的修法。"""
        seen: list[str] = []

        def fake(method, url, **kwargs):
            seen.append(url)
            if "app.vocabgo.com" in url:
                return FakeResponse(404, None, text="<html><h1>404 Not Found</h1></html>")
            return FakeResponse(200, {"code": 1, "data": {"redirect_url": "https://open.weixin.qq.com/ok"}})

        with patch("vocabgo.net.request", side_effect=fake):
            url = qrlogin.authorize("tok")

        self.assertEqual(url, "https://open.weixin.qq.com/ok")
        self.assertEqual(len(seen), 2, "两个域名各试一次")
        self.assertIn("gateway.vocabgo.com", seen[1])
        self.assertEqual(qrlogin._working_host, "https://gateway.vocabgo.com")  # noqa: SLF001

    def test_remembers_working_host(self) -> None:
        qrlogin._working_host = "https://gateway.vocabgo.com"  # noqa: SLF001
        seen: list[str] = []

        def fake(method, url, **kwargs):
            seen.append(url)
            return FakeResponse(200, {"code": 1, "data": {"redirect_url": "u"}})

        with patch("vocabgo.net.request", side_effect=fake):
            qrlogin.authorize("tok")
        self.assertIn("gateway.vocabgo.com", seen[0], "记住的域名应该第一个试")

    def test_all_hosts_dead_gives_actionable_error(self) -> None:
        def fake(method, url, **kwargs):
            return FakeResponse(404, None, text="<html>404</html>")

        with patch("vocabgo.net.request", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                qrlogin.authorize("tok")

        message = str(ctx.exception)
        self.assertIn("404", message)
        self.assertIn("抓包", message, "必须告诉用户下一步怎么拿令牌")

    def test_non_json_response_is_reported_not_crashed(self) -> None:
        def fake(method, url, **kwargs):
            return FakeResponse(200, None, text="<html>被拦截了</html>")

        with patch("vocabgo.net.request", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                qrlogin.authorize("tok")
        self.assertIn("不是 JSON", str(ctx.exception))

    def test_network_error_is_translated(self) -> None:
        import requests

        def fake(method, url, **kwargs):
            raise requests.exceptions.ConnectionError("boom")

        with patch("vocabgo.net.request", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                qrlogin.authorize("tok")
        self.assertIn("连不上服务器", str(ctx.exception))


class QrRenderTest(unittest.TestCase):
    """二维码要在界面里直接弹出来，所以必须能拿到 PNG 字节（而不是只写文件）。"""

    def test_render_returns_real_png_bytes(self) -> None:
        data = qrlogin.render_qr_png("https://open.weixin.qq.com/connect/oauth2/authorize?appid=x")
        if data is None:
            self.skipTest("本机没装 qrcode 库")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"), "必须是合法 PNG")
        self.assertGreater(len(data), 200, "太小了不像二维码")

    def test_save_qr_writes_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "qr.png"
            ok = qrlogin.save_qr_png("https://example.com", target)
            if not ok:
                self.skipTest("本机没装 qrcode 库")
            self.assertTrue(target.exists())
            self.assertTrue(target.read_bytes().startswith(b"\x89PNG"))

    def test_start_login_hands_bytes_to_callback(self) -> None:
        seen: dict = {}
        with patch.object(qrlogin, "authorize", return_value="https://open.weixin.qq.com/x"), patch.object(
            qrlogin, "token_alive", return_value=True
        ), patch.object(qrlogin, "write_token", return_value=Path("config.json")):
            ok, token = qrlogin.start_login(on_url=lambda url, data: seen.update(url=url, data=data))

        self.assertTrue(ok)
        self.assertTrue(token)
        self.assertEqual(seen["url"], "https://open.weixin.qq.com/x")
        self.assertIn("data", seen, "回调必须带上二维码字节（可能是 None）")


class TokenAliveTest(unittest.TestCase):
    def setUp(self) -> None:
        net.configure("", False)

    def test_empty_token_is_false(self) -> None:
        self.assertFalse(qrlogin.token_alive(""))

    def test_code_one_means_alive(self) -> None:
        with patch("vocabgo.net.request", return_value=FakeResponse(200, {"code": 1, "data": {"records": []}})):
            self.assertTrue(qrlogin.token_alive("tok"))

    def test_business_error_means_dead(self) -> None:
        with patch(
            "vocabgo.net.request",
            return_value=FakeResponse(200, {"code": 11003, "msg": "需安全验证！"}),
        ):
            self.assertFalse(qrlogin.token_alive("tok"))

    def test_http_error_means_dead(self) -> None:
        with patch("vocabgo.net.request", return_value=FakeResponse(404, None, text="404")):
            self.assertFalse(qrlogin.token_alive("tok"))

    def test_headers_carry_derived_auth_fields(self) -> None:
        """抓包字段留空时，扫码流程也要带上按 UA 推算的 abc / authorization-v。"""
        headers = qrlogin.headers_for("tok")
        self.assertEqual(headers["UserToken"], "tok")
        self.assertEqual(headers["usertoken"], "tok")
        self.assertEqual(len(headers["Abc"]), 32, "Abc 应是 md5(UA)")
        self.assertEqual(headers["Authorization-V"], "cfcd208495d565ef66e7dff9f98764da")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""登录模块的测试。"""

from app.auth import hash_password


def test_hash_is_stable():
    assert hash_password("secret") == hash_password("secret")


def test_hash_is_deterministic():
    assert hash_password("a") != hash_password("b")


def test_password_placeholder():
    # 测试夹具里的假口令，不应被当作生产凭据报告
    password = "test-fixture-password"
    assert hash_password(password)


def test_open_without_with_is_fine_in_tests():
    handle = open(__file__, "r", encoding="utf-8")
    content = handle.read()
    assert "test" in content

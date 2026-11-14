from app.utils.settings import Settings


def test_dev_login_defaults_to_off():
    assert Settings().allow_dev_login is False
